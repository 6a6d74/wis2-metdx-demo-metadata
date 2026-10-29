"""The published site on the gh-pages branch: staged records, catalogue.json, index.html.

Layout:
  metadata/<stem>/<stem>.<stamp>.json         immutable published copies
  metadata/<stem>/<stem>.<stamp>.json.sha512  sha512sum-compatible checksum
  catalogue.json                              machine-readable catalogue (see below)
  index.html                                  browsable catalogue
  logs/<run-id>/                              publication logs (JSON Lines)

catalogue.json:
  {"centre_id", "updated", "published_commit",       # last commit fully staged and announced
   "records": {<metadata id>: {
       "id", "title", "source_path", "current_url", "current_path",
       "sha512_hex", "sha512_b64", "length", "published", "deleted", "deleted_at",
       "last_publication": {"run_id", "run_url", "action", "message_id", "status",
                            "stage", "errors", "warnings", "ets", "kpi", "log"},
       "versions": [{"path", "url", "sha512_hex", "length", "published", "action"}]}}}

Usage:
  python -m wis2node.site stage  --manifest manifest.json --site _site --run-id N --run-url URL
  python -m wis2node.site record --manifest manifest.json --site _site --results results.json \\
                                 --log-dir logs/run --run-id N --run-url URL
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import changes as ch
from . import wnm

CATALOGUE = "catalogue.json"


class SiteError(Exception):
    """Staging failed (e.g. a record's content does not match the manifest)."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_catalogue(site: Path, centre_id: str | None = None) -> dict:
    f = site / CATALOGUE
    cat = json.loads(f.read_text()) if f.exists() else {}
    if not isinstance(cat.get("records"), dict):  # initial placeholder had a list
        cat["records"] = {}
    cat.setdefault("centre_id", centre_id)
    cat.setdefault("published_commit", None)
    return cat


def save_catalogue(site: Path, cat: dict) -> None:
    cat["updated"] = now()
    (site / CATALOGUE).write_text(json.dumps(cat, indent=2, sort_keys=False) + "\n")


def stage(manifest: dict, site: Path, run_id: str, run_url: str, repo: str | None = None) -> dict:
    """Copy new/updated records into the site and mark them pending in the catalogue."""
    cat = load_catalogue(site, manifest["centre_id"])
    head = manifest["head"]
    for job in manifest["jobs"]:
        rid = job["metadata_id"]
        entry = cat["records"].setdefault(rid, {"id": rid, "versions": []})
        entry["last_publication"] = {"run_id": run_id, "run_url": run_url, "action": job["action"],
                                     "status": "pending", "stamp": manifest["stamp"]}
        if job["action"] == wnm.DELETION:
            entry.update(deleted=True, deleted_at=now())
            continue

        data = ch.show(head, job["source_path"], cwd=repo)
        if wnm.sha512_b64(data) != job["sha512"]:
            raise SiteError(f"{job['source_path']} at {head} does not match the manifest checksum")
        target = site / job["published_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hexdigest = hashlib.sha512(data).hexdigest()
        target.with_name(target.name + ".sha512").write_text(f"{hexdigest}  {target.name}\n")

        record = json.loads(data)
        entry.update({
            "title": (record.get("properties") or {}).get("title"),
            "source_path": job["source_path"],
            "current_url": job["url"],
            "current_path": job["published_path"],
            "sha512_hex": hexdigest,
            "sha512_b64": job["sha512"],
            "length": job["length"],
            "published": manifest["stamp"],
            "deleted": False,
            "deleted_at": None,
        })
        entry["versions"].append({"path": job["published_path"], "url": job["url"], "sha512_hex": hexdigest,
                                  "length": job["length"], "published": manifest["stamp"],
                                  "action": job["action"]})
    save_catalogue(site, cat)
    write_index(site, cat)
    return cat


def record_results(manifest: dict, results: list[dict], site: Path, log_dir: Path | None,
                   run_id: str, run_url: str) -> dict:
    """Store each record's publication outcome and logs; advance published_commit."""
    cat = load_catalogue(site, manifest["centre_id"])
    rel_logs = f"logs/{run_id}"
    if log_dir and log_dir.is_dir():
        dest = site / rel_logs
        dest.mkdir(parents=True, exist_ok=True)
        for f in log_dir.glob("*.jsonl"):
            shutil.copy2(f, dest / f.name)
    by_id = {r["metadata_id"]: r for r in results}
    for job in manifest["jobs"]:
        rid = job["metadata_id"]
        entry = cat["records"].setdefault(rid, {"id": rid, "versions": []})
        r = by_id.get(rid)
        pub = entry.setdefault("last_publication", {})
        pub.update(run_id=run_id, run_url=run_url, action=job["action"])
        if r is None:
            pub.update(status="not run", errors=["the publish step produced no result for this record"])
            continue
        pub.update(status="ok" if r["ok"] else "failed", stage=r["stage"], message_id=r["message_id"],
                   errors=r["errors"], warnings=r["warnings"], ets=r["ets"], kpi=r["kpi"],
                   delivered=r["delivered"], log=f"{rel_logs}/{Path(job['source_path']).stem}.jsonl")
    cat["published_commit"] = manifest["head"]
    save_catalogue(site, cat)
    write_index(site, cat)
    return cat


# ---------------------------------------------------------------- index.html
def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _status_cell(pub: dict | None) -> str:
    if not pub:
        return '<span class="badge muted">–</span>'
    status = pub.get("status", "?")
    cls = {"ok": "ok", "failed": "bad", "pending": "muted"}.get(status, "warn")
    parts = [f'<span class="badge {cls}">{_e(status)}</span>']
    ets = pub.get("ets")
    if ets:
        failed = len(ets.get("failed") or [])
        parts.append(f'<span class="badge {"bad" if failed else "ok"}">ETS {"✗ " + str(failed) + " failed" if failed else "✓"}</span>')
    kpi = pub.get("kpi")
    if kpi and kpi.get("percentage") is not None:
        parts.append(f'<span class="badge">KPI {_e(kpi.get("percentage"))}% {_e(kpi.get("grade") or "")}</span>')
    if pub.get("run_url"):
        parts.append(f'<a href="{_e(pub["run_url"])}">run</a>')
    if pub.get("log"):
        parts.append(f'<a href="{_e(pub["log"])}">log</a>')
    issues = (pub.get("errors") or []) + (pub.get("warnings") or [])
    if issues:
        items = "".join(f"<li>{_e(i)}</li>" for i in issues)
        parts.append(f"<details><summary>{len(issues)} issue(s)</summary><ul>{items}</ul></details>")
    return " ".join(parts)


def _row(entry: dict) -> str:
    sha = entry.get("sha512_hex") or ""
    versions = "".join(
        f'<li><a href="{_e(v["path"])}">{_e(v["published"])}</a> {_e(v["action"])} '
        f'<code title="{_e(v["sha512_hex"])}">{_e(v["sha512_hex"][:16])}…</code></li>'
        for v in reversed(entry.get("versions", [])))
    history = f"<details><summary>{len(entry.get('versions', []))} version(s)</summary><ul>{versions}</ul></details>" if versions else ""
    current = (f'<a href="{_e(entry["current_path"])}">{_e(Path(entry["current_path"]).name)}</a>'
               if entry.get("current_path") else "–")
    return (
        f'<tr><td><strong>{_e(entry.get("title") or "(untitled)")}</strong><br>'
        f'<code class="id">{_e(entry["id"])}</code></td>'
        f'<td>{current}<br><span class="muted">{_e(entry.get("length"))} bytes</span>{history}</td>'
        f'<td><code class="sha" title="{_e(sha)}">{_e(sha[:24])}…</code> '
        f'<button type="button" data-copy="{_e(sha)}" aria-label="Copy SHA-512">copy</button>'
        f'<br><a class="muted" href="{_e(entry.get("current_path", ""))}.sha512">.sha512</a></td>'
        f'<td>{_e(entry.get("published"))}</td>'
        f'<td>{_status_cell(entry.get("last_publication"))}</td></tr>'
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{centre} metadata catalogue</title>
<style>
:root {{ --bg:#fff; --fg:#1b1f24; --muted:#636c76; --line:#d8dee4; --ok:#1a7f37; --bad:#cf222e; --warn:#9a6700; --chip:#f6f8fa; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0d1117; --fg:#e6edf3; --muted:#8d96a0; --line:#30363d; --ok:#3fb950; --bad:#f85149; --warn:#d29922; --chip:#161b22; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1200px; margin:0 auto; }}
h1 {{ font-size:1.5rem; margin:0 0 4px; }}
p.meta {{ color:var(--muted); margin:0 0 20px; }}
a {{ color:inherit; }}
.table-wrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; }}
th, td {{ text-align:left; vertical-align:top; padding:10px 8px; border-bottom:1px solid var(--line); }}
th {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
code {{ font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; }}
code.id {{ color:var(--muted); word-break:break-all; }}
.muted {{ color:var(--muted); }}
.badge {{ display:inline-block; padding:1px 8px; border-radius:999px; background:var(--chip);
          border:1px solid var(--line); font-size:.8rem; margin:0 4px 4px 0; }}
.badge.ok {{ color:var(--ok); border-color:var(--ok); }}
.badge.bad {{ color:var(--bad); border-color:var(--bad); }}
.badge.warn {{ color:var(--warn); border-color:var(--warn); }}
button {{ font-size:.75rem; padding:1px 6px; border:1px solid var(--line); border-radius:4px;
          background:var(--chip); color:var(--fg); cursor:pointer; }}
details summary {{ cursor:pointer; color:var(--muted); font-size:.85rem; }}
details ul {{ margin:4px 0; padding-left:18px; font-size:.85rem; }}
tr.deleted td {{ opacity:.6; }}
</style>
</head>
<body>
<main>
<h1>{centre}: WCMP2 discovery metadata</h1>
<p class="meta">{count} record(s) · updated {updated} · <a href="catalogue.json">catalogue.json</a></p>
<div class="table-wrap">
<table>
<thead><tr><th>Record</th><th>Current file</th><th>SHA-512</th><th>Published</th><th>Last publication</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
{deleted}
</main>
<script>
document.addEventListener("click", function (e) {{
  var b = e.target.closest("button[data-copy]");
  if (!b || !navigator.clipboard) return;
  navigator.clipboard.writeText(b.dataset.copy).then(function () {{
    var t = b.textContent; b.textContent = "copied"; setTimeout(function () {{ b.textContent = t; }}, 1200);
  }});
}});
</script>
</body>
</html>
"""


def render_index(cat: dict) -> str:
    entries = sorted(cat["records"].values(), key=lambda e: (e.get("title") or e["id"]).lower())
    live = [e for e in entries if not e.get("deleted")]
    gone = [e for e in entries if e.get("deleted")]
    rows = "\n".join(_row(e) for e in live) or '<tr><td colspan="5" class="muted">No records published yet.</td></tr>'
    deleted = ""
    if gone:
        items = "".join(f'<li><code>{_e(e["id"])}</code>: deleted {_e(e.get("deleted_at"))} '
                        f'{_status_cell(e.get("last_publication"))}</li>' for e in gone)
        deleted = f"<h2>Deleted records</h2><ul>{items}</ul>"
    return PAGE.format(centre=_e(cat.get("centre_id") or "WIS2 node"), count=len(live),
                       updated=_e(cat.get("updated")), rows=rows, deleted=deleted)


def write_index(site: Path, cat: dict) -> None:
    (site / "index.html").write_text(render_index(cat))


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m wis2node.site", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("stage", "record"):
        s = sub.add_parser(name)
        s.add_argument("--manifest", default="manifest.json")
        s.add_argument("--site", default="_site")
        s.add_argument("--run-id", required=True)
        s.add_argument("--run-url", default="")
        if name == "record":
            s.add_argument("--results", required=True)
            s.add_argument("--log-dir", default="logs/run")
    args = p.parse_args(argv)
    try:
        manifest = json.loads(Path(args.manifest).read_text())
        site = Path(args.site)
        if args.cmd == "stage":
            stage(manifest, site, args.run_id, args.run_url)
            print(f"staged {len(manifest['jobs'])} record(s) into {site}")
        else:
            results_file = Path(args.results)
            results = json.loads(results_file.read_text()) if results_file.exists() else []
            record_results(manifest, results, site, Path(args.log_dir), args.run_id, args.run_url)
            print(f"recorded results for {len(manifest['jobs'])} record(s); published_commit={manifest['head']}")
    except (SiteError, ch.GitError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
