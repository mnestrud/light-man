#!/usr/bin/env python3
"""Local MQTT helper for Light Man development.

Everything it can emit is validated against the device's own Zigbee2MQTT converter
data (resolved exposes + options, read live from bridge/devices) — no hand-written
command lists, no guessing. Two layers:

  * state commands  -> definition.exposes (settable) -> zigbee2mqtt/<name>/set
  * config options  -> definition.options (ea.SET, write-only, e.g. hue_native_control,
                       transition) -> zigbee2mqtt/bridge/request/{device,group}/options

Reads are read-only. Writes default to a DRY RUN; pass --commit to publish.

Modes:
  bridge                          Z2M group membership + device count
  tap   [--seconds N]             live switch /action capture
  sub   <topic> [--seconds N]     subscribe to any topic (state is the retained
                                  zigbee2mqtt/<name> payload)
  caps  <device>                  canonical settable exposes + options for its model
  set   <device> <json> [--commit]      validated state /set
  options <device|group> [<json>] [--commit]   read/set options (hue_native_control)

Credentials come from HA's MQTT config entry at runtime (override with
--host/--port/--user/--password). The in-container broker host (core-mosquitto) is
rewritten to the HA host (--external-host, default 'botworth').
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

DEFAULT_HA_CONFIG = "//botworth/config"
DEFAULT_EXTERNAL_HOST = "botworth"
INTERNAL_HOSTS = {"core-mosquitto", "localhost", "127.0.0.1", "homeassistant"}

ACCESS_SET = 0b010  # Z2M access bitmask: 1=published, 2=set, 4=get
# Universal light /set conveniences (not individual exposes leaves):
UNIVERSAL_SET = {"transition"}
COLOR_LEAVES = {"x", "y", "hue", "saturation"}


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
    return (
        host,
        port,
        args.user or cfg.get("username"),
        args.password or cfg.get("password"),
    )


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


def fetch_retained(
    client: mqtt.Client, topics: list[str], seconds: float
) -> dict[str, Any]:
    """Subscribe to topics and collect their retained payloads for `seconds`."""
    seen: dict[str, Any] = {}

    def on_message(_c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            seen[msg.topic] = json.loads(msg.payload)
        except json.JSONDecodeError:
            seen[msg.topic] = msg.payload.decode("utf-8", "replace")

    client.on_message = on_message
    client.subscribe([(t, 0) for t in topics])
    client.loop_start()
    time.sleep(seconds)
    client.loop_stop()
    return seen


def bridge_request(
    client: mqtt.Client, endpoint: str, payload: dict[str, Any], seconds: float = 6.0
) -> dict[str, Any] | None:
    """Publish a bridge/request and return the matching bridge/response (by txn)."""
    txn = f"lm-{int(time.time() * 1000)}"
    result: dict[str, Any] = {}

    def on_message(_c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            data = json.loads(msg.payload)
        except json.JSONDecodeError:
            return
        if data.get("transaction") == txn:
            result["resp"] = data

    client.on_message = on_message
    client.subscribe(f"zigbee2mqtt/bridge/response/{endpoint}")
    client.loop_start()
    client.publish(
        f"zigbee2mqtt/bridge/request/{endpoint}",
        json.dumps({**payload, "transaction": txn}),
    )
    deadline = time.time() + seconds
    while time.time() < deadline and "resp" not in result:
        time.sleep(0.1)
    client.loop_stop()
    return result.get("resp")


def flatten(items: list[dict[str, Any]] | None, out: dict[str, Any]) -> dict[str, Any]:
    """Flatten an exposes/options tree into {property: feature} for settable leaves."""
    for ex in items or []:
        if "features" in ex:
            flatten(ex["features"], out)
        elif ex.get("access", ACCESS_SET) & ACCESS_SET and "property" in ex:
            out[ex["property"]] = ex
    return out


def describe(feat: dict[str, Any]) -> str:
    """One-line description of a settable feature's type and constraints."""
    kind = feat.get("type")
    if kind == "numeric":
        unit = feat.get("unit", "")
        return (
            f"numeric [{feat.get('value_min')}..{feat.get('value_max')} {unit}]".strip()
        )
    if kind == "enum":
        return f"enum {feat.get('values')}"
    if kind == "binary":
        return f"binary on={feat.get('value_on')!r} off={feat.get('value_off')!r}"
    return str(kind)


