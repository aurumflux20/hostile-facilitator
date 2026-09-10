"""Real-client adapter — run the battery against an ACTUAL x402 payment client.

The battery in hostile.py judges retry behaviour by counting distinct settled
nonces. To test a real client we sit where its facilitator would: we intercept
the client's outbound settle/verify HTTP calls, model the ambiguous failure,
and read the payment nonce the client presents. A correct client re-presents
the same nonce on retry; a broken one mints a new one and we count two.

Two ways to point a real client at us, both zero-key and local:

1. FACILITATOR_URL — the common case. Most x402 clients take a facilitator
   base URL. Start `hostile-facilitator serve` and set the client's
   facilitator to http://127.0.0.1:<port>. Every /settle and /verify the
   client makes lands here; we inject the mode and count nonces from the
   x402 payment payload (EIP-3009 authorization.nonce, or the top-level
   `nonce`, or an Idempotency-Key header — whatever the client presents as
   the payment's stable identity).

2. In-process callback — for unit-testing a client library directly, pass a
   `client_pay(facilitator)` thunk to the Python battery (see clients.py).

This file implements (1): a tiny stdlib http.server that IS the hostile
facilitator, so any language's client can be tested over the wire.
"""
from __future__ import annotations
import json, time, threading
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .hostile import (ALL_MODES, RECONCILE_MODES, ACCEPT_THEN_TIMEOUT,
                      FIVE_XX_AFTER_SETTLE, DOUBLE_402, SLOW_ANSWER, CLEAN,
                      RECONCILE_UNAVAILABLE, RECONCILE_SOFT_404,
                      RECONCILE_OVERSIZED, RECONCILE_TRUNCATED)


def _extract_nonce(body: dict, headers) -> str | None:
    """Find the payment's stable identity in whatever shape the client sent.
    A client that presents the same one across a retry is safe; a client that
    sends a fresh one each time is the bug we are hunting."""
    # Idempotency-Key header (Stripe-style, and what many SDKs expose)
    for h in ("Idempotency-Key", "idempotency-key", "X-Idempotency-Key"):
        if headers.get(h):
            return headers.get(h)
    # x402 payment payload: authorization.nonce (EIP-3009) or top-level nonce
    payload = body.get("paymentPayload") or body.get("payment") or body
    auth = (payload.get("payload") or {}).get("authorization") if isinstance(payload, dict) else None
    if isinstance(auth, dict) and auth.get("nonce"):
        return str(auth["nonce"])
    for k in ("nonce", "authorizationNonce", "id"):
        if isinstance(payload, dict) and payload.get(k):
            return str(payload[k])
    return None


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        pass  # broken pipes on the timeout path are by design; do not print


