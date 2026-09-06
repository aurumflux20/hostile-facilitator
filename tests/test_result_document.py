"""The run as data. A printed scorecard cannot be signed or re-checked;
this document can. It must never claim more than the battery measured."""
import json
from hostile_facilitator import __version__
from hostile_facilitator.cli import result_document
from hostile_facilitator.adapter import ALL_MODES

PASS_ROWS = [{"mode": m, "distinct": 1, "settle_calls": 1, "unidentified": 0, "passed": True}
             for m in ALL_MODES]


def _fail_rows():
    rows = [dict(r) for r in PASS_ROWS]
    rows[0].update(distinct=2, settle_calls=2, passed=False)
    return rows


def _doc(rows):
    return result_document(["./pay.sh"], rows, facilitator_env="FACILITATOR_URL",
                           started=1788600000.0, finished=1788600042.0)


def test_document_names_the_version_that_produced_it():
    d = _doc(PASS_ROWS)
    assert d["version"] == __version__
    assert d["tool"] == "hostile-facilitator"
    assert d["battery"]["count"] == len(ALL_MODES)


def test_clean_run_is_a_pass_with_no_double_paying_modes():
    d = _doc(PASS_ROWS)
    assert d["summary"] == {"safe": len(ALL_MODES), "total": len(ALL_MODES),
                            "verdict": "PASS", "double_paying_modes": [], "errored_modes": []}


def test_one_double_payment_makes_the_whole_run_fail_and_names_the_mode():
    d = _doc(_fail_rows())
    assert d["summary"]["verdict"] == "FAIL"
    assert d["summary"]["double_paying_modes"] == [ALL_MODES[0]]
    assert d["summary"]["safe"] == len(ALL_MODES) - 1


def test_an_errored_mode_is_never_counted_as_safe():
    rows = [dict(r) for r in PASS_ROWS]
    rows[1] = {"mode": ALL_MODES[1], "error": "client command not found", "distinct": None, "passed": False}
    d = _doc(rows)
    assert d["summary"]["verdict"] == "FAIL"
    assert ALL_MODES[1] in d["summary"]["errored_modes"]
    assert ALL_MODES[1] not in d["summary"]["double_paying_modes"]


def test_document_is_json_serialisable_and_records_the_target():
    d = _doc(PASS_ROWS)
    round_trip = json.loads(json.dumps(d))
    assert round_trip["target"]["command"] == ["./pay.sh"]
    assert round_trip["started_at"].endswith("Z")