def find_named(items: list[dict[str, Any]], friendly: str) -> dict[str, Any] | None:
    """Find a device/group entry by exact friendly_name."""
    for item in items:
        if item.get("friendly_name") == friendly:
            return item
    return None


def value_error(key: str, feat: dict[str, Any], val: Any) -> str:
    """Return an error if val violates the feature's constraints, else ''."""
    kind = feat.get("type")
    if kind == "numeric":
        if not isinstance(val, (int, float)):
            return f"{key}: expected a number"
        lo, hi = feat.get("value_min"), feat.get("value_max")
        if lo is not None and val < lo:
            return f"{key}: {val} < min {lo}"
        if hi is not None and val > hi:
            return f"{key}: {val} > max {hi}"
    elif kind == "enum" and val not in feat.get("values", []):
        return f"{key}: {val!r} not in {feat.get('values')}"
    elif kind == "binary":
        allowed = {feat.get("value_on"), feat.get("value_off"), "TOGGLE"}
        if val not in allowed:
            return f"{key}: {val!r} not in {sorted(str(a) for a in allowed)}"
    return ""


def validate(
    payload: dict[str, Any], feats: dict[str, Any], color_ok: bool
) -> list[str]:
    """Return errors for keys/values not canonically allowed by feats."""
    errors: list[str] = []
    for key, val in payload.items():
        if key in UNIVERSAL_SET:
            if not isinstance(val, (int, float)):
                errors.append(f"{key}: expected a number")
        elif key == "color":
            if not color_ok:
                errors.append("color: device exposes no settable color")
        elif key in feats:
            msg = value_error(key, feats[key], val)
            if msg:
                errors.append(msg)
        else:
            errors.append(f"{key}: not settable on this device")
    return errors


def get_device(client: mqtt.Client, friendly: str, seconds: float) -> dict[str, Any]:
    """Fetch a device entry (with its definition) from bridge/devices."""
    seen = fetch_retained(client, ["zigbee2mqtt/bridge/devices"], seconds)
    dev = find_named(seen.get("zigbee2mqtt/bridge/devices", []), friendly)
    if dev is None:
        raise SystemExit(f"No device named {friendly!r} in bridge/devices.")
    return dev


def run_bridge(client: mqtt.Client, seconds: float) -> None:
    """Print Z2M group membership + device count."""
    seen = fetch_retained(
        client, ["zigbee2mqtt/bridge/groups", "zigbee2mqtt/bridge/devices"], seconds
    )
    groups = seen.get("zigbee2mqtt/bridge/groups", [])
    print(f"\n=== groups ({len(groups)}) ===")
    for group in groups:
        members = group.get("members", [])
        print(
            f"[{group.get('id')}] {group.get('friendly_name')} ({len(members)} members)"
        )
        for member in members:
            print(f"    {member.get('ieee_address')}  ep{member.get('endpoint')}")
    print(f"\n=== devices: {len(seen.get('zigbee2mqtt/bridge/devices', []))} ===")


