from pathlib import Path

ROOT = Path(__file__).parent.parent
import json

import pytest

from wis2node import changes as ch
from wis2node import validate as v

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def run(repo, cfg, base, head, kpi=False):
    return v.validate(ch.changed_records(base, head, cwd=str(repo.path)), cfg, repo.path, base, run_kpis=kpi)


def by_path(results):
    return {r.path: r for r in results}


def test_valid_new_record_passes(repo, cfg, record):
    base = repo.commit()
    repo.write("metadata/obs.json", record)
    (r,) = run(repo, cfg, base, repo.commit(), kpi=True)
    assert r.ok, r.errors
    assert r.status == "A" and r.metadata_id == record["id"]
    assert r.ets["summary"]["FAILED"] == 0
    assert r.kpi["summary"]["grade"]
    # unregistered test centre-id is relaxed to warnings
    assert any("centre_id" in w for w in r.warnings)


def test_strict_centre_id_fails(repo, cfg, record):
    cfg["validation"]["relax_centre_id_checks"] = False
    base = repo.commit()
    repo.write("metadata/obs.json", record)
    (r,) = run(repo, cfg, base, repo.commit())
    assert any("ETS identifier" in e for e in r.errors)


def test_invalid_json_and_missing_id(repo, cfg):
    base = repo.commit()
    repo.write("metadata/bad.json", "{not json")
    repo.write("metadata/noid.json", {"type": "Feature"})
    res = by_path(run(repo, cfg, base, repo.commit()))
    assert "not valid JSON" in res["metadata/bad.json"].errors[0]
    assert '"id"' in res["metadata/noid.json"].errors[0]


def test_schema_failure_is_caught_even_though_ets_summary_omits_it(repo, cfg, record):
    del record["properties"]["contacts"]
    base = repo.commit()
    repo.write("metadata/obs.json", record)
    (r,) = run(repo, cfg, base, repo.commit())
    assert not r.ok
    assert any("schema validation failed" in e for e in r.errors)


def test_ets_crash_is_an_error_not_an_exception(repo, cfg, record):
    del record["properties"]  # ETS indexes record['properties'] directly
    base = repo.commit()
    repo.write("metadata/obs.json", record)
    (r,) = run(repo, cfg, base, repo.commit())
    assert any("ETS could not run" in e for e in r.errors)


def test_wrong_centre_prefix(repo, cfg, record):
    cfg["validation"]["require_centre_id_prefix"] = True
    record["id"] = "urn:wmo:md:xx-other:obs"
    base = repo.commit()
    repo.write("metadata/obs.json", record)
    (r,) = run(repo, cfg, base, repo.commit())
    assert any("urn:wmo:md:io-wis2dev-metdx-demo:" in e for e in r.errors)


def test_duplicate_id_across_files(repo, cfg, record):
    repo.write("metadata/existing.json", record)
    base = repo.commit()
    repo.write("metadata/copy.json", record)
    (r,) = run(repo, cfg, base, repo.commit())
    assert any("also used by metadata/existing.json" in e for e in r.errors)


def test_modified_record_must_advance_updated(repo, cfg, record):
    repo.write("metadata/obs.json", record)
    base = repo.commit()
    record["properties"]["title"] = "Changed title"
    repo.write("metadata/obs.json", record)
    (r,) = run(repo, cfg, base, repo.commit())
    assert any("properties.updated must be later" in e for e in r.errors)


def test_modified_record_with_newer_updated_passes(repo, cfg, record):
    repo.write("metadata/obs.json", record)
    base = repo.commit()
    record["properties"]["updated"] = "2026-10-30T09:00:00Z"
    repo.write("metadata/obs.json", record)
    (r,) = run(repo, cfg, base, repo.commit())
    assert r.ok, r.errors


def test_modified_record_cannot_change_id(repo, cfg, record):
    repo.write("metadata/obs.json", record)
    base = repo.commit()
    record["id"] = record["id"] + "-v2"
    record["properties"]["updated"] = "2026-10-30T09:00:00Z"
    repo.write("metadata/obs.json", record)
    (r,) = run(repo, cfg, base, repo.commit())
    assert any("id changed" in e for e in r.errors)


def test_deletion_and_rename_notes(repo, cfg, record):
    other = json.loads(json.dumps(record))
    other["id"] = record["id"] + "-2"
    repo.write("metadata/obs.json", record)
    repo.write("metadata/gone.json", other)
    base = repo.commit()
    repo.git("mv", "metadata/obs.json", "metadata/renamed.json")
    repo.remove("metadata/gone.json")
    res = by_path(run(repo, cfg, base, repo.commit()))
    assert res["metadata/gone.json"].ok
    assert "deletion notice will be published" in res["metadata/gone.json"].notes[0]
    assert "treated as a rename" in res["metadata/obs.json"].notes[0]
    assert res["metadata/renamed.json"].ok


def test_markdown_report(repo, cfg, record):
    base = repo.commit()
    repo.write("metadata/obs.json", record)
    repo.write("metadata/bad.json", "{")
    md = v.to_markdown(run(repo, cfg, base, repo.commit()), ["metadata/sub/x.json"])
    assert md.startswith(v.COMMENT_MARKER)
    assert "2 problem(s)" in md and "`metadata/sub/x.json`" in md
    assert v.to_markdown([], []).rstrip().endswith("No metadata records changed.")


def test_cli_exit_codes(repo, cfg, record, monkeypatch, tmp_path):
    base = repo.commit()
    repo.write("metadata/obs.json", record)
    repo.commit()
    monkeypatch.chdir(repo.path)
    conf = str(ROOT / "config" / "node.yml")
    md = tmp_path / "report.md"
    assert v.main(["--base", base, "--config", conf, "--no-kpi", "--markdown", str(md)]) == 0
    assert "all checks passed" in md.read_text()
    repo.write("metadata/bad.json", "{")
    repo.commit()
    assert v.main(["--base", base, "--config", conf, "--no-kpi"]) == 1
    assert v.main(["--base", "deadbeef", "--config", conf]) == 2


def test_example_template_passes_validation(repo, cfg):
    """examples/record-template.json is what collaborators copy: keep it valid."""
    from pathlib import Path
    template = (Path(__file__).parent.parent / "examples" / "record-template.json").read_text()
    base = repo.commit()
    repo.write("metadata/my-dataset.json", template)
    (r,) = run(repo, cfg, base, repo.commit())
    assert r.ok, r.errors
    assert r.ets["summary"]["FAILED"] == 0


def test_other_centre_prefix_allowed_when_check_off(repo, cfg, record):
    cfg["validation"]["require_centre_id_prefix"] = False   # as in this repo's config (demo)
    record["id"] = "urn:wmo:md:xx-other:obs"
    base = repo.commit()
    repo.write("metadata/obs.json", record)
    (r,) = run(repo, cfg, base, repo.commit())
    assert r.ok, r.errors


def test_centre_prefix_required_by_default(repo, cfg, record):
    del cfg["validation"]["require_centre_id_prefix"]
    record["id"] = "urn:wmo:md:xx-other:obs"
    base = repo.commit()
    repo.write("metadata/obs.json", record)
    (r,) = run(repo, cfg, base, repo.commit())
    assert any("must start with" in e for e in r.errors)
