"""Two reference x402 clients, so the battery can be proven honest before it
judges anyone else's code: it MUST fail the broken one and clear the safe one."""
from .hostile import (ProviderError, Rechallenge, ReconcileUnavailable,
                      TimeoutError as FacTimeout)

# A stable per-purchase authorization identity. In real x402 this is the
# EIP-3009 nonce / idempotency key the client puts in the payment header.

def naive_client(fac):
    """BROKEN: on any ambiguous failure, it 'retries the payment' by minting a
    FRESH nonce — so the facilitator sees a brand-new payment and settles again.
    This is the exact bug we file against real clients."""
    attempt = 0
    while attempt < 3:
        attempt += 1
        nonce = f"naive-{attempt}"          # <-- new identity every retry: the bug
        try:
            return fac.settle(nonce)
        except (FacTimeout, ProviderError, Rechallenge):
            continue                         # "failed — pay again"
    return None


def safe_client(fac):
    """CORRECT: one authorization for the purchase. On an ambiguous failure the
    outcome is UNKNOWN, so it re-presents the SAME nonce; the facilitator
    recognises it and settles exactly once. It never fabricates a new payment."""
    nonce = "safe-fixed-nonce"              # <-- stable identity across retries
    attempt = 0
    while attempt < 3:
        attempt += 1
        try:
            return fac.settle(nonce)
        except (FacTimeout, ProviderError, Rechallenge):
            # Unknown outcome. Before retrying, ask whether it already landed.
            try:
                verdict = fac.reconcile(nonce)
            except ReconcileUnavailable:
                # "Could not determine" is TERMINAL. We cannot prove the effect
                # is absent, so we must not retry. Hold and surface.
                return None
            if verdict == "found_once":
                return {"status": "settled", "replay": True}   # settle, don't replay
            # Authoritatively absent: re-present the SAME authorization.
            continue
    return None
