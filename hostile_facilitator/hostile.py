"""Hostile Facilitator — a retry-safety conformance battery for x402 payment clients.

A real facilitator, at the worst moment, does things that look like failure but
aren't: it settles the payment on-chain and THEN the connection drops, or it
returns a 502 after the money already moved, or it times out after accepting.
A correct client treats that ambiguous outcome as UNKNOWN and, on retry,
re-presents the SAME payment authorization (same nonce) so the facilitator can
recognise it and settle exactly once. A broken client mints a fresh nonce on
retry — and pays twice.

This tool is the adversary. It stands in for the facilitator, deliberately
producing each ambiguous failure, and — because every settle attempt passes
through it — it can COUNT how many distinct payments a client actually made for
one logical purchase. One = safe. Two = a real double-pay, caught.

No keys, no chain, no real money: the "payment" is a signed authorization the
harness models directly. The point is the client's retry behaviour, which is
where the bug lives.
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, field


# ---- the hostile facilitator ------------------------------------------------

# Failure modes it can inject on the FIRST settle of a purchase. Each one leaves
# the world in the same true state — the payment SETTLED — but hands the client
# a signal that is easy to misread as "it failed, try again".
ACCEPT_THEN_TIMEOUT = "accept_then_timeout"   # settle, then hang past client timeout
FIVE_XX_AFTER_SETTLE = "5xx_after_settle"     # settle, then return 502
DOUBLE_402 = "double_402"                     # re-challenge a paid request once
SLOW_ANSWER = "slow_answer"                   # settle, answer just under the wire
CLEAN = "clean"                               # control: settle, answer 200

# The reconciliation read itself fails. This is the recursive case: the client is
# unsure whether the effect landed, asks the provider, and the ASKING fails too.
# "Could not determine" is terminal — a client that reads it as "absent" and
# retries reintroduces the exact double-pay the read exists to prevent.
RECONCILE_UNAVAILABLE = "reconcile_unavailable"

# The half of that class which actually ships. Here the reconciliation read does
# NOT raise — it returns, with a success status, carrying something that is not
# an answer. Every one of these is settled in truth, so a client that derives
# "absent" from a failed parse, an empty match or a status code pays twice.
#
# Origin: solim, MCP discussion #3188 (9 Sep 2026), from two production bugs —
# `grep -q` under `pipefail` turning SIGPIPE into 141 and reading it as "no
# match" above the 64KB pipe buffer, and `grep -c` returning 1 for a zero count,
# conflating "genuinely none" with "the command failed".
RECONCILE_SOFT_404 = "reconcile_soft_404"     # HTTP 200 + "not available" prose
RECONCILE_OVERSIZED = "reconcile_oversized"   # record present, body past the buffer
RECONCILE_TRUNCATED = "reconcile_truncated"   # body cut mid-record; parse yields nothing

# The control for over-refusal. The tool is declared safe to replay, so a client
# SHOULD retry freely. A gate that refuses here is the fail-annoying direction:
# it bricks legitimate work to avoid a duplicate that was never possible.
DECLARED_SAFE = "declared_safe"

# Modes where the reconciliation read is the thing under test. The first is the
# clean half (the read fails loudly); the other three are the success-shaped half.
RECONCILE_MODES = [RECONCILE_UNAVAILABLE, RECONCILE_SOFT_404,
                   RECONCILE_OVERSIZED, RECONCILE_TRUNCATED]

ALL_MODES = [ACCEPT_THEN_TIMEOUT, FIVE_XX_AFTER_SETTLE, DOUBLE_402, SLOW_ANSWER,
             *RECONCILE_MODES, DECLARED_SAFE, CLEAN]


@dataclass
class Facilitator:
    """Stands in for the settlement provider. Records every settle it accepts,
    keyed by the payment's nonce — exactly what an EIP-3009 nonce or an
    idempotency key buys you: the provider can recognise a re-presented payment."""
    mode: str = CLEAN
    client_timeout_s: float = 0.5
    _settled_nonces: set[str] = field(default_factory=set)   # the on-chain truth
    _settle_calls: int = 0
    _reconcile_calls: int = 0

    @property
    def declares_safe_to_replay(self) -> bool:
        """What the tool declares about itself. Only DECLARED_SAFE says replay is
        safe; every other mode leaves the effect class unsafe."""
        return self.mode == DECLARED_SAFE

    def reconcile_raw(self, nonce: str) -> tuple[int, bytes]:
        """The read as it arrives on the wire, before anyone interprets it.

        This is the surface the plumbing bugs live on: in the three
        success-shaped modes the status is 200 and nothing raises, so a client
        that trusts the status, or that treats a failed parse as "absent",
        convicts itself here rather than at the provider."""
        self._reconcile_calls += 1
        settled = nonce in self._settled_nonces
        record = {"found": settled, "nonce": nonce if settled else None}

        if self.mode == RECONCILE_SOFT_404:
            # 200 with prose. No status code carries the bad news.
            return 200, (b"<html><body><h1>Record</h1><p>The record you requested "
                         b"is Not Available at this time. Please try again.</p>"
                         b"</body></html>")
        if self.mode == RECONCILE_OVERSIZED:
            # The record IS here. There is simply more body than a buffered read
            # survives — so "absent" gets reported precisely when there was MORE
            # evidence to read, which is what made solim's take days to surface.
            head = json.dumps(record).encode()
            filler = b'{"unrelated":"' + (b"x" * (6 * 1024 * 1024)) + b'"}'
            return 200, head + b"\n" + filler
        if self.mode == RECONCILE_TRUNCATED:
            body = json.dumps(record).encode()
            return 200, body[: max(1, len(body) // 2)]   # cut mid-record

        if self.mode == RECONCILE_UNAVAILABLE:
            raise ReconcileUnavailable("the reconciliation read itself failed")
        return 200, json.dumps(record).encode()

    def reconcile(self, nonce: str) -> str:
        """The 'did this land?' read as a VERDICT — the tri-state solim argues
        for: found / absent / could-not-determine, where the third is terminal
        and is never collapsed into the second.

        Every success-shaped non-answer maps to 'could not determine'. Answering
        'absent' from a soft-404, an oversized body or a truncated parse is the
        defect this mode exists to catch, so the reference read must not do it."""
        if self.mode in (RECONCILE_SOFT_404, RECONCILE_OVERSIZED,
                         RECONCILE_TRUNCATED):
            self.reconcile_raw(nonce)   # count the call; the answer is unusable
            raise ReconcileUnavailable(
                f"the reconciliation read returned a success-shaped non-answer "
                f"({self.mode}) — could not determine, which is terminal")
        self._reconcile_calls += 1
        if self.mode == RECONCILE_UNAVAILABLE:
            raise ReconcileUnavailable("the reconciliation read itself failed")
        if nonce in self._settled_nonces:
            return "found_once"
        return "absent"

    def settle(self, nonce: str) -> dict:
        """One settle attempt. Returns what the client observes, and raises the
        transport failures a real provider raises AFTER the money has moved."""
        self._settle_calls += 1

        # Idempotent core: a nonce already settled never settles again. This is
        # the property a correct client leans on by re-presenting the same nonce.
        already = nonce in self._settled_nonces
        if not already:
            self._settled_nonces.add(nonce)   # <-- the money moves here, once per nonce

        # Now misbehave — but only on the FIRST, genuinely-new settlement, which
        # is the ambiguous case. A recognised replay always answers cleanly.
        if already:
            return {"status": "settled", "replay": True}

        if self.mode == DECLARED_SAFE:
            # Declared safe to replay: the server dedupes internally, so a replay
            # is harmless. Answer cleanly; the point is whether the client dares.
            return {"status": "settled", "replay": False}
        if self.mode in (RECONCILE_UNAVAILABLE, RECONCILE_SOFT_404,
                         RECONCILE_OVERSIZED, RECONCILE_TRUNCATED):
            # Ambiguous like accept-then-timeout, but the reconcile read will
            # also fail to give a usable answer — so the client cannot resolve
            # it and must hold. The money HAS moved; only the news is lost.
            raise ProviderError(504, "gateway timeout (settle may have landed)")
        if self.mode == ACCEPT_THEN_TIMEOUT:
            time.sleep(self.client_timeout_s + 0.2)   # client gives up first
            raise TimeoutError("no response (but the settle already landed)")
        if self.mode == SLOW_ANSWER:
            time.sleep(max(0.0, self.client_timeout_s - 0.1))
            return {"status": "settled", "replay": False}
        if self.mode == FIVE_XX_AFTER_SETTLE:
            raise ProviderError(502, "bad gateway (but the settle already landed)")
        if self.mode == DOUBLE_402:
            raise Rechallenge()   # "pay again" — a correct client must not
        return {"status": "settled", "replay": False}

    @property
    def distinct_payments(self) -> int:
        return len(self._settled_nonces)


class ProviderError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(f"{code} {msg}"); self.code = code
class Rechallenge(Exception):
    pass
class ReconcileUnavailable(Exception):
    """The reconciliation read failed. Verdict is 'could not determine' — terminal."""
    pass
class TimeoutError(Exception):  # noqa: A001 - deliberately shadow for a self-contained model
    pass


# ---- the battery ------------------------------------------------------------

@dataclass
class Result:
    mode: str
    distinct_payments: int
    passed: bool
    detail: str


def run_purchase(client_pay, mode: str) -> Result:
    """Drive ONE logical purchase through a client against a facilitator in `mode`.
    `client_pay(facilitator)` performs the client's own pay+retry logic.
    We then read the ground truth: how many distinct payments actually settled.

    Most modes ask "did you over-fire?" — one settlement or fewer passes. Two
    modes invert that, because a gate can fail in both directions:

    * DECLARED_SAFE asks "did you over-REFUSE?" The tool says replay is safe, so
      a client that never completes the purchase is failing — being too cautious
      is a real bug, not a conservative virtue.
    * RECONCILE_UNAVAILABLE asks "did you respect 'could not determine'?" The
      effect may have landed and the read that would settle it is broken, so the
      only safe outcome is to hold at one-or-fewer and surface."""
    fac = Facilitator(mode=mode)
    try:
        client_pay(fac)
    except Exception:
        # A client that surfaces the ambiguous failure as an error is fine —
        # what matters is whether it PAID twice, which we read below.
        pass
    n = fac.distinct_payments

    if mode == DECLARED_SAFE:
        # Over-refusal check: the purchase must actually complete, exactly once.
        ok = n == 1
        detail = ("exactly one settlement" if n == 1
                  else "never completed a purchase the tool declared safe to replay "
                       "— gate is over-cautious"
                  if n == 0 else f"{n} settlements even though dedup was promised")
        return Result(mode, n, ok, detail)

    ok = n <= 1
    if mode in RECONCILE_MODES:
        if mode == RECONCILE_UNAVAILABLE:
            how = "failed"
        else:
            how = "returned a success-shaped non-answer"
        detail = ("held on an unresolvable outcome (correct: 'could not determine' "
                  "is terminal)" if n <= 1
                  else f"{n} settlements — retried after the reconciliation read "
                       f"{how}, collapsing 'could not determine' into 'absent'")
        return Result(mode, n, ok, detail)

    detail = ("exactly one settlement" if n == 1
              else f"{n} distinct settlements for one purchase — DOUBLE PAY"
              if n > 1 else "no settlement")
    return Result(mode, n, ok, detail)


def battery(client_pay, label: str) -> list[Result]:
    return [run_purchase(client_pay, m) for m in ALL_MODES]


def scorecard(results: list[Result], label: str) -> str:
    passed = sum(r.passed for r in results)
    lines = [f"\n  hostile-facilitator — {label}: {passed}/{len(results)} safe"]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines.append(f"    [{mark}] {r.mode:<22} {r.detail}")
    if passed < len(results):
        lines.append("    → On an ambiguous outcome, a retry minted a fresh nonce and paid again.")
        lines.append("      The fix: treat unknown as UNKNOWN, re-present the SAME authorization on retry.")
    return "\n".join(lines)


# ---- the instrument check ---------------------------------------------------

def reconcile_controls(client_reconcile) -> tuple[bool, str]:
    """Exercise a client's OWN reconciliation checker against a case that must
    answer 'settled' and one that must answer 'not settled'.

    solim's rule, on MCP discussion #3188: *a checker validated only against the
    negative is indistinguishable from a function that returns a constant.* Both
    of their production bugs passed every negative test they had, because both
    only ever returned the negative.

    This is the mutation control applied to the read itself: before any verdict
    from a reconciliation checker means anything, the checker has to be shown
    capable of producing both answers.

    `client_reconcile(facilitator, nonce)` returns the client's verdict string.
    """
    # Positive control: the payment really did settle, and the read is clean.
    pos = Facilitator(mode=CLEAN)
    pos.settle("nonce-present")
    try:
        got_pos = client_reconcile(pos, "nonce-present")
    except Exception as exc:
        return False, (f"positive control: checker raised {exc!r} on a record that "
                       f"is definitely present")
    if got_pos != "found_once":
        return False, (f"positive control FAILED: a settled record read as "
                       f"{got_pos!r}. A checker that cannot say 'found' is "
                       f"indistinguishable from one that returns a constant, and "
                       f"every 'absent' it has ever returned is unevidenced.")

    # Negative control: nothing was ever settled, and the read is clean.
    neg = Facilitator(mode=CLEAN)
    try:
        got_neg = client_reconcile(neg, "nonce-never-settled")
    except Exception as exc:
        return False, f"negative control: checker raised {exc!r} on an absent record"
    if got_neg != "absent":
        return False, (f"negative control FAILED: an unsettled record read as "
                       f"{got_neg!r} — the checker cannot say 'not settled', so it "
                       f"will hold forever on payments that never happened.")

    return True, "reconciliation checker can return both answers"
