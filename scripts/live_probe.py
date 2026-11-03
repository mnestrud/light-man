#!/usr/bin/env python3
"""Measure what the lights actually do, from the bulbs' own reports.

Every Light Man Hue bulb pushes its composite state (cluster 0xFC03 attribute
0x0002) unprompted; Zigbee2MQTT publishes it as ``philips_raw``. Z2M republishes
the cached value on every state message, so a report is *fresh* only when a
bulb's ``philips_raw`` differs from its previous value. No polling, no extra RF.

Modes (all read-only except ``get``, which sends one Zigbee read):
  capture [--seconds N] [--out FILE]
      Record switch actions, every ``zigbee2mqtt/<target>/set``, every fresh bulb
      report (decoded), and Z2M failure lines, with local millisecond timestamps,
      to JSON Lines. The first message per bulb is recorded as its baseline.
  analyze FILE [--step N]
      Tap latency (action → first /set → first fresh report from a targeted bulb),
      group sends per 10 s window and per hour, per-bulb report steps and rates.
  get DEVICE
      One ``/get`` of a bulb's composite state, decoded.

Run with the venv python:  .venv/bin/python scripts/live_probe.py capture --seconds 600
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import statistics
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import paho.mqtt.client as mqtt

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import mqtt_dump  # noqa: E402  # sibling helper: broker credentials + client

BASE = "zigbee2mqtt"
WINDOW_S = 10.0
TAP_SET_WINDOW_S = 3.0
TAP_REPORT_WINDOW_S = 5.0


def _load_hue_frame() -> Any:
    """Load the integration's pure frame codec without importing the HA package."""
    path = REPO / "custom_components" / "light_man" / "hue_frame.py"
    spec = importlib.util.spec_from_file_location("hue_frame", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hue_frame"] = module
    spec.loader.exec_module(module)
    return module


HUE = _load_hue_frame()


def _decoded(raw: str) -> dict[str, Any]:
    try:
        frame = HUE.decode_hex(raw)
    except ValueError as err:
        return {"error": str(err)}
    return {
        "on": frame.on,
        "brightness": frame.brightness,
        "mirek": frame.mirek,
        "xy": frame.xy,
    }


def connect(args: argparse.Namespace) -> mqtt.Client:
    """Connect with the broker credentials HA uses (via mqtt_dump)."""
    host, port, user, password = mqtt_dump.resolve_connection(args)
    client = mqtt_dump.make_client(user, password)
    client.connect(host, port, keepalive=30)
    return client


class Recorder:
    """Turns raw MQTT messages into capture records."""

    def __init__(self, sink: Any) -> None:
        """Write records to ``sink`` (a text file)."""
        self._sink = sink
        self._last_raw: dict[str, str] = {}
        self.counts: dict[str, int] = defaultdict(int)

    def _write(self, record: dict[str, Any]) -> None:
        self._sink.write(json.dumps(record) + "\n")
        self.counts[record["kind"]] += 1

    def on_message(self, _c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        """Classify one message; ignore anything that is not of interest."""
        now = round(time.time(), 3)
        topic = msg.topic
        try:
            data = json.loads(msg.payload)
        except json.JSONDecodeError, UnicodeDecodeError:
            return
        if topic == f"{BASE}/bridge/groups":
            self._write({"t": now, "kind": "groups", "groups": data})
        elif topic == f"{BASE}/bridge/devices" and isinstance(data, list):
            names = {d["ieee_address"]: d.get("friendly_name") for d in data}
            self._write({"t": now, "kind": "devices", "names": names})
        elif topic == f"{BASE}/bridge/logging":
            if isinstance(data, dict) and data.get("level") == "error":
                self._write({"t": now, "kind": "z2m_error", "message": data["message"]})
        elif topic.startswith(f"{BASE}/bridge/"):
            return
        elif topic.endswith("/set"):
            self._write({"t": now, "kind": "set", "topic": topic, "payload": data})
        elif isinstance(data, dict):
            self._device_message(now, topic, data)

    def _device_message(self, now: float, topic: str, data: dict[str, Any]) -> None:
        action = data.get("action")
        if isinstance(action, str) and action:
            self._write({"t": now, "kind": "action", "topic": topic, "action": action})
            return
        raw = data.get("philips_raw")
        if not isinstance(raw, str):
            return
        name = topic.removeprefix(f"{BASE}/")
        previous = self._last_raw.get(name)
        self._last_raw[name] = raw
        if previous == raw:
            return
        kind = "baseline" if previous is None else "report"
        self._write({"t": now, "kind": kind, "bulb": name, "raw": raw, **_decoded(raw)})


def run_capture(client: mqtt.Client, seconds: float | None, out: Path) -> None:
    """Record until ``seconds`` elapse or Ctrl-C."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as sink:
        recorder = Recorder(sink)
        client.on_message = recorder.on_message
        client.subscribe(f"{BASE}/#")
        client.loop_start()
        print(f"capturing to {out} (Ctrl-C to stop)")
        deadline = time.time() + seconds if seconds else None
        with contextlib.suppress(KeyboardInterrupt):
            while deadline is None or time.time() < deadline:
                time.sleep(0.5)
                sink.flush()
        client.loop_stop()
    print("records:", dict(recorder.counts))


def run_get(client: mqtt.Client, device: str, seconds: float) -> None:
    """Ask Z2M to read one bulb's composite state and print it decoded."""
    got: dict[str, Any] = {}
    sent_at = time.time()

    def on_message(_c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(msg.payload)
            if (
                isinstance(data, dict)
                and "philips_raw" in data
                and time.time() > sent_at
            ):
                got["raw"] = data["philips_raw"]

    client.on_message = on_message
    client.subscribe(f"{BASE}/{device}")
    client.loop_start()
    time.sleep(0.5)
    sent_at = time.time()
    client.publish(f"{BASE}/{device}/get", json.dumps({"state": ""}))
    deadline = time.time() + seconds
    while time.time() < deadline and "raw" not in got:
        time.sleep(0.1)
    client.loop_stop()
    if "raw" not in got:
        raise SystemExit("no composite-state reply (bulb off the mesh or not Hue?)")
    print(device, got["raw"], _decoded(got["raw"]))


# --- analysis ---------------------------------------------------------------


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _targets(records: list[dict[str, Any]]) -> tuple[set[str], dict[str, set[str]]]:
    """Group /set topics and each group's member bulb names (bridge/groups+devices)."""
    groups_rec = next((r for r in records if r["kind"] == "groups"), None)
    devices_rec = next((r for r in records if r["kind"] == "devices"), None)
    if groups_rec is None:
        return set(), {}
    names: dict[str, str] = devices_rec["names"] if devices_rec else {}
    members = {
        f"{BASE}/{g['friendly_name']}/set": {
            names.get(m["ieee_address"], m["ieee_address"]) for m in g["members"]
        }
        for g in groups_rec["groups"]
    }
    return set(members), members


def _percentiles(values: list[float]) -> str:
    if not values:
        return "n/a"
    values = sorted(values)
    p95 = values[min(len(values) - 1, round(0.95 * (len(values) - 1)))]
    return (
        f"n={len(values)} p50={statistics.median(values) * 1000:.0f} ms "
        f"p95={p95 * 1000:.0f} ms max={values[-1] * 1000:.0f} ms"
    )


def _tap_latency(records: list[dict[str, Any]], members: dict[str, set[str]]) -> None:
    to_set: list[float] = []
    to_report: list[float] = []
    for i, rec in enumerate(records):
        if rec["kind"] != "action":
            continue
        later = records[i + 1 :]
        first_set = next(
            (
                r
                for r in later
                if r["kind"] == "set" and r["t"] - rec["t"] <= TAP_SET_WINDOW_S
            ),
            None,
        )
        if first_set is None:
            continue
        to_set.append(first_set["t"] - rec["t"])
        target_bulbs = members.get(first_set["topic"]) or {
            first_set["topic"].removeprefix(f"{BASE}/").removesuffix("/set")
        }
        first_report = next(
            (
                r
                for r in later
                if r["kind"] == "report"
                and r["bulb"] in target_bulbs
                and r["t"] >= first_set["t"]
                and r["t"] - rec["t"] <= TAP_REPORT_WINDOW_S
            ),
            None,
        )
        if first_report is not None:
            to_report.append(first_report["t"] - rec["t"])
    print("tap → Light Man /set:   ", _percentiles(to_set))
    print("tap → first bulb report:", _percentiles(to_report))


def _group_windows(records: list[dict[str, Any]], group_topics: set[str]) -> None:
    sends = [
        r["t"] for r in records if r["kind"] == "set" and r["topic"] in group_topics
    ]
    window: deque[float] = deque()
    worst = 0
    for t in sends:
        window.append(t)
        while t - window[0] > WINDOW_S:
            window.popleft()
        worst = max(worst, len(window))
    span_h = (records[-1]["t"] - records[0]["t"]) / 3600 if records else 0
    rate = len(sends) / span_h if span_h else 0.0
    print(
        f"group sends: {len(sends)} ({rate:.0f}/h), "
        f"worst {WINDOW_S:.0f} s window {worst}"
    )


def _report_steps(records: list[dict[str, Any]], step_limit: int) -> None:
    last: dict[str, dict[str, Any]] = {}
    worst: dict[str, tuple[int, float]] = {}
    flagged: list[str] = []
    for rec in records:
        if rec["kind"] not in ("baseline", "report") or rec.get("brightness") is None:
            continue
        prev = last.get(rec["bulb"])
        last[rec["bulb"]] = rec
        if prev is None or rec["kind"] == "baseline" or not rec.get("on"):
            continue
        step = abs(rec["brightness"] - (prev.get("brightness") or 0))
        dt = max(rec["t"] - prev["t"], 0.001)
        if step > worst.get(rec["bulb"], (0, 0.0))[0]:
            worst[rec["bulb"]] = (step, step / dt)
        if step > step_limit:
            flagged.append(
                f"  {rec['t']:.3f} {rec['bulb']}: {step} points in {dt:.1f} s"
            )
    print(
        f"bulbs reporting: {len(last)}; "
        f"report-to-report steps > {step_limit}: {len(flagged)}"
    )
    for line in flagged[:20]:
        print(line)
    for bulb, (step, rate) in sorted(worst.items(), key=lambda kv: -kv[1][0])[:10]:
        print(f"  worst {bulb}: {step} points ({rate:.1f}/s)")


def run_analyze(path: Path, step_limit: int) -> None:
    """Print the measurements for one capture file."""
    records = _load(path)
    if not records:
        raise SystemExit("empty capture")
    counts: dict[str, int] = defaultdict(int)
    for rec in records:
        counts[rec["kind"]] += 1
    span = records[-1]["t"] - records[0]["t"]
    print(f"{path}: {span / 60:.1f} min, {dict(counts)}")
    group_topics, members = _targets(records)
    _group_windows(records, group_topics)
    _tap_latency(records, members)
    _report_steps(records, step_limit)
    errors = [r for r in records if r["kind"] == "z2m_error"]
    print(f"Z2M error lines: {len(errors)}")


def main() -> None:
    """Parse arguments and run a mode."""
    parser = argparse.ArgumentParser(
        description="Measure the lights from their own reports."
    )
    parser.add_argument("--ha-config", default=mqtt_dump.DEFAULT_HA_CONFIG)
    parser.add_argument("--external-host", default=mqtt_dump.DEFAULT_EXTERNAL_HOST)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    sub = parser.add_subparsers(dest="mode", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("--seconds", type=float, default=None)
    cap.add_argument(
        "--out",
        type=Path,
        default=REPO / "captures" / f"capture-{time.strftime('%Y%m%d-%H%M%S')}.jsonl",
    )
    ana = sub.add_parser("analyze")
    ana.add_argument("file", type=Path)
    ana.add_argument("--step", type=int, default=2)
    get = sub.add_parser("get")
    get.add_argument("device")
    get.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()
    if args.mode == "analyze":
        run_analyze(args.file, args.step)
        return
    client = connect(args)
    if args.mode == "capture":
        run_capture(client, args.seconds, args.out)
    else:
        run_get(client, args.device, args.seconds)
    client.disconnect()


if __name__ == "__main__":
    main()
