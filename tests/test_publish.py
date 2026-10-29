import json
from pathlib import Path

import pytest

from fakes import FakeWIS2
from wis2node import publish, wnm
from wis2node.helper import Helper, Job, JsonlLog, Router
from wis2node.mqtt import BrokerError
from wis2node.pages import PagesError

ROOT = Path(__file__).parent.parent
PASSWORD = "pw"


@pytest.fixture
def net():
    return FakeWIS2()


@pytest.fixture
def fast_cfg(cfg, monkeypatch):
    monkeypatch.delenv("LOCAL_BROKER_URL", raising=False)
    cfg["timeouts"].update(delivery=1, monitor=1, pages_live=1, broker_connect=1)
    cfg["local_broker"]["url"] = "mqtts://local.example:8883"   # in Actions: the LOCAL_BROKER_URL secret
    return cfg


def job(record, action="canonical", path="metadata/obs.json"):
    data = json.dumps(record).encode()
    deletion = action == "deletion"
    return Job(action=action, source_path=path, metadata_id=record["id"],
               url=f"https://pages.example/{path}", geometry=record["geometry"],
               datetime=wnm.record_datetime(record),
               sha512=None if deletion else wnm.sha512_b64(data), length=None if deletion else len(data))


def run(cfg, net, jobs, tmp_path, **kw):
    """Run against the fake network; Pages and the GDC are stubbed unless overridden."""
    kw.setdefault("live_check", lambda url, sha, timeout: 0.0)
    kw.setdefault("gdc_present", lambda base, mid, dt, timeout, interval: 0.5)
    kw.setdefault("gdc_absent", lambda base, mid, timeout, interval: 0.5)
    return publish.run(cfg, jobs, PASSWORD, tmp_path / "logs", factory=net.factory, **kw)


# ---------------------------------------------------------------- happy paths
def test_publish_verified_end_to_end(fast_cfg, net, record, tmp_path):
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert r.ok, r.errors
    assert r.stage == "done" and set(r.delivered) == {"local", "gb"}
    assert r.ets["failed"] == [] and r.kpi["grade"] == "A"
    topic, msg = net.published[0]
    assert topic == "origin/a/wis2/io-wis2dev-metdx-demo/metadata"
    assert msg["id"] == r.message_id and msg["links"][0]["rel"] == "canonical"
    # every client was closed
    assert all(c.closed for c in net.clients.values())
    # logs: per-record and run-level monitor log
    kinds = [json.loads(l)["kind"] for l in (tmp_path / "logs" / "obs.jsonl").read_text().splitlines()]
    assert kinds[:3] == ["pages-live", "wnm", "delivered"] and kinds[-1] == "result"
    assert (tmp_path / "logs" / "monitor.jsonl").read_text().count('"kind": "monitor"') == 2


def test_several_records_in_parallel(fast_cfg, net, record, tmp_path):
    jobs = []
    for i in range(5):
        rec = json.loads(json.dumps(record))
        rec["id"] = f"{record['id']}-{i}"
        jobs.append(job(rec, path=f"metadata/obs{i}.json"))
    results = run(fast_cfg, net, jobs, tmp_path)
    assert all(r.ok for r in results), [r.errors for r in results]
    assert len({r.message_id for r in results}) == 5


def test_deletion_completes_on_gdc_report(fast_cfg, net, record, tmp_path):
    fast_cfg["timeouts"]["monitor"] = 30   # must finish on the report, not the timeout
    called = []
    import time
    t = time.monotonic()
    (r,) = run(fast_cfg, net, [job(record, "deletion")], tmp_path,
               live_check=lambda *a: called.append(a) or 0.0)
    assert r.ok, r.errors
    assert time.monotonic() - t < 5
    assert called == []  # deletions don't wait for Pages
    assert net.published[0][1]["links"][0]["rel"] == "deletion"
    assert r.gdc_events[0]["severity"] == "INFO" and "deleted" in r.gdc_events[0]["description"]


