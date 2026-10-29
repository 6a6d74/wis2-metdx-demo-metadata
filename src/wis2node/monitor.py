"""Interpret WIS2 monitoring events (CloudEvents on monitor/a/wis2/<centre-id>).

Matching is deliberately tolerant: the event type codelist is still a draft
and the live Global Broker already uses types (int.wmo.wis.wme.wnm.validation.*)
that differ from the spec examples. We classify on the type and on the shape
of `data.content`.
"""
from __future__ import annotations

from datetime import datetime

WNM_EVENT, ETS_REPORT, KPI_REPORT, ITEM_EVENT, OTHER = "wnm", "ets", "kpi", "item", "other"


def content(event: dict) -> dict:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    c = data.get("content")
    return c if isinstance(c, dict) else {}


def report(event: dict) -> dict:
    """The ETS/KPI report: the content itself (per spec), or nested under content.report."""
    c = content(event)
    return c["report"] if isinstance(c.get("report"), dict) else c


def classify(event: dict) -> str:
    etype = str(event.get("type", ""))
    rtype = str(report(event).get("report_type", "")).lower()
    if rtype == "ets" or etype.endswith(".wcmp2.ets"):
        return ETS_REPORT
    if rtype == "kpi" or etype.endswith(".wcmp2.kpi"):
        return KPI_REPORT
    if ".wnm." in etype or isinstance(content(event).get("wnm"), dict):
        return WNM_EVENT
    # the GDC reports record download failures and deletions as "item.download"
    if etype.endswith(".item.download"):
        return ITEM_EVENT
    return OTHER


def wnm_id(event: dict) -> str | None:
    wnm = content(event).get("wnm")
    if isinstance(wnm, dict) and isinstance(wnm.get("id"), str):
        return wnm["id"]
    ref = (event.get("data") or {}).get("ref")
    return ref if isinstance(ref, str) else None


def metadata_id(event: dict) -> str | None:
    r = report(event)
    mid = r.get("metadata_id") or content(event).get("metadata_id")
    if isinstance(mid, str):
        return mid
    wnm = content(event).get("wnm")
    if isinstance(wnm, dict):
        mid = (wnm.get("properties") or {}).get("metadata_id")
        return mid if isinstance(mid, str) else None
    return None


def link_hrefs(event: dict) -> list[str]:
    """hrefs of the event's links (top level, and under data)."""
    hrefs = []
    for links in (event.get("links"), (event.get("data") or {}).get("links")):
        if isinstance(links, list):
            hrefs += [lk["href"] for lk in links if isinstance(lk, dict) and isinstance(lk.get("href"), str)]
    return hrefs


def link_filenames(event: dict) -> list[str]:
    """Last path segment of each link: our timestamped filename survives the cache hop
    (the GDC's events link to the Global Cache copy, not to our Pages URL)."""
    return [h.rstrip("/").rsplit("/", 1)[-1] for h in link_hrefs(event)]


def description(event: dict) -> str:
    return str(content(event).get("description") or "")


def severity(event: dict) -> str:
    return str((event.get("data") or {}).get("severity", "")).upper()


def event_time(event: dict) -> datetime | None:
    value = event.get("time")
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def title(event: dict) -> str:
    return str(content(event).get("title") or event.get("type", "event"))


def errors(event: dict) -> list[str]:
    errs = content(event).get("errors")
    return [str(e) for e in errs] if isinstance(errs, list) else []


def ets_failures(event: dict) -> list[dict]:
    """Tests with code FAILED (plus a schema validation failure if reported)."""
    r = report(event)
    failed = [t for t in r.get("tests", []) if isinstance(t, dict) and t.get("code") == "FAILED"]
    validation = r.get("validation")
    if isinstance(validation, dict) and validation.get("code") == "FAILED":
        failed.append(validation)
    count = (r.get("summary") or {}).get("FAILED")
    if not failed and isinstance(count, int) and count > 0:
        failed.append({"id": "summary", "code": "FAILED",
                       "message": f"summary reports {count} failed test(s) without details"})
    return failed


def kpi_summary(event: dict) -> dict:
    s = report(event).get("summary")
    return s if isinstance(s, dict) else {}


def short_test_id(test: dict) -> str:
    return str(test.get("id", "?")).rstrip("/").rsplit("/", 1)[-1]
