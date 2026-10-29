from pathlib import Path

ROOT = Path(__file__).parent.parent
import pytest

from wis2node import config


def write(tmp_path, text):
    f = tmp_path / "node.yml"
    f.write_text(text)
    return f


def test_repo_config_loads(cfg):
    assert cfg["centre_id"] == "io-wis2dev-metdx-demo"
    assert cfg["local_broker"]["publish"]["username"] == "sgfit"
    assert cfg["timeouts"]["delivery"] == 60


def test_env_overrides_with_types():
    env = {
        "WIS2_CENTRE_ID": "xx-other",
        "WIS2_LOCAL_BROKER_URL": "mqtts://example.org:8883",
        "WIS2_TIMEOUTS_DELIVERY": "90",
        "WIS2_KPI_WARN_BELOW_PERCENTAGE": "75",
        "WIS2_VALIDATION_RELAX_CENTRE_ID_CHECKS": "false",
    }
    c = config.load(ROOT / "config" / "node.yml", env=env)
    assert c["centre_id"] == "xx-other"
    assert c["local_broker"]["url"] == "mqtts://example.org:8883"
    assert c["timeouts"]["delivery"] == 90
    assert c["kpi"]["warn_below_percentage"] == 75.0
    assert c["validation"]["relax_centre_id_checks"] is False


def test_bad_int_override_rejected():
    with pytest.raises(config.ConfigError, match="WIS2_TIMEOUTS_DELIVERY"):
        config.load(ROOT / "config" / "node.yml", env={"WIS2_TIMEOUTS_DELIVERY": "soon"})


def test_missing_required_keys(tmp_path):
    f = write(tmp_path, "centre_id: x\nlocal_broker:\n  url: mqtts://a\n")
    with pytest.raises(config.ConfigError, match="global_broker.url"):
        config.load(f, env={})


def test_missing_and_invalid_files(tmp_path):
    with pytest.raises(config.ConfigError, match="not found"):
        config.load(tmp_path / "nope.yml", env={})
    with pytest.raises(config.ConfigError, match="not valid YAML"):
        config.load(write(tmp_path, "a: [unclosed"), env={})


def test_publisher_password_only_from_env():
    assert config.publisher_password({"LOCAL_BROKER_PUB_PASSWORD": "s3cret"}) == "s3cret"
    with pytest.raises(config.ConfigError):
        config.publisher_password({})


def test_config_loads_without_local_broker_url():
    # validation runs on PRs without secrets, so the URL must not be required at load time
    c = config.load(ROOT / "config" / "node.yml", env={})
    assert c["local_broker"]["url"] == ""


def test_local_broker_url_resolution():
    cfg = {"local_broker": {"url": ""}}
    assert config.local_broker_url(cfg, {"LOCAL_BROKER_URL": "mqtts://a:8883"}) == "mqtts://a:8883"
    assert config.local_broker_url({"local_broker": {"url": "mqtts://b:8883"}}, {}) == "mqtts://b:8883"
    with pytest.raises(config.ConfigError, match="LOCAL_BROKER_URL"):
        config.local_broker_url(cfg, {})