def test_deletion_not_found_is_error(fast_cfg, net, record, tmp_path):
    net.gdc_delete_result = "not found"
    (r,) = run(fast_cfg, net, [job(record, "deletion")], tmp_path)
    assert not r.ok and "not found" in r.errors[0]


def test_deletion_without_report_warns(fast_cfg, net, record, tmp_path):
    net.gdc_delete_result = None
    (r,) = run(fast_cfg, net, [job(record, "deletion")], tmp_path)
    assert r.ok and "no deletion report" in r.warnings[0]


def test_gdc_download_failure_is_error(fast_cfg, net, record, tmp_path):
    net.gdc_download_fails = True
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert not r.ok
    assert any("WCMP2 access failure (404 Client Error" in e for e in r.errors)
    assert any("no WCMP2 ETS report" in e for e in r.errors)


# ---------------------------------------------------------------- failures
def test_gb_not_forwarding(fast_cfg, net, record, tmp_path):
    net.gb_forwards = False
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert not r.ok
    assert any("not delivered by the Global Broker" in e for e in r.errors)
    assert any("no WCMP2 ETS report" in e for e in r.errors)


def test_local_not_delivering(fast_cfg, net, record, tmp_path):
    net.local_delivers = False
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert r.errors == ["message not delivered by the local broker within 1s"]


def test_gb_validation_error_fails_record(fast_cfg, net, record, tmp_path):
    net.gb_validation_errors = ["WNM links require one and only one of rel=canonical ..."]
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert not r.ok and len(r.gb_events) == 1
    assert "rel=canonical" in r.errors[0]


def test_gdc_ets_failure_is_error(fast_cfg, net, record, tmp_path):
    net.gdc_ets_failed = [("identifier", "Invalid centre_id: io-wis2dev-metdx-demo")]
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert r.errors == ["GDC ETS identifier FAILED: Invalid centre_id: io-wis2dev-metdx-demo"]


def test_gdc_ets_failure_can_be_downgraded(fast_cfg, net, record, tmp_path):
    fast_cfg["monitor"]["ets_failures_as_warnings"] = ["identifier"]
    net.gdc_ets_failed = [("identifier", "Invalid centre_id")]
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert r.ok and r.warnings == ["GDC ETS identifier FAILED: Invalid centre_id"]


def test_missing_kpi_is_fine_unless_required(fast_cfg, net, record, tmp_path):
    net.gdc_sends_kpi = False   # the Labs GDC runs with RUN_KPI off
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert r.ok and r.kpi is None and r.ets is not None


def test_kpi_recorded_when_sent(fast_cfg, net, record, tmp_path):
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert r.kpi["grade"] == "A"


def test_missing_kpi_times_out_when_required(fast_cfg, net, record, tmp_path):
    fast_cfg["monitor"]["require_kpi"] = True
    net.gdc_sends_kpi = False
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert r.errors == ["no WCMP2 KPI report from the GDC within 1s"]


def test_kpi_threshold_warns(fast_cfg, net, record, tmp_path):
    fast_cfg["kpi"]["warn_below_percentage"] = 90
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert r.ok and "below the threshold" in r.warnings[0]


def test_stale_ets_ignored(fast_cfg, net, record, tmp_path):
    net.gdc_stale_ets = True
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert r.ets is None and "no WCMP2 ETS report" in r.errors[0]
    assert '"kind": "stale-event"' in (tmp_path / "logs" / "obs.jsonl").read_text()


def test_publish_refused(fast_cfg, net, record, tmp_path):
    net.refuse_publish = True
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert r.stage == "publish" and "Not authorized" in r.errors[0]


def test_pages_never_live(fast_cfg, net, record, tmp_path):
    def never(url, sha, timeout):
        raise PagesError(f"{url} not live with expected content after {timeout}s (HTTP 404)")
    (r,) = run(fast_cfg, net, [job(record)], tmp_path, live_check=never)
    assert r.stage == "pages" and "HTTP 404" in r.errors[0]
    assert net.published == []


