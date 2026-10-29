"""Confirm that the Global Discovery Catalogue holds (or no longer holds) a record.

The GDC is an OGC API - Records endpoint; a record is at <url>/items/<metadata id>.
The GDC adds its own properties and links, so the published version is identified
by its `properties.updated` (else `created`) time rather than by its bytes.
"""
from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import quote

import certifi
import requests

from .wnm import record_datetime


class GDCError(Exception):
    """The GDC did not reach the expected state in time."""


def item_url(base_url: str, metadata_id: str) -> str:
    return f"{base_url.rstrip('/')}/items/{quote(metadata_id, safe='')}?f=json"


def _same_time(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a == b
    try:
        return datetime.fromisoformat(a.replace("Z", "+00:00")) == datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return a == b


def _poll(url: str, done, describe, timeout: float, interval: float, session, clock, sleep) -> float:
    http = session or requests.Session()
    start = clock()
    last = "no response"
    while True:
        try:
            r = http.get(url, timeout=min(30.0, max(timeout, 1.0)), verify=certifi.where(),
                         headers={"Cache-Control": "no-cache", "Accept": "application/json"})
            if done(r):
                return clock() - start
            last = describe(r)
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
        if clock() - start >= timeout:
            raise GDCError(last)
        sleep(interval)


def wait_for_record(base_url: str, metadata_id: str, expected_datetime: str | None, timeout: float,
                    interval: float = 10.0, session=None, clock=time.monotonic, sleep=time.sleep) -> float:
    """Wait until the GDC serves the record with the expected updated/created time."""
    url = item_url(base_url, metadata_id)

    def gdc_time(r):
        try:
            return record_datetime(r.json())
        except ValueError:
            return None

    def done(r):
        return r.status_code == 200 and _same_time(gdc_time(r), expected_datetime)

    def describe(r):
        if r.status_code == 404:
            return "record not in the GDC"
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        return f"GDC has a different version (updated/created {gdc_time(r)}, expected {expected_datetime})"

    try:
        return _poll(url, done, describe, timeout, interval, session, clock, sleep)
    except GDCError as exc:
        raise GDCError(f"Labs GDC check failed after {timeout:g}s: {exc} ({url})") from None


def wait_for_absence(base_url: str, metadata_id: str, timeout: float, interval: float = 10.0,
                     session=None, clock=time.monotonic, sleep=time.sleep) -> float:
    """Wait until the GDC no longer serves the record (HTTP 404)."""
    url = item_url(base_url, metadata_id)
    try:
        return _poll(url, lambda r: r.status_code == 404,
                     lambda r: "record still in the GDC" if r.status_code == 200 else f"HTTP {r.status_code}",
                     timeout, interval, session, clock, sleep)
    except GDCError as exc:
        raise GDCError(f"Labs GDC deletion check failed after {timeout:g}s: {exc} ({url})") from None
