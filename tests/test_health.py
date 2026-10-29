import pytest
import requests

from fakes import FakeWIS2
from wis2node import health

PAGES = "https://pages.example/site/"


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def http(responses):
    """Fake requests.get keyed on URL substring."""
    def get(url, **kw):
        for key, resp in responses.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected URL {url}")
    return get


HEALTHY = {"wis2-discovery-metadata": Resp(200, {"id": "wis2-discovery-metadata"}),
           "catalogue.json": Resp(200, {"records": {}})}


@pytest.fixture
def hcfg(cfg, monkeypatch):
    monkeypatch.delenv("LOCAL_BROKER_URL", raising=False)
    cfg["local_broker"]["url"] = "mqtts://local.example:8883"
    cfg["timeouts"]["broker_connect"] = 1
    return cfg


def test_all_healthy(hcfg):
    checks = health.run_checks(hcfg, "pw", PAGES, factory=FakeWIS2().factory, get=http(HEALTHY))
    assert [(c.name, c.ok) for c in checks] == [("brokers", True), ("gdc", True), ("pages", True)]
    assert "all checks passed" in health.to_markdown(checks)


def test_each_failure_reported_independently(hcfg):
    net = FakeWIS2()
    net.refuse_connect = {"local-pub"}
    get = http({"wis2-discovery-metadata": Resp(503), "catalogue.json": requests.ConnectionError("down")})
    checks = {c.name: c for c in health.run_checks(hcfg, "pw", PAGES, factory=net.factory, get=get)}
    assert not checks["brokers"].ok and "local-pub" in checks["brokers"].detail
    assert not checks["gdc"].ok and "HTTP 503" in checks["gdc"].detail
    assert not checks["pages"].ok and "ConnectionError" in checks["pages"].detail
    assert "3 check(s) failing" in health.to_markdown(list(checks.values()))


def test_missing_password_and_bad_responses(hcfg):
    get = http({"wis2-discovery-metadata": Resp(200, None), "catalogue.json": Resp(200, {"other": 1})})
    checks = {c.name: c for c in health.run_checks(hcfg, None, PAGES, factory=FakeWIS2().factory, get=get)}
    assert "LOCAL_BROKER_PUB_PASSWORD" in checks["brokers"].detail
    assert "not JSON" in checks["gdc"].detail
    assert "no 'records'" in checks["pages"].detail


def test_optional_checks_skipped_when_unconfigured(hcfg):
    hcfg["global_discovery_catalogue"]["url"] = ""
    checks = health.run_checks(hcfg, "pw", None, factory=FakeWIS2().factory, get=http({}))
    assert [c.name for c in checks] == ["brokers"]