def test_one_failure_does_not_stop_others(fast_cfg, net, record, tmp_path):
    other = json.loads(json.dumps(record))
    other["id"] += "-2"
    def live(url, sha, timeout):
        if "bad" in url:
            raise RuntimeError("boom")
        return 0.0
    results = run(fast_cfg, net, [job(record, path="metadata/bad.json"), job(other)], tmp_path, live_check=live)
    by = {r.source_path: r for r in results}
    assert "unexpected RuntimeError at stage pages" in by["metadata/bad.json"].errors[0]
    assert by["metadata/obs.json"].ok


@pytest.mark.parametrize("refused", ["local-pub", "local-sub", "gb-sub"])
def test_connection_refused_closes_everything(fast_cfg, net, refused):
    net.refuse_connect = {refused}
    with pytest.raises(BrokerError, match=refused):
        publish.preflight(fast_cfg, PASSWORD, factory=net.factory)
    assert all(c.closed or not c.connected for c in net.clients.values())


# ---------------------------------------------------------------- router
def test_router_ignores_retained_unrelated_and_junk(tmp_path, record):
    log = JsonlLog(tmp_path / "run.jsonl")
    router = Router(log)
    h = Helper(job(record), centre_id="io-wis2dev-metdx-demo", publisher=None, router=router,
               timeouts={"delivery": 1, "monitor": 1, "pages_live": 1}, log=JsonlLog(tmp_path / "h.jsonl"))
    router.register(h)
    on_gb = router.handler("gb")
    on_gb("origin/a/wis2/io-wis2dev-metdx-demo/metadata", json.dumps({"id": h.message_id}).encode(), True)
    on_gb("origin/a/wis2/io-wis2dev-metdx-demo/metadata", json.dumps({"id": "someone-else"}).encode(), False)
    on_gb("monitor/a/wis2/io-wis2dev-metdx-demo", b"not json", False)
    on_gb("monitor/a/wis2/io-wis2dev-metdx-demo", json.dumps({"type": "x", "id": "e"}).encode(), False)
    assert h.inbox.empty()
    on_gb("origin/a/wis2/io-wis2dev-metdx-demo/metadata", json.dumps({"id": h.message_id}).encode(), False)
    assert h.inbox.get_nowait()[0] == "delivery"
    log.close()
    kinds = [json.loads(l)["kind"] for l in (tmp_path / "run.jsonl").read_text().splitlines()]
    assert kinds == ["unparseable", "monitor"]


# ---------------------------------------------------------------- plan
def test_plan_actions_and_urls(repo, record):
    def rec(suffix):
        r = json.loads(json.dumps(record))
        r["id"] = f"{record['id']}-{suffix}"
        return r
    repo.write("metadata/keep.json", rec("keep"))
    repo.write("metadata/gone.json", rec("gone"))
    repo.write("metadata/old-name.json", rec("renamed"))
    base = repo.commit()
    updated = rec("keep")
    updated["properties"]["updated"] = "2026-10-30T00:00:00Z"
    repo.write("metadata/keep.json", updated)
    repo.remove("metadata/gone.json")
    repo.git("mv", "metadata/old-name.json", "metadata/new-name.json")
    repo.write("metadata/brand-new.json", rec("new"))
    head = repo.commit()
    catalogue = {"records": {rec("gone")["id"]: {"current_url": "https://pages.example/metadata/gone/gone.1.json"}}}
    m = publish.plan(base, head, "https://pages.example/", "20261030T120000Z", "io-wis2dev-metdx-demo",
                     catalogue, cwd=str(repo.path))
    jobs = {j["source_path"]: j for j in m["jobs"]}
    assert set(jobs) == {"metadata/keep.json", "metadata/gone.json", "metadata/new-name.json",
                         "metadata/brand-new.json"}   # the renamed file's deletion is dropped
    assert jobs["metadata/keep.json"]["action"] == "update"
    assert jobs["metadata/new-name.json"]["action"] == "update"
    assert jobs["metadata/brand-new.json"]["action"] == "canonical"
    assert jobs["metadata/brand-new.json"]["url"] == \
        "https://pages.example/metadata/brand-new/brand-new.20261030T120000Z.json"
    assert jobs["metadata/gone.json"]["action"] == "deletion"
    assert jobs["metadata/gone.json"]["url"] == "https://pages.example/metadata/gone/gone.1.json"
    assert jobs["metadata/gone.json"]["sha512"] is None
    assert jobs["metadata/keep.json"]["datetime"] == "2026-10-30T00:00:00Z"


