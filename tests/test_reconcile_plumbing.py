"""The reconciliation read that fails in a success-shaped way.

Origin: solim, on MCP discussion #3188 (9 Sep 2026), with two dated production
bugs of their own — a `grep -q` gate whose SIGPIPE became 141 under `pipefail`
and was read as "no match" above the 64KB pipe buffer, and a `grep -c` counter
where "genuinely zero" and "the command failed" arrive as the same value with
the same status.

The class: a reconcile read whose "not found" is DERIVED FROM AN ERROR PATH.
`RECONCILE_UNAVAILABLE` already covers the clean half — the read raises, and a
correct client holds. These three cover the half that actually ships, where the
read RETURNS, looks fine, and means nothing. Truth in all three is SETTLED, so a
client that reads "absent" and retries pays twice.

Nothing upstream catches this, because the defect is in the client's own
plumbing rather than the provider's behaviour.
"""
import json
import pytest

from hostile_facilitator.hostile import (
    Facilitator, ReconcileUnavailable, ProviderError,
    RECONCILE_SOFT_404, RECONCILE_OVERSIZED, RECONCILE_TRUNCATED,
    RECONCILE_UNAVAILABLE, CLEAN, ALL_MODES,
    run_purchase, reconcile_controls,
)

PLUMBING_MODES = [RECONCILE_SOFT_404, RECONCILE_OVERSIZED, RECONCILE_TRUNCATED]


@pytest.mark.parametrize("mode", PLUMBING_MODES)
def test_the_record_really_is_settled(mode):
    """Precondition for the whole class: the payment landed. Any read that says
    'absent' is wrong, not merely unlucky."""
    fac = Facilitator(mode=mode)
    with pytest.raises(ProviderError):
        fac.settle("nonce-1")
    assert fac.distinct_payments == 1


@pytest.mark.parametrize("mode", PLUMBING_MODES)
def test_the_raw_read_looks_successful(mode):
    """The trap: status is 200 and nothing raises. A client that trusts the
    status code, or that derives absence from a failed parse, is now wrong."""
    fac = Facilitator(mode=mode)
    with pytest.raises(ProviderError):
        fac.settle("nonce-1")
    status, body = fac.reconcile_raw("nonce-1")
    assert status == 200, "a success-shaped non-answer must not announce itself"
    assert b"nonce-1" not in body or mode is RECONCILE_OVERSIZED


def test_soft_404_carries_prose_not_a_status():
    fac = Facilitator(mode=RECONCILE_SOFT_404)
    with pytest.raises(ProviderError):
        fac.settle("nonce-1")
    status, body = fac.reconcile_raw("nonce-1")
    assert status == 200
    assert b"not available" in body.lower()


def test_oversized_body_contains_the_record_but_defeats_a_buffered_read():
    """solim's 5.7MB case. The record IS in the body — the read fails because of
    how much body there is, so 'absent' is reported precisely when there was MORE
    evidence to read."""
    fac = Facilitator(mode=RECONCILE_OVERSIZED)
    with pytest.raises(ProviderError):
        fac.settle("nonce-1")
    status, body = fac.reconcile_raw("nonce-1")
    assert len(body) > 64 * 1024, "must exceed the pipe buffer that hid the original bug"
    assert b"nonce-1" in body, "the truth is present; only the plumbing loses it"


def test_truncated_body_cuts_mid_record():
    fac = Facilitator(mode=RECONCILE_TRUNCATED)
    with pytest.raises(ProviderError):
        fac.settle("nonce-1")
    status, body = fac.reconcile_raw("nonce-1")
    with pytest.raises(Exception):
        json.loads(body)


@pytest.mark.parametrize("mode", PLUMBING_MODES)
def test_the_verdict_api_refuses_to_say_absent(mode):
    """Our own reference read must map every success-shaped non-answer to
    'could not determine'. Returning 'absent' here is the bug itself."""
    fac = Facilitator(mode=mode)
    with pytest.raises(ProviderError):
        fac.settle("nonce-1")
    with pytest.raises(ReconcileUnavailable):
        fac.reconcile("nonce-1")


@pytest.mark.parametrize("mode", PLUMBING_MODES)
def test_a_client_that_reads_absent_and_retries_double_pays(mode):
    """The whole point. This client is not careless — it asks the provider before
    retrying, which is the behaviour the spec asks for. It just derives 'absent'
    from an error path."""
    def naive_client(fac):
        nonce = "nonce-1"
        try:
            return fac.settle(nonce)
        except ProviderError:
            pass
        status, body = fac.reconcile_raw(nonce)
        try:
            settled = json.loads(body).get("found") is True   # parse fails -> False
        except Exception:
            settled = False                                   # <-- the defect
        if not settled:
            fac.settle("nonce-2")                             # fresh nonce: pays again

    result = run_purchase(naive_client, mode)
    assert result.distinct_payments == 2
    assert not result.passed


@pytest.mark.parametrize("mode", PLUMBING_MODES)
def test_a_tri_state_client_holds(mode):
    """settled / not settled / read failed — and read-failed is not collapsed."""
    def tristate_client(fac):
        nonce = "nonce-1"
        try:
            return fac.settle(nonce)
        except ProviderError:
            pass
        try:
            verdict = fac.reconcile(nonce)
        except ReconcileUnavailable:
            raise                       # terminal; hold and surface
        if verdict == "absent":
            fac.settle("nonce-2")

    result = run_purchase(tristate_client, mode)
    assert result.distinct_payments == 1
    assert result.passed


def test_modes_are_in_the_battery():
    for m in PLUMBING_MODES:
        assert m in ALL_MODES


# ---- the instrument check ---------------------------------------------------

def test_a_checker_that_always_says_absent_fails_the_positive_control():
    """solim: 'a checker validated only against the negative is indistinguishable
    from a function that returns a constant' — which is literally what theirs was."""
    ok, detail = reconcile_controls(lambda fac, nonce: "absent")
    assert not ok
    assert "positive" in detail.lower()


def test_a_checker_that_always_says_found_fails_the_negative_control():
    ok, detail = reconcile_controls(lambda fac, nonce: "found_once")
    assert not ok
    assert "negative" in detail.lower()


def test_an_honest_checker_passes_both_controls():
    ok, detail = reconcile_controls(lambda fac, nonce: fac.reconcile(nonce))
    assert ok, detail
