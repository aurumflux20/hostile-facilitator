"""The hostile facilitator must speak the real x402 wire format.

If a real seller cannot parse our replies it fails on FORMAT, before any retry
behaviour is reached — and every mode then reads as "one settlement, safe".
That is a false pass, so the shape is tested here against the x402 v2 schema
(SettleResponse needs success/transaction/network; /supported must exist).
"""
import json
import sys
import urllib.request

import pytest

from hostile_facilitator.adapter import HostileServer, run_client_command
from hostile_facilitator.cli import main, result_document
from hostile_facilitator.hostile import ALL_MODES, CLEAN, FIVE_XX_AFTER_SETTLE, RECONCILE_SOFT_404

V2_BODY = {
    "x402Version": 2,
    "paymentPayload": {
        "x402Version": 2,
        "accepted": {"scheme": "exact", "network": "eip155:84532"},
        "payload": {"authorization": {"from": "0xBuyer", "nonce": "0xabc"}},
    },
    "paymentRequirements": {"scheme": "exact", "network": "eip155:84532"},
}


def _post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


def test_supported_lists_exact_kinds_so_a_seller_can_start():
    with HostileServer(mode=CLEAN) as srv:
        doc = _get(srv.port, "/supported")
    kinds = {(k["x402Version"], k["scheme"], k["network"]) for k in doc["kinds"]}
    assert (2, "exact", "eip155:84532") in kinds
    assert (1, "exact", "base-sepolia") in kinds


def test_settle_reply_has_the_v2_required_fields_and_echoes_the_network():
    with HostileServer(mode=CLEAN) as srv:
        r = _post(srv.port, "/settle", V2_BODY)
    assert r["success"] is True
    assert r["network"] == "eip155:84532"
    assert r["payer"] == "0xBuyer"
    assert r["transaction"].startswith("0x") and len(r["transaction"]) == 66


def test_a_replayed_nonce_returns_the_same_transaction_not_a_second_payment():
    with HostileServer(mode=CLEAN) as srv:
        first = _post(srv.port, "/settle", V2_BODY)
        again = _post(srv.port, "/settle", V2_BODY)
        assert srv.distinct_payments == 1
    assert again["transaction"] == first["transaction"]
    assert again["replay"] is True


def test_verify_reply_names_the_payer():
    with HostileServer(mode=CLEAN) as srv:
        r = _post(srv.port, "/verify", V2_BODY)
    assert r == {"isValid": True, "payer": "0xBuyer"}


def test_replies_parse_with_the_official_x402_models_when_installed():
    schemas = pytest.importorskip("x402.schemas")
    with HostileServer(mode=CLEAN) as srv:
        schemas.SupportedResponse.model_validate(_get(srv.port, "/supported"))
        schemas.VerifyResponse.model_validate(_post(srv.port, "/verify", V2_BODY))
        s = schemas.SettleResponse.model_validate(_post(srv.port, "/settle", V2_BODY))
    assert s.success and s.network == "eip155:84532"


def test_the_client_is_told_the_timeout_the_battery_assumes():
    probe = [sys.executable, "-c",
             "import os,json,urllib.request;u=os.environ['FACILITATOR_URL'];"
             "t=os.environ['HOSTILE_CLIENT_TIMEOUT'];"
             "urllib.request.urlopen(urllib.request.Request(u+'/settle',"
             "data=json.dumps({'nonce':'t'+t}).encode(),"
             "headers={'Content-Type':'application/json'},method='POST'),timeout=5)"]
    r = run_client_command(probe, mode=CLEAN, client_timeout_s=3.0)
    assert r["distinct"] == 1, "the probe read HOSTILE_CLIENT_TIMEOUT and settled once"


# ---- a harness that never completes a purchase is not a score --------------

def test_cli_refuses_to_score_a_command_that_never_purchases(capsys):
    code = main(["test", "--client-timeout", "1", "--", sys.executable, "-c", "pass"])
    out = capsys.readouterr().out
    assert code == 2
    assert "HARNESS INVALID" in out
    assert "safe" not in out.split("HARNESS INVALID")[0].lower()


def _rows(**over):
    rows = [{"mode": m, "distinct": 1, "settle_calls": 1, "unidentified": 0, "passed": True,
             "verdict": "pass", "exercised": True} for m in ALL_MODES]
    for r in rows:
        r.update(over.get(r["mode"], {}))
    return rows


def _doc(rows):
    return result_document(["./pay.sh"], rows, facilitator_env="FACILITATOR_URL",
                           started=1788600000.0, finished=1788600042.0)


def test_document_marks_an_unexercised_mode_and_does_not_count_it_safe():
    d = _doc(_rows(**{RECONCILE_SOFT_404: {"verdict": "not_exercised", "exercised": False}}))
    assert d["summary"]["safe"] == len(ALL_MODES) - 1
    assert d["summary"]["not_exercised_modes"] == [RECONCILE_SOFT_404]
    assert d["summary"]["verdict"] == "INCOMPLETE"


def test_document_with_no_clean_settlement_is_invalid_not_pass():
    d = _doc(_rows(**{m: {"distinct": 0, "settle_calls": 0} for m in ALL_MODES}))
    assert d["harness"] == {"valid": False, "clean_control_settlements": 0}
    assert d["summary"]["verdict"] == "INVALID"
    assert d["summary"]["safe"] == 0


def test_a_double_pay_still_fails_even_if_other_modes_were_not_exercised():
    d = _doc(_rows(**{FIVE_XX_AFTER_SETTLE: {"distinct": 2, "passed": False, "verdict": "fail"},
                      RECONCILE_SOFT_404: {"verdict": "not_exercised", "exercised": False}}))
    assert d["summary"]["verdict"] == "FAIL"
    assert d["summary"]["double_paying_modes"] == [FIVE_XX_AFTER_SETTLE]
