#!/usr/bin/env python3
"""Check the publisher account on the local broker.

Usernames, the GB URL and the centre-id come from config/node.yml (run from the
repo root, with the package installed: pip install -e .). The local broker URL is
not in the repo: set LOCAL_BROKER_URL in the environment.

The password is read, in order of preference, from:
  1. the WIS2_LOCAL_BROKER_PUB_PASSWORD environment variable
  2. the macOS keychain item "wis2-metdx-demo-pub" (account = publisher username)
  3. an interactive prompt (not echoed)
It is never printed.

Default: connect as the publisher and report the CONNACK result.
--publish: also subscribe (as everyone) on the local broker and the GB,
publish one clearly-marked test message to origin/a/wis2/<centre>/metadata,
and report the PUBACK reason code, whether both brokers delivered it, and any
monitor events the GB raises about it. The message is deliberately not a valid
WNM, so in the relaxed test environment the GB is expected to forward it and
also publish a validation error event.
"""
import argparse
import getpass
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import certifi
import paho.mqtt.client as mqtt

from wis2node import config

CFG = config.load()
CENTRE = CFG["centre_id"]
LOCAL = config.local_broker_url(CFG)  # LOCAL_BROKER_URL env var (kept as a secret)
GB = CFG["global_broker"]["url"]
PUB_USER = CFG["local_broker"]["publish"]["username"]
LOCAL_SUB = CFG["local_broker"]["subscribe"]
GB_SUB = CFG["global_broker"]["subscribe"]
KEYCHAIN_SERVICE = "wis2-metdx-demo-pub"


def password() -> str:
    if pw := os.environ.get("WIS2_LOCAL_BROKER_PUB_PASSWORD"):
        print("password: from environment")
        return pw
    try:
        pw = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", PUB_USER, "-w"],
            check=True, capture_output=True, text=True,
        ).stdout.rstrip("\n")
        print("password: from keychain")
        return pw
    except (subprocess.CalledProcessError, FileNotFoundError):
        return getpass.getpass(f"{PUB_USER} password: ")


def client(url: str, user: str, pw: str, name: str, on_message=None):
    u = urlparse(url)
    state = {"connack": None, "subacks": [], "pubacks": {}}
    connected = threading.Event()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"metdx-check-{name}-{uuid.uuid4().hex[:6]}", protocol=mqtt.MQTTv5)
    c.username_pw_set(user, pw)
    c.tls_set(ca_certs=certifi.where())

    def on_connect(cl, ud, flags, rc, props):
        state["connack"] = rc
        connected.set()

    def on_publish(cl, ud, mid, rc, props):
        state["pubacks"][mid] = rc

    def on_subscribe(cl, ud, mid, rcs, props):
        state["subacks"].extend(str(r) for r in rcs)

    c.on_connect, c.on_publish, c.on_subscribe = on_connect, on_publish, on_subscribe
    if on_message:
        c.on_message = on_message
    c.connect(u.hostname, u.port or 8883, keepalive=30)
    c.loop_start()
    if not connected.wait(15):
        raise SystemExit(f"[{name}] no CONNACK within 15 s")
    print(f"[{name}] CONNACK as {user!r}: {state['connack']}")
    return c, state


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--publish", action="store_true", help="also run the end-to-end publish test")
    p.add_argument("--wait", type=int, default=30, help="seconds to wait for delivery/monitor events")
    p.add_argument("--log", default=None,
                   help="JSONL file for the full monitor events (default: logs/check_publisher-<UTC time>.jsonl)")
    args = p.parse_args()

    pub, pub_state = client(LOCAL, PUB_USER, password(), "publisher")
    if pub_state["connack"].is_failure:
        pub.loop_stop()
        return 1
    if not args.publish:
        pub.disconnect()
        pub.loop_stop()
        print("publisher connection OK")
        return 0

    msg_id = str(uuid.uuid4())
    seen = {"local": False, "gb": False, "monitor": []}

    def watcher(key):
        def on_message(cl, ud, m):
            try:
                body = json.loads(m.payload)
            except ValueError:
                return
            if m.topic.startswith("monitor/"):
                if msg_id in m.payload.decode("utf-8", "replace"):
                    seen["monitor"].append({"topic": m.topic, "event": body})
            elif body.get("id") == msg_id:
                seen[key] = True
        return on_message

    topic = f"origin/a/wis2/{CENTRE}/metadata"
    local, ls = client(LOCAL, LOCAL_SUB["username"], LOCAL_SUB["password"], "local-sub", watcher("local"))
    gb, gs = client(GB, GB_SUB["username"], GB_SUB["password"], "gb-sub", watcher("gb"))
    local.subscribe(topic, qos=1)
    gb.subscribe([(topic, 1), (f"monitor/a/wis2/{CENTRE}/#", 1)])
    time.sleep(3)
    print(f"[local-sub] SUBACK: {ls['subacks']}\n[gb-sub] SUBACK: {gs['subacks']}")

    payload = {"id": msg_id, "test": "metdx-demo publisher ACL check - not a WNM, please ignore",
               "pubtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    print(f"[publisher] message on {topic}:\n{json.dumps(payload, indent=2)}")
    info = pub.publish(topic, json.dumps(payload), qos=1)
    info.wait_for_publish(15)
    time.sleep(1)
    print(f"[publisher] PUBACK on {topic}: {pub_state['pubacks'].get(info.mid, 'none')}")

    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline and not (seen["local"] and seen["gb"] and seen["monitor"]):
        time.sleep(0.5)
    print(f"delivered via local broker: {seen['local']}")
    print(f"delivered via Global Broker: {seen['gb']}")
    print(f"GB monitor events about this message: {len(seen['monitor'])}")
    log = Path(args.log or f"logs/check_publisher-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as fh:
        for ev in seen["monitor"]:
            fh.write(json.dumps(ev) + "\n")
    print(f"monitor events written to {log}")
    for ev in seen["monitor"]:
        print(f"--- {ev['topic']}\n{json.dumps(ev['event'], indent=2)}")

    for c in (pub, local, gb):
        c.disconnect()
        c.loop_stop()
    return 0 if seen["local"] and seen["gb"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
