# hostile-facilitator

[![Retry-Safety](https://img.shields.io/badge/retry--safety-checked-2ea44f?logo=shieldsdotio)](https://github.com/aurumflux20/hostile-facilitator)

> This battery is the conformance suite of the draft **MCP retry-safety proposal** ([SEP working draft](https://github.com/YoadElkayam/mcp-fuse/tree/main/sep), discussion [#3188](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/3188)) — ported into its `tools/call` fixture and used to verify the reference implementation, where it caught two real double-executions before scoring it 7/7.

**An AI agent that pays twice for one order is a refund storm, a chargeback, and a trust problem — and it happens on a dropped connection, not a bug you'd catch in review. This tells you in 60 seconds whether your agent does it.**

Here's the trap. A payment settles on-chain, and *then* the connection drops — a timeout, a 502, whatever. Your client reads that as "failed," retries, and sends a fresh payment. Both go through. Your customer paid twice, and every log on your side shows one clean payment after one transient error. Nobody notices until the refunds start.

The happy path and the clean-failure path both get tested. The settled-but-looks-failed path almost never does — because you need a facilitator that misbehaves on cue. This is that facilitator: it does the worst-moment things real ones do, and counts how many times your agent *actually* paid for one order. One is safe. Two is the money you're about to lose.

`hostile-facilitator` is the adversary. It stands in for the facilitator, deliberately produces each ambiguous failure, and — because every settle passes through it — counts how many distinct payments your client *actually* made for one purchase. One is safe. Two is a real double-charge, caught.

No keys. No chain. No real money. Just your client's retry behaviour, which is where the bug lives.

## 60 seconds

```bash
pip install "git+https://github.com/aurumflux20/hostile-facilitator@v0.1.0"

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
      - uses: aurumflux20/hostile-facilitator@v0.1.0
        with:
          client-command: "python scripts/pay_once.py"   # your one-purchase client
```

It posts a sticky PR comment with the scorecard and fails the build on a double-pay.
Earn the badge for your README once it's green:

```md
[![Retry-Safety](https://img.shields.io/badge/retry--safety-checked-2ea44f?logo=shieldsdotio)](https://github.com/aurumflux20/hostile-facilitator)
```

## If it fails, and you want the whole path checked

This tool tests one purchase against five failure modes. It won't tell you whether the
*rest* of your money path holds — the reservation lifecycle, every settle-timeout
branch, or whether what you believe you spent matches what actually settled.

We do that as a fixed-scope **Retry Safety Review**: one money path, five working days,
a written report tied to your own file and line numbers. **$1,200, refunded in full if
we find nothing.** [Book it](https://buy.stripe.com/28E7sL91C9naapQbBVdIA0l) ·
[what's involved](https://github.com/aurumflux20/seal/blob/main/SUPPORT.md)

It's the same reading that found the bugs behind this tool — four projects have shipped
fixes from it, including a company running 1M+ paid API calls a month.

---

MIT © AurumFlux AI, Inc — part of the [Seal](https://github.com/aurumflux20/seal) work on exactly-once for agents that move money.
