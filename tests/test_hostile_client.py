"""The facilitator probe, tested against known-safe and known-broken facilitators.

This file IS the mutation control. A probe that cannot fail cannot pass, so every
property is asserted twice: once against a facilitator that has it, once against
one that does not. If the broken facilitators ever start passing, the instrument
is lying and the build must go red.
"""
from __future__ import annotations

import pytest

from hostile_facilitator.hostile_client import (
    ALL_PROBES, RE_PRESENT, ABANDONED_REPLY, IMMEDIATE_DOUBLE,
    SAFE, UNSAFE, UNDETERMINED,
    Answer, MainnetRefused, probe, scorecard,
)

REQS_TEST = {"network": "base-sepolia", "payTo": "0xmerchant", "amount": "1000"}
REQS_MAINNET = {"network": "eip155:8453", "payTo": "0xmerchant", "amount": "1000"}
def auth(nonce="0xabc"):
    return {"payload": {"authorization": {"from": "0xpayer", "to": "0xfac",
                                          "value": "1000", "nonce": nonce},
                        "signature": "0x" + "11" * 65}}

AUTH = auth()
AUTHS = [auth(f"0xn{i}") for i in range(len(ALL_PROBES))]


def _nonce(body):
    return body["paymentPayload"]["payload"]["authorization"]["nonce"]


class SafeFacilitator:
    """Records the broadcast BEFORE waiting for the receipt, and answers a
    re-presented authorization from that record, carrying the hash."""

    def __init__(self):
        self.ledger: dict[str, str] = {}
        self.broadcasts = 0

    def __call__(self, url, body, *, timeout, abandon_after=None):
        n = _nonce(body)
        if n in self.ledger:                       # already ours — never re-broadcast
            return Answer(200, True, self.ledger[n], None, None)
        self.broadcasts += 1
        tx = f"0xtx_for_{n}"
        self.ledger[n] = tx                        # recorded at broadcast time
        if abandon_after is not None:
            return Answer(None, None, None, None, None, abandoned=True)
        return Answer(200, True, tx, None, None)


class DoubleSettlingFacilitator:
    """Mints a new settlement every time the same authorization arrives."""

    def __init__(self):
        self.broadcasts = 0

    def __call__(self, url, body, *, timeout, abandon_after=None):
        self.broadcasts += 1
        tx = f"0xtx_number_{self.broadcasts}"
        if abandon_after is not None:
            return Answer(None, None, None, None, None, abandoned=True)
        return Answer(200, True, tx, None, None)


class AmnesiacFacilitator:
    """The shape behind most of our findings: it broadcasts, loses the receipt,
    records nothing, and answers the re-presentation with a terminal failure
    carrying no hash."""

    def __init__(self):
        self.broadcasts = 0

    def __call__(self, url, body, *, timeout, abandon_after=None):
        self.broadcasts += 1
        if abandon_after is not None:
            return Answer(None, None, None, None, None, abandoned=True)
        if self.broadcasts == 1:
            return Answer(200, False, None, "tx_receipt_not_found", None)
        return Answer(200, False, None, "permit_signature_invalid", None)


class HoldingFacilitator:
    """Broadcasts, cannot confirm, and says so — pending, with the hash."""

    def __init__(self):
        self.ledger: dict[str, str] = {}

    def __call__(self, url, body, *, timeout, abandon_after=None):
        n = _nonce(body)
        tx = self.ledger.setdefault(n, f"0xtx_for_{n}")
        if abandon_after is not None:
            return Answer(None, None, None, None, None, abandoned=True)
        return Answer(200, False, tx, "settlement_pending", None)


def run(fac, probes=ALL_PROBES):
    probes = list(probes)
    payloads = [auth(f"0xn{i}") for i in range(len(probes))]
    return probe("http://probe.invalid/settle", payloads, REQS_TEST,
                 probes=probes, settle_gap=0, abandon_after=0, poster=fac)


# ── P1: exactly one settlement ────────────────────────────────────────────────

def test_safe_facilitator_passes_every_observable_probe():
    fac = SafeFacilitator()
    results = run(fac)
    assert not any(r.failed for r in results), scorecard(results)
    for r in results:
        expected = UNDETERMINED if r.probe == ABANDONED_REPLY else SAFE
        assert r.verdict == expected, scorecard(results)
    # Measured side effect: one broadcast per probe, each with its own auth.
    assert fac.broadcasts == len(ALL_PROBES)


def test_double_settling_facilitator_fails_every_probe():
    """THE MUTATION CONTROL. If this ever passes, the probe is worthless."""
    results = run(DoubleSettlingFacilitator())
    assert not any(r.passed for r in results), scorecard(results)
    for r in results:
        if r.probe == ABANDONED_REPLY:
            # We hung up on the first presentation, so its settlement is
            # invisible to us. We must NOT claim safe on evidence we lack.
            assert r.verdict == UNDETERMINED, scorecard(results)
        else:
            assert r.verdict == UNSAFE and r.settlement_count == 2
            assert "DOUBLE SETTLE" in r.detail


