import pytest
import requests

from wis2node import gdc

BASE = "https://gdc.example/collections/wis2-discovery-metadata/"
MID = "urn:wmo:md:io-wis2dev-metdx-demo:obs"


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class Session:
    def __init__(self, responses):
        self.responses, self.urls = list(responses), []

    def get(self, url, **kw):
        self.urls.append(url)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def rec(updated):
    return {"id": MID, "properties": {"created": "2026-01-01T00:00:00Z", "updated": updated}}


def test_item_url_encodes_id():
    assert gdc.item_url(BASE, MID) == \
        "https://gdc.example/collections/wis2-discovery-metadata/items/urn%3Awmo%3Amd%3Aio-wis2dev-metdx-demo%3Aobs?f=json"


def test_waits_until_expected_version():
    s = Session([Resp(404), requests.ConnectionError("x"), Resp(200, rec("2026-10-29T00:00:00Z")),
                 Resp(200, rec("2026-10-30T00:00:00.000Z"))])   # same instant, different format
    clock = Clock()
    waited = gdc.wait_for_record(BASE, MID, "2026-10-30T00:00:00Z", timeout=100, interval=10,
                                 session=s, clock=clock, sleep=clock.sleep)
    assert waited == 30 and len(s.urls) == 4


def test_timeout_says_why():
    clock = Clock()
    with pytest.raises(gdc.GDCError, match="different version .*2026-10-29"):
        gdc.wait_for_record(BASE, MID, "2026-10-30T00:00:00Z", timeout=25, interval=10,
                            session=Session([Resp(200, rec("2026-10-29T00:00:00Z"))]), clock=clock, sleep=clock.sleep)
    clock = Clock()
    with pytest.raises(gdc.GDCError, match="record not in the GDC"):
        gdc.wait_for_record(BASE, MID, None, timeout=5, interval=10,
                            session=Session([Resp(404)]), clock=clock, sleep=clock.sleep)


def test_absence():
    clock = Clock()
    assert gdc.wait_for_absence(BASE, MID, timeout=60, interval=10, session=Session([Resp(200, rec("x")), Resp(404)]),
                                clock=clock, sleep=clock.sleep) == 10
    clock = Clock()
    with pytest.raises(gdc.GDCError, match="still in the GDC"):
        gdc.wait_for_absence(BASE, MID, timeout=15, interval=10, session=Session([Resp(200, rec("x"))]),
                             clock=clock, sleep=clock.sleep)


def test_current_version():
    assert gdc.current_version(BASE, MID, session=Session([Resp(404)])) == ("absent", None)
    assert gdc.current_version(BASE, MID, session=Session([Resp(200, rec("2026-10-30T11:24:00Z"))])) == \
        ("present", "2026-10-30T11:24:00Z")
    assert gdc.current_version(BASE, MID, session=Session([Resp(500)])) == ("unknown", None)
    assert gdc.current_version(BASE, MID, session=Session([requests.ConnectionError("x")])) == ("unknown", None)
