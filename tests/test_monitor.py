import json
from pathlib import Path

from wis2node import monitor as mon

FIXTURES = Path(__file__).parent / "fixtures"
GB_EVENTS = json.loads((FIXTURES / "gb-validation-events.json").read_text())


def test_live_gb_validation_events():
    with_errors, without = GB_EVENTS
    for ev in GB_EVENTS:
        assert mon.classify(ev) == mon.WNM_EVENT
        assert mon.wnm_id(ev) == "47505241-def9-411f-ae4a-cefa53abc7a2"
        assert mon.severity(ev) == "ERROR"
        assert mon.event_time(ev) is not None
    assert "rel=canonical" in mon.errors(with_errors)[0]
    assert mon.errors(without) == []


SPEC_ETS = {  # shape from the WMEM draft spec example
    "specversion": "1.0", "type": "int.wmo.wis.wme.event.wcmp2.ets",
    "source": "ca-eccc-msc-global-discovery-catalogue", "subject": "de-dwd",
    "id": "e1", "time": "2026-10-30T10:00:00Z",
    "data": {"severity": "INFO", "content": {
        "id": "r1", "report_type": "ets", "metadata_id": "urn:wmo:md:de-dwd:icon-eps.ALL",
        "summary": {"PASSED": 11, "FAILED": 1, "SKIPPED": 0},
        "tests": [{"id": "http://wis.wmo.int/spec/wcmp/2/conf/core/identifier", "code": "FAILED",
                   "message": "Invalid centre_id"},
                  {"id": "http://wis.wmo.int/spec/wcmp/2/conf/core/title", "code": "PASSED"}]}}}


def test_spec_ets_report():
    assert mon.classify(SPEC_ETS) == mon.ETS_REPORT
    assert mon.metadata_id(SPEC_ETS) == "urn:wmo:md:de-dwd:icon-eps.ALL"
    (f,) = mon.ets_failures(SPEC_ETS)
    assert mon.short_test_id(f) == "identifier"


def test_ets_report_nested_and_type_variants():
    ev = json.loads(json.dumps(SPEC_ETS))
    ev["type"] = "int.wmo.wis.wme.something.new"
    ev["data"]["content"] = {"title": "x", "report": ev["data"]["content"]}
    assert mon.classify(ev) == mon.ETS_REPORT
    assert mon.metadata_id(ev) == "urn:wmo:md:de-dwd:icon-eps.ALL"


def test_ets_failed_count_without_details():
    ev = json.loads(json.dumps(SPEC_ETS))
    ev["data"]["content"]["tests"] = []
    (f,) = mon.ets_failures(ev)
    assert "1 failed" in f["message"]


def test_kpi_report_and_other():
    kpi = {"type": "int.wmo.wis.wme.event.wcmp2.kpi",
           "data": {"content": {"report_type": "kpi", "metadata_id": "m",
                                "summary": {"percentage": 89.13, "grade": "A"}}}}
    assert mon.classify(kpi) == mon.KPI_REPORT
    assert mon.kpi_summary(kpi)["grade"] == "A"
    assert mon.classify({"type": "int.wmo.wis.wme.event", "data": {"content": {"title": "notice"}}}) == mon.OTHER
    assert mon.classify({}) == mon.OTHER and mon.wnm_id({}) is None and mon.metadata_id({}) is None
