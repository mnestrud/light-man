#!/usr/bin/env python3
"""Hue bulb group-table normalize + audit (Light Man maintenance).

Group commands are Zigbee multicasts (network broadcasts); a bulb obeys them
from its own firmware NVRAM group table, which can drift from Z2M's
`bridge/groups` view. A stale entry makes a bulb apply a group it shouldn't —
the living-room split (2026-06-09): three bulbs carried a group the hallway
also used, so the hallway's `color` broadcast flipped them to xy.

This reconciles every Philips/Hue *light* bulb's NVRAM to Z2M's view:
genGroups `removeAll` (wipes the WHOLE NVRAM table, including stale entries Z2M
doesn't track) then re-add to exactly the groups Z2M lists for it. A bulb in no
groups is wiped only (no group created). Read-only by default; `--commit`
applies. Full process + per-bulb log: docs/reference/group-normalize-audit.md.

Subcommands:
  audit                 list every Hue light, its endpoint, and its Z2M groups
  normalize [--commit]  per bulb: removeAll + re-add to its Z2M groups
  probe [--commit]      fire a distinctive rgb at each rgb group, /get every Hue
                        light, flag any NON-member that adopts the color
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import TYPE_CHECKING, Any

sys.path.insert(0, "scripts")
from mqtt_dump import (
    DEFAULT_EXTERNAL_HOST,
    DEFAULT_HA_CONFIG,
    INTERNAL_HOSTS,
    load_ha_mqtt_config,
    make_client,
)

if TYPE_CHECKING:
    import paho.mqtt.client as mqtt

HUE_VENDORS = {"Philips", "Signify Netherlands B.V."}
# Distinctive probe color, far from any adaptive value (pure blue → xy ~0.15,0.06).
PROBE_RGB = {"r": 0, "g": 0, "b": 255}


class Bridge:
    """A live MQTT connection with retained reads + txn-correlated requests."""

    def __init__(self) -> None:
        """Connect, start the loop, and subscribe to group-member responses."""
        cfg = load_ha_mqtt_config(DEFAULT_HA_CONFIG)
        host = cfg.get("broker")
        host = DEFAULT_EXTERNAL_HOST if host in INTERNAL_HOSTS else host
        self.client = make_client(cfg.get("username"), cfg.get("password"))
        self._resp: dict[str, dict[str, Any]] = {}
        self._state: dict[str, dict[str, Any]] = {}
        self._txn = 0
        self.client.on_message = self._on_message
        self.client.connect(host, int(cfg.get("port", 1883)), 30)
        self.client.loop_start()
        time.sleep(2)
        self.client.subscribe("zigbee2mqtt/bridge/response/group/members/#")
        time.sleep(1)

    def _on_message(self, _c: Any, _u: Any, m: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(m.payload)
        except json.JSONDecodeError:
            return
        if "/bridge/response/group/members/" in m.topic:
            txn = payload.get("transaction")
            if txn is not None:
                self._resp[txn] = payload
        else:
            self._state[m.topic] = payload

    def _fetch(self, topic: str, seconds: float = 8.0) -> Any:
        """Read a retained bridge topic off the *persistent* loop.

        NOT mqtt_dump.fetch_retained — that clobbers ``on_message`` and stops the
        loop, which kills request/response correlation mid-run.
        """
        self.client.subscribe(topic)
        self._state.pop(topic, None)
        deadline = time.time() + seconds
        while time.time() < deadline:
            if topic in self._state:
                return self._state[topic]
            time.sleep(0.2)
        return self._state.get(topic, [])

    def devices(self) -> list[dict[str, Any]]:
        """Return the retained ``bridge/devices`` list."""
        return self._fetch("zigbee2mqtt/bridge/devices")

    def groups(self) -> list[dict[str, Any]]:
        """Return the retained ``bridge/groups`` list."""
        return self._fetch("zigbee2mqtt/bridge/groups")

    def request(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Send a bridge request; return the txn-matched response (None on timeout)."""
        self._txn += 1
        txn = f"zgb-{self._txn}"
        body = {**payload, "transaction": txn}
        self.client.publish(f"zigbee2mqtt/bridge/request/{endpoint}", json.dumps(body))
        for _ in range(100):  # up to 10s
            if txn in self._resp:
                return self._resp.pop(txn)
            time.sleep(0.1)
        return None

    def read_states(self, topics: list[str], seconds: float = 5.0) -> dict[str, Any]:
        """``/get`` each topic and return the fresh device states."""
        for t in topics:
            self.client.subscribe(t)
        self._state.clear()
        for t in topics:
            self.client.publish(
                t + "/get", '{"state":"","color":"","color_temp":""}', 0, False
            )
        time.sleep(seconds)
        return dict(self._state)

    def close(self) -> None:
        """Stop the loop and disconnect."""
        self.client.loop_stop()
        self.client.disconnect()


def _light_endpoint(dev: dict[str, Any]) -> str | None:
    """Return the group-bearing light endpoint (genGroups + color/level cluster)."""
    for epn, ep in (dev.get("endpoints") or {}).items():
        ins = ep.get("clusters", {}).get("input", [])
        if "genGroups" in ins and ("lightingColorCtrl" in ins or "genLevelCtrl" in ins):
            return epn
    return None


