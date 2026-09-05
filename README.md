# hostile-facilitator

[![Retry-Safety](https://img.shields.io/badge/retry--safety-checked-2ea44f?logo=shieldsdotio)](https://github.com/aurumflux20/hostile-facilitator)

> This battery is the conformance suite of the draft **MCP retry-safety proposal** ([SEP working draft](https://github.com/YoadElkayam/mcp-fuse/tree/main/sep), discussion [#3188](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/3188)) — ported into its `tools/call` fixture and used to verify the reference implementation, where it caught two real double-executions before scoring it 7/7.

> **Free to be listed.** [Submit any implementation for grading](https://github.com/aurumflux20/hostile-facilitator/issues/new?template=submit.yml) — yours or someone else's — and it gets read and put on the public [Retry-Safety Index](https://aurumflux.co/retry-safety/) at no cost. If a finding is confirmed you are **counted, never named**, until you ship a fix; when you do, the row goes up with credit and your time-to-fix. Only a full battery run against a *live* system is paid, and nothing above requires it.

**An AI agent that pays twice for one order is a refund storm, a chargeback, and a trust problem — and it happens on a dropped connection, not a bug you'd catch in review. This tells you in 60 seconds whether your agent does it.**

Here's the trap. A payment settles on-chain, and *then* the connection drops — a timeout, a 502, whatever. Your client reads that as "failed," retries, and sends a fresh payment. Both go through. Your customer paid twice, and every log on your side shows one clean payment after one transient error. Nobody notices until the refunds start.

The happy path and the clean-failure path both get tested. The settled-but-looks-failed path almost never does — because you need a facilitator that misbehaves on cue. This is that facilitator: it does the worst-moment things real ones do, and counts how many times your agent *actually* paid for one order. One is safe. Two is the money you're about to lose.

`hostile-facilitator` is the adversary. It stands in for the facilitator, deliberately produces each ambiguous failure, and — because every settle passes through it — counts how many distinct payments your client *actually* made for one purchase. One is safe. Two is a real double-charge, caught.

No keys. No chain. No real money. Just your client's retry behaviour, which is where the bug lives.

## 60 seconds

```bash
pip install "git+https://github.com/aurumflux20/hostile-facilitator@v0.1.1"

# prove the instrument is honest (catches a broken client, clears a safe one):
hostile-facilitator selftest

# test YOUR client in one command — it runs the whole battery for you.
# Give it a command that makes ONE purchase and reads the facilitator URL
# from an env var (default FACILITATOR_URL):
hostile-facilitator test -- your-client --pay-once
#   → 5/5 safe, or a FAIL row per ambiguous failure your client double-pays on.

# or drive it by hand against one failure mode:
hostile-facilitator serve --mode accept_then_timeout
```


## Proof on a real chain

The battery above models settlement in memory — fast and honest, but a finding
written from it carries a caveat: *not a live reproduction*. `proof` removes the
caveat. It starts a local [anvil](https://getfoundry.sh) node, deploys an EIP-3009
token with the same `transferWithAuthorization` / `authorizationState` surface real
USDC exposes, and settles for real — counting payments from `Transfer` logs on the
chain rather than from its own bookkeeping.

```bash
hostile-facilitator proof     # needs foundry: curl -L https://foundry.paradigm.xyz | bash && foundryup
```

```
  hostile-facilitator - ON-CHAIN proof (payments counted from Transfer logs)

    naive (known-broken): 3/7 safe
      [FAIL] accept_then_timeout    2 REAL transfers for one purchase - DOUBLE PAY
      [FAIL] 5xx_after_settle       2 REAL transfers for one purchase - DOUBLE PAY
      [FAIL] double_402             2 REAL transfers for one purchase - DOUBLE PAY
      [FAIL] reconcile_unavailable  2 REAL transfers for one purchase - DOUBLE PAY

    safe (known-correct): 7/7 safe

  instrument valid on-chain: True
```

Two transfers for one purchase is no longer an argument about control flow: it is
two entries in a ledger anyone can re-read. `tests/test_chain.py` asserts both
directions and skips itself when foundry is absent.

## The failure modes

Each one leaves the world in the same true state — the payment **settled** — and hands your client a signal that's easy to misread as "it failed, try again":

| mode | what it does |
|---|---|
| `accept_then_timeout` | settles, then hangs past your client's timeout |
| `5xx_after_settle` | settles, then returns 502 |
| `double_402` | re-challenges a request that already paid |
| `slow_answer` | settles, answers just under the wire |
| `reconcile_unavailable` | settles ambiguously, **and the "did it land?" read also fails** |
| `declared_safe` | the tool declares replay is safe — checks you're not *over*-refusing |
| `clean` | control: settles, answers 200 |

The last two matter because a retry gate fails in two directions. Everything else
here asks "did you fire twice?" — `declared_safe` asks "did you refuse work that
was safe?", and `reconcile_unavailable` asks the hardest one: when the effect may
have landed *and* the read that would tell you is broken, do you hold? "Could not
determine" is terminal; a client that reads it as "didn't happen" and retries has
reintroduced the exact double-pay the read exists to prevent.

## How it recognises a payment

Your client's stable payment identity — an EIP-3009 `authorization.nonce`, a top-level `nonce`, or an `Idempotency-Key` header — is how the facilitator knows a re-presented payment from a brand-new one. A client that sends **no** stable identity can't be safe under retries, and the tool says so.

## The fix, when it fails

Treat an ambiguous outcome as `unknown`, never as `failed`. On retry, re-present the **same** authorization; let the facilitator settle it once. Don't mint a fresh nonce for a payment you already sent.

## Gate every PR (GitHub Action)

Keep a client retry-safe forever: drop this into `.github/workflows/retry-safety.yml`
and every PR that makes your payment client double-pay fails the check.

```yaml
name: Retry-Safety
on: [pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - uses: aurumflux20/hostile-facilitator@v0.1.1
        with:
          client-command: "python scripts/pay_once.py"   # your one-purchase client
```

It posts a sticky PR comment with the scorecard and fails the build on a double-pay.
Earn the badge for your README once it's green:

```md
[![Retry-Safety](https://img.shields.io/badge/retry--safety-checked-2ea44f?logo=shieldsdotio)](https://github.com/aurumflux20/hostile-facilitator)
```

## Conformance (x402 §5.3.6)

§5.3.6 of the x402 settlement-status amendment makes a conformance claim *checkable* instead of declarative: name the battery you ran, count **settlements actually recorded** rather than response bodies, and carry a **mutation control** — evidence the same battery fails against an implementation known to be unsafe. The clause that does the work: *a self-administered pass reported without a control is a declaration, not a verification.* (Committed in [zjzJoez/x402#1](https://github.com/zjzJoez/x402/pull/1) against [x402-foundation/x402#3325](https://github.com/x402-foundation/x402/pull/3325); under review, not yet ratified.)

This battery satisfies the first two requirements and ships its own mutation control (`hostile-facilitator selftest`), so you can run it yourself and see exactly where you stand — free, MIT, no signup. That part never costs anything.

What you cannot self-issue is the third-party half. We run the battery against your live facilitator or client and issue a **signed conformance result you can publish**: findings within five business days with any failing case reproduced in full, and a clean run signed within 24 hours.

**Founding rate — the first three implementations to carry a public result: $300** ([checkout](https://buy.stripe.com/14A6oHb9K1UI69A21ldIA0o)), on one condition: the result is published on the [Retry-Safety Index](https://aurumflux.co/retry-safety/), because a verification nobody can check isn't one. Standard rate after the founding three is $1,200.

## If it fails, and you want the whole path checked

This tool tests one purchase against the ambiguous-failure battery. It won't tell you whether the
*rest* of your money path holds — the reservation lifecycle, every settle-timeout branch, or
whether what you believe you spent matches what actually settled.

Two ways to take it further, both written-only, no calls:

- **Attestation run — $1,200.** We run the full battery against your *live* endpoint and issue a
  signed result you can publish. Findings within five business days; a clean run signed within
  24 hours. [Checkout](https://buy.stripe.com/28E7sL91C9naapQbBVdIA0l)
- **Money-path review — $12,000, fixed scope.** Every path that moves or counts money, each one
  graded, the unsafe ones with a failing case that reproduces it — file and line, 7–10 days.
  **If we can't show you a real double-fire on a path you actually run, there is no invoice.**
  [Details](https://aurumflux.co/)

It's the same reading that found the bugs behind this tool — four projects have shipped fixes
from it, including a company running 1M+ paid API calls a month.

---

MIT © AurumFlux AI, Inc — part of the [Seal](https://github.com/aurumflux20/seal) work on exactly-once for agents that move money.
