"""CLI: `hostile-facilitator serve` (test any client over the wire) and
`hostile-facilitator selftest` (prove the instrument is honest)."""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
from .adapter import HostileServer, ALL_MODES, battery_over_command, run_client_command
from .hostile import CLEAN, battery, scorecard
from .clients import naive_client, safe_client


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _verdict(r: dict) -> str:
    """pass | fail | not_exercised | error. Rows written before verdicts
    existed fall back to the old distinct<=1 rule."""
    if r.get("error"):
        return "error"
    return r.get("verdict") or ("pass" if r.get("passed") else "fail")


def result_document(command, rows, *, facilitator_env: str, started: float, finished: float) -> dict:
    """The run as data, not as terminal output.

    A printed scorecard cannot be signed, cannot be re-checked, and cannot be
    handed to anyone who wasn't watching the terminal. This is the same run in
    a form a third party can verify: which battery version produced it, what
    command was driven, and per mode how many DISTINCT settlements landed for
    one purchase.

    It never claims more than the run measured. A mode the client never
    reached is not counted safe, and a run whose clean control did not settle
    exactly once is INVALID: the command never made a normal purchase, so
    "no double payment" in the other modes means nothing.
    """
    from . import __version__
    clean = next((r for r in rows if r.get("mode") == CLEAN), {})
    harness_valid = clean.get("distinct") == 1
    verdicts = [(r["mode"], _verdict(r)) for r in rows]
    failed = [m for m, v in verdicts if v in ("fail", "error")]
    skipped = [m for m, v in verdicts if v == "not_exercised"]
    doubles = [r["mode"] for r in rows if (r.get("distinct") or 0) > 1]
    errors = [m for m, v in verdicts if v == "error"]
    if not harness_valid:
        verdict = "INVALID"
    elif failed:
        verdict = "FAIL"
    elif skipped:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"
    return {
        "tool": "hostile-facilitator",
        "version": __version__,
        "schema": "https://aurumflux.co/hostile-facilitator/result/v1",
        "target": {"command": list(command), "facilitator_env": facilitator_env},
        "battery": {"modes": list(ALL_MODES), "count": len(ALL_MODES)},
        "harness": {"valid": harness_valid, "clean_control_settlements": clean.get("distinct")},
        "started_at": _utc(started),
        "finished_at": _utc(finished),
        "results": rows,
        "summary": {
            "safe": sum(1 for _, v in verdicts if v == "pass") if harness_valid else 0,
            "total": len(rows),
            "verdict": verdict,
            "double_paying_modes": doubles,
            "errored_modes": errors,
            "not_exercised_modes": skipped,
        },
    }


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



def _write_json(json_out, command, rows, facilitator_env, started):
    doc = result_document(command, rows, facilitator_env=facilitator_env,
                          started=started, finished=time.time())
    out = Path(json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"\n  result written: {json_out}")
    return doc


def _test(command: list[str], facilitator_env: str, timeout: float, json_out: str = "") -> int:
    """Exit 0: no double payment seen. 1: a double payment (or an error).
    2: the harness is invalid, so nothing was scored."""
    started = time.time()
    # The clean control runs first. If one normal purchase does not settle
    # exactly once, the command is not making a purchase through us, and every
    # other mode would read "no double payment" for the wrong reason.
    clean = run_client_command(command, facilitator_env=facilitator_env, mode=CLEAN,
                               client_timeout_s=timeout)
    if clean.get("distinct") != 1:
        got = clean.get("error") or f"{clean.get('distinct')} settlement(s)"
        print(f"\n  hostile-facilitator — HARNESS INVALID: the clean control produced {got}, expected exactly 1.")
        print(f"    Nothing was scored. Check that the command reads ${facilitator_env}, makes one")
        print("    purchase, and that its seller accepts this facilitator's replies.")
        if json_out:
            _write_json(json_out, command, [clean], facilitator_env, started)
        return 2
    rest = battery_over_command(command, facilitator_env=facilitator_env, client_timeout_s=timeout,
                                modes=[m for m in ALL_MODES if m != CLEAN])
    rows = rest + [clean]
    safe = sum(1 for r in rows if _verdict(r) == "pass")
    print(f"\n  hostile-facilitator — your client: {safe}/{len(rows)} safe")
    for r in rows:
        v = _verdict(r)
        if v == "error":
            print(f"    [ERR ] {r['mode']:<22} {r['error']}"); continue
        n = r["distinct"]
        if v == "not_exercised":
            print(f"    [----] {r['mode']:<22} held, but never asked whether the payment landed — not counted safe")
            continue
        detail = ("exactly one settlement" if n == 1
                  else f"{n} distinct settlements for one purchase — DOUBLE PAY" if n and n > 1
                  else "no settlement")
        print(f"    [{'PASS' if v == 'pass' else 'FAIL'}] {r['mode']:<22} {detail}")
    if any(_verdict(r) in ("fail", "error") for r in rows):
        print("\n    → On an ambiguous outcome your client paid again. Treat unknown as UNKNOWN and")
        print("      re-present the SAME payment authorization on retry instead of minting a new nonce.")
    if json_out:
        _write_json(json_out, command, rows, facilitator_env, started)
        print("  sign it so a third party can check the claim without trusting the runner:")
        print(f"    coherence conformance {json_out} --out session.json")
        print("    coherence attest --session session.json --key <key> --anchor rekor")
    return 1 if any(_verdict(r) in ("fail", "error") for r in rows) else 0