def test_plan_only_and_no_changes(repo, record):
    base = repo.commit()
    repo.write("metadata/a.json", record)
    other = json.loads(json.dumps(record))
    other["id"] += "-b"
    repo.write("metadata/b.json", other)
    head = repo.commit()
    m = publish.plan(base, head, "https://x", "S", "c", only=["metadata/b.json"], cwd=str(repo.path))
    assert [j["source_path"] for j in m["jobs"]] == ["metadata/b.json"]
    assert publish.plan(head, head, "https://x", "S", "c", cwd=str(repo.path))["jobs"] == []


# ---------------------------------------------------------------- CLI
def test_cli_plan_and_dry_run(repo, record, monkeypatch, capsys):
    base = repo.commit()
    repo.write("metadata/obs.json", record)
    repo.commit()
    monkeypatch.chdir(repo.path)
    conf = str(ROOT / "config" / "node.yml")
    assert publish.main(["--config", conf, "plan", "--base", base, "--base-url", "https://x",
                         "--stamp", "20261030T000000Z", "--out", "m.json"]) == 0
    capsys.readouterr()
    assert publish.main(["--config", conf, "run", "--manifest", "m.json", "--dry-run"]) == 0
    (out,) = json.loads(capsys.readouterr().out)
    assert out["topic"] == "origin/a/wis2/io-wis2dev-metdx-demo/metadata"
    assert out["message"]["links"][0]["href"] == "https://x/metadata/obs/obs.20261030T000000Z.json"


def test_cli_errors_exit_2(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCAL_BROKER_PUB_PASSWORD", raising=False)
    conf = str(ROOT / "config" / "node.yml")
    assert publish.main(["--config", conf, "preflight"]) == 2           # no password
    assert publish.main(["--config", conf, "run", "--manifest", str(tmp_path / "none.json")]) == 2
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"centre_id": "xx-other", "jobs": []}))
    assert publish.main(["--config", conf, "run", "--manifest", str(m)]) == 2


def test_markdown_report(fast_cfg, net, record, tmp_path):
    fast_cfg["monitor"]["require_kpi"] = True
    net.gdc_sends_kpi = False
    results = run(fast_cfg, net, [job(record)], tmp_path)
    md = publish.to_markdown(results)
    assert "1 of 1 record(s) failed" in md and "no WCMP2 KPI report" in md
    assert "No metadata records" in publish.to_markdown([])


# ---------------------------------------------------------------- GDC confirmation
def test_gdc_confirms_record(fast_cfg, net, record, tmp_path):
    seen = []
    def present(base, mid, dt, timeout, interval):
        seen.append((base, mid, dt, timeout))
        return 12.0
    (r,) = run(fast_cfg, net, [job(record)], tmp_path, gdc_present=present)
    assert r.ok and r.gdc["status"] == "present" and r.gdc["seconds"] == 12.0
    assert seen == [(fast_cfg["global_discovery_catalogue"]["url"], record["id"],
                     record["properties"]["updated"], fast_cfg["timeouts"]["gdc"])]
    assert r.gdc["url"].endswith("/items/urn%3Awmo%3Amd%3Aio-wis2dev-metdx-demo%3Atest-surface-observations?f=json")
    assert "| [present]" in publish.to_markdown([r])


def test_gdc_never_shows_record(fast_cfg, net, record, tmp_path):
    from wis2node.gdc import GDCError
    def absent(base, mid, dt, timeout, interval):
        raise GDCError("Labs GDC check failed after 120s: record not in the GDC (url)")
    (r,) = run(fast_cfg, net, [job(record)], tmp_path, gdc_present=absent)
    assert not r.ok and r.stage == "done"
    assert r.gdc["status"] == "not confirmed" and "record not in the GDC" in r.errors[0]


