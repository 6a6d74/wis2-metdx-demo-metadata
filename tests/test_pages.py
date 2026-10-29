import pytest
import requests

from wis2node import pages
from wis2node.wnm import sha512_b64


class Resp:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
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


def test_published_path_and_url():
    assert pages.published_path("metadata/obs.json", "20261030T120000Z") == \
        "metadata/obs/obs.20261030T120000Z.json"
    assert pages.join_url("https://x/site/", "/metadata/a.json") == "https://x/site/metadata/a.json"


def test_waits_through_404_and_stale_content():
    body = b'{"id": "x"}'
    s = Session([Resp(404), requests.ConnectionError("reset"), Resp(200, b"old"), Resp(200, body)])
    clock = Clock()
    waited = pages.wait_until_live("https://x/a.json", sha512_b64(body), timeout=60, interval=5,
                                   session=s, clock=clock, sleep=clock.sleep)
    assert s.calls == 4 and waited == 15


def test_timeout_reports_last_status():
    clock = Clock()
    with pytest.raises(pages.PagesError, match="HTTP 200 but content differs"):
        pages.wait_until_live("https://x/a.json", "expected", timeout=12, interval=5,
                              session=Session([Resp(200, b"other")]), clock=clock, sleep=clock.sleep)
