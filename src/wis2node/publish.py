"""Publish WIS2 notifications for changed metadata records.

Three steps, run as separate workflow jobs because the Pages deployment needs its
own job (and environment):

  plan       git diff -> manifest.json: what to announce, and the timestamped URL
             each new/updated record will be published at
  preflight  check that the local broker (publisher and subscriber credentials)
             and the Global Broker accept connections
  run        subscribe, then one helper thread per manifest job: wait until the
             record is live, publish the WNM, confirm delivery on both brokers,
             collect GB/GDC monitoring events, confirm the GDC holds the record

Exit codes: 0 all good, 1 one or more records failed, 2 configuration/preflight error.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from pathlib import Path
from typing import Callable

from . import changes as ch
from . import config as cfgmod
from . import wnm
from .helper import GB, LOCAL, Helper, Job, JsonlLog, Result, Router
from .mqtt import BrokerClient, BrokerError
from .pages import join_url, published_path, timestamp
from .validate import parse_record

LOG = logging.getLogger("wis2node.publish")


# ---------------------------------------------------------------- plan
def _id_at(rev: str, path: str, cwd: str | None) -> str | None:
    record, _ = parse_record(ch.show(rev, path, cwd=cwd))
    return record["id"] if record else None


def resolve_base(explicit: str | None, catalogue: dict | None, fallback: str | None) -> str:
    """Diff from: --base, else the catalogue's last published commit, else the push's
    'before' commit, else nothing (= every record is new)."""
    return explicit or (catalogue or {}).get("published_commit") or fallback or ch.ZERO_SHA


def plan(base: str, head: str, base_url: str, stamp: str, centre_id: str,
         catalogue: dict | None = None, only: list[str] | None = None, cwd: str | None = None,
         republish: list[str] | None = None) -> dict:
    """Work out what to announce. Raises ValueError for records that can't be published.

    `republish` (paths, or ["all"]) ignores the diff and re-announces those records as
    they are at `head`: "update" if the catalogue already holds them, else "canonical".
    """
    known = {rid for rid, e in ((catalogue or {}).get("records") or {}).items()
             if isinstance(e, dict) and not e.get("deleted")}
    if republish:
        at_head = [p for p in ch.git("ls-tree", "-r", "--name-only", head, "--", ch.METADATA_DIR,
                                     cwd=cwd).splitlines() if ch.is_record_path(p)]
        wanted = at_head if republish == ["all"] else [Path(p).as_posix() for p in republish]
        missing = sorted(set(wanted) - set(at_head))
        if missing:
            raise ValueError(f"not records at {head}: {', '.join(missing)}")
        found = [ch.Change(ch.MODIFIED if _id_at(head, p, cwd) in known else ch.ADDED, p) for p in wanted]
    else:
        found = ch.changed_records(base, head, cwd=cwd)
    if only:
        wanted = {Path(p).as_posix() for p in only}
        found = [c for c in found if c.path in wanted]

    current: dict[str, tuple[ch.Change, dict, bytes]] = {}
    deleted: dict[str, tuple[ch.Change, dict]] = {}
    for c in found:
        if c.status == ch.DELETED:
            record, err = parse_record(ch.show(base, c.path, cwd=cwd))
            if err:
                raise ValueError(f"{c.path}: previous version unreadable ({err}): cannot announce deletion")
            deleted[record["id"]] = (c, record)
        else:
            data = ch.show(head, c.path, cwd=cwd)
            record, err = parse_record(data)
            if err:
                raise ValueError(f"{c.path}: {err}")
            current[record["id"]] = (c, record, data)

    jobs = []
    for rid, (c, record, data) in current.items():
        # a file renamed with the same id is an update of an existing record, not a new one
        action = wnm.UPDATE if (c.status == ch.MODIFIED or rid in deleted) else wnm.CANONICAL
        path = published_path(c.path, stamp)
        jobs.append(Job(action=action, source_path=c.path, metadata_id=rid, url=join_url(base_url, path),
                        geometry=record.get("geometry"), datetime=wnm.record_datetime(record),
                        sha512=wnm.sha512_b64(data), length=len(data), published_path=path))
    for rid, (c, record) in deleted.items():
        if rid in current:
            continue
        url = ((catalogue or {}).get("records", {}).get(rid, {}).get("current_url")
               or join_url(base_url, c.path))
        jobs.append(Job(action=wnm.DELETION, source_path=c.path, metadata_id=rid, url=url,
                        geometry=record.get("geometry"), datetime=wnm.record_datetime(record)))

    jobs.sort(key=lambda j: j.source_path)
    head = ch.git("rev-parse", head, cwd=cwd).strip()
    return {"centre_id": centre_id, "stamp": stamp, "base_url": base_url, "base": base, "head": head,
            "jobs": [asdict(j) for j in jobs]}


def load_manifest(path: str) -> tuple[dict, list[Job]]:
    manifest = json.loads(Path(path).read_text())
    known = {f.name for f in fields(Job)}
    return manifest, [Job(**{k: v for k, v in j.items() if k in known}) for j in manifest["jobs"]]


# ---------------------------------------------------------------- brokers
ClientFactory = Callable[..., BrokerClient]


def open_clients(cfg: dict, password: str, router: Router | None, factory: ClientFactory = BrokerClient,
                 ) -> dict[str, BrokerClient]:
    """Connect publisher + both subscribers; on any failure close what was opened and raise."""
    prefix = cfg["centre_id"]
    lb, gb = cfg["local_broker"], cfg["global_broker"]
    local_url = cfgmod.local_broker_url(cfg)
    # (url, username, password, message handler, label used in logs: hides the local broker host)
    specs = {
        "local-pub": (local_url, lb["publish"]["username"], password, None, "local broker"),
        "local-sub": (local_url, lb["subscribe"]["username"], lb["subscribe"]["password"],
                      router.handler(LOCAL) if router else None, "local broker"),
        "gb-sub": (gb["url"], gb["subscribe"]["username"], gb["subscribe"]["password"],
                   router.handler(GB) if router else None, gb["url"]),
    }
    timeout = cfg["timeouts"]["broker_connect"]
    opened: dict[str, BrokerClient] = {}
    try:
        for name, (url, user, pw, handler, label) in specs.items():
            client = factory(name, url, user, pw, prefix, on_message=handler, label=label)
            client.connect(timeout)
            opened[name] = client
            LOG.info("%s: connected to %s as %s", name, label, user)
    except BrokerError:
        close_all(opened)
        raise
    return opened


def close_all(clients: dict[str, BrokerClient]) -> None:
    for c in clients.values():
        try:
            c.close()
        except Exception:
            LOG.warning("error closing %s", c.name, exc_info=True)


def record_centres(jobs: list[Job]) -> set[str]:
    """Centre-ids named in the records' ids (urn:wmo:md:<centre-id>:...)."""
    parts = (j.metadata_id.split(":") for j in jobs)
    return {p[3] for p in parts if len(p) > 4 and p[3]}


