# LATCH — the receiver obligation, executable

150 lines, no dependencies, bare `node`. Not a product, not a new algorithm, and
not something to install. It is the smallest artifact that **is** the rule, so an
implementer can read the rule instead of arguing about the prose.

```bash
node latch-test.mjs      # 14 properties
node latch-mutate.mjs    # 8 mutations — every property must be destroyable
```

## The rule

> **"Could not determine" is a terminal answer. It is never "did not happen."**

A payment settles. The reply is lost. Almost every implementation reads that as
failure and asks again — and a conforming client signs a **new** authorization,
so every replay and nonce guard correctly lets the second payment through. It is
not a replay. It is an unknown collapsed into a negative.

## Two worlds that can genuinely disagree

`A` is the payer side: what the sender believes it did. `B` is the world: what
actually landed.

**A move is not atomic across them.** `send()` debits A and puts the transfer in
flight. B does not see it until `settleWorld()` runs. Between those two moments
the worlds disagree — and that gap *is* the failure class.

This is the part most models get wrong. If one function writes both sides, the
bug it claims to prevent is unreachable, and every test passes for the wrong
reason.

| state | meaning |
|---|---|
| `sealed` | a latch exists, nothing sent |
| `inflight` | A sent, B has not confirmed — **not** failure, **not** success |
| `held` | a witness cannot be read; terminal for this intent, never reopened by guessing |
| `open` | both worlds independently say exactly one; only now may you spend |
| `broken` | some world says more than one; **sticky** — no later clean read un-breaks it |

## The properties, and why the mutation control matters

`latch-test.mjs` asserts 14 properties. `latch-mutate.mjs` breaks the model 8
different ways and re-runs the **unchanged** tests. Each mutation must turn its
test red. A test that stays green while the property it names is destroyed is a
lying test, and the script exits non-zero so CI says so.

```
  [CAUGHT ] forged file becomes evidence                      expected red: 3
  [CAUGHT ] send proceeds on an unknown witness               expected red: 8, 9
  [CAUGHT ] broken is not sticky (terminal flag dropped)      expected red: 12
  [CAUGHT ] terminal latches are reconciled again             expected red: 14
  [CAUGHT ] in-flight counts as open (B never consulted)      expected red: 4, 5, 6
  [CAUGHT ] spend allowed in any state                        expected red: 5
  [CAUGHT ] send credits B directly (worlds fused)            expected red: 4
  [CAUGHT ] a second debit is not counted                     expected red: 11

instrument valid: every mutation was caught by the test that claims that property.
```

Three of these escaped on the first run. Two were bad tests of ours — one titled
"open only when BOTH worlds say one" that only ever walked the happy path where B
already agreed, and one expectation that was simply wrong. The third was a real
hole: a `broken` latch could be **laundered into `held`** by losing a witness
afterwards, because an unknown read was allowed to downgrade a verdict that had
already been reached. That is now property 14, and the guard that prevents it is
load-bearing rather than decorative.

We publish that because it is the point. The control is what makes the number
mean anything.

## The two hard cases

Most suites in this space stop at the happy path. These are the two that matter:

**Property 9 — sent, then the answer is lost, then a retry.** Money has left A.
The witness goes dark. The client asks again. The latch **holds**: no second
debit, balance unchanged at 60, state `held`. The unknown is terminal for that
intent.

**Property 10 — the hold resolves truthfully, not by guessing.** The transfer
actually lands while the witness is dark. When the witness comes back, the latch
opens *because B now independently says one* — not because time passed, not
because a retry looked clean.

## What this is not

No network, no crypto, no persistence, no concurrency. `hash` is FNV-1a — a
checksum for a demo chain, not a security primitive. It models control flow and
nothing else. Real implementations need a durable ledger and a real chain read;
[Seal](https://github.com/aurumflux20/seal) is that, and this is the physics it
is built on, small enough to audit over coffee.

## Related

- **[hostile-facilitator](../)** — the battery. Tests a *client*: does yours
  re-present the same authorization when the answer is lost?
- **`hostile_facilitator/hostile_client.py`** — the mirror. Tests a
  *facilitator*: presented the same authorization twice, does it settle once and
  say so?
- **[Retry-Safety Index](https://aurumflux.co/retry-safety/)** — who passes,
  with evidence on every row.
