"""On-chain proof: the battery's verdicts, settled as real EIP-3009 transfers.

These are the tests that let a finding drop the phrase "not a live reproduction".
The count is read from `Transfer` logs on a local anvil node, so a FAIL here is
two entries in a ledger, not an inference about control flow.

Skipped automatically when foundry is not installed.
"""

from __future__ import annotations

import shutil

import pytest

from hostile_facilitator.chain import Chain, OnChainFacilitator, MERCHANT
from hostile_facilitator.hostile import (
    ACCEPT_THEN_TIMEOUT, CLEAN, DOUBLE_402, FIVE_XX_AFTER_SETTLE,
    RECONCILE_UNAVAILABLE, Rechallenge,
)

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("anvil", "cast", "forge")),
    reason="on-chain mode needs foundry (anvil/cast/forge) on PATH",
)

AMOUNT = 1_000_000


@pytest.fixture(scope="module")
def chain():
    with Chain(port=8699) as c:
        yield c


def _broken_client(fac: OnChainFacilitator, order: str) -> None:
    """The defect in one line: on an ambiguous outcome, mint a FRESH nonce and pay again."""
    try:
        fac.settle(f"{order}")
    except Exception:
        try:
            fac.settle(f"{order}-retry-fresh-nonce")   # a new authorization: guards pass it
        except Exception:
            pass


def _safe_client(fac: OnChainFacilitator, order: str) -> None:
    """The fix: an ambiguous outcome is terminal. Reconcile; if it landed, stop.
    If the read itself fails, hold — never re-present under a new identity."""
    try:
        fac.settle(order)
    except Rechallenge:
        return                                    # a re-challenge is not a reason to pay twice
    except Exception:
        try:
            if fac.reconcile(order) == "settled":
                return
        except Exception:
            return                                # could not determine → terminal, hold
        fac.settle(order)                         # same nonce: the chain dedupes it


@pytest.mark.parametrize("mode", [ACCEPT_THEN_TIMEOUT, FIVE_XX_AFTER_SETTLE, DOUBLE_402])
def test_broken_client_double_charges_on_chain(chain, mode):
    before = chain.transfers()
    bal = chain.balance(MERCHANT)
    fac = OnChainFacilitator(chain=chain, mode=mode, client_timeout_s=0.2)
    _broken_client(fac, f"broken-{mode}")
    moved = chain.transfers() - before
    assert moved == 2, f"{mode}: expected a real double-charge, saw {moved} transfers"
    assert chain.balance(MERCHANT) - bal == 2 * AMOUNT


@pytest.mark.parametrize(
    "mode", [ACCEPT_THEN_TIMEOUT, FIVE_XX_AFTER_SETTLE, DOUBLE_402, RECONCILE_UNAVAILABLE, CLEAN]
)
def test_safe_client_pays_once_on_chain(chain, mode):
    before = chain.transfers()
    fac = OnChainFacilitator(chain=chain, mode=mode, client_timeout_s=0.2)
    _safe_client(fac, f"safe-{mode}")
    moved = chain.transfers() - before
    assert moved <= 1, f"{mode}: a correct client paid {moved} times"


def test_chain_replay_guard_is_real(chain):
    """The same authorization twice moves money once — the guard we rely on works,
    which is why a fresh nonce (not a replay) is what actually double-charges."""
    before = chain.transfers()
    fac = OnChainFacilitator(chain=chain, mode=CLEAN)
    fac.settle("replay-proof")
    fac.settle("replay-proof")
    assert chain.transfers() - before == 1