class HostileServer:
    """The hostile facilitator, over HTTP. One instance = one purchase test in
    one mode. Counts distinct payment nonces presented to /settle."""
    def __init__(self, mode: str = CLEAN, client_timeout_s: float = 2.0):
        self.mode = mode
        self.client_timeout_s = client_timeout_s
        self.settled_nonces: set[str] = set()
        self.settle_calls = 0
        self.reconcile_calls = 0
        self.unidentified_calls = 0   # client sent no stable id at all — also a smell
        self._httpd = None
        self._thread = None
        self.port = None

    def _handler(self):
        server = self
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _read(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try: return json.loads(raw or b"{}")
                except Exception: return {}
            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                try:
                    self.send_response(code); self.send_header("Content-Type","application/json")
                    self.send_header("Content-Length", str(len(b))); self.end_headers()
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionError):
                    # Expected on the accept-then-timeout path: the client gave
                    # up before we answered. The settle still counted; the whole
                    # point is that the client cannot tell — so stay silent.
                    pass
            def _reconcile(self):
                """The 'did it land?' read, served exactly as a real one would
                arrive. In the three success-shaped modes the status is 200 and
                the transport is clean — the client's OWN plumbing is what
                decides whether a present record reads as absent."""
                server.reconcile_calls += 1
                q = self.path.split("?", 1)[1] if "?" in self.path else ""
                nonce = None
                for part in q.split("&"):
                    if part.startswith("nonce="):
                        nonce = unquote(part[6:])
                if nonce is None:
                    nonce = (self._read() or {}).get("nonce")
                settled = nonce in server.settled_nonces
                m = server.mode

                if m == RECONCILE_UNAVAILABLE:
                    # The clean half: the read fails loudly. Easy to handle right.
                    return self._json(503, {"error": "reconciliation unavailable"})

                if m == RECONCILE_SOFT_404:
                    body = (b"<html><body><h1>Record</h1><p>The record you "
                            b"requested is Not Available at this time. Please "
                            b"try again.</p></body></html>")
                    return self._raw(200, body, "text/html")

                if m == RECONCILE_OVERSIZED:
                    # The record IS in here. There is simply more body than a
                    # buffered read survives, so "absent" gets reported exactly
                    # when there was MORE evidence to read.
                    head = json.dumps({"found": settled, "nonce": nonce}).encode()
                    filler = b'{"unrelated":"' + (b"x" * (6 * 1024 * 1024)) + b'"}'
                    return self._raw(200, head + b"\n" + filler)

                if m == RECONCILE_TRUNCATED:
                    body = json.dumps({"found": settled, "nonce": nonce}).encode()
                    # Promise the full length, deliver half, then close: a real
                    # truncation on the wire, not a shorter valid document.
                    return self._raw(200, body[: max(1, len(body)//2)],
                                     content_length=len(body))

                return self._json(200, {"found": settled, "nonce": nonce})

            def do_GET(self):
                if self.path.split("?", 1)[0].rstrip("/").endswith("/reconcile"):
                    return self._reconcile()
                return self._json(404, {"error": "unknown route"})

            def _raw(self, code, body: bytes, ctype="application/json",
                     content_length=None):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length",
                                     str(content_length if content_length is not None
                                         else len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    if content_length is not None and content_length != len(body):
                        self.close_connection = True   # truncate on the wire
                except (BrokenPipeError, ConnectionError):
                    pass

            def do_POST(self):
                body = self._read()
                path = self.path.rstrip("/")
                if path.endswith("/reconcile"):
                    return self._reconcile()
                if path.endswith("/verify"):
                    return self._json(200, {"isValid": True})
                if not path.endswith("/settle"):
                    return self._json(404, {"error": "unknown route"})
                server.settle_calls += 1
                nonce = _extract_nonce(body, self.headers)
                if nonce is None:
                    server.unidentified_calls += 1
                    nonce = f"__anon-{server.settle_calls}"  # each anon call is a distinct payment
                already = nonce in server.settled_nonces
                if not already:
                    server.settled_nonces.add(nonce)         # the money moves, once per nonce
                if already:
                    return self._json(200, {"status":"settled","replay":True})
                m = server.mode
                if m == ACCEPT_THEN_TIMEOUT:
                    time.sleep(server.client_timeout_s + 1.0)   # outlast the client
                    return self._json(200, {"status":"settled"})  # client already gone
                if m == SLOW_ANSWER:
                    time.sleep(max(0.0, server.client_timeout_s - 0.3))
                    return self._json(200, {"status":"settled"})
                if m == FIVE_XX_AFTER_SETTLE:
                    return self._json(502, {"error":"bad gateway (settle already landed)"})
                if m == DOUBLE_402:
                    return self._json(402, {"error":"payment required","x402":True})
                if m in RECONCILE_MODES:
                    # The money moved; the news is lost. The client must now ask
                    # /reconcile — and that read is the thing on trial.
                    return self._json(504, {"error":"gateway timeout (settle may have landed)"})
                return self._json(200, {"status":"settled"})
        return H

    def __enter__(self):
        self._httpd = _QuietServer(("127.0.0.1", 0), self._handler())
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self
    def __exit__(self, *a):
        if self._httpd: self._httpd.shutdown()

    @property
    def distinct_payments(self) -> int:
        return len(self.settled_nonces)


# ---- orchestration: run a REAL client command through the whole battery ------

import os, subprocess

def run_client_command(command: list[str], *, facilitator_env: str = "FACILITATOR_URL",
                       mode: str = CLEAN, client_timeout_s: float = 5.0,
                       run_timeout_s: float = 30.0) -> "dict":
    """Start the hostile facilitator in `mode`, point the client at it via an
    env var, run the client command once (one logical purchase), and read how
    many distinct payments actually settled. The client must (a) read the
    facilitator URL from `facilitator_env` and (b) make exactly one purchase."""
    with HostileServer(mode=mode, client_timeout_s=client_timeout_s) as srv:
        env = dict(os.environ)
        env[facilitator_env] = f"http://127.0.0.1:{srv.port}"
        try:
            subprocess.run(command, env=env, timeout=run_timeout_s,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            pass  # a client that hangs on the ambiguous case still gets scored below
        except FileNotFoundError as e:
            return {"mode": mode, "error": f"client command not found: {e}", "distinct": None, "passed": False}
        n = srv.distinct_payments
        passed = n <= 1
        # A mode is only EVIDENCE if the client actually reached the thing under
        # test. In the reconcile modes the subject is the client's own read path;
        # a client that takes the 504 and gives up has not double-paid, but it has
        # not demonstrated anything about that path either. Scoring it "safe"
        # would be a verdict the run cannot support — the same false-pass shape
        # this battery exists to catch, so it is reported as its own state.
        exercised = (mode not in RECONCILE_MODES) or srv.reconcile_calls > 0
        verdict = "pass" if (passed and exercised) else ("fail" if not passed
                                                        else "not_exercised")
        return {"mode": mode, "distinct": n, "settle_calls": srv.settle_calls,
                "reconcile_calls": srv.reconcile_calls,
                "unidentified": srv.unidentified_calls,
                "exercised": exercised, "verdict": verdict, "passed": passed}


def battery_over_command(command: list[str], *, facilitator_env: str = "FACILITATOR_URL",
                         client_timeout_s: float = 5.0, run_timeout_s: float = 30.0) -> list[dict]:
    return [run_client_command(command, facilitator_env=facilitator_env, mode=m,
                               client_timeout_s=client_timeout_s, run_timeout_s=run_timeout_s)
            for m in ALL_MODES]


def summarize(results: list[dict], label: str) -> str:
    """Print what the run can actually support, and nothing more."""
    passed = sum(1 for r in results if r.get("verdict") == "pass")
    failed = [r for r in results if r.get("verdict") == "fail"]
    skipped = [r for r in results if r.get("verdict") == "not_exercised"]
    lines = [f"\n  hostile-facilitator — {label}: {passed}/{len(results)} proven safe"]
    for r in results:
        v = r.get("verdict", "?")
        mark = {"pass": "PASS", "fail": "FAIL", "not_exercised": "----"}.get(v, "????")
        note = ""
        if v == "fail":
            note = f"{r['distinct']} distinct settlements for one purchase — DOUBLE PAY"
        elif v == "not_exercised":
            note = ("held, but never performed the reconciliation read — this mode "
                    "proved nothing about your read path")
        else:
            note = "exactly one settlement" if r.get("distinct") == 1 else "no settlement"
        lines.append(f"    [{mark}] {r['mode']:<22} {note}")
    if skipped:
        lines.append(f"    → {len(skipped)} mode(s) not exercised. Not a pass: the "
                     f"client never asked whether the payment landed.")
    if failed:
        lines.append("    → On an ambiguous outcome, a retry minted a fresh nonce and paid again.")
    return "\n".join(lines)