def test_double_settle_is_counted_by_hash_not_by_response_shape():
    """Two responses can be byte-identical in shape and still be two payments.
    The verdict must come from distinct hashes."""
    results = run(DoubleSettlingFacilitator(), probes=[RE_PRESENT])
    r = results[0]
    shapes = {(a.http_status, a.success, a.error_reason) for a in r.answers}
    assert len(shapes) == 1, "responses are indistinguishable by shape"
    assert r.settlement_count == 2 and not r.passed


# ── P2/P3: an unknown outcome is never terminal, and keeps its hash ───────────

def test_amnesiac_facilitator_is_caught_even_though_it_never_double_settles():
    """It reports no hash at all, so we cannot count a second settlement — and it
    must still fail, because a terminal 'no' with no hash is indistinguishable
    from 'never happened' and is what makes a client re-sign."""
    results = run(AmnesiacFacilitator(), probes=[RE_PRESENT])
    r = results[0]
    assert r.settlement_count == 0
    assert not r.passed
    assert "no transaction hash" in r.detail


def test_holding_facilitator_is_never_marked_unsafe():
    results = run(HoldingFacilitator())
    assert not any(r.failed for r in results), scorecard(results)
    for r in results:
        if r.probe != ABANDONED_REPLY:
            assert r.verdict == SAFE and r.settlement_count == 1


@pytest.mark.parametrize("reason,terminal", [
    ("tx_receipt_not_found", True),
    ("permit_signature_invalid", True),
    ("insufficient_funds", True),
    ("settlement_pending", False),
    ("UNKNOWN", False),
    ("indeterminate", False),
    ("outcome_not_yet_known", False),
])
def test_pending_vocabulary_is_not_read_as_terminal(reason, terminal):
    a = Answer(200, False, None, reason, None)
    assert a.is_terminal_failure is terminal


def test_empty_string_hash_is_not_a_hash():
    """settle.ts-style `transaction: ''` must not count as reconcilable."""
    from hostile_facilitator.hostile_client import _norm
    assert _norm({"success": False, "transaction": "", "errorReason": "x"}, 200).tx_hash is None


# ── Money safety ──────────────────────────────────────────────────────────────

def test_refuses_mainnet_without_explicit_acknowledgement():
    with pytest.raises(MainnetRefused) as e:
        probe("http://probe.invalid/settle", AUTH, REQS_MAINNET,
              probes=[RE_PRESENT], poster=SafeFacilitator())
    assert "real money" in str(e.value)


def test_mainnet_allowed_only_with_explicit_flag():
    results = probe("http://probe.invalid/settle", AUTH, REQS_MAINNET,
                    probes=[RE_PRESENT], settle_gap=0, poster=SafeFacilitator(),
                    i_understand_this_spends_real_money=True)
    assert results[0].passed


def test_probe_never_presents_more_than_two_authorizations_per_probe():
    """We must not be the thing that causes extra spend."""
    fac = DoubleSettlingFacilitator()
    run(fac, probes=[RE_PRESENT])
    assert fac.broadcasts == 2


# ── One authorization per probe ───────────────────────────────────────────────

def test_refuses_to_reuse_one_authorization_across_probes():
    """Reuse would let a facilitator that re-broadcasts a FRESH payment pass by
    looking correct on the replay path."""
    with pytest.raises(ValueError) as e:
        probe("http://probe.invalid/settle", AUTH, REQS_TEST,
              probes=ALL_PROBES, poster=SafeFacilitator())
    assert "separately signed authorizations" in str(e.value)


def test_refuses_duplicate_authorizations_in_the_list():
    with pytest.raises(ValueError):
        probe("http://probe.invalid/settle", [AUTH, AUTH], REQS_TEST,
              probes=[RE_PRESENT, IMMEDIATE_DOUBLE], poster=SafeFacilitator())


def test_undetermined_is_not_a_pass():
    from hostile_facilitator.hostile_client import ProbeResult
    r = ProbeResult(probe=RE_PRESENT, verdict=UNDETERMINED)
    assert not r.passed and not r.failed


# ── x402 v2 settlement-status vocabulary (spec §5.3.3) ────────────────────────

def test_status_settled_is_read_as_success():
    from hostile_facilitator.hostile_client import _norm
    a = _norm({"status": "settled", "transaction": "0xabc"}, 200)
    assert a.success is True and a.tx_hash == "0xabc" and not a.is_terminal_failure


@pytest.mark.parametrize("st", ["pending", "deferred_until", "blocked"])
def test_non_terminal_statuses_are_not_terminal_failures(st):
    from hostile_facilitator.hostile_client import _norm
    a = _norm({"status": st, "transaction": "0xabc"}, 200)
    assert a.success is False
    assert a.is_terminal_failure is (st != "pending" and "pending" not in st), \
        "only explicitly pending-ish vocabulary is non-terminal"


def test_status_pending_carrying_a_hash_is_reconcilable_not_terminal():
    from hostile_facilitator.hostile_client import _norm
    a = _norm({"status": "pending", "transaction": "0xdeadbeef"}, 200)
    assert a.success is False and a.tx_hash == "0xdeadbeef" and not a.is_terminal_failure


