"""Hostile CLIENT — the other half of the battery.

`adapter.py` plays a hostile *facilitator* to test a **client**: does the client
re-present the same authorization when the answer is lost, or mint a fresh one
and pay twice?

This module is the mirror. It plays a hostile *client* to test a **facilitator**:
when the same signed authorization arrives more than once — because the first
reply never came back — does the facilitator settle it exactly once and say so,
or does it settle again, or deny a payment that already landed?

Nobody could answer that question about their own facilitator without doing it by
hand. Every finding we filed this week was produced this way manually. This is
that procedure, executed and counted.

WHAT THIS ASSERTS — properties, not implementations
---------------------------------------------------
A facilitator is retry-safe when, for ONE signed authorization presented N times:

  P1  exactly one settlement exists                          (no double settle)
  P2  an outcome it cannot determine is never reported as
      terminal failure                                        ("could not
                                                                determine" is
                                                                never "did not
                                                                happen")
  P3  once a transaction has been broadcast, every subsequent
      answer about that authorization carries its hash        (reconcilable)
  P4  a terminal rejection is a real rejection — insufficient
      funds, bad signature — never a manufactured one         (no invented
                                                                failures)

P1 is counted, never inferred from response equality. Two responses that look
identical are not evidence of one settlement; two distinct transaction hashes
are evidence of two.

WHAT IT CANNOT DO — stated up front
-----------------------------------
We are the client here, so we cannot inject faults inside the facilitator. We can
only do what a real client does when the network fails it: abandon the read and
present again. That covers the shape that produced every finding we have filed,
and it does not cover faults that need privileged access to reproduce.

A verdict here is about the authorizations we presented, on the network we
presented them on. It is not a proof of correctness for all inputs.

MONEY SAFETY
------------
Presenting a real signed authorization to a real facilitator MOVES REAL MONEY,
and the entire point of this tool is to find out whether it moves twice. It
refuses to run against a non-test network unless the caller passes
`i_understand_this_spends_real_money=True`, and it never presents more
authorizations than the caller supplied.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Networks we will act on without an explicit override. Anything not listed is
# treated as real money.
TEST_NETWORKS = frozenset({
    "base-sepolia", "eip155:84532",
    "sepolia", "eip155:11155111",
    "solana-devnet", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    "avalanche-fuji", "eip155:43113",
    "polygon-amoy", "eip155:80002",
})

# The probes. Each one presents the SAME authorization; they differ in how the
# first presentation is interrupted.
RE_PRESENT = "re_present_same_auth"
ABANDONED_REPLY = "abandoned_reply"
IMMEDIATE_DOUBLE = "immediate_double_present"
ALL_PROBES = [RE_PRESENT, ABANDONED_REPLY, IMMEDIATE_DOUBLE]


class MainnetRefused(RuntimeError):
    """Raised rather than spend real money by accident."""


@dataclass
class Answer:
    """One facilitator response, normalized."""
    http_status: int | None
    success: bool | None
    tx_hash: str | None
    error_reason: str | None
    raw: Any
    abandoned: bool = False          # we hung up before reading it

    @property
    def is_terminal_failure(self) -> bool:
        """Says 'this did not happen' — success false with a reason that is not
        an explicitly pending/indeterminate one."""
        if self.success is not False:
            return False
        reason = (self.error_reason or "").lower()
        pending_words = ("pending", "unknown", "indeterminate", "not_yet",
                         "in_flight", "unresolved", "settlement_pending")
        return not any(w in reason for w in pending_words)


SAFE = "SAFE"
UNSAFE = "UNSAFE"
UNDETERMINED = "UNDETERMINED"


@dataclass
class ProbeResult:
    probe: str
    answers: list[Answer] = field(default_factory=list)
    settlements: set[str] = field(default_factory=set)
    verdict: str = UNDETERMINED
    detail: str = ""

    @property
    def settlement_count(self) -> int:
        return len(self.settlements)

    @property
    def passed(self) -> bool:
        """Only an observed pass is a pass. UNDETERMINED is never safe — that is
        the same rule we grade facilitators against, applied to ourselves."""
        return self.verdict == SAFE

    @property
    def failed(self) -> bool:
        return self.verdict == UNSAFE


def _norm(raw: Any, http_status: int | None) -> Answer:
    """Pull the three things that matter out of whatever shape came back."""
    if not isinstance(raw, dict):
        return Answer(http_status, None, None, None, raw)
    success = raw.get("success")
    if success is None and "settled" in raw:
        success = bool(raw.get("settled"))
    # x402 v2 settlement-status vocabulary (spec §5.3.3). `status` is
    # authoritative over `success` where both appear: terminality comes from the
    # status, never from the code.
    st = raw.get("status")
    if isinstance(st, str):
        s = st.strip().lower()
        if s == "settled":
            success = True
        elif s in ("pending", "deferred_until", "blocked", "canceled", "expired"):
            success = False
            if not raw.get("errorReason") and not raw.get("error_reason"):
                raw = {**raw, "errorReason": s}
    tx = (raw.get("transaction") or raw.get("txHash") or raw.get("tx_hash")
          or raw.get("transactionHash") or raw.get("signature") or None)
    # An empty string is not a hash. This distinction is the whole point of P3.
    if isinstance(tx, str) and not tx.strip():
        tx = None
    reason = (raw.get("errorReason") or raw.get("error_reason")
              or raw.get("error") or raw.get("reason") or None)
    if reason is not None and not isinstance(reason, str):
        reason = json.dumps(reason)
    return Answer(http_status, success, tx, reason, raw)


def _post(url: str, body: dict, *, timeout: float,
          abandon_after: float | None = None) -> Answer:
    """POST a settle. If abandon_after is set, hang up mid-read — the facilitator
    has our payment and we never learn the outcome. That is the whole failure
    class, reproduced honestly from the client side."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "hostile-facilitator/probe (retry-safety)"},
    )
    if abandon_after is not None:
        try:
            resp = urllib.request.urlopen(req, timeout=abandon_after)
            resp.read(1)          # touch the stream, then walk away
            resp.close()
        except Exception:
            pass                   # the abandonment is the point, not an error
        return Answer(None, None, None, None, None, abandoned=True)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode(errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:                     # 4xx/5xx with a body
        payload, status = e.read().decode(errors="replace"), e.code
    except Exception as e:                                   # timeout, refused…
        return Answer(None, None, None, f"transport: {type(e).__name__}", None)
    try:
        return _norm(json.loads(payload), status)
    except json.JSONDecodeError:
        return _norm(payload, status)


def _network_of(requirements: dict) -> str:
    return str(requirements.get("network") or requirements.get("chain") or "")


def probe(
    settle_url: str,
    payment_payload: dict | list[dict],
    payment_requirements: dict,
    *,
    probes: Iterable[str] = ALL_PROBES,
    timeout: float = 20.0,
    abandon_after: float = 0.35,
    settle_gap: float = 1.5,
    i_understand_this_spends_real_money: bool = False,
    poster: Callable[..., Answer] | None = None,
) -> list[ProbeResult]:
    """Present a signed authorization to a live facilitator under each probe and
    count how many distinct settlements it produces.

    `payment_payload` is the exact body your client would send. We never modify
    the authorization — a modified one is a different payment and proves nothing.

    **One authorization per probe.** Each probe needs its own freshly signed
    authorization: once the first probe settles one, every later probe using it
    would only be exercising the replay path, and a facilitator that fails the
    fresh path could pass by looking safe on the replay. Pass a list of
    payloads, one per probe. A single payload is accepted only for a single
    probe.
    """
    probes = list(probes)
    payloads = ([payment_payload] if isinstance(payment_payload, dict)
                else list(payment_payload))
    if len(payloads) < len(probes):
        raise ValueError(
            f"{len(probes)} probes need {len(probes)} separately signed authorizations, got "
            f"{len(payloads)}. Reusing one authorization means every probe after the first "
            f"tests only the replay path — a facilitator that re-broadcasts a FRESH payment "
            f"would pass by looking correct on the replay. Sign one per probe, or run one "
            f"probe at a time."
        )
    nonces = [json.dumps(p, sort_keys=True) for p in payloads[:len(probes)]]
    if len(set(nonces)) != len(nonces):
        raise ValueError("two probes were given identical authorizations — see above")

    network = _network_of(payment_requirements)
    if not i_understand_this_spends_real_money and network not in TEST_NETWORKS:
        raise MainnetRefused(
            f"network {network!r} is not a known test network. This probe presents a real "
            f"signed authorization and is designed to detect a SECOND settlement — on a live "
            f"network that means real money moved twice. Re-run with "
            f"i_understand_this_spends_real_money=True only if you own this money path and "
            f"accept that outcome."
        )

    post = poster or _post
    results: list[ProbeResult] = []

    for name, payload in zip(probes, payloads):
        body = {"paymentPayload": payload,
                "paymentRequirements": payment_requirements}
        r = ProbeResult(probe=name)

        if name == ABANDONED_REPLY:
            r.answers.append(post(settle_url, body, timeout=timeout,
                                  abandon_after=abandon_after))
            time.sleep(settle_gap)
            r.answers.append(post(settle_url, body, timeout=timeout))
        elif name == IMMEDIATE_DOUBLE:
            r.answers.append(post(settle_url, body, timeout=timeout))
            r.answers.append(post(settle_url, body, timeout=timeout))
        else:                                                # RE_PRESENT
            r.answers.append(post(settle_url, body, timeout=timeout))
            time.sleep(settle_gap)
            r.answers.append(post(settle_url, body, timeout=timeout))

        for a in r.answers:
            if a.tx_hash:
                r.settlements.add(a.tx_hash)

        _judge(r)
        results.append(r)

    return results


def _judge(r: ProbeResult) -> None:
    """Apply P1–P3 to one probe's answers. Order matters: a double settle is the
    worst outcome and must not be masked by a later, tidier answer."""
    n = r.settlement_count
    heard = [a for a in r.answers if not a.abandoned]
    last = heard[-1] if heard else None
    # We hung up on a presentation, so its outcome was never observed. Anything
    # it settled is invisible to a hash count — a failed lookup is not a zero.
    blind = any(a.abandoned for a in r.answers)

    if n > 1:
        r.verdict = UNSAFE
        r.detail = (f"{n} distinct settlements for one authorization — DOUBLE SETTLE "
                    f"({', '.join(sorted(s[:14] + '…' for s in r.settlements))})")
        return

    # P2/P3: after we presented a payment, a terminal 'no' carrying no hash is
    # the category error — it cannot be distinguished from 'it never happened'.
    if last is not None and last.is_terminal_failure and not last.tx_hash:
        r.verdict = UNSAFE
        r.detail = (f"terminal failure with no transaction hash "
                    f"(errorReason={last.error_reason!r}) — a client cannot tell this from "
                    f"'never settled' and will re-sign; if the first presentation did land, "
                    f"that is a second payment")
        return

    if blind:
        # A single observed hash proves nothing here: we cannot tell "it answered
        # from its ledger" from "it broadcast a second time and this is the new
        # one." Only an on-chain count can close this, and we do not have one.
        r.verdict = UNDETERMINED
        r.detail = ("first presentation was abandoned, so its outcome was never observed — "
                    "a single hash afterwards cannot distinguish a ledger answer from a "
                    "re-broadcast. Count transfers for this authorization on-chain to "
                    "settle it. No safety claim is made from this probe alone")
        return

    if n == 1 and last is not None and last.success:
        r.verdict = SAFE
        r.detail = "exactly one settlement, re-presentation answered from the ledger"
        return
    if n == 1:
        r.verdict = SAFE
        r.detail = (f"exactly one settlement; re-presentation answered "
                    f"{last.error_reason!r} but carried the hash — reconcilable"
                    if last and last.tx_hash else "exactly one settlement")
        return
    if last is not None and not last.is_terminal_failure and last.success is False:
        r.verdict = SAFE
        r.detail = f"held as non-terminal ({last.error_reason!r}) — correct, nothing settled"
        return

    r.verdict = UNDETERMINED
    r.detail = "no settlement and no interpretable answer — no verdict is available"


def scorecard(results: list[ProbeResult]) -> str:
    safe = sum(1 for r in results if r.passed)
    unsafe = sum(1 for r in results if r.failed)
    undet = len(results) - safe - unsafe
    head = f"\n  facilitator retry-safety: {safe}/{len(results)} probes safe"
    if undet:
        head += f", {undet} undetermined (not a pass)"
    lines = [head]
    for r in results:
        tag = {SAFE: "PASS", UNSAFE: "FAIL", UNDETERMINED: "????"}[r.verdict]
        lines.append(f"    [{tag}] {r.probe:<26} {r.detail}")
    if unsafe:
        lines.append("\n    A facilitator that cannot answer a re-presented authorization "
                     "from its own ledger\n    forces the client to guess. The fix: record the "
                     "broadcast before waiting for\n    the receipt, and answer a "
                     "re-presentation from that record, carrying the hash.")
    return "\n".join(lines) + "\n"