def run_capture(client: mqtt.Client, topics: list[str], seconds: float | None) -> None:
    """Print live messages on the given topic filters until time/interrupt."""

    def on_message(_c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        print(f"{time.strftime('%H:%M:%S')}  {msg.topic}  {msg.payload.decode()}")

    client.on_message = on_message
    client.subscribe([(t, 0) for t in topics])
    print(f"Subscribed: {', '.join(topics)}  (Ctrl-C to stop)")
    client.loop_start()
    try:
        time.sleep(seconds) if seconds else [time.sleep(1) for _ in iter(int, 1)]
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()


def run_caps(client: mqtt.Client, friendly: str, seconds: float) -> None:
    """Print the canonical settable exposes + options for a device's model."""
    defn = get_device(client, friendly, seconds).get("definition") or {}
    exposes = flatten(defn.get("exposes"), {})
    options = flatten(defn.get("options"), {})
    print(f"\n{defn.get('vendor')} {defn.get('model')} — {defn.get('description')}")
    print("\nstate /set keys (zigbee2mqtt/<name>/set):")
    for prop in sorted(exposes):
        print(f"    {prop}: {describe(exposes[prop])}")
    if any(p in exposes for p in COLOR_LEAVES):
        print("    color: composite {x,y}|{hue,saturation}|{r,g,b}|{hex}")
    print("    transition: numeric seconds (universal)")
    print("\noptions (bridge/request/device|group/options):")
    for prop in sorted(options):
        print(f"    {prop}: {describe(options[prop])}")


def member_exposes(
    grp: dict[str, Any], devices: list[dict[str, Any]]
) -> dict[str, Any]:
    """Union of settable exposes across a group's member devices."""
    by_ieee = {d.get("ieee_address"): d for d in devices}
    feats: dict[str, Any] = {}
    for member in grp.get("members", []):
        dev = by_ieee.get(member.get("ieee_address"))
        if dev:
            flatten((dev.get("definition") or {}).get("exposes"), feats)
    return feats


def resolve_target(
    client: mqtt.Client, target: str, seconds: float
) -> tuple[dict[str, Any], str]:
    """Return (settable exposes, label) for a /set target (device OR group)."""
    seen = fetch_retained(
        client, ["zigbee2mqtt/bridge/devices", "zigbee2mqtt/bridge/groups"], seconds
    )
    devices = seen.get("zigbee2mqtt/bridge/devices", [])
    dev = find_named(devices, target)
    if dev:
        defn = dev.get("definition") or {}
        model = f"{defn.get('vendor')} {defn.get('model')}"
        return flatten(defn.get("exposes"), {}), model
    grp = find_named(seen.get("zigbee2mqtt/bridge/groups", []), target)
    if grp:
        n = len(grp.get("members", []))
        return member_exposes(grp, devices), f"group of {n} (member-union)"
    raise SystemExit(f"No device or group named {target!r}.")


def apply_and_read(
    client: mqtt.Client, state_topic: str, body: str, seconds: float
) -> dict[str, Any] | None:
    """Publish a /set and capture the state Z2M republishes after applying."""
    captured: dict[str, Any] = {}

    def on_message(_c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        if msg.topic == state_topic:
            with contextlib.suppress(json.JSONDecodeError):
                captured["state"] = json.loads(msg.payload)

    client.on_message = on_message
    client.subscribe(state_topic)
    client.loop_start()
    client.publish(f"{state_topic}/set", body, qos=0)
    deadline = time.time() + seconds
    while time.time() < deadline and "state" not in captured:
        time.sleep(0.1)
    client.loop_stop()
    return captured.get("state")


def report_applied(payload: dict[str, Any], state: dict[str, Any] | None) -> None:
    """Interpret the device's reported result against what was requested."""
    if state is None:
        print("  (no state echo within timeout — target off or non-reporting)")
        return
    for key, want in payload.items():
        if key == "transition":
            continue
        got = state.get(key)
        flag = "" if got == want else "  <-- differs"
        print(f"  {key}: requested {want!r} -> reported {got!r}{flag}")


def run_set(
    client: mqtt.Client, target: str, payload_json: str, commit: bool, seconds: float
) -> None:
    """Validate a state /set against the target's exposes, publish, read back."""
    payload = json.loads(payload_json)
    feats, label = resolve_target(client, target, seconds)
    color_ok = any(p in feats for p in COLOR_LEAVES)
    errors = validate(payload, feats, color_ok)
    if errors:
        print(f"REJECTED for {target} ({label}):")
        for err in errors:
            print(f"  - {err}")
        keys = sorted(set(feats) | UNIVERSAL_SET | ({"color"} if color_ok else set()))
        print("Settable state keys:", ", ".join(keys))
        raise SystemExit(2)
    state_topic, body = f"zigbee2mqtt/{target}", json.dumps(payload)
    if not commit:
        print(f"DRY RUN ({label}). Would publish:\n  {state_topic}/set  {body}")
        print("Re-run with --commit to publish.")
        return
    state = apply_and_read(client, state_topic, body, seconds)
    print(f"PUBLISHED  {state_topic}/set  {body}")
    report_applied(payload, state)


def run_options(
    client: mqtt.Client,
    target: str,
    payload_json: str | None,
    commit: bool,
    ha_config: str,
    seconds: float,
) -> None:
    """Read (no JSON) or set (JSON) config options on a device or group."""
    seen = fetch_retained(
        client, ["zigbee2mqtt/bridge/devices", "zigbee2mqtt/bridge/groups"], seconds
    )
    dev = find_named(seen.get("zigbee2mqtt/bridge/devices", []), target)
    grp = find_named(seen.get("zigbee2mqtt/bridge/groups", []), target)
    if dev is None and grp is None:
        raise SystemExit(f"{target!r} is neither a device nor a group.")
    kind = "device" if dev else "group"
    if (
        payload_json is None
    ):  # READ — options are write-only, so read from config at rest
        opts = read_config_options(ha_config, kind, target)
        print(f"{kind} {target!r} options (configuration.yaml): {json.dumps(opts)}")
        if dev:
            avail = flatten((dev.get("definition") or {}).get("options"), {})
            print("settable options:", ", ".join(sorted(avail)) or "(none)")
        return
    options = json.loads(payload_json)
    endpoint = f"{kind}/options"
    req = {"id": target, "options": options}
    if not commit:
        print(f"DRY RUN. Would request bridge/request/{endpoint}: {json.dumps(req)}")
        print("Z2M validates option keys; re-run with --commit to apply.")
        return
    resp = bridge_request(client, endpoint, req)
    if resp is None:
        raise SystemExit("No bridge response within timeout (is Z2M running?).")
    if resp.get("status") != "ok":
        raise SystemExit(f"Z2M ERROR: {resp.get('error')}")
    data = resp.get("data", {})
    print(f"OK: {target} options {data.get('from')} -> {data.get('to')}")
    if data.get("restart_required"):
        print("  NOTE: restart_required=true — restart Z2M for full effect.")


def read_config_options(ha_config: str, kind: str, target: str) -> dict[str, Any]:
    """Read a group/device's stored options from Z2M configuration.yaml."""
    import yaml

    path = Path(ha_config) / "zigbee2mqtt" / "configuration.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = data.get("groups" if kind == "group" else "devices", {}) or {}
    for entry in section.values() if isinstance(section, dict) else []:
        if entry.get("friendly_name") == target:
            return {
                k: v for k, v in entry.items() if k not in ("devices", "friendly_name")
            }
    return {}


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description="Light Man MQTT helper (validated).")
    parser.add_argument("--ha-config", default=DEFAULT_HA_CONFIG)
    parser.add_argument("--external-host", default=DEFAULT_EXTERNAL_HOST)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("bridge").add_argument("--seconds", type=float, default=4.0)
    tap = sub.add_parser("tap")
    tap.add_argument("--seconds", type=float, default=None)
    generic = sub.add_parser("sub")
    generic.add_argument("topic")
    generic.add_argument("--seconds", type=float, default=None)
    caps = sub.add_parser("caps")
    caps.add_argument("device")
    caps.add_argument("--seconds", type=float, default=4.0)
    setter = sub.add_parser("set")
    setter.add_argument("device")
    setter.add_argument("payload")
    setter.add_argument("--commit", action="store_true")
    setter.add_argument("--seconds", type=float, default=4.0)
    opt = sub.add_parser("options")
    opt.add_argument("target")
    opt.add_argument("payload", nargs="?", default=None)
    opt.add_argument("--commit", action="store_true")
    opt.add_argument("--seconds", type=float, default=4.0)
    return parser


def main() -> None:
    """Resolve the connection, connect, and run the requested mode."""
    # Windows consoles default to cp1252 and crash on non-latin payload bytes
    # (e.g. U+2212 in Z2M device descriptions); force UTF-8 output.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    host, port, user, password = resolve_connection(args)
    print(f"Connecting to {host}:{port} as {user!r} ...")
    client = make_client(user, password)
    client.connect(host, port, keepalive=30)
    if args.mode == "bridge":
        run_bridge(client, args.seconds)
    elif args.mode == "tap":
        run_capture(client, ["zigbee2mqtt/+/action"], args.seconds)
    elif args.mode == "sub":
        run_capture(client, [args.topic], args.seconds)
    elif args.mode == "caps":
        run_caps(client, args.device, args.seconds)
    elif args.mode == "set":
        run_set(client, args.device, args.payload, args.commit, args.seconds)
    else:
        run_options(
            client, args.target, args.payload, args.commit, args.ha_config, args.seconds
        )
    client.disconnect()


if __name__ == "__main__":
    main()
