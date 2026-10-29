"""Validate WCMP2 records changed in a pull request.

Errors (fail the check):
  - not valid JSON, or not a JSON object with a string "id"
  - WCMP2 schema validation failure (checked separately: pywcmp leaves it out
    of the ETS summary)
  - any ETS test FAILED, or the ETS crashing on the record
  - id does not start with urn:wmo:md:<centre-id>: (unless
    validation.require_centre_id_prefix is false)
  - id used by another record in metadata/
  - a modified record changed its id, or did not advance properties.updated
  - files under metadata/ that are not metadata/<name>.json

Warnings (reported only): ETS WARNING results, KPI below the configured threshold.
The KPI report is informational.

Usage:
  python -m wis2node.validate --base HEAD^1 --head HEAD   # changes in a PR merge commit
  python -m wis2node.validate metadata/a.json ...         # specific files, treated as new
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import changes as ch
from . import config as cfgmod

MAX_LISTED_ERRORS = 20
COMMENT_MARKER = "<!-- wis2node-validate -->"


@dataclass
class RecordResult:
    path: str
    status: str
    metadata_id: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ets: dict | None = None
    kpi: dict | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_record(data: bytes) -> tuple[dict | None, str | None]:
    try:
        record = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        return None, f"not valid JSON: {exc}"
    if not isinstance(record, dict):
        return None, "not a JSON object"
    if not isinstance(record.get("id"), str) or not record["id"]:
        return None, 'missing or non-string "id"'
    return record, None


def parse_datetime(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def run_ets(record: dict, relax_centre_id: bool, result: RecordResult) -> None:
    from pywcmp.wcmp2.ets import WMOCoreMetadataProfileTestSuite2

    suite = WMOCoreMetadataProfileTestSuite2(record)
    schema = suite.test_requirement_validation()
    if schema["code"] == "FAILED":
        errs = schema.get("errors", [])
        result.errors.append(f"WCMP2 schema validation failed ({len(errs)} error(s))")
        result.errors.extend(f"schema: {e}" for e in errs[:MAX_LISTED_ERRORS])
        if len(errs) > MAX_LISTED_ERRORS:
            result.errors.append(f"schema: ... and {len(errs) - MAX_LISTED_ERRORS} more")
    try:
        report = suite.run_tests(relax_centre_id_checks=relax_centre_id)
    except Exception as exc:  # the ETS indexes fields directly and can crash on bad records
        result.errors.append(f"ETS could not run on this record: {type(exc).__name__}: {exc}")
        return
    report["validation"] = schema
    result.ets = report
    for test in report["tests"]:
        name = test["id"].rsplit("/", 1)[-1]
        msg = f"ETS {name}: {test.get('message', '')}".rstrip(": ")
        if test["code"] == "FAILED":
            result.errors.append(msg)
        elif test["code"] == "WARNING":
            result.warnings.append(msg)


def run_kpi(record: dict, warn_below: float | None, result: RecordResult) -> None:
    from pywcmp.wcmp2.kpi import WMOCoreMetadataProfileKeyPerformanceIndicators

    try:
        report = WMOCoreMetadataProfileKeyPerformanceIndicators(record).evaluate()
    except Exception as exc:
        result.warnings.append(f"KPI could not run on this record: {type(exc).__name__}: {exc}")
        return
    result.kpi = report
    pct = report["summary"].get("percentage")
    if warn_below is not None and pct is not None and pct < warn_below:
        result.warnings.append(f"KPI {pct}% is below the configured threshold of {warn_below}%")


def check_local_rules(record: dict, status: str, previous: dict | None, centre_id: str,
                      id_paths: dict[str, list[str]], path: str, result: RecordResult,
                      require_prefix: bool = True) -> None:
    rid = record["id"]
    prefix = f"urn:wmo:md:{centre_id}:"
    if require_prefix and (not rid.startswith(prefix) or rid == prefix):
        result.errors.append(f'id must start with "{prefix}" followed by a local identifier')

    others = [p for p in id_paths.get(rid, []) if p != path]
    if others:
        result.errors.append(f"id is also used by {', '.join(sorted(others))}")

    if status != ch.MODIFIED or previous is None:
        return
    if previous.get("id") != rid:
        result.errors.append(
            f'id changed from "{previous.get("id")}" to "{rid}": to replace a record, '
            "delete the old file and add a new one")
    new_updated = parse_datetime(record.get("properties", {}).get("updated"))
    old_updated = parse_datetime((previous.get("properties") or {}).get("updated"))
    if new_updated is None:
        result.errors.append("a modified record must set properties.updated (RFC 3339 with timezone)")
    elif old_updated is not None and new_updated <= old_updated:
        result.errors.append(
            f"properties.updated must be later than the published version "
            f"({previous['properties']['updated']})")


def index_ids(root: Path) -> dict[str, list[str]]:
    """Map record id -> paths, over every record in the working tree."""
    ids: dict[str, list[str]] = {}
    for f in sorted((root / ch.METADATA_DIR).glob("*.json")):
        record, _ = parse_record(f.read_bytes())
        if record:
            ids.setdefault(record["id"], []).append(f.relative_to(root).as_posix())
    return ids


def validate(changes: list[ch.Change], cfg: dict, root: Path, base: str | None,
             run_kpis: bool = True) -> list[RecordResult]:
    centre_id = cfg["centre_id"]
    relax = cfgmod.get(cfg, "validation", "relax_centre_id_checks", default=True)
    require_prefix = cfgmod.get(cfg, "validation", "require_centre_id_prefix", default=True)
    warn_below = cfgmod.get(cfg, "kpi", "warn_below_percentage")
    id_paths = index_ids(root)
    results = []

    for change in changes:
        result = RecordResult(change.path, change.status)
        results.append(result)

        if change.status == ch.DELETED:
            old, err = parse_record(ch.show(base, change.path, cwd=str(root))) if base else (None, None)
            result.metadata_id = old["id"] if old else None
            if result.metadata_id and result.metadata_id in id_paths:
                result.notes.append(
                    f"id is still present in {', '.join(id_paths[result.metadata_id])}: "
                    "treated as a rename, so no deletion will be published")
            elif result.metadata_id:
                result.notes.append("a deletion notice will be published for this record")
            else:
                result.notes.append(f"previous version unreadable ({err}): no deletion can be published")
            continue

        record, err = parse_record((root / change.path).read_bytes())
        if err:
            result.errors.append(err)
            continue
        result.metadata_id = record["id"]

        previous = None
        if change.status == ch.MODIFIED and base:
            previous, _ = parse_record(ch.show(base, change.path, cwd=str(root)))

        check_local_rules(record, change.status, previous, centre_id, id_paths, change.path, result,
                          require_prefix=require_prefix)
        run_ets(record, relax, result)
        if run_kpis:
            run_kpi(record, warn_below, result)

    return results


STATUS_LABEL = {ch.ADDED: "new", ch.MODIFIED: "modified", ch.DELETED: "deleted"}


def to_markdown(results: list[RecordResult], stray: list[str]) -> str:
    lines = [COMMENT_MARKER, "## WCMP2 validation", ""]
    if not results and not stray:
        lines.append("No metadata records changed.")
        return "\n".join(lines) + "\n"

    failed = sum(not r.ok for r in results) + (1 if stray else 0)
    lines.append(f"**{'❌ ' + str(failed) + ' problem(s) to fix' if failed else '✅ all checks passed'}**")
    lines += ["", "| Record | Change | Result | ETS | KPI |", "|---|---|---|---|---|"]
    for r in results:
        ets = ("–" if r.ets is None else
               " / ".join(f"{v} {k.lower()}" for k, v in r.ets["summary"].items() if v))
        kpi = ("–" if r.kpi is None else
               f"{r.kpi['summary'].get('percentage')}% (grade {r.kpi['summary'].get('grade')})")
        icon = "❌" if r.errors else ("⚠️" if r.warnings else "✅")
        lines.append(f"| `{r.path}`<br>{r.metadata_id or ''} | {STATUS_LABEL[r.status]} | {icon} | {ets} | {kpi} |")

    for r in results:
        if not (r.errors or r.warnings or r.notes or r.kpi):
            continue
        lines += ["", f"### `{r.path}`"]
        lines += [f"- ❌ {e}" for e in r.errors]
        lines += [f"- ⚠️ {w}" for w in r.warnings]
        lines += [f"- ℹ️ {n}" for n in r.notes]
        if r.kpi:
            lines += ["", "<details><summary>KPI details</summary>", ""]
            for t in r.kpi["tests"]:
                comments = "; ".join(t.get("comments") or []) or "–"
                lines.append(f"- **{t['title']}**: {t['score']}/{t['total']}: {comments}")
            lines += ["", "</details>"]

    if stray:
        lines += ["", "### Files in the wrong place",
                  "Records must be `metadata/<name>.json` (no sub-folders, `.json` extension):"]
        lines += [f"- ❌ `{p}`" for p in stray]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="*", help="validate these files as new records (no git needed)")
    p.add_argument("--base", help="base revision (with --head: validate records changed between them)")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--config", default=str(cfgmod.DEFAULT_PATH))
    p.add_argument("--markdown", help="also write the report as Markdown to this file")
    p.add_argument("--json", dest="json_out", help="also write full results as JSON to this file")
    p.add_argument("--no-kpi", action="store_true", help="skip the KPI evaluation")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    root = Path.cwd()
    try:
        cfg = cfgmod.load(args.config)
        if args.paths:
            changes = [ch.Change(ch.ADDED, Path(x).as_posix()) for x in args.paths]
            stray = [c.path for c in changes if not ch.is_record_path(c.path)]
            changes = [c for c in changes if ch.is_record_path(c.path)]
            base = None
        elif args.base:
            changes = ch.changed_records(args.base, args.head)
            stray = ch.other_paths(args.base, args.head)
            base = args.base
        else:
            p.error("give record paths, or --base (and optionally --head)")
        results = validate(changes, cfg, root, base, run_kpis=not args.no_kpi)
    except (cfgmod.ConfigError, ch.GitError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = to_markdown(results, stray)
    print(report)
    if args.markdown:
        Path(args.markdown).write_text(report)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            [r.__dict__ for r in results], indent=2, default=str))
    return 0 if all(r.ok for r in results) and not stray else 1


if __name__ == "__main__":
    sys.exit(main())