def test_gdc_deletion_checks_absence(fast_cfg, net, record, tmp_path):
    called = []
    (r,) = run(fast_cfg, net, [job(record, "deletion")], tmp_path,
               gdc_absent=lambda base, mid, timeout, interval: called.append(mid) or 3.0,
               gdc_present=lambda *a, **k: pytest.fail("presence check on a deletion"))
    assert r.ok and r.gdc["status"] == "removed" and called == [record["id"]]


def test_gdc_check_disabled_when_no_url(fast_cfg, net, record, tmp_path):
    fast_cfg["global_discovery_catalogue"]["url"] = ""
    (r,) = run(fast_cfg, net, [job(record)], tmp_path,
               gdc_present=lambda *a, **k: pytest.fail("GDC should not be polled"))
    assert r.ok and r.gdc is None


def test_gdc_skipped_when_publish_fails(fast_cfg, net, record, tmp_path):
    net.refuse_publish = True
    (r,) = run(fast_cfg, net, [job(record)], tmp_path,
               gdc_present=lambda *a, **k: pytest.fail("GDC should not be polled"))
    assert r.gdc is None and r.stage == "publish"


# ---------------------------------------------------------------- local broker URL kept private
def test_local_broker_url_from_secret_and_hidden_in_logs(fast_cfg, net, monkeypatch, caplog):
    import logging
    monkeypatch.setenv("LOCAL_BROKER_URL", "mqtts://secret-host.example:8883")
    with caplog.at_level(logging.INFO):
        publish.preflight(fast_cfg, PASSWORD, factory=net.factory)
    assert net.urls["local-pub"] == ("mqtts://secret-host.example:8883", "local broker")
    assert net.urls["local-sub"][0] == "mqtts://secret-host.example:8883"
    assert "secret-host" not in caplog.text and "local broker" in caplog.text


def test_missing_local_broker_url_is_a_config_error(fast_cfg, net):
    from wis2node.config import ConfigError
    fast_cfg["local_broker"]["url"] = ""
    with pytest.raises(ConfigError, match="LOCAL_BROKER_URL"):
        publish.preflight(fast_cfg, PASSWORD, factory=net.factory)


# ---------------------------------------------------------------- records naming other centres
def other_centre(record, centre="xx-other"):
    r = json.loads(json.dumps(record))
    r["id"] = f"urn:wmo:md:{centre}:obs"
    return r


def test_subscribes_to_monitor_topics_of_record_centres(fast_cfg, net, record, tmp_path):
    run(fast_cfg, net, [job(other_centre(record)), job(record, path="metadata/own.json")], tmp_path)
    assert set(net.clients["gb-sub"].subs) == {
        "origin/a/wis2/io-wis2dev-metdx-demo/metadata",
        "monitor/a/wis2/io-wis2dev-metdx-demo/#",
        "monitor/a/wis2/xx-other/#"}


def test_other_centre_record_accepted_when_gdc_allows(fast_cfg, net, record, tmp_path):
    net.gdc_enforces_topic_match = False   # e.g. a GDC configured for the demo
    (r,) = run(fast_cfg, net, [job(other_centre(record))], tmp_path)
    assert r.ok, r.errors
    assert r.ets["failed"] == []


def test_gdc_topic_mismatch_is_reported_not_a_false_pass(fast_cfg, net, record, tmp_path):
    from wis2node.gdc import GDCError

    def not_registered(*args, **kwargs):
        raise GDCError("record not in the GDC")

    (r,) = run(fast_cfg, net, [job(other_centre(record))], tmp_path, gdc_present=not_registered)
    assert not r.ok
    assert any("Topic mismatch (io-wis2dev-metdx-demo != xx-other)" in e for e in r.errors)
    assert any("record not in the GDC" in e for e in r.errors)


def test_download_failure_matched_via_cache_url_filename(fast_cfg, net, record, tmp_path):
    net.gdc_download_fails = True   # its link is the cache copy's URL, not our Pages URL
    (r,) = run(fast_cfg, net, [job(record)], tmp_path)
    assert any("WCMP2 access failure" in e for e in r.errors)
