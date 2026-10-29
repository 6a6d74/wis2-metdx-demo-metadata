"""Daily health check: can we publish right now?

Checks, each independently (one failure doesn't hide the others):
  brokers  the same preflight as a real publication: local broker (publisher and
           subscriber logins) and the Global Broker accept connections
  gdc      the Global Discovery Catalogue API answers for our collection
  pages    the published catalogue (catalogue.json) is served by GitHub Pages

Usage:
  python -m wis2node.health [--pages-url URL] [--summary FILE] [--report FILE]

Exit codes: 0 all healthy, 1 one or more checks failed, 2 configuration error.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import certifi
import requests

from . import config as cfgmod
from .mqtt import BrokerClient, BrokerError
from .pages import join_url
from .publish import preflight


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def check_brokers(cfg: dict, password: str, factory=BrokerClient) -> Check:
    try:
        preflight(cfg, password, factory=factory)
        return Check("brokers", True, "local broker (publisher + subscriber) and Global Broker accept connections")
    except (BrokerError, cfgmod.ConfigError) as exc:
        return Check("brokers", False, str(exc))


def check_url(name: str, url: str, expect_json_key: str, get: Callable = requests.get) -> Check:
    try:
        r = get(url, timeout=30, verify=certifi.where(), headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        return Check(name, False, f"{url}: {type(exc).__name__}: {exc}")
    if r.status_code != 200:
        return Check(name, False, f"{url}: HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        return Check(name, False, f"{url}: response is not JSON")
    if expect_json_key not in body:
        return Check(name, False, f"{url}: unexpected response (no '{expect_json_key}')")
    return Check(name, True, f"{url}: OK")


def run_checks(cfg: dict, password: str | None, pages_url: str | None, factory=BrokerClient,
               get: Callable = requests.get) -> list[Check]:
    checks = []
    if password is None:
        checks.append(Check("brokers", False, f"{cfgmod.PUBLISHER_PASSWORD_ENV} is not set"))
    else:
        checks.append(check_brokers(cfg, password, factory))

    gdc = cfgmod.get(cfg, "global_discovery_catalogue", "url")
    if gdc:
        checks.append(check_url("gdc", f"{gdc.rstrip('/')}?f=json", "id", get))

    base = cfgmod.get(cfg, "pages", "base_url") or pages_url
    if base:
        checks.append(check_url("pages", join_url(base, "catalogue.json"), "records", get))
    return checks


def to_markdown(checks: list[Check]) -> str:
    bad = [c for c in checks if not c.ok]
    lines = ["## Health check", "",
             f"**{'❌ ' + str(len(bad)) + ' check(s) failing' if bad else '✅ all checks passed'}**", "",
             "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {c.name} | {'✅' if c.ok else '❌'} | {c.detail} |" for c in checks]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m wis2node.health", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(cfgmod.DEFAULT_PATH))
    p.add_argument("--pages-url", help="Pages base URL (if pages.base_url is not set)")
    p.add_argument("--summary", help="append the Markdown report here (e.g. $GITHUB_STEP_SUMMARY)")
    p.add_argument("--report", help="also write the Markdown report to this file")
    args = p.parse_args(argv)
    try:
        cfg = cfgmod.load(args.config)
    except cfgmod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        password = cfgmod.publisher_password()
    except cfgmod.ConfigError:
        password = None

    checks = run_checks(cfg, password, args.pages_url)
    report = to_markdown(checks)
    print(report)
    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write(report)
    if args.report:
        Path(args.report).write_text(report)
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