def subscribe_all(cfg: dict, clients: dict[str, BrokerClient], jobs: list[Job] | None = None) -> None:
    """The GDC reports on monitor/a/wis2/<centre-id of the record>, which differs from ours
    when a record's id names another centre, so subscribe to those centres' topics too."""
    centre = cfg["centre_id"]
    timeout = cfg["timeouts"]["broker_connect"]
    monitor = [f"monitor/a/wis2/{c}/#" for c in sorted({centre} | record_centres(jobs or []))]
    clients["local-sub"].subscribe([f"origin/a/wis2/{centre}/metadata"], timeout)
    clients["gb-sub"].subscribe([f"origin/a/wis2/{centre}/metadata", *monitor], timeout)


def preflight(cfg: dict, password: str, factory: ClientFactory = BrokerClient) -> None:
    clients = open_clients(cfg, password, None, factory)
    close_all(clients)


# ---------------------------------------------------------------- run
def run(cfg: dict, jobs: list[Job], password: str, log_dir: Path, wait_for_pages: bool = True,
        factory: ClientFactory = BrokerClient, **helper_overrides) -> list[Result]:
    run_log = JsonlLog(log_dir / "monitor.jsonl")
    router = Router(run_log)
    clients = open_clients(cfg, password, router, factory)
    try:
        subscribe_all(cfg, clients, jobs)
        timeouts = dict(cfg["timeouts"])
        gdc_url = cfgmod.get(cfg, "global_discovery_catalogue", "url") or None
        gdc_interval = cfgmod.get(cfg, "global_discovery_catalogue", "poll_interval", default=10)
        helpers = [Helper(job, centre_id=cfg["centre_id"], publisher=clients["local-pub"], router=router,
                          timeouts=timeouts, log=JsonlLog(log_dir / f"{Path(job.source_path).stem}.jsonl"),
                          wait_for_pages=wait_for_pages,
                          ets_failures_as_warnings=cfgmod.get(cfg, "monitor", "ets_failures_as_warnings", default=[]),
                          kpi_warn_below=cfgmod.get(cfg, "kpi", "warn_below_percentage"),
                          require_kpi=bool(cfgmod.get(cfg, "monitor", "require_kpi", default=False)),
                          gdc_url=gdc_url, gdc_interval=gdc_interval, **helper_overrides)
                   for job in jobs]
        workers = max(1, min(len(helpers), cfgmod.get(cfg, "publish", "max_workers", default=4)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="helper") as pool:
            results = list(pool.map(lambda h: h.run(), helpers))
        for h in helpers:
            h.log.close()
        return results
    finally:
        close_all(clients)
        run_log.close()


# ---------------------------------------------------------------- report
def to_markdown(results: list[Result], dry_run: bool = False) -> str:
    lines = ["## WIS2 publication" + (" (dry run)" if dry_run else ""), ""]
    if not results:
        return "\n".join(lines + ["No metadata records to announce."]) + "\n"
    bad = sum(not r.ok for r in results)
    lines.append(f"**{'❌ ' + str(bad) + ' of ' + str(len(results)) + ' record(s) failed' if bad else '✅ ' + str(len(results)) + ' record(s) published and verified'}**")
    lines += ["", "| Record | Action | Local | GB | GB events | GDC ETS | GDC KPI | In GDC | Result |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        def seen(src):
            return f"{r.delivered[src]}s" if src in r.delivered else "–"
        ets = ("–" if r.ets is None else "rejected" if r.ets.get("rejected") else
               ", ".join(f"{v} {k.lower()}" for k, v in (r.ets.get("summary") or {}).items() if v) or "received")
        kpi = "–" if r.kpi is None else f"{r.kpi.get('percentage')}% ({r.kpi.get('grade')})"
        gdc = "–" if r.gdc is None else (f"[{r.gdc['status']}]({r.gdc['url']})"
                                          + (f" {r.gdc['seconds']}s" if "seconds" in r.gdc else ""))
        icon = "✅" if r.ok else f"❌ ({r.stage})"
        lines.append(f"| [`{r.source_path}`]({r.url})<br>{r.metadata_id} | {r.action} | {seen(LOCAL)} | {seen(GB)} "
                     f"| {len(r.gb_events)} | {ets} | {kpi} | {gdc} | {icon} |")
    for r in results:
        if r.errors or r.warnings:
            lines += ["", f"### `{r.source_path}`", f"message id `{r.message_id}`"]
            lines += [f"- ❌ {e}" for e in r.errors] + [f"- ⚠️ {w}" for w in r.warnings]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m wis2node.publish", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(cfgmod.DEFAULT_PATH))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="write the manifest of records to announce")
    sp.add_argument("--base", help="diff from this commit (default: catalogue's published_commit)")
    sp.add_argument("--fallback-base", help="diff from this if there is no --base or published_commit")
    sp.add_argument("--republish", nargs="*", help="re-announce these metadata/ paths (or 'all') as they are at --head")
    sp.add_argument("--head", default="HEAD")
    sp.add_argument("--base-url", required=True, help="public base URL of the Pages site")
    sp.add_argument("--stamp", help="timestamp for published filenames (default: now, UTC)")
    sp.add_argument("--catalogue", help="current catalogue.json (to find deleted records' URLs)")
    sp.add_argument("--only", nargs="*", help="limit to these metadata/ paths")
    sp.add_argument("--out", default="manifest.json")

    sub.add_parser("preflight", help="check both brokers accept our connections")

    sr = sub.add_parser("run", help="publish and verify the manifest's notifications")
    sr.add_argument("--manifest", default="manifest.json")
    sr.add_argument("--log-dir", default="logs/run")
    sr.add_argument("--dry-run", action="store_true", help="print the messages; connect to nothing")
    sr.add_argument("--no-pages", action="store_true", help="don't wait for records to be live on Pages")
    sr.add_argument("--summary", help="write a Markdown report here (e.g. $GITHUB_STEP_SUMMARY)")
    sr.add_argument("--results", help="write the full results as JSON here")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    try:
        cfg = cfgmod.load(args.config)
        base_url = cfgmod.get(cfg, "pages", "base_url") or None

        if args.cmd == "plan":
            catalogue = json.loads(Path(args.catalogue).read_text()) if args.catalogue else None
            base = resolve_base(args.base, catalogue, args.fallback_base)
            manifest = plan(base, args.head, base_url or args.base_url, args.stamp or timestamp(),
                            cfg["centre_id"], catalogue, args.only, republish=args.republish or None)
            print(f"base {base} -> head {args.head}")
            Path(args.out).write_text(json.dumps(manifest, indent=2))
            for j in manifest["jobs"]:
                print(f"{j['action']:9} {j['source_path']} -> {j['url']}")
            print(f"{len(manifest['jobs'])} record(s) to announce; manifest written to {args.out}")
            return 0

        if args.cmd == "preflight":
            preflight(cfg, cfgmod.publisher_password())
            print("preflight OK: local broker (publisher + subscriber) and Global Broker accept connections")
            return 0

        manifest, jobs = load_manifest(args.manifest)
        if manifest.get("centre_id") != cfg["centre_id"]:
            raise cfgmod.ConfigError(f"manifest is for {manifest.get('centre_id')!r}, config for {cfg['centre_id']!r}")
        if args.dry_run:
            msgs = []
            for j in jobs:
                m = wnm.build(centre_id=cfg["centre_id"], metadata_id=j.metadata_id, geometry=j.geometry,
                              href=j.url, rel=j.action, datetime_=j.datetime, integrity=j.sha512, length=j.length)
                msgs.append({"topic": f"origin/a/wis2/{cfg['centre_id']}/metadata", "message": m})
            print(json.dumps(msgs, indent=2))
            return 0
        if not jobs:
            results = []
        else:
            results = run(cfg, jobs, cfgmod.publisher_password(), Path(args.log_dir),
                          wait_for_pages=not args.no_pages)
    except (cfgmod.ConfigError, ch.GitError, BrokerError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = to_markdown(results)
    print(report)
    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write(report)
    if args.results:
        Path(args.results).write_text(json.dumps([asdict(r) for r in results], indent=2))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
