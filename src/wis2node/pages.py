"""Published file locations on GitHub Pages, and waiting until a file is live.

Each publication gets a new, immutable, timestamped path so that the Pages CDN
can never serve a stale copy:  metadata/<stem>/<stem>.<YYYYMMDDTHHMMSSZ>.json
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath

import certifi
import requests

from .wnm import sha512_b64

LOG = logging.getLogger(__name__)
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class PagesError(Exception):
    """A published file did not become available with the expected content."""


def timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime(TIMESTAMP_FORMAT)


def published_path(source_path: str, stamp: str) -> str:
    """metadata/<name>.json -> metadata/<name>/<name>.<stamp>.json"""
    stem = PurePosixPath(source_path).stem
    return f"metadata/{stem}/{stem}.{stamp}.json"


def join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def wait_until_live(url: str, expected_sha512: str, timeout: float, interval: float = 5.0,
                    session: requests.Session | None = None, clock=time.monotonic, sleep=time.sleep) -> float:
    """Poll `url` until it returns 200 with exactly the expected bytes.

    Returns the seconds waited. Raises PagesError on timeout, reporting the last status.
    Only call this after the Pages deployment has completed, so that the CDN never
    caches a 404 for the new path.
    """
    http = session or requests.Session()
    start = clock()
    last = "no response"
    while True:
        try:
            r = http.get(url, timeout=min(30.0, max(timeout, 1.0)), verify=certifi.where(),
                         headers={"Cache-Control": "no-cache"})
            if r.status_code == 200 and sha512_b64(r.content) == expected_sha512:
                return clock() - start
            last = (f"HTTP {r.status_code}" if r.status_code != 200
                    else "HTTP 200 but content differs from the committed record")
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
        if clock() - start >= timeout:
            raise PagesError(f"{url} not live with expected content after {timeout:g}s ({last})")
        LOG.debug("waiting for %s (%s)", url, last)
        sleep(interval)
