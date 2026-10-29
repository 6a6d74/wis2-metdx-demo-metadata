import pytest

from wis2node.mqtt import BrokerClient, BrokerError, parse_url


def test_parse_url():
    assert parse_url("mqtts://h.example:8883") == ("h.example", 8883, True)
    assert parse_url("mqtt://h.example") == ("h.example", 1883, False)
    with pytest.raises(BrokerError) as e:
        parse_url("https://secret-host.example")
    assert "secret-host" not in str(e.value)


def test_connection_error_uses_label_not_host():
    c = BrokerClient("local-pub", "mqtts://secret-host.invalid:8883", "u", "p", "test", label="local broker")
    with pytest.raises(BrokerError) as e:
        c.connect(2)
    assert "local broker" in str(e.value) and "secret-host" not in str(e.value)
