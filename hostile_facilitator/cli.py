"""CLI: `hostile-facilitator serve` (test any client over the wire) and
`hostile-facilitator selftest` (prove the instrument is honest)."""
from __future__ import annotations
import argparse, sys, time
from .adapter import HostileServer, ALL_MODES, battery_over_command
from .hostile import battery, scorecard
from .clients import naive_client, safe_client


def _selftest() -> int:
    naive = battery(naive_client, "naive (known-broken)")
    safe = battery(safe_client, "safe (known-correct)")
    print(scorecard(naive, "naive client (known-broken)"))
    print(scorecard(safe, "safe client (known-correct)"))
    ok = sum(r.passed for r in safe) == len(safe) and sum(r.passed for r in naive) < len(naive)
    print(f"\ninstrument valid: {ok}")
    return 0 if ok else 1


def _serve(mode: str, timeout: float, hold: float) -> int:
    srv = HostileServer(mode=mode, client_timeout_s=timeout)
    srv.__enter__()
    print(f"""
hostile-facilitator — listening on http://127.0.0.1:{srv.port}   (mode: {mode})

  Point your x402 client's facilitator URL at the address above, then drive
  ONE purchase through it. Endpoints: POST /verify , POST /settle.
  The client's payment nonce (EIP-3009 authorization.nonce, top-level nonce,
  or an Idempotency-Key header) is how we recognise a re-presented payment.

  When your client finishes (or times out and retries), stop this with Ctrl-C.
  A correct client settles ONCE; a client that mints a fresh nonce on the
  ambiguous retry settles more than once — that's the double-pay.
""")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.__exit__()
    n = srv.distinct_payments
    verdict = "PASS — exactly one settlement" if n <= 1 else f"FAIL — {n} settlements for one purchase (DOUBLE PAY)"
    print(f"\n  settle calls: {srv.settle_calls}   distinct payments: {n}\n  [{'PASS' if n<=1 else 'FAIL'}] {verdict}")
    if srv.unidentified_calls:
        print(f"  note: {srv.unidentified_calls} settle call(s) carried no stable payment id — "
              "a client with no idempotency identity cannot be safe under retries.")
    return 0 if n <= 1 else 1



def _test(command: list[str], facilitator_env: str, timeout: float) -> int:
    rows = battery_over_command(command, facilitator_env=facilitator_env, client_timeout_s=timeout)
    safe = sum(1 for r in rows if r["passed"])
    print(f"\n  hostile-facilitator — your client: {safe}/{len(rows)} safe")
    for r in rows:
        if r.get("error"):
            print(f"    [ERR ] {r['mode']:<22} {r['error']}"); continue
        n = r["distinct"]
        detail = ("exactly one settlement" if n == 1
                  else f"{n} distinct settlements for one purchase — DOUBLE PAY" if n and n > 1
                  else "no settlement (did the client read $%s and make one purchase?)" % facilitator_env)
        print(f"    [{'PASS' if r['passed'] else 'FAIL'}] {r['mode']:<22} {detail}")
    if safe < len(rows):
        print("\n    → On an ambiguous outcome your client paid again. Treat unknown as UNKNOWN and")
        print("      re-present the SAME payment authorization on retry instead of minting a new nonce.")
    return 0 if safe == len(rows) else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hostile-facilitator",
        description="Retry-safety conformance battery for x402 payment clients.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the hostile facilitator; point a client at it")
    s.add_argument("--mode", choices=ALL_MODES, default="accept_then_timeout",
                   help="which ambiguous failure to inject on the first settle")
    s.add_argument("--client-timeout", type=float, default=5.0,
                   help="seconds the client is assumed to wait before giving up")
    sub.add_parser("selftest", help="prove the instrument catches a broken client and clears a safe one")
    tp = sub.add_parser("test", help="run YOUR client command through the whole battery and score it")
    tp.add_argument("--facilitator-env", default="FACILITATOR_URL",
                    help="env var your client reads the facilitator URL from (default FACILITATOR_URL)")
    tp.add_argument("--client-timeout", type=float, default=5.0)
    tp.add_argument("command", nargs=argparse.REMAINDER,
                    help="after --, the command that makes ONE purchase (reads the facilitator URL from the env var)")
    args = p.parse_args(argv)
    if args.cmd == "selftest":
        return _selftest()
    if args.cmd == "serve":
        return _serve(args.mode, args.client_timeout, args.client_timeout)
    if args.cmd == "test":
        cmd = args.command
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("usage: hostile-facilitator test [--facilitator-env VAR] -- <command that makes one purchase>")
            return 2
        return _test(cmd, args.facilitator_env, args.client_timeout)
    return 2

if __name__ == "__main__":
    sys.exit(main())
