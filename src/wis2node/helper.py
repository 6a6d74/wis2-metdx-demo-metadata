"""Per-record publishing helpers and the router that feeds them broker traffic.

The orchestrator opens the shared subscriptions once (local + GB origin topic,
GB monitor topic) and installs `Router.handler(...)` as their message callback.
Each `Helper` registers its message id and metadata id with the router *before*
publishing, then consumes its own inbox queue: delivery confirmations and
monitoring events that concern it.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from . import monitor as mon
from . import wnm
from .gdc import GDCError, current_version, item_url, same_version, wait_for_absence, wait_for_record
from .mqtt import BrokerError
from .pages import PagesError, wait_until_live

LOG = logging.getLogger(__name__)

LOCAL, GB = "local", "gb"
SOURCES = (LOCAL, GB)


class Publisher(Protocol):
    def publish(self, topic: str, payload: bytes, timeout: float, qos: int = 1) -> None: ...


@dataclass
class Job:
    """One record to announce. Built by `publish plan`, carried in the manifest."""
    action: str                   # wnm.CANONICAL / UPDATE / DELETION
    source_path: str              # metadata/<name>.json
    metadata_id: str
    url: str                      # where the record is (or was) published
    geometry: dict | None = None
    datetime: str | None = None
    sha512: str | None = None     # None for deletions
    length: int | None = None
    published_path: str | None = None  # path within the Pages site (not for deletions)


@dataclass
class Result:
    source_path: str
    metadata_id: str
    action: str
    url: str
    message_id: str | None = None
    ok: bool = False
    stage: str = "pending"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    delivered: dict[str, float] = field(default_factory=dict)  # source -> seconds after publish
    gb_events: list[dict] = field(default_factory=list)        # summaries of GB WNM events
    gdc_events: list[dict] = field(default_factory=list)       # GDC download/deletion reports
    ets: dict | None = None
    kpi: dict | None = None
    gdc: dict | None = None                                    # Labs GDC confirmation
    timings: dict[str, float] = field(default_factory=dict)
    log_file: str | None = None


class JsonlLog:
    """Thread-safe JSON Lines writer."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, kind: str, **fields) -> None:
        line = json.dumps({"logged": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                           "kind": kind, **fields}, default=str)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class Router:
    """Routes incoming broker messages to the helper they concern."""

    def __init__(self, run_log: JsonlLog | None = None):
        self._by_message: dict[str, "Helper"] = {}
        self._by_metadata: dict[str, "Helper"] = {}
        self._by_file: dict[str, "Helper"] = {}  # published filename (unique per publication)
        self._lock = threading.Lock()
        self._run_log = run_log

    def register(self, helper: "Helper") -> None:
        with self._lock:
            self._by_message[helper.message_id] = helper
            self._by_metadata[helper.job.metadata_id] = helper
            self._by_file[_filename(helper.job.url)] = helper

    def unregister(self, helper: "Helper") -> None:
        with self._lock:
            self._by_message.pop(helper.message_id, None)
            if self._by_metadata.get(helper.job.metadata_id) is helper:
                del self._by_metadata[helper.job.metadata_id]
            if self._by_file.get(_filename(helper.job.url)) is helper:
                del self._by_file[_filename(helper.job.url)]

    def handler(self, source: str) -> Callable[[str, bytes, bool], None]:
        def on_message(topic: str, payload: bytes, retained: bool) -> None:
            try:
                self.dispatch(source, topic, payload, retained)
            except Exception:  # never let a bad message kill paho's network thread
                LOG.exception("error routing message from %s on %s", source, topic)
        return on_message

    def dispatch(self, source: str, topic: str, payload: bytes, retained: bool = False) -> None:
        try:
            body = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            if self._run_log and topic.startswith("monitor/"):
                self._run_log.write("unparseable", source=source, topic=topic,
                                    payload=payload.decode("utf-8", "replace"))
            return

        if topic.startswith("monitor/"):
            target = self._match_event(body)
            if self._run_log:
                self._run_log.write("monitor", source=source, topic=topic, retained=retained,
                                    matched=target.job.source_path if target else None, event=body)
            if target and not retained:
                target.inbox.put(("event", source, topic, body))
            return

        with self._lock:
            target = self._by_message.get(body.get("id"))
        if target and not retained:
            target.inbox.put(("delivery", source, topic, body))

    def _match_event(self, event: dict) -> "Helper | None":
        with self._lock:
            wid = mon.wnm_id(event)
            if wid and wid in self._by_message:
                return self._by_message[wid]
            if mon.classify(event) == mon.WNM_EVENT:
                return None
            # GDC reports: by metadata id (ETS, KPI, deletion), else by our published
            # filename in the event's links (download failures and "Topic mismatch"
            # carry no metadata id, only a link to the (cached) record)
            target = self._by_metadata.get(mon.metadata_id(event) or "")
            if target is None:
                target = next((self._by_file[f] for f in mon.link_filenames(event) if f in self._by_file), None)
            return target


