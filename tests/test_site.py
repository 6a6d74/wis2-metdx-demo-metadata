import hashlib
import json

import pytest

from wis2node import publish, site

URL = "https://6a6d74.github.io/wis2-metdx-demo-metadata"


def variant(record, suffix, **props):
    r = json.loads(json.dumps(record))
    r["id"] = f"{record['id']}-{suffix}"
    r["properties"].update(props)
    return r


@pytest.fixture
def site_dir(tmp_path):
    d = tmp_path / "_site"
    d.mkdir()
    (d / "catalogue.json").write_text('{"centre_id": "io-wis2dev-metdx-demo", "records": []}')
    return d


def result_for(job, ok=True, **kw):
    return {"metadata_id": job["metadata_id"], "ok": ok, "stage": "done", "message_id": "m-" + job["metadata_id"][-3:],
            "errors": [] if ok else ["boom"], "warnings": [], "delivered": {"local": 0.1, "gb": 0.2},
            "ets": {"summary": {"PASSED": 12, "FAILED": 0}, "failed": []},
            "kpi": {"percentage": 85.0, "grade": "A"}, **kw}


def test_stage_record_cycle(repo, record, site_dir, tmp_path):
    a, b = variant(record, "a", title="Alpha <records>"), variant(record, "b")
    repo.write("metadata/a.json", a)
    repo.write("metadata/b.json", b)
    head = repo.commit()
    m = publish.plan(site.ch.ZERO_SHA, head, URL, "20261030T120000Z", "io-wis2dev-metdx-demo", cwd=str(repo.path))

    cat = site.stage(m, site_dir, "101", "https://run/101", repo=str(repo.path))
    pub = site_dir / "metadata/a/a.20261030T120000Z.json"
    data = (repo.path / "metadata/a.json").read_bytes()
    assert pub.read_bytes() == data
    sidecar = (site_dir / "metadata/a/a.20261030T120000Z.json.sha512").read_text()
    assert sidecar == f"{hashlib.sha512(data).hexdigest()}  a.20261030T120000Z.json\n"
    entry = cat["records"][a["id"]]
    assert entry["current_url"] == f"{URL}/metadata/a/a.20261030T120000Z.json"
    assert entry["last_publication"]["status"] == "pending" and len(entry["versions"]) == 1
    assert cat["published_commit"] is None  # only advanced once announced

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.jsonl").write_text('{"kind": "result"}\n')
    jobs = {j["source_path"]: j for j in m["jobs"]}
    results = [result_for(jobs["metadata/a.json"]), result_for(jobs["metadata/b.json"], ok=False)]
    cat = site.record_results(m, results, site_dir, logs, "101", "https://run/101")
    assert cat["published_commit"] == head
    assert cat["records"][a["id"]]["last_publication"]["status"] == "ok"
    assert cat["records"][b["id"]]["last_publication"]["errors"] == ["boom"]
    assert (site_dir / "logs/101/a.jsonl").exists()

    index = (site_dir / "index.html").read_text()
    assert "Alpha &lt;records&gt;" in index           # escaped
    assert hashlib.sha512(data).hexdigest() in index   # full checksum available to copy
    assert "metadata/a/a.20261030T120000Z.json" in index
    assert "2 record(s)" in index


def test_update_then_delete(repo, record, site_dir):
    a = variant(record, "a")
    repo.write("metadata/a.json", a)
    c1 = repo.commit()
    m1 = publish.plan(site.ch.ZERO_SHA, c1, URL, "S1", "io-wis2dev-metdx-demo", cwd=str(repo.path))
    site.stage(m1, site_dir, "1", "", repo=str(repo.path))
    site.record_results(m1, [result_for(m1["jobs"][0])], site_dir, None, "1", "")

    a["properties"]["updated"] = "2026-10-31T00:00:00Z"
    repo.write("metadata/a.json", a)
    c2 = repo.commit()
    cat = site.load_catalogue(site_dir)
    m2 = publish.plan(publish.resolve_base(None, cat, None), c2, URL, "S2", "io-wis2dev-metdx-demo",
                      cat, cwd=str(repo.path))
    assert [j["action"] for j in m2["jobs"]] == ["update"]
    cat = site.stage(m2, site_dir, "2", "", repo=str(repo.path))
    site.record_results(m2, [result_for(m2["jobs"][0])], site_dir, None, "2", "")
    assert len(cat["records"][a["id"]]["versions"]) == 2
    assert (site_dir / "metadata/a/a.S1.json").exists()   # old versions stay resolvable

    repo.remove("metadata/a.json")
    c3 = repo.commit()
    cat = site.load_catalogue(site_dir)
    m3 = publish.plan(publish.resolve_base(None, cat, None), c3, URL, "S3", "io-wis2dev-metdx-demo",
                      cat, cwd=str(repo.path))
    (job,) = m3["jobs"]
    assert job["action"] == "deletion" and job["url"] == f"{URL}/metadata/a/a.S2.json"
    cat = site.stage(m3, site_dir, "3", "", repo=str(repo.path))
    assert cat["records"][a["id"]]["deleted"] is True
    assert "Deleted records" in (site_dir / "index.html").read_text()


def test_stage_rejects_checksum_mismatch(repo, record, site_dir):
    repo.write("metadata/a.json", record)
    head = repo.commit()
    m = publish.plan(site.ch.ZERO_SHA, head, URL, "S", "io-wis2dev-metdx-demo", cwd=str(repo.path))
    m["jobs"][0]["sha512"] = "tampered"
    with pytest.raises(site.SiteError, match="does not match"):
        site.stage(m, site_dir, "1", "", repo=str(repo.path))


def test_record_handles_missing_results(repo, record, site_dir):
    repo.write("metadata/a.json", record)
    head = repo.commit()
    m = publish.plan(site.ch.ZERO_SHA, head, URL, "S", "io-wis2dev-metdx-demo", cwd=str(repo.path))
    site.stage(m, site_dir, "1", "", repo=str(repo.path))
    cat = site.record_results(m, [], site_dir, None, "1", "")
    assert cat["records"][record["id"]]["last_publication"]["status"] == "not run"


def test_republish(repo, record, site_dir):
    a, b = variant(record, "a"), variant(record, "b")
    repo.write("metadata/a.json", a)
    repo.write("metadata/b.json", b)
    head = repo.commit()
    cat = {"records": {a["id"]: {"id": a["id"], "deleted": False}}}
    m = publish.plan("ignored", head, URL, "S", "c", cat, cwd=str(repo.path), republish=["all"])
    assert {j["source_path"]: j["action"] for j in m["jobs"]} == \
        {"metadata/a.json": "update", "metadata/b.json": "canonical"}
    m = publish.plan("ignored", head, URL, "S", "c", cat, cwd=str(repo.path), republish=["metadata/b.json"])
    assert [j["source_path"] for j in m["jobs"]] == ["metadata/b.json"]
    with pytest.raises(ValueError, match="not records"):
        publish.plan("ignored", head, URL, "S", "c", cat, cwd=str(repo.path), republish=["metadata/zzz.json"])


def test_resolve_base_order():
    assert publish.resolve_base("x", {"published_commit": "y"}, "z") == "x"
    assert publish.resolve_base(None, {"published_commit": "y"}, "z") == "y"
    assert publish.resolve_base(None, {"published_commit": None}, "z") == "z"
    assert publish.resolve_base(None, None, None) == site.ch.ZERO_SHA


def test_empty_index_renders(site_dir):
    cat = site.load_catalogue(site_dir)
    assert cat["records"] == {}
    assert "No records published yet" in site.render_index(cat)