def _proof(port: int) -> int:
    """On-chain proof: settle real EIP-3009 transfers on a local anvil node and
    count payments from Transfer logs. Removes the "not a live reproduction"
    caveat from any finding written off this battery."""
    try:
        from .chain import Chain, OnChainFacilitator, MERCHANT
    except Exception as e:                                   # pragma: no cover
        print(f"on-chain mode unavailable: {e}"); return 2
    from .hostile import ALL_MODES as MODES, Rechallenge

    def broken(fac, order):
        try: fac.settle(order)
        except Exception:
            try: fac.settle(order + "-retry-fresh-nonce")
            except Exception: pass

    def safe(fac, order):
        try: fac.settle(order)
        except Rechallenge: return
        except Exception:
            try:
                if fac.reconcile(order) == "settled": return
            except Exception: return
            try: fac.settle(order)
            except Exception: pass

    try:
        chain_cm = Chain(port=port)
    except Exception as e:
        print(f"on-chain mode unavailable: {e}"); return 2
    with chain_cm as ch:
        print(f"\n  local chain up — token {ch.token}  (anvil :{port})")
        rows = []
        for who, fn in (("naive (known-broken)", broken), ("safe (known-correct)", safe)):
            for mode in MODES:
                before = ch.transfers()
                fac = OnChainFacilitator(chain=ch, mode=mode, client_timeout_s=0.2)
                fn(fac, f"{who[:5]}-{mode}")
                rows.append((who, mode, ch.transfers() - before))
        print("\n  hostile-facilitator — ON-CHAIN proof (payments counted from Transfer logs)")
        for who in ("naive (known-broken)", "safe (known-correct)"):
            mine = [r for r in rows if r[0] == who]
            ok = sum(1 for _, m, n in mine if (n == 1 if m == "declared_safe" else n <= 1))
            print(f"\n    {who}: {ok}/{len(mine)} safe")
            for _, m, n in mine:
                good = (n == 1) if m == "declared_safe" else (n <= 1)
                word = ("exactly one transfer" if n == 1 else
                        f"{n} REAL transfers for one purchase — DOUBLE PAY" if n > 1 else "no transfer")
                print(f"      [{'PASS' if good else 'FAIL'}] {m:<22} {word}")
        naive_fails = sum(1 for w, m, n in rows if w.startswith("naive") and n > 1)
        safe_ok = all((n == 1 if m == "declared_safe" else n <= 1)
                      for w, m, n in rows if w.startswith("safe"))
        print(f"\n  instrument valid on-chain: {naive_fails > 0 and safe_ok}"
              f"   (broken client double-charged in {naive_fails} mode(s); safe client never did)")
        return 0 if (naive_fails > 0 and safe_ok) else 1


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
    pr = sub.add_parser("proof", help="on-chain proof: real EIP-3009 transfers on a local anvil node (needs foundry)")
    pr.add_argument("--port", type=int, default=8555, help="port for the local anvil node")
    tp = sub.add_parser("test", help="run YOUR client command through the whole battery and score it")
    tp.add_argument("--facilitator-env", default="FACILITATOR_URL",
                    help="env var your client reads the facilitator URL from (default FACILITATOR_URL)")
    tp.add_argument("--client-timeout", type=float, default=5.0)
    tp.add_argument("--json", default="", dest="json_out", metavar="PATH",
                    help="also write the run as a machine-readable result file (signable, re-checkable)")
    tp.add_argument("command", nargs=argparse.REMAINDER,
                    help="after --, the command that makes ONE purchase (reads the facilitator URL from the env var)")
    args = p.parse_args(argv)
    if args.cmd == "selftest":
        return _selftest()
    if args.cmd == "proof":
        return _proof(args.port)
    if args.cmd == "serve":
        return _serve(args.mode, args.client_timeout, args.client_timeout)
    if args.cmd == "test":
        cmd = args.command
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("usage: hostile-facilitator test [--facilitator-env VAR] -- <command that makes one purchase>")
            return 2
        return _test(cmd, args.facilitator_env, args.client_timeout, args.json_out)
    return 2

if __name__ == "__main__":
    sys.exit(main())