def _filename(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


class Helper:
    """Publishes one record's notification and verifies what happens to it."""

    def __init__(self, job: Job, *, centre_id: str, publisher: Publisher, router: Router,
                 timeouts: dict, log: JsonlLog, wait_for_pages: bool = True,
                 ets_failures_as_warnings: list[str] | None = None,
                 kpi_warn_below: float | None = None, require_kpi: bool = False, clock=time.monotonic,
                 live_check: Callable[..., float] = wait_until_live,
                 gdc_url: str | None = None, gdc_interval: float = 10.0,
                 gdc_present: Callable[..., float] = wait_for_record,
                 gdc_absent: Callable[..., float] = wait_for_absence,
                 gdc_version: Callable[..., tuple[str, str | None]] = current_version):
        self.job = job
        self.centre_id = centre_id
        self.publisher = publisher
        self.router = router
        self.timeouts = timeouts
        self.log = log
        self.wait_for_pages = wait_for_pages
        self.ets_relaxed = set(ets_failures_as_warnings or [])
        self.kpi_warn_below = kpi_warn_below
        self.require_kpi = require_kpi
        self._deletion_reported = False
        self.clock = clock
        self.live_check = live_check
        self.gdc_url = gdc_url
        self.gdc_interval = gdc_interval
        self.gdc_present = gdc_present
        self.gdc_absent = gdc_absent
        self.gdc_version = gdc_version
        self._gdc_before: tuple[str, str | None] | None = None  # what the GDC held before we published
        self._soft_errors: list[str] = []  # errors that the GDC registering this version overrides
        self.inbox: queue.Queue = queue.Queue()
        self.message = self._build_message()
        self.message_id = self.message["id"]
        self.topic = f"origin/a/wis2/{centre_id}/metadata"
        self.result = Result(job.source_path, job.metadata_id, job.action, job.url,
                             message_id=self.message_id, log_file=str(log.path))
        self._seen_events: set[str] = set()
        self._published_at: datetime | None = None  # wall clock, to compare with event times
        self._t0: float | None = None                 # monotonic, for delivery latency

    def _build_message(self) -> dict:
        j = self.job
        return wnm.build(centre_id=self.centre_id, metadata_id=j.metadata_id, geometry=j.geometry,
                         href=j.url, rel=j.action, datetime_=j.datetime,
                         integrity=j.sha512, length=j.length)

    # --- main sequence ---
    def run(self) -> Result:
        r = self.result
        try:
            if self.job.action != wnm.DELETION and self.wait_for_pages:
                r.stage = "pages"
                r.timings["pages_live"] = round(self.live_check(
                    self.job.url, self.job.sha512, self.timeouts["pages_live"]), 1)
                self.log.write("pages-live", url=self.job.url, seconds=r.timings["pages_live"])

            if self.gdc_url and self.job.action != wnm.DELETION:
                self._gdc_before = self.gdc_version(self.gdc_url, self.job.metadata_id)
                self.log.write("gdc-before", status=self._gdc_before[0], updated=self._gdc_before[1])

            r.stage = "publish"
            self.router.register(self)
            self.message["properties"]["pubtime"] = wnm.utc_now()
            self.log.write("wnm", topic=self.topic, message=self.message)
            self._published_at = datetime.now(timezone.utc)
            t0 = self._t0 = self.clock()
            self.publisher.publish(self.topic, json.dumps(self.message).encode(), self.timeouts["delivery"])
            r.timings["puback"] = round(self.clock() - t0, 2)

            r.stage = "delivery"
            self._await(lambda: all(s in r.delivered for s in SOURCES), t0, self.timeouts["delivery"])
            missing = [s for s in SOURCES if s not in r.delivered]
            if missing:
                names = {LOCAL: "local broker", GB: "Global Broker"}
                r.errors.append("message not delivered by the " + " or the ".join(names[m] for m in missing)
                                + f" within {self.timeouts['delivery']}s")

            r.stage = "monitor"
            t1 = self.clock()
            if self.job.action == wnm.DELETION:
                self._await(lambda: self._deletion_reported, t1, self.timeouts["monitor"])
                if not self._deletion_reported:
                    r.warnings.append(f"no deletion report from the GDC within {self.timeouts['monitor']}s")
            else:
                self._await(lambda: r.ets is not None and (r.kpi is not None or not self.require_kpi),
                            t1, self.timeouts["monitor"])
                if r.ets is None:
                    msg = f"no WCMP2 ETS report from the GDC within {self.timeouts['monitor']}s"
                    r.errors.append(msg)
                    self._soft_errors.append(msg)
                if r.kpi is None and self.require_kpi:
                    r.errors.append(f"no WCMP2 KPI report from the GDC within {self.timeouts['monitor']}s")
            r.timings["monitor"] = round(self.clock() - t1, 1)

            if self.gdc_url:
                r.stage = "gdc"
                self._check_gdc()
                self._apply_gdc_confirmation()
            self._await(lambda: True, 0, 0)  # pick up anything that arrived meanwhile (e.g. a late KPI)
            r.stage = "done"
        except (PagesError, BrokerError) as exc:
            r.errors.append(str(exc))
        except Exception as exc:  # report, never crash the other helpers
            LOG.exception("helper for %s failed", self.job.source_path)
            r.errors.append(f"unexpected {type(exc).__name__} at stage {r.stage}: {exc}")
        finally:
            self.router.unregister(self)
            r.ok = not r.errors
            self.log.write("result", result=asdict(r))
        return r

    def _check_gdc(self) -> None:
        """Confirm the GDC now holds this version of the record (or, for a deletion, doesn't hold it)."""
        r, j = self.result, self.job
        deletion = j.action == wnm.DELETION
        url = item_url(self.gdc_url, j.metadata_id)
        try:
            if deletion:
                secs = self.gdc_absent(self.gdc_url, j.metadata_id, self.timeouts["gdc"], interval=self.gdc_interval)
            else:
                secs = self.gdc_present(self.gdc_url, j.metadata_id, j.datetime, self.timeouts["gdc"],
                                        interval=self.gdc_interval)
            r.gdc = {"status": "removed" if deletion else "present", "seconds": round(secs, 1), "url": url}
            r.timings["gdc"] = round(secs, 1)
        except GDCError as exc:
            r.gdc = {"status": "not confirmed", "url": url}
            r.errors.append(str(exc))
        self.log.write("gdc", **r.gdc)

    def _apply_gdc_confirmation(self) -> None:
        """The GDC check is ground truth. If the GDC changed to exactly this version after we
        published, a rejection event or a missing ETS report is downgraded to a warning (e.g.
        the Labs GDC reports "Topic mismatch" but registers the record anyway). If it already
        held this version before, registration can't be attributed to this notification."""
        r = self.result
        if not self._soft_errors or not r.gdc or r.gdc.get("status") != "present":
            return
        before_status, before = self._gdc_before or ("unknown", None)
        if before_status == "present" and same_version(before, self.job.datetime):
            r.warnings.append("the GDC already held this version before publishing, so it can't be confirmed "
                              "that this notification was registered (change properties.updated to verify)")
            return
        if before_status == "unknown":
            return
        for msg in self._soft_errors:
            if msg in r.errors:
                r.errors.remove(msg)
                r.warnings.append(f"{msg}; but the GDC registered this version anyway "
                                  f"(updated {before or 'absent'} -> {self.job.datetime})")
        if r.ets and r.ets.get("rejected"):
            r.ets["registered_anyway"] = True

    def _await(self, done: Callable[[], bool], start: float, timeout: float) -> None:
        """Consume the inbox until done() or timeout, handling every item."""
        while not done():
            remaining = timeout - (self.clock() - start)
            if remaining <= 0:
                return
            try:
                item = self.inbox.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            self._handle(*item)
        # drain anything already queued so late errors are not lost
        while True:
            try:
                self._handle(*self.inbox.get_nowait())
            except queue.Empty:
                return

    # --- inbox items ---
    def _handle(self, kind: str, source: str, topic: str, body: dict) -> None:
        if kind == "delivery":
            if source not in self.result.delivered:
                self.result.delivered[source] = round(self.clock() - (self._t0 or self.clock()), 2)
                self.log.write("delivered", source=source, topic=topic, message_id=body.get("id"))
            return

        eid = body.get("id")
        if eid in self._seen_events:  # QoS 1 redelivery
            return
        if eid:
            self._seen_events.add(eid)
        cls = mon.classify(body)
        if cls in (mon.ETS_REPORT, mon.KPI_REPORT) and self._is_stale(body):
            self.log.write("stale-event", source=source, topic=topic, event=body)
            return
        self.log.write("event", source=source, topic=topic, classified=cls, event=body)
        {mon.WNM_EVENT: self._on_wnm_event, mon.ETS_REPORT: self._on_ets,
         mon.KPI_REPORT: self._on_kpi, mon.ITEM_EVENT: self._on_item}.get(cls, self._on_other)(body)

    def _is_stale(self, event: dict) -> bool:
        t = mon.event_time(event)
        if t is None or self._published_at is None:
            return False
        skew = timedelta(seconds=self.timeouts.get("clock_skew", 30))
        return t < self._published_at - skew

    def _on_wnm_event(self, event: dict) -> None:
        sev = mon.severity(event)
        detail = "; ".join(mon.errors(event))
        summary = {"type": event.get("type"), "severity": sev, "title": mon.title(event),
                   "errors": mon.errors(event), "source": event.get("source")}
        self.result.gb_events.append(summary)
        text = f"{event.get('source', 'GB')}: {mon.title(event)}" + (f" ({detail})" if detail else "")
        if sev in ("ERROR", "CRITICAL"):
            self.result.errors.append(text)
        elif sev == "WARNING":
            self.result.warnings.append(text)

    def _on_ets(self, event: dict) -> None:
        failures = mon.ets_failures(event)
        if not failures and mon.severity(event) in ("ERROR", "CRITICAL"):
            # e.g. "Topic mismatch": the GDC rejected the record before running the tests
            reason = mon.description(event) or mon.title(event)
            self.result.ets = {"rejected": reason, "summary": None, "source": event.get("source"), "failed": []}
            msg = f"GDC rejected the record: {reason}"
            self.result.errors.append(msg)
            self._soft_errors.append(msg)
            return
        self.result.ets = {"summary": mon.report(event).get("summary"), "source": event.get("source"),
                           "failed": [{"test": mon.short_test_id(t), "message": t.get("message")} for t in failures]}
        for t in failures:
            name = mon.short_test_id(t)
            text = f"GDC ETS {name} FAILED" + (f": {t['message']}" if t.get("message") else "")
            (self.result.warnings if name in self.ets_relaxed else self.result.errors).append(text)

    def _on_kpi(self, event: dict) -> None:
        s = mon.kpi_summary(event)
        self.result.kpi = {"percentage": s.get("percentage"), "grade": s.get("grade"),
                           "score": s.get("score"), "total": s.get("total"), "source": event.get("source")}
        pct = s.get("percentage")
        if self.kpi_warn_below is not None and isinstance(pct, (int, float)) and pct < self.kpi_warn_below:
            self.result.warnings.append(f"GDC KPI {pct}% is below the threshold of {self.kpi_warn_below}%")

    def _on_item(self, event: dict) -> None:
        """GDC download failure, or the GDC's report on a deletion."""
        sev = mon.severity(event)
        text = f"{event.get('source', 'GDC')}: {mon.title(event)}" + \
            (f" ({mon.description(event)})" if mon.description(event) else "")
        self.result.gdc_events.append({"type": event.get("type"), "severity": sev, "title": mon.title(event),
                                       "description": mon.description(event), "source": event.get("source")})
        if self.job.action == wnm.DELETION and mon.metadata_id(event) == self.job.metadata_id:
            self._deletion_reported = True
        if sev in ("ERROR", "CRITICAL"):
            self.result.errors.append(text)
        elif sev == "WARNING":
            self.result.warnings.append(text)

    def _on_other(self, event: dict) -> None:
        if mon.severity(event) in ("ERROR", "CRITICAL"):
            self.result.warnings.append(f"{event.get('source', '?')}: {mon.title(event)} ({event.get('type')})")
