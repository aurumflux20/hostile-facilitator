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
