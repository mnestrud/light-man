#!/usr/bin/env python3
"""Local MQTT inspection helper for Light Man development (read-only).

Subscribes to Zigbee2MQTT bridge topics or captures live switch action/state
messages, to verify the Phase-1 pre-checks (group membership, action payloads,
switch-state shape) without publishing anything.

Broker credentials are pulled from Home Assistant's own MQTT config entry at
runtime (nothing is committed); override with --host/--port/--user/--password.
HA stores the broker as its in-container hostname (e.g. core-mosquitto); from this
machine we connect to the HA host instead (--external-host, default 'botworth').

Run from the venv (Git Bash), e.g.:
  python scripts/mqtt_dump.py bridge
  python scripts/mqtt_dump.py tap --seconds 30
  python scripts/mqtt_dump.py sub 'zigbee2mqtt/<switch>' --seconds 15
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

DEFAULT_HA_CONFIG = "//botworth/config"
DEFAULT_EXTERNAL_HOST = "botworth"
INTERNAL_HOSTS = {"core-mosquitto", "localhost", "127.0.0.1", "homeassistant"}


def load_ha_mqtt_config(ha_config: str) -> dict[str, Any]:
    """Return broker/port/username/password from HA's MQTT config entry."""
    path = Path(ha_config) / ".storage" / "core.config_entries"
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data["data"]["entries"]:
        if entry.get("domain") == "mqtt":
            return {**(entry.get("data") or {}), **(entry.get("options") or {})}
    raise SystemExit("No MQTT config entry found in HA storage.")


def resolve_connection(
    args: argparse.Namespace,
) -> tuple[str, int, str | None, str | None]:
    """Resolve (host, port, user, password), falling back to HA config."""
    cfg: dict[str, Any] = {}
    if not (args.host and args.user and args.password):
        cfg = load_ha_mqtt_config(args.ha_config)
    host = args.host or cfg.get("broker") or args.external_host
    if host in INTERNAL_HOSTS:
        host = args.external_host
    port = args.port or int(cfg.get("port", 1883))
    user = args.user or cfg.get("username")
    password = args.password or cfg.get("password")
    return host, port, user, password


def make_client(user: str | None, password: str | None) -> mqtt.Client:
    """Build a paho v2 client with credentials and a connack reporter."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if user:
        client.username_pw_set(user, password)

    def on_connect(
        _c: mqtt.Client, _u: Any, _flags: Any, reason_code: Any, _props: Any
    ) -> None:
        print(f"  connack: {reason_code}")

    client.on_connect = on_connect
    return client


def run_bridge(client: mqtt.Client, seconds: float) -> None:
    """Print Z2M group membership + device/bridge counts from retained topics."""
    seen: dict[str, Any] = {}

    def on_message(_c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            seen[msg.topic] = json.loads(msg.payload)
        except json.JSONDecodeError:
            seen[msg.topic] = msg.payload.decode("utf-8", "replace")

    client.on_message = on_message
    client.subscribe(
        [
            ("zigbee2mqtt/bridge/groups", 0),
            ("zigbee2mqtt/bridge/devices", 0),
            ("zigbee2mqtt/bridge/info", 0),
        ]
    )
    client.loop_start()
    time.sleep(seconds)
    client.loop_stop()

    groups = seen.get("zigbee2mqtt/bridge/groups", [])
    print(f"\n=== Zigbee2MQTT groups ({len(groups)}) ===")
    for group in groups:
        members = group.get("members", [])
        name = group.get("friendly_name")
        print(f"[{group.get('id')}] {name} ({len(members)} members)")
        for member in members:
            print(f"    {member.get('ieee_address')}  ep{member.get('endpoint')}")
    print(f"\n=== devices: {len(seen.get('zigbee2mqtt/bridge/devices', []))} ===")


def run_capture(client: mqtt.Client, topics: list[str], seconds: float | None) -> None:
    """Print live messages on the given topic filters until time/interrupt."""

    def on_message(_c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        stamp = time.strftime("%H:%M:%S")
        print(f"{stamp}  {msg.topic}  {msg.payload.decode('utf-8', 'replace')}")

    client.on_message = on_message
    client.subscribe([(t, 0) for t in topics])
    print(f"Subscribed: {', '.join(topics)}  (Ctrl-C to stop)")
    client.loop_start()
    try:
        if seconds:
            time.sleep(seconds)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        description="Light Man MQTT inspection helper (read-only)."
    )
    parser.add_argument("--ha-config", default=DEFAULT_HA_CONFIG)
    parser.add_argument("--external-host", default=DEFAULT_EXTERNAL_HOST)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    sub = parser.add_subparsers(dest="mode", required=True)
    bridge = sub.add_parser("bridge", help="Dump Z2M group membership + counts.")
    bridge.add_argument("--seconds", type=float, default=4.0)
    tap = sub.add_parser("tap", help="Capture live switch action messages.")
    tap.add_argument("--seconds", type=float, default=None)
    generic = sub.add_parser("sub", help="Subscribe to an arbitrary topic filter.")
    generic.add_argument("topic")
    generic.add_argument("--seconds", type=float, default=None)
    return parser


def main() -> None:
    """Resolve the connection, connect, and run the requested mode."""
    args = build_parser().parse_args()
    host, port, user, password = resolve_connection(args)
    print(f"Connecting to {host}:{port} as {user!r} ...")
    client = make_client(user, password)
    client.connect(host, port, keepalive=30)
    if args.mode == "bridge":
        run_bridge(client, args.seconds)
    elif args.mode == "tap":
        run_capture(client, ["zigbee2mqtt/+/action"], args.seconds)
    else:
        run_capture(client, [args.topic], args.seconds)
    client.disconnect()


if __name__ == "__main__":
    main()