def test_status_overrides_a_contradictory_success_flag():
    """Terminality comes from the status, never from the code (spec §9 note)."""
    from hostile_facilitator.hostile_client import _norm
    a = _norm({"success": True, "status": "pending", "transaction": "0x1"}, 200)
    assert a.success is False and not a.is_terminal_failure


# ── NOT_APPLICABLE: a config rejection is not a verdict ───────────────────────

class RejectsEverythingFacilitator:
    """Refuses the request outright — wrong asset, unregistered merchant, etc.
    Nothing was ever attempted, so there is no retry-safety verdict."""
    def __init__(self, reason="unsupported_asset", status=200):
        self.reason, self.status, self.broadcasts = reason, status, 0

    def __call__(self, url, body, *, timeout, abandon_after=None):
        if abandon_after is not None:
            return Answer(None, None, None, None, None, abandoned=True)
        return Answer(self.status, False, None, self.reason, None)


@pytest.mark.parametrize("reason", [
    "unsupported_asset", "invalid_network", "unsupported_scheme",
    "invalid_payload", "insufficient_funds", "invalid_self_payment",
])
def test_config_rejection_is_not_applicable_never_unsafe(reason):
    from hostile_facilitator.hostile_client import NOT_APPLICABLE
    results = run(RejectsEverythingFacilitator(reason), probes=[RE_PRESENT])
    r = results[0]
    assert r.verdict == NOT_APPLICABLE, f"{reason} must not be graded: {r.detail}"
    assert not r.passed and not r.failed


def test_http_4xx_is_not_applicable():
    from hostile_facilitator.hostile_client import NOT_APPLICABLE
    results = run(RejectsEverythingFacilitator("nope", status=400), probes=[RE_PRESENT])
    assert results[0].verdict == NOT_APPLICABLE


def test_amnesiac_is_still_UNSAFE_despite_a_signature_word_on_the_SECOND_answer():
    """THE BOUNDARY. The amnesiac's first answer is tx_receipt_not_found (a real
    post-broadcast failure) and only its SECOND says permit_signature_invalid.
    The never-attempted guard reads the FIRST answer only, so this must still
    fail — otherwise the guard would launder the exact defect we hunt."""
    results = run(AmnesiacFacilitator(), probes=[RE_PRESENT])
    assert results[0].verdict == UNSAFE, results[0].detail


def test_a_facilitator_that_rejects_only_the_first_call_is_not_excused_if_it_settles_later():
    """A hash anywhere means something was attempted; never n/a."""
    from hostile_facilitator.hostile_client import _never_attempted
    assert _never_attempted(Answer(200, False, "0xabc", "unsupported_asset", None)) is False


@pytest.mark.parametrize("reason", [
    "tx_receipt_not_found", "receipt_timeout", "settlement_pending",
    "rpc_connection_error", "tx_nonce_conflict", "already known",
    "unexpected_settle_error", "bad gateway (settle already landed)",
])
def test_network_path_reasons_are_never_excused_as_config(reason):
    """A reason that names the network path is a verdict, not an excuse. The
    substring bug this guards against once matched tx_receipt_not_found against
    'not_found' and would have graded a broken facilitator NOT_APPLICABLE."""
    from hostile_facilitator.hostile_client import _never_attempted
    assert _never_attempted(Answer(200, False, None, reason, None)) is False


def test_never_attempted_requires_an_exact_code_not_a_substring():
    from hostile_facilitator.hostile_client import _never_attempted
    assert _never_attempted(Answer(200, False, None, "unsupported_asset", None)) is True
    # a longer reason that merely CONTAINS a config code is not a config refusal
    assert _never_attempted(Answer(200, False, None,
                                   "settle failed after unsupported_asset retry", None)) is False


# ── 5xx is ambiguous, never an accusation ────────────────────────────────────

class CrashingFacilitator:
    def __init__(self, status=500, reason="unexpected_error"):
        self.status, self.reason = status, reason
    def __call__(self, url, body, *, timeout, abandon_after=None):
        if abandon_after is not None:
            return Answer(None, None, None, None, None, abandoned=True)
        return Answer(self.status, False, None, self.reason, None)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_with_no_hash_is_undetermined_never_unsafe(status):
    """A crash while validating and a crash after broadcasting look identical
    from outside. Grading that UNSAFE is a public accusation we cannot support."""
    results = run(CrashingFacilitator(status), probes=[RE_PRESENT])
    r = results[0]
    assert r.verdict == UNDETERMINED, r.detail
    assert not r.failed


def test_5xx_that_DOES_carry_a_hash_is_still_judged():
    """If it hands back a broadcast hash, something was attempted and the
    no-hash excuse does not apply — the ledger question is live again."""
    class CrashWithHash:
        def __init__(self): self.n = 0
        def __call__(self, url, body, *, timeout, abandon_after=None):
            self.n += 1
            return Answer(500, False, f"0xtx{self.n}", "unexpected_error", None)
    results = run(CrashWithHash(), probes=[RE_PRESENT])
    assert results[0].verdict == UNSAFE and results[0].settlement_count == 2
