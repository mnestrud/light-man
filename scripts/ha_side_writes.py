#!/usr/bin/env python3
"""List what changes lights and switches from the HA side, outside Light Man.

Light Man is meant to be the only thing that sets the lights. It publishes MQTT
itself and never calls an HA light/switch service, so anything HA does to a light
or a switch shows up in one of two places:

1. **HA logbook** — a light/switch state change that carries a context: the
   automation, script, scene or user that caused it. A change with no context came
   from the device itself or from MQTT (Light Man, a paddle, a binding).
2. **MQTT capture** (``live_probe.py capture``, optional) — every ``/set`` message.
   MQTT does not say who published, so a message is called "not Light Man" from
   its shape: Light Man's light sends always carry ``transition`` with a
   brightness (or are a zone-clear OFF), its switch writes are a bare ``state`` to
   a sync switch, and its bar commands go to the switch by name (the HA LED
   script addresses the switch by IEEE address). This also catches a service
   call that changed no state (e.g. turning on a light that is on).

Usage (from the repo root, with the venv):
    scripts/ha_side_writes.py --since 2026-11-04T10:05 [--until ...] [--capture FILE]

Times are local. Reads only: the HA REST API (token in ``~/.ha-token``), the
stored config on the share, and the capture file.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HOST = os.environ.get("HA_WS_HOST", "meddle.home.botworth.net")
TOKEN_FILE = Path(os.environ.get("HA_TOKEN_FILE", "~/.ha-token")).expanduser()
STORE = Path("/mnt/ha-config/.storage/light_man_config")
Z2M = "zigbee2mqtt/"
BAR_KEYS = {"ledIntensityWhenOn", "led_effect", "individual_led_effect"}
DOMAINS = ("light", "switch")
OWN_ENTITIES = ("switch.light_man_", "switch.tech_closet_light_man_")
CHUNK = timedelta(hours=24)


def _local(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone()


def _logbook(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Fetch the logbook for [start, end) in day-sized requests."""
    token = TOKEN_FILE.read_text().strip()
    entries: list[dict[str, Any]] = []
    at = start
    while at < end:
        to = min(at + CHUNK, end)
        url = (
            f"https://{HOST}/api/logbook/{urllib.parse.quote(at.isoformat())}"
            f"?end_time={urllib.parse.quote(to.isoformat())}"
        )
        request = urllib.request.Request(  # noqa: S310 - fixed https host
            url, headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
            entries.extend(json.load(response))
        at = to
    return entries


def _cause(entry: dict[str, Any]) -> str | None:
    """Return who caused a state change, or None when HA did not cause it."""
    if entry.get("context_user_id"):
        service = entry.get("context_service")
        return f"a user (UI, app or API){f', {service}' if service else ''}"
    cause = entry.get("context_entity_id")
    if not cause:
        return None
    if cause.split(".")[0] in DOMAINS:
        # Another light/switch changed in the same context: one command to a
        # group or a binding, reported by each member. Not an HA actor.
        return None
    return str(entry.get("context_name") or cause)


def logbook_report(start: datetime, end: datetime) -> None:
    """Print every HA actor that changed a light or switch, with its targets."""
    by_cause: dict[str, dict[str, list[datetime]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for entry in _logbook(start, end):
        entity = str(entry.get("entity_id") or "")
        if entity.split(".")[0] not in DOMAINS or entity.startswith(OWN_ENTITIES):
            continue
        cause = _cause(entry)
        if cause is not None:
            target = f"{entity} -> {entry.get('state')}"
            by_cause[cause][target].append(_local(entry["when"]))
    print(f"\nHA logbook: light/switch changes caused from HA, {start:%m-%d %H:%M} to")
    print(f"{end:%m-%d %H:%M} (changes with no HA cause are left out)\n")
    if not by_cause:
        print("  none")
    for cause, targets in sorted(by_cause.items()):
        total = sum(len(times) for times in targets.values())
        print(f"  {cause}  ({total} changes)")
        for target, times in sorted(targets.items()):
            print(
                f"      {len(times):4d} x {target:62s} "
                f"first {min(times):%m-%d %H:%M}  last {max(times):%m-%d %H:%M}"
            )


def _light_man_topics() -> tuple[set[str], set[str], set[str], set[str]]:
    """Return Light Man's (light topics, zone-off topics, switches, sync switches)."""
    data = json.loads(STORE.read_text())["data"]
    lights = {s["consolidated_topic"] for s in data["sources"].values()}
    switches: set[str] = set()
    for room in data["rooms"].values():
        lights |= {light["set_topic"] for light in room.get("lights", [])}
        switches |= {switch["topic"] for switch in room.get("switches", [])}
    off: set[str] = set()
    sync: set[str] = set()
    for zone in data.get("occupancy_zones", {}).values():
        off |= set(zone.get("off_lights", []))
        sync |= set(zone.get("sync_switches", []))
    return lights | off, off, switches, sync


def _from_light_man(
    topic: str, payload: dict[str, Any], owned: tuple[set[str], ...]
) -> bool | None:
    """Say whether Light Man sent a ``/set``; None when the device is not its own."""
    lights, off, switches, sync = owned
    base = topic.removesuffix("/set")
    keys = set(payload)
    if base in switches:
        if keys <= BAR_KEYS:
            return True
        return keys == {"state"} and base in sync
    if topic in lights:
        if "transition" not in keys:
            return False
        return "brightness" in keys or (payload.get("state") == "OFF" and topic in off)
    return None


def capture_report(path: Path, start: datetime, end: datetime) -> None:
    """Print the ``/set`` messages in a capture that Light Man did not send."""
    owned = _light_man_topics()
    names: dict[str, str] = {}
    found: dict[tuple[str, str, str], list[datetime]] = defaultdict(list)
    with path.open(encoding="utf-8") as rows:
        for line in rows:
            row = json.loads(line)
            if row.get("kind") == "devices":
                names = row.get("names", {})
            if row.get("kind") != "set" or not isinstance(row.get("payload"), dict):
                continue
            when = datetime.fromtimestamp(row["t"]).astimezone()
            if not start <= when < end:
                continue
            device = row["topic"].removeprefix(Z2M).removesuffix("/set")
            topic = f"{Z2M}{names.get(device, device)}/set"
            verdict = _from_light_man(topic, row["payload"], owned)
            if verdict and device.startswith("0x"):
                verdict = False  # Light Man addresses devices by name only
            if verdict:
                continue
            group = "a Light Man device" if verdict is False else "other device"
            shape = json.dumps(row["payload"], sort_keys=True)[:90]
            found[(group, topic.removeprefix(Z2M), shape)].append(when)
    print(f"\nMQTT capture {path.name}: /set messages Light Man did not send\n")
    if not found:
        print("  none")
    for (group, topic, shape), times in sorted(found.items()):
        print(
            f"  {len(times):4d} x [{group}] {topic}  {shape}  "
            f"first {min(times):%m-%d %H:%M}  last {max(times):%m-%d %H:%M}"
        )


def main() -> None:
    """Parse arguments and print both reports."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--since", required=True, help="local time, e.g. 2026-11-04T10:05"
    )
    parser.add_argument("--until", help="local time (default: now)")
    parser.add_argument("--capture", type=Path, action="append", default=[])
    args = parser.parse_args()
    start = _local(args.since)
    end = _local(args.until) if args.until else datetime.now().astimezone()
    logbook_report(start, end)
    for capture in args.capture:
        capture_report(capture, start, end)


if __name__ == "__main__":
    main()
