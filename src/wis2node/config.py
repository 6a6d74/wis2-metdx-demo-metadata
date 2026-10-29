"""Node configuration: config/node.yml, overridden by WIS2_* environment variables.

Every leaf key in the YAML file can be overridden by an environment variable named
WIS2_ followed by its path in upper case, joined with underscores. For example,
local_broker.url becomes WIS2_LOCAL_BROKER_URL and timeouts.delivery becomes
WIS2_TIMEOUTS_DELIVERY. Overrides take the type of the value they replace.

Secrets are never read from the file: see `publisher_password()`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path("config/node.yml")
ENV_PREFIX = "WIS2_"
PUBLISHER_PASSWORD_ENV = "LOCAL_BROKER_PUB_PASSWORD"

REQUIRED = [
    ("centre_id",),
    ("local_broker", "url"),
    ("local_broker", "subscribe", "username"),
    ("local_broker", "subscribe", "password"),
    ("local_broker", "publish", "username"),
    ("global_broker", "url"),
    ("global_broker", "subscribe", "username"),
    ("global_broker", "subscribe", "password"),
    ("timeouts", "broker_connect"),
    ("timeouts", "pages_live"),
    ("timeouts", "delivery"),
    ("timeouts", "monitor"),
]


class ConfigError(Exception):
    """The configuration is missing, malformed or incomplete."""


def _coerce(raw: str, current: Any, name: str) -> Any:
    if raw.lower() in ("null", "none", ""):
        return None
    try:
        if isinstance(current, bool):
            if raw.lower() not in ("true", "false"):
                raise ValueError
            return raw.lower() == "true"
        if isinstance(current, int):
            return int(raw)
        if isinstance(current, float) or current is None:
            # None-valued keys are numeric thresholds; fall back to string.
            try:
                return float(raw)
            except ValueError:
                return raw
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a valid {type(current).__name__}") from None
    return raw


def _apply_env(node: dict, env: dict[str, str], path: tuple[str, ...] = ()) -> None:
    for key, value in node.items():
        here = path + (key,)
        if isinstance(value, dict):
            _apply_env(value, env, here)
            continue
        name = ENV_PREFIX + "_".join(here).upper()
        if name in env:
            node[key] = _coerce(env[name], value, name)


def load(path: Path | str = DEFAULT_PATH, env: dict[str, str] | None = None) -> dict:
    """Load the config file, apply environment overrides and check required keys."""
    path = Path(path)
    try:
        cfg = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {path} is not valid YAML: {exc}") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"config file {path} must contain a mapping")

    _apply_env(cfg, dict(os.environ) if env is None else env)

    missing = [".".join(keys) for keys in REQUIRED if get(cfg, *keys) in (None, "")]
    if missing:
        raise ConfigError(f"missing required config: {', '.join(missing)}")
    return cfg


def get(cfg: dict, *keys: str, default: Any = None) -> Any:
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def publisher_password(env: dict[str, str] | None = None) -> str:
    """The local broker publisher password, from the environment only."""
    value = (dict(os.environ) if env is None else env).get(PUBLISHER_PASSWORD_ENV)
    if not value:
        raise ConfigError(f"{PUBLISHER_PASSWORD_ENV} is not set")
    return value
