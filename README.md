# hostile-facilitator

[![Retry-Safety](https://img.shields.io/badge/retry--safety-checked-2ea44f?logo=shieldsdotio)](https://github.com/aurumflux20/hostile-facilitator)

**Point your x402 payment client at a facilitator that fails the way real ones do — and find out in 60 seconds if it double-pays.**

A real payment facilitator, at the worst moment, does things that *look* like failure but aren't: it settles the payment and then the connection drops, or it returns a 502 after the money already moved, or it times out after accepting. A correct client treats that ambiguous outcome as **unknown** and, on retry, re-presents the **same** payment authorization so exactly one settlement happens. A broken client mints a fresh nonce on retry — and pays twice.

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

## The failure modes

Each one leaves the world in the same true state — the payment **settled** — and hands your client a signal that's easy to misread as "it failed, try again":

| mode | what it does |
|---|---|
| `accept_then_timeout` | settles, then hangs past your client's timeout |
| `5xx_after_settle` | settles, then returns 502 |
| `double_402` | re-challenges a request that already paid |
| `slow_answer` | settles, answers just under the wire |
| `clean` | control: settles, answers 200 |

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

---

MIT © AurumFlux AI, Inc — part of the [Seal](https://github.com/aurumflux20/seal) work on exactly-once for agents that move money.
