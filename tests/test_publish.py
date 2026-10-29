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
def fast_cfg(cfg):
    cfg["timeouts"].update(delivery=1, monitor=1, pages_live=1, broker_connect=1)
    return cfg


def job(record, action="canonical", path="metadata/obs.json"):
    data = json.dumps(record).encode()
    deletion = action == "deletion"
    return Job(action=action, source_path=path, metadata_id=record["id"],
               url=f"https://pages.example/{path}", geometry=record["geometry"],
               datetime=wnm.record_datetime(record),
               sha512=None if deletion else wnm.sha512_b64(data), length=None if deletion else len(data))


def run(cfg, net, jobs, tmp_path, **kw):
    return publish.run(cfg, jobs, PASSWORD, tmp_path / "logs", factory=net.factory,
                       live_check=kw.pop("live_check", lambda url, sha, timeout: 0.0), **kw)


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


def test_deletion_needs_no_gdc_reports(fast_cfg, net, record, tmp_path):
    called = []
    (r,) = run(fast_cfg, net, [job(record, "deletion")], tmp_path,
               live_check=lambda *a: called.append(a) or 0.0)
    assert r.ok, r.errors
    assert called == []  # deletions don't wait for Pages
    assert net.published[0][1]["links"][0]["rel"] == "deletion"


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


def test_missing_kpi_times_out(fast_cfg, net, record, tmp_path):
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
    net.gdc_sends_kpi = False
    results = run(fast_cfg, net, [job(record)], tmp_path)
    md = publish.to_markdown(results)
    assert "1 of 1 record(s) failed" in md and "no WCMP2 KPI report" in md
    assert "No metadata records" in publish.to_markdown([])
