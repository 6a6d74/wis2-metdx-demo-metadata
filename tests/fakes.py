"""An in-memory stand-in for the local broker, the Global Broker and the GDC."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

from wis2node.mqtt import BrokerError


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeWIS2:
    """Behaviour knobs; `factory` creates fake BrokerClients bound to this network."""

    def __init__(self, centre="io-wis2dev-metdx-demo"):
        self.centre = centre
        self.clients = {}
        self.urls = {}                    # client name -> (url, label)
        self.published = []
        self.refuse_connect = set()       # client names
        self.refuse_publish = False
        self.gb_forwards = True
        self.local_delivers = True
        self.gb_validation_errors = []    # list of error strings -> one ERROR event each
        self.gdc_ets_failed = []          # list of (test short id, message)
        self.gdc_sends_ets = True
        self.gdc_sends_kpi = True
        self.gdc_kpi_percentage = 85.0
        self.gdc_stale_ets = False        # ETS event dated well before publication
        self.gdc_report_style = "spec"    # "spec": report fields in data.content
        self.gdc_download_fails = False   # GDC can't fetch the record: item.download ERROR, no ETS
        self.gdc_delete_result = "deleted"  # "deleted" (INFO), "not found" (ERROR) or None (no report)
        self.gdc_enforces_topic_match = True  # real GDC: record centre-id must equal the topic's
        self.gdc_source = "io-wis2dev-labs-global-discovery-catalogue"   # the target (config)
        self.gdc_kpi_delay = 0.0          # seconds between the target's ETS and KPI reports
        self.other_gdcs = []              # [(source, {option overrides})]: other catalogue instances

    def factory(self, name, url, username, password, prefix, on_message=None, label=None):
        self.urls[name] = (url, label)
        c = FakeClient(self, name, on_message)
        self.clients[name] = c
        return c

    def _deliver(self, client_name, topic, payload):
        c = self.clients.get(client_name)
        if c and c.connected and any(topic_matches(s, topic) for s in c.subs) and c.on_message:
            c.on_message(topic, payload, False)

    def _event(self, etype, source, content, severity="INFO", links=None):
        return {"links": links or [], **{"specversion": "1.0", "type": etype, "source": source, "subject": self.centre,
                "id": str(uuid.uuid4()), "time": iso(datetime.now(timezone.utc)),
                "datacontenttype": "application/json",
                "data": {"conformsTo": ["http://wis.wmo.int/spec/wme/1/conf/monitoring-event-message-core"],
                         "severity": severity, "content": content}}}

    def on_publish(self, topic, payload):
        self.published.append((topic, json.loads(payload)))
        msg = json.loads(payload)
        mon = f"monitor/a/wis2/{self.centre}"
        if self.local_delivers:
            self._deliver("local-sub", topic, payload)
        if not self.gb_forwards:
            return
        self._deliver("gb-sub", topic, payload)
        for err in self.gb_validation_errors:
            ev = self._event("int.wmo.wis.wme.wnm.validation.schema", "io-wis2dev-global-broker",
                             {"wnm": msg, "title": "WIS2 Notification Message not compliant with the defined schema",
                              "errors": [err]}, "ERROR")
            self._deliver("gb-sub", mon, json.dumps(ev).encode())
        rel = msg["links"][0]["rel"]
        mid = msg["properties"]["metadata_id"]
        # like the real chain: a GDC works from the Global Cache copy (same filename, other host)
        # and reports ETS/KPI on the monitor topic of the centre named in the record's id
        filename = msg["links"][0]["href"].rsplit("/", 1)[-1]
        cached = f"https://cache.example/data/{self.centre}/metadata/20261030T000000Z/{filename}"
        ctx = dict(rel=rel, mid=mid, cached=cached, record_centre=mid.split(":")[3], mon=mon)
        # the target catalogue, with all the behaviour knobs
        self._gdc(self.gdc_source, enforce=self.gdc_enforces_topic_match, ets_failed=self.gdc_ets_failed,
                  sends_ets=self.gdc_sends_ets, sends_kpi=self.gdc_sends_kpi, delete_result=self.gdc_delete_result,
                  download_fails=self.gdc_download_fails, stale=self.gdc_stale_ets,
                  kpi_delay=self.gdc_kpi_delay, **ctx)
        # other catalogue instances that also process our notifications
        for source, opts in self.other_gdcs:
            self._gdc(source, **{"enforce": True, "ets_failed": [], "sends_ets": True, "sends_kpi": False,
                                 "delete_result": "deleted", "download_fails": False, "stale": False,
                                 "kpi_delay": 0.0, **opts}, **ctx)

    def _gdc(self, source, *, enforce, ets_failed, sends_ets, sends_kpi, delete_result, download_fails, stale,
             kpi_delay, rel, mid, cached, record_centre, mon):
        rec_mon = f"monitor/a/wis2/{record_centre}"
        if rel == "deletion":
            if delete_result:
                ok = delete_result == "deleted"
                ev = self._event("int.wmo.wis.wme.event.item.download", source,
                                 {"metadata_id": mid, "description": f"metadata {mid} {delete_result}",
                                  "title": "WIS2 GDC WCMP2 deletion report"}, "INFO" if ok else "ERROR")
                self._deliver("gb-sub", mon, json.dumps(ev).encode())
            return
        if download_fails:
            ev = self._event("int.wmo.wis.wme.event.item.download", source,
                             {"description": "404 Client Error: Not Found", "title": "WCMP2 access failure"},
                             "ERROR", links=[{"rel": "related", "href": cached}])
            self._deliver("gb-sub", mon, json.dumps(ev).encode())
            return
        if enforce and record_centre != self.centre:
            ev = self._event("int.wmo.wis.wme.event.wcmp2.ets", source,
                             {"description": f"Topic mismatch ({self.centre} != {record_centre})",
                              "title": "Topic mismatch"}, "ERROR", links=[{"rel": "related", "href": cached}])
            self._deliver("gb-sub", rec_mon, json.dumps(ev).encode())
            return
        if sends_ets:
            tests = [{"id": f"http://wis.wmo.int/spec/wcmp/2/conf/core/{t}", "code": "FAILED", "message": m}
                     for t, m in ets_failed]
            tests.append({"id": "http://wis.wmo.int/spec/wcmp/2/conf/core/title", "code": "PASSED"})
            report = {"id": str(uuid.uuid4()), "report_type": "ets", "metadata_id": mid, "tests": tests,
                      "summary": {"PASSED": 1, "FAILED": len(ets_failed), "SKIPPED": 0}}
            ev = self._event("int.wmo.wis.wme.event.wcmp2.ets", source, report)
            if stale:
                ev["time"] = iso(datetime.now(timezone.utc) - timedelta(hours=1))
            self._deliver("gb-sub", rec_mon, json.dumps(ev).encode())
        if sends_kpi:
            report = {"id": str(uuid.uuid4()), "report_type": "kpi", "metadata_id": mid, "tests": [],
                      "summary": {"total": 20, "score": 17, "percentage": self.gdc_kpi_percentage, "grade": "A"}}
            ev = self._event("int.wmo.wis.wme.event.wcmp2.kpi", source, report)
            send = lambda: self._deliver("gb-sub", rec_mon, json.dumps(ev).encode())  # noqa: E731
            if kpi_delay:
                threading.Timer(kpi_delay, send).start()   # like the real GDC: KPI a little after ETS
            else:
                send()


class FakeClient:
    def __init__(self, net: FakeWIS2, name, on_message):
        self.net, self.name, self.on_message = net, name, on_message
        self.connected = False
        self.closed = False
        self.subs = []

    def connect(self, timeout):
        if self.name in self.net.refuse_connect:
            raise BrokerError(f"[{self.name}] connection refused by fake: Not authorized")
        self.connected = True

    def subscribe(self, topics, timeout, qos=1):
        self.subs.extend(topics)

    def publish(self, topic, payload, timeout, qos=1):
        if self.net.refuse_publish:
            raise BrokerError(f"[{self.name}] publish to {topic} refused: Not authorized")
        self.net.on_publish(topic, payload)

    def close(self):
        self.connected = False
        self.closed = True
