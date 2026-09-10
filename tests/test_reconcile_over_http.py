"""The reconcile read over the wire, where the client's own plumbing is on trial.

In-process, `Facilitator.reconcile()` does the tri-state mapping FOR the caller,
so a client that would mis-parse a real body can still pass. Over HTTP there is
no such help: the client gets the bytes and decides for itself. That is the
surface these three modes are actually about.
"""
import json
import urllib.request
import urllib.error
import pytest

from hostile_facilitator.adapter import HostileServer, run_client_command
from hostile_facilitator.hostile import (
    RECONCILE_SOFT_404, RECONCILE_OVERSIZED, RECONCILE_TRUNCATED,
    RECONCILE_UNAVAILABLE, CLEAN,
)

PLUMBING = [RECONCILE_SOFT_404, RECONCILE_OVERSIZED, RECONCILE_TRUNCATED]


def _settle(port, nonce):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/settle",
        data=json.dumps({"nonce": nonce}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def _reconcile_raw(port, nonce):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/reconcile?nonce={nonce}", method="GET")
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return None, b""


@pytest.mark.parametrize("mode", PLUMBING)
def test_settle_is_ambiguous_and_the_money_moved(mode):
    with HostileServer(mode=mode) as srv:
        assert _settle(srv.port, "n1") == 504
        assert srv.distinct_payments == 1


@pytest.mark.parametrize("mode", PLUMBING)
def test_the_read_never_yields_an_authoritative_absent(mode):
    """The one property that matters. Soft-404 and oversized arrive as a clean
    200; truncation breaks at the transport layer, which a strict reader raises
    on. Either way the client must reach "could not determine" — what it must
    never reach is a confident "no, that payment never happened", because the
    payment did happen and it is about to be made again."""
    with HostileServer(mode=mode) as srv:
        _settle(srv.port, "n1")
        status, body = _reconcile_raw(srv.port, "n1")
        if mode is RECONCILE_TRUNCATED:
            assert status is None or status == 200
        else:
            assert status == 200, "the bad news must not arrive as a status code"
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        assert parsed is None or parsed.get("found") is not False, (
            "a settled record must never come back as an authoritative 'absent'")


def test_unavailable_is_the_easy_half():
    with HostileServer(mode=RECONCILE_UNAVAILABLE) as srv:
        _settle(srv.port, "n1")
        status, _ = _reconcile_raw(srv.port, "n1")
        assert status == 503, "the clean half announces itself; that is why it is easy"


def test_oversized_body_really_is_oversized_and_holds_the_record():
    with HostileServer(mode=RECONCILE_OVERSIZED) as srv:
        _settle(srv.port, "n1")
        status, body = _reconcile_raw(srv.port, "n1")
        assert len(body) > 64 * 1024
        assert b'"found": true' in body or b'"found":true' in body


def test_clean_mode_still_answers_honestly():
    """The negative control for the route itself: if /reconcile could not return
    a true 'found', every hold it produces would be meaningless."""
    with HostileServer(mode=CLEAN) as srv:
        _settle(srv.port, "n1")
        status, body = _reconcile_raw(srv.port, "n1")
        assert status == 200 and json.loads(body)["found"] is True
        status, body = _reconcile_raw(srv.port, "never-settled")
        assert json.loads(body)["found"] is False


# ---- the false-pass guard ---------------------------------------------------

@pytest.mark.parametrize("mode", PLUMBING + [RECONCILE_UNAVAILABLE])
def test_a_client_that_never_reconciles_is_not_scored_safe(mode):
    """The defect I nearly shipped: with no /reconcile route, a client that took
    the 504 and stopped would settle once, score n<=1, and be reported SAFE on a
    mode it never touched. Holding is not the same as proving."""
    quiet = ["python3", "-c",
             "import os,json,urllib.request;"
             "u=os.environ['FACILITATOR_URL'];"
             "req=urllib.request.Request(u+'/settle',"
             "data=json.dumps({'nonce':'n1'}).encode(),"
             "headers={'Content-Type':'application/json'},method='POST');"
             "\ntry:\n urllib.request.urlopen(req,timeout=5)\nexcept Exception:\n pass"]
    r = run_client_command(quiet, mode=mode, run_timeout_s=20)
    assert r["distinct"] <= 1, "it did not double-pay"
    assert r["passed"] is True, "the double-pay property held"
    assert r["reconcile_calls"] == 0
    assert r["exercised"] is False
    assert r["verdict"] == "not_exercised", "must not be reported as proven safe"


def test_a_client_that_reconciles_and_holds_is_scored_safe():
    tristate = ["python3", "-c",
                "import os,json,urllib.request;"
                "u=os.environ['FACILITATOR_URL'];"
                "req=urllib.request.Request(u+'/settle',"
                "data=json.dumps({'nonce':'n1'}).encode(),"
                "headers={'Content-Type':'application/json'},method='POST');"
                "\ntry:\n urllib.request.urlopen(req,timeout=5)\nexcept Exception:\n pass"
                "\ntry:\n urllib.request.urlopen(u+'/reconcile?nonce=n1',timeout=15).read()\nexcept Exception:\n pass"]
    r = run_client_command(tristate, mode=RECONCILE_SOFT_404, run_timeout_s=25)
    assert r["reconcile_calls"] >= 1
    assert r["exercised"] is True
    assert r["verdict"] == "pass"
