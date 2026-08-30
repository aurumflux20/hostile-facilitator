"""The instrument must catch a broken client and clear a safe one — over the
wire AND in-process. If it can't tell them apart it cannot judge anyone."""
import json, urllib.request
from hostile_facilitator.hostile import battery
from hostile_facilitator.clients import naive_client, safe_client
from hostile_facilitator.adapter import HostileServer, ALL_MODES


def test_in_process_catches_naive_clears_safe():
    naive = battery(naive_client, "naive")
    safe = battery(safe_client, "safe")
    assert sum(r.passed for r in safe) == len(safe)      # safe passes all
    assert sum(r.passed for r in naive) < len(naive)     # naive gets caught
    # specifically caught on the ambiguous modes, not the clean ones
    caught = {r.mode for r in naive if not r.passed}
    assert "accept_then_timeout" in caught
    assert "5xx_after_settle" in caught
    assert "clean" not in caught


def _post(port, path, body, timeout):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def test_over_the_wire_double_pay_is_counted():
    # a real HTTP client that mints a fresh nonce on the timeout retry pays twice
    with HostileServer(mode="accept_then_timeout", client_timeout_s=1.0) as srv:
        for attempt in (1, 2, 3):
            body = {"paymentPayload": {"payload": {"authorization": {"nonce": f"n{attempt}"}}}}
            try: _post(srv.port, "/settle", body, timeout=1.0)
            except Exception: pass
        assert srv.distinct_payments > 1   # DOUBLE PAY caught


def test_over_the_wire_same_nonce_settles_once():
    with HostileServer(mode="5xx_after_settle", client_timeout_s=1.0) as srv:
        for _ in range(3):
            body = {"paymentPayload": {"payload": {"authorization": {"nonce": "stable"}}}}
            try: _post(srv.port, "/settle", body, timeout=1.0)
            except Exception: pass
        assert srv.distinct_payments == 1   # re-presented nonce → exactly once


def test_no_stable_id_is_flagged_unsafe():
    with HostileServer(mode="clean") as srv:
        for _ in range(2):
            try: _post(srv.port, "/settle", {"paymentPayload": {}}, timeout=1.0)
            except Exception: pass
        assert srv.unidentified_calls >= 1   # no idempotency identity = smell


def test_battery_over_a_real_subprocess_command(tmp_path):
    """The `test` path drives an external client process and scores it."""
    from hostile_facilitator.adapter import battery_over_command
    broken = tmp_path / "c.py"
    broken.write_text(
        "import os,json,urllib.request\n"
        "u=os.environ['FACILITATOR_URL']\n"
        "for a in (1,2,3):\n"
        "  b={'paymentPayload':{'payload':{'authorization':{'nonce':f'n{a}'}}}}\n"
        "  try:\n"
        "    urllib.request.urlopen(urllib.request.Request(u+'/settle',data=json.dumps(b).encode(),headers={'Content-Type':'application/json'}),timeout=2);break\n"
        "  except Exception: continue\n")
    import sys
    rows = battery_over_command([sys.executable, str(broken)], client_timeout_s=1.0)
    # broken client double-pays on ambiguous modes, clean on control
    assert any(r["mode"] == "clean" and r["passed"] for r in rows)
    assert any(not r["passed"] and (r["distinct"] or 0) > 1 for r in rows)
