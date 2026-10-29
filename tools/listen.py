#!/usr/bin/env python3
"""Listen on a broker and log every message for this centre (JSON Lines).

Usage: python tools/listen.py [--broker gb|local] [--minutes N] [--out FILE]
Subscribes to origin/a/wis2/<centre>/# and, on the GB, monitor/a/wis2/<centre>/#.
"""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import certifi
import paho.mqtt.client as mqtt

from wis2node import config
from wis2node.mqtt import parse_url

p = argparse.ArgumentParser()
p.add_argument("--broker", choices=["gb", "local"], default="gb")
p.add_argument("--minutes", type=float, default=30)
p.add_argument("--out", default=None)
args = p.parse_args()

cfg = config.load()
centre = cfg["centre_id"]
b = cfg["global_broker" if args.broker == "gb" else "local_broker"]
host, port, tls = parse_url(b["url"])
topics = [f"origin/a/wis2/{centre}/#"] + ([f"monitor/a/wis2/{centre}/#"] if args.broker == "gb" else [])
out = Path(args.out or f"logs/listen-{args.broker}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl")
out.parent.mkdir(parents=True, exist_ok=True)
fh = out.open("a")


def on_connect(c, u, f, rc, props):
    print(f"connected to {host}: {rc}; subscribing {topics}", flush=True)
    c.subscribe([(t, 1) for t in topics])


def on_message(c, u, m):
    try:
        body = json.loads(m.payload)
    except ValueError:
        body = m.payload.decode("utf-8", "replace")
    rec = {"received": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "topic": m.topic,
           "retain": bool(m.retain), "payload": body}
    fh.write(json.dumps(rec) + "\n")
    fh.flush()
    kind = body.get("type", "WNM") if isinstance(body, dict) else "?"
    print(f"{rec['received']} {m.topic} {kind}", flush=True)


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{centre}-listen-{uuid.uuid4().hex[:6]}",
                protocol=mqtt.MQTTv5)
c.username_pw_set(b["subscribe"]["username"], b["subscribe"]["password"])
if tls:
    c.tls_set(ca_certs=certifi.where())
c.on_connect, c.on_message = on_connect, on_message
c.connect(host, port, 60)
c.loop_start()
print(f"logging to {out} for {args.minutes} min", flush=True)
time.sleep(args.minutes * 60)
c.disconnect()
c.loop_stop()
fh.close()