def hue_lights(devs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every Philips/Hue device that exposes a `light` (bulbs, not sensors/switches)."""
    out = []
    for d in devs:
        defi = d.get("definition") or {}
        if defi.get("vendor") in HUE_VENDORS and any(
            e.get("type") == "light" for e in (defi.get("exposes") or [])
        ):
            out.append(d)
    return out


def intended_groups(ieee: str, groups: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Return the (group_name, endpoint) memberships Z2M lists for a bulb."""
    out = []
    for g in groups:
        for m in g.get("members", []):
            if m.get("ieee_address") == ieee:
                out.append((g["friendly_name"], int(m.get("endpoint", 11))))
    return out


def cmd_audit(br: Bridge) -> None:
    """List every Hue light, its endpoint, and its Z2M groups (read-only)."""
    devs, groups = br.devices(), br.groups()
    lights = hue_lights(devs)
    print(f"Hue lights: {len(lights)}")
    nogroup = 0
    for d in lights:
        ie = d["ieee_address"]
        ep = _light_endpoint(d)
        gs = intended_groups(ie, groups)
        if not gs:
            nogroup += 1
        names = [f"{n}@{e}" for n, e in gs] or ["NONE"]
        print(f"  {d['friendly_name']:<36} ep={ep} -> {', '.join(names)}")
    print(f"no-group bulbs (wipe only): {nogroup}")


def cmd_normalize(br: Bridge, commit: bool) -> None:
    """Wipe + re-add each Hue light's groups to match Z2M (dry-run unless commit)."""
    devs, groups = br.devices(), br.groups()
    lights = hue_lights(devs)
    print(
        f"{'COMMIT' if commit else 'DRY-RUN'} normalize over {len(lights)} Hue lights\n"
    )
    results = []
    for d in lights:
        name, ie = d["friendly_name"], d["ieee_address"]
        ep = int(_light_endpoint(d) or 11)
        gs = intended_groups(ie, groups)
        plan = f"removeAll(ep{ep}) + re-add {[n for n, _ in gs] or 'NONE'}"
        if not commit:
            print(f"  [plan] {name:<36} {plan}")
            results.append((name, "planned", ""))
            continue
        r = br.request("group/members/remove_all", {"device": name, "endpoint": ep})
        rm = (r or {}).get("status", "NO-RESP")
        adds = []
        for gname, gep in gs:
            ar = br.request(
                "group/members/add", {"group": gname, "device": name, "endpoint": gep}
            )
            adds.append((gname, (ar or {}).get("status", "NO-RESP")))
        ok = rm == "ok" and all(s == "ok" for _, s in adds)
        bad = [f"{g}:{s}" for g, s in adds if s != "ok"]
        detail = f"removeAll={rm}" + (f" adds_failed={bad}" if bad else "")
        print(f"  [{'ok ' if ok else 'FAIL'}] {name:<36} {detail}")
        results.append((name, "ok" if ok else "FAIL", detail))
    if commit:
        fails = [r for r in results if r[1] == "FAIL"]
        print(f"\ndone: {len(results) - len(fails)} ok, {len(fails)} FAIL")
        for n, _, det in fails:
            print(f"  FAIL {n}: {det}")


def cmd_probe(br: Bridge, commit: bool) -> None:
    """Fire a distinctive rgb at each rgb group; flag any non-member that adopts it."""
    devs, groups = br.devices(), br.groups()
    lights = hue_lights(devs)
    name_by_ieee = {d["ieee_address"]: d["friendly_name"] for d in lights}
    light_topics = ["zigbee2mqtt/" + d["friendly_name"] for d in lights]
    rgb_groups = ["zgb_hallway_up", "zgb_hallway_down"]
    if not commit:
        print("DRY-RUN: would fire", PROBE_RGB, "at", rgb_groups, "and /get all lights")
        return
    for gname in rgb_groups:
        members = {
            m["ieee_address"]
            for g in groups
            if g["friendly_name"] == gname
            for m in g.get("members", [])
        }
        print(
            f"\n--- probe {gname}: fire {PROBE_RGB}, read all {len(lights)} lights ---"
        )
        br.client.publish(
            f"zigbee2mqtt/{gname}/set",
            json.dumps({"brightness": 80, "transition": 1.0, "color": PROBE_RGB}),
            0,
            False,
        )
        time.sleep(4)
        states = br.read_states(light_topics, 5)
        adopters = []
        for d in lights:
            ie = d["ieee_address"]
            s = states.get("zigbee2mqtt/" + d["friendly_name"], {})
            x = (s.get("color") or {}).get("x")
            is_probe_color = (
                s.get("color_mode") == "xy" and x is not None and abs(x - 0.15) < 0.05
            )
            if is_probe_color and ie not in members:
                adopters.append(name_by_ieee[ie])
        if adopters:
            print(f"  LEAK — non-members adopted {gname}'s color: {adopters}")
        else:
            print("  clean — no non-member adopted the color")
        # reset the group back to color_temp
        br.client.publish(
            f"zigbee2mqtt/{gname}/set",
            json.dumps({"brightness": 80, "transition": 1.0, "color_temp": 370}),
            0,
            False,
        )
        time.sleep(2)


def main() -> None:
    """Parse args and dispatch the subcommand."""
    p = argparse.ArgumentParser(description="Hue bulb group-table normalize + audit.")
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("audit")
    sub.add_parser("normalize").add_argument("--commit", action="store_true")
    sub.add_parser("probe").add_argument("--commit", action="store_true")
    args = p.parse_args()
    br = Bridge()
    try:
        if args.mode == "audit":
            cmd_audit(br)
        elif args.mode == "normalize":
            cmd_normalize(br, args.commit)
        else:
            cmd_probe(br, args.commit)
    finally:
        br.close()


if __name__ == "__main__":
    main()
