#!/usr/bin/env python3
"""A0 live per-bulb tests (plan Workstream A0 §1-§4, §6, §7). DEVICE WRITES.

Reads results from the bulb's own composite-state reports (``philips_raw``),
falling back to one ``/get`` when no fresh report arrives. Every bulb is restored
to its pre-test state (on/off, brightness, colour) at the end, even on error.

  .venv/bin/python scripts/a0_bulb_test.py basic  "<bulb>" [--colour]
  .venv/bin/python scripts/a0_bulb_test.py fade   "<bulb>" --seconds 300
  .venv/bin/python scripts/a0_bulb_test.py xypath "<bulb>" --to-xy 0.30,0.25

"binding ON" is emulated with the standard ZCL ``genOnOff.on`` command — the same
command an Inovelli switch sends over its binding — via Z2M's generic ``command``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import live_probe  # noqa: E402  # loads the frame codec + broker connection

if TYPE_CHECKING:
    import paho.mqtt.client as mqtt

BASE = "zigbee2mqtt"
OUT = REPO / "captures"


class Bulb:
    """Subscribes to one bulb and collects its fresh reports and Z2M errors."""

    def __init__(self, client: mqtt.Client, name: str) -> None:
        """Start listening to ``name``'s state topic and bridge/logging."""
        self.client = client
        self.name = name
        self.reports: list[dict[str, Any]] = []
        self.errors: list[tuple[float, str]] = []
        self._last_raw: str | None = None
        self.last_seen: tuple[float, str] | None = None  # any message, fresh or not
        self._lock = threading.Lock()
        client.on_message = self._on_message
        client.subscribe(f"{BASE}/{name}")
        client.subscribe(f"{BASE}/bridge/logging")
        client.loop_start()

    def _on_message(self, _c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        now = time.time()
        with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
            data = json.loads(msg.payload)
            if msg.topic.endswith("bridge/logging"):
                text = str(data.get("message", ""))
                if data.get("level") == "error" and self.name in text:
                    with self._lock:
                        self.errors.append((now, text[:160]))
                return
            raw = data.get("philips_raw") if isinstance(data, dict) else None
            if isinstance(raw, str):
                self.last_seen = (now, raw)
            if isinstance(raw, str) and raw != self._last_raw:
                self._last_raw = raw
                with self._lock:
                    self.reports.append(
                        {"t": now, "raw": raw, **live_probe._decoded(raw)}
                    )

    def set(self, payload: dict[str, Any]) -> float:
        """Publish a /set; return the send time."""
        sent = time.time()
        self.client.publish(f"{BASE}/{self.name}/set", json.dumps(payload))
        print(f"  {time.strftime('%H:%M:%S')} /set {payload}")
        return sent

    def zcl_on(self) -> float:
        """Send the same standard ZCL On a switch binding sends."""
        body = {"command": {"cluster": "genOnOff", "command": "on", "payload": {}}}
        sent = time.time()
        self.client.publish(f"{BASE}/{self.name}/set", json.dumps(body))
        print(f"  {time.strftime('%H:%M:%S')} ZCL genOnOff.on (binding emulation)")
        return sent

    def get(self) -> None:
        """Ask for one composite-state read (the reply lands in ``reports``)."""
        self.client.publish(f"{BASE}/{self.name}/get", json.dumps({"state": ""}))

    def state_after(self, since: float, wait: float) -> dict[str, Any] | None:
        """Settled state: wait, then one /get (bulbs push the settled value ~10 s late).

        Pushed reports mark when the bulb *started* acting (first report ≤ 1 s);
        the settled value is read explicitly so checks do not wait 11 s each.
        """
        deadline = since + wait
        while time.time() < deadline:
            time.sleep(0.1)
        asked = time.time()
        self.get()
        while time.time() < asked + 3.0:
            time.sleep(0.1)
            if self.last_seen and self.last_seen[0] >= asked:
                raw = self.last_seen[1]
                return {"t": self.last_seen[0], "raw": raw, **live_probe._decoded(raw)}
        return None

    def started_at(self, since: float) -> float | None:
        """Seconds from ``since`` to the first pushed report (the bulb began acting)."""
        first = next((r for r in self.reports if r["t"] >= since), None)
        return None if first is None else first["t"] - since

    def first_after(self, since: float) -> dict[str, Any] | None:
        """First fresh report after ``since``."""
        return next((r for r in self.reports if r["t"] >= since), None)

    def close(self) -> None:
        """Stop the network loop."""
        self.client.loop_stop()


def _check(label: str, got: dict[str, Any] | None, **want: Any) -> bool:
    if got is None:
        print(f"  FAIL {label}: no report and no /get reply")
        return False
    bad = []
    for key, expected in want.items():
        value = got.get(key)
        if key in ("brightness", "mirek") and isinstance(value, int):
            if abs(value - expected) > 1:
                bad.append(f"{key}={value} want {expected}±1")
        elif key == "xy" and value is not None:
            if max(abs(value[0] - expected[0]), abs(value[1] - expected[1])) > 0.01:
                bad.append(f"xy={value} want {expected}")
        elif value != expected:
            bad.append(f"{key}={value} want {expected}")
    verdict = "PASS" if not bad else "FAIL " + ", ".join(bad)
    print(f"  {verdict} {label}: reported {got.get('raw')} {_brief(got)}")
    return not bad


def _brief(r: dict[str, Any]) -> str:
    keys = ("on", "brightness", "mirek", "xy")
    return (
        "{" + ", ".join(f"{k}={r.get(k)}" for k in keys if r.get(k) is not None) + "}"
    )


def _baseline(bulb: Bulb) -> dict[str, Any]:
    start = time.time()
    bulb.get()
    state = bulb.state_after(start, 3.0)
    if state is None:
        raise SystemExit(f"{bulb.name}: no composite state — aborting before any write")
    print(f"  baseline {state['raw']} {_brief(state)}")
    return state


def _restore(bulb: Bulb, base: dict[str, Any]) -> None:
    payload: dict[str, Any] = {"transition": 0.4}
    if base.get("brightness") is not None:
        payload["brightness"] = base["brightness"]
    if base.get("xy") is not None and base.get("mirek") is None:
        payload["color"] = {"x": base["xy"][0], "y": base["xy"][1]}
    elif base.get("mirek") is not None:
        payload["color_temp"] = base["mirek"]
    payload["state"] = "ON" if base.get("on") else "OFF"
    sent = bulb.set(payload)
    got = bulb.state_after(sent, 3.0)
    want = {
        k: base[k] for k in ("on", "brightness", "mirek") if base.get(k) is not None
    }
    _check("restore to baseline", got, **want)


def run_basic(bulb: Bulb, colour: bool) -> list[str]:
    """§1 atomic set, §2 colour, §3 staging while off + binding ON, §4 responses."""
    results: list[str] = []
    sent = bulb.set(
        {"state": "ON", "brightness": 120, "color_temp": 350, "transition": 1}
    )
    ok = _check(
        "§1a on + 120/350",
        bulb.state_after(sent, 3.0),
        on=True,
        brightness=120,
        mirek=350,
    )
    results.append(f"§1a {'pass' if ok else 'fail'}")
    sent = bulb.set({"brightness": 200, "color_temp": 250, "transition": 1})
    ok = _check(
        "§1b 200/250 atomic", bulb.state_after(sent, 3.0), brightness=200, mirek=250
    )
    results.append(f"§1b {'pass' if ok else 'fail'}")
    if colour:
        sent = bulb.set(
            {"brightness": 120, "color": {"x": 0.30, "y": 0.25}, "transition": 1}
        )
        ok = _check(
            "§2a xy", bulb.state_after(sent, 3.0), brightness=120, xy=(0.30, 0.25)
        )
        results.append(f"§2a {'pass' if ok else 'fail'}")
        sent = bulb.set({"color_temp": 370, "transition": 1})
        ok = _check("§2b back to CT", bulb.state_after(sent, 3.0), mirek=370)
        results.append(f"§2b {'pass' if ok else 'fail'}")
    else:
        sent = bulb.set({"color": {"x": 0.30, "y": 0.25}, "transition": 1})
        got = bulb.state_after(sent, 3.0)
        print(f"  INFO §2 CT-only given xy: reported {got and _brief(got)}")
        results.append("§2 ct-only: recorded")
    sent = bulb.set({"state": "OFF", "transition": 1})
    _check("§3a off", bulb.state_after(sent, 3.0), on=False)
    sent = bulb.set({"brightness": 60, "color_temp": 450, "transition": 1})
    got = bulb.state_after(sent, 3.0)
    ok = _check("§3b staged while off", got, on=False, brightness=60, mirek=450)
    results.append(f"§3b {'pass' if ok else 'fail'}")
    sent = bulb.zcl_on()
    got = bulb.state_after(sent, 2.5)
    first = [r for r in bulb.reports if r["t"] >= sent][:1]
    start_note = (
        f"first push {first[0]['t'] - sent:.1f}s {_brief(first[0])}"
        if first
        else "no push"
    )
    print(f"  INFO §3c binding ON: {start_note}")
    ok = _check("§3c lands on staged", got, on=True, brightness=60, mirek=450)
    results.append(f"§3c {'pass' if ok else 'fail'} ({start_note})")
    timed_out = [e for e in bulb.errors if "timed out" in e[1]]
    other = [e for e in bulb.errors if "timed out" not in e[1]]
    print(
        f"  §4 Z2M errors for this bulb: timed out {len(timed_out)}, other {len(other)}"
    )
    results.append(f"§4 timeouts {len(timed_out)} other {len(other)}")
    return results


def run_fade(bulb: Bulb, seconds: float) -> list[str]:
    """§6 long fade: 20 % -> 80 % over ``seconds``; reports give the trajectory."""
    bulb.set({"state": "ON", "brightness": 51, "color_temp": 400, "transition": 0.4})
    time.sleep(3.0)
    sent = bulb.set({"brightness": 204, "color_temp": 300, "transition": seconds})
    checkpoints = [0.25, 0.5, 0.75, 1.0]
    rows = []
    for frac in checkpoints:
        target = sent + frac * seconds
        while time.time() < target:
            time.sleep(0.5)
        bulb.get()
        time.sleep(1.5)
        last = bulb.reports[-1] if bulb.reports else None
        expect = round(51 + (204 - 51) * frac)
        rows.append((frac, last and last.get("brightness"), expect))
        print(
            f"  t={frac * seconds:5.0f}s reported {last and _brief(last)} "
            f"linear≈{expect}"
        )
    during = [r for r in bulb.reports if r["t"] > sent]
    print(f"  reports during fade: {len(during)}")
    worst = max(abs((b or 0) - e) for _f, b, e in rows)
    return [
        f"§6 fade {seconds:.0f}s: max deviation from linear {worst} points, "
        f"{len(during)} reports"
    ]


def run_xypath(bulb: Bulb, from_mirek: int, to_xy: tuple[float, float]) -> list[str]:
    """§7 cross-mode glide: CT start, then 60 s fade to an xy target; sample path."""
    bulb.set(
        {"state": "ON", "brightness": 150, "color_temp": from_mirek, "transition": 0.4}
    )
    time.sleep(3.0)
    start = bulb.state_after(time.time() - 2, 0.1)
    sent = bulb.set({"color": {"x": to_xy[0], "y": to_xy[1]}, "transition": 60})
    path = []
    for _ in range(12):
        time.sleep(5.0)
        bulb.get()
        time.sleep(0.5)
        if bulb.reports:
            path.append(bulb.reports[-1].get("xy"))
    print(f"  start {start and _brief(start)}")
    print(f"  xy path every 5 s: {path}")
    del sent
    return [f"§7 xy path recorded ({len(path)} samples)"]


def main() -> None:
    """Run one test on one bulb, always restoring its state."""
    parser = argparse.ArgumentParser(description="A0 live bulb tests (device writes)")
    parser.add_argument("test", choices=["basic", "fade", "xypath"])
    parser.add_argument("bulb")
    parser.add_argument("--colour", action="store_true")
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--from-mirek", type=int, default=370)
    parser.add_argument("--to-xy", default="0.30,0.25")
    for flag in ("--ha-config", "--external-host", "--host", "--user", "--password"):
        parser.add_argument(flag, default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    args.ha_config = args.ha_config or live_probe.mqtt_dump.DEFAULT_HA_CONFIG
    args.external_host = (
        args.external_host or live_probe.mqtt_dump.DEFAULT_EXTERNAL_HOST
    )
    client = live_probe.connect(args)
    bulb = Bulb(client, args.bulb)
    time.sleep(1.0)
    print(f"== {args.test} on {args.bulb}")
    base = _baseline(bulb)
    results: list[str] = []
    try:
        if args.test == "basic":
            results = run_basic(bulb, args.colour)
        elif args.test == "fade":
            results = run_fade(bulb, args.seconds)
        else:
            x, y = (float(v) for v in args.to_xy.split(","))
            results = run_xypath(bulb, args.from_mirek, (x, y))
    finally:
        _restore(bulb, base)
        OUT.mkdir(exist_ok=True)
        log = (
            OUT / f"a0-{args.test}-{args.bulb.replace(' ', '_')}-"
            f"{time.strftime('%Y%m%d-%H%M%S')}.json"
        )
        log.write_text(
            json.dumps(
                {
                    "bulb": args.bulb,
                    "test": args.test,
                    "baseline": base,
                    "results": results,
                    "reports": bulb.reports,
                    "errors": bulb.errors,
                },
                indent=1,
            )
        )
        bulb.close()
        client.disconnect()
    print("RESULT", args.bulb, results)


if __name__ == "__main__":
    main()
