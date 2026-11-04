"""Network-health checks computed from a ``live_probe.py`` capture.

Each check returns rows ``(label, target, passed)`` for ``health_report.py``.
What the bulbs did is read from their own composite-state reports
(``philips_raw``); what Light Man did is read from the ``/set`` sends.

Lessons from 2026-11-03 built in:

- A bulb waits about 10 s between reports, so a change within 10 s of its last
  report is reported late. Taps in that gap are skipped and counted, not failed.
- Only taps Light Man answers with a send to the tapped room are latency
  samples (paddle on/off go through the switch's own binding).
- An *off* held bulb takes the flood's value until its re-assert lands; that is
  invisible, so drift is checked on lit bulbs only.
- Mid-fade samples of short interactive fades and switch ramps are not jumps:
  report-to-report rates are judged by the class of the Light Man send that
  caused them, and changes with no Light Man send in between are skipped.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

BASE = "zigbee2mqtt"
CAP = 8
TAP_LATENCY_S = 1.0
TAP_SEND_WINDOW_S = 2.0
TAP_REPORT_WINDOW_S = 12.0
REPORT_CADENCE_S = 10.0
SPACING_FRACTION = 0.9
GLIDE_MIN_TRANSITION_S = 30.0
GLIDE_PCT_PER_S = 0.2
INTERACTIVE_PCT_PER_S = 50.0
REASSERT_MAX_TRANSITION_S = 0.5
REASSERT_AFTER_FLOOD_S = 1.0
DRIFT_SETTLE_S = 1.0
DRIFT_POINTS = 1
DRIFT_MIRED = 5
LOOK_ACTIONS = {"config_single", "config_double"}

Row = tuple[str, str, bool]


@dataclass
class House:
    """What the capture and Light Man's config say about the house."""

    groups: dict[str, set[str]]  # group /set topic -> member bulb names
    switch_room: dict[str, str]  # switch topic -> room /set topic
    sources: dict[str, str] = field(default_factory=dict)  # source -> consolidated

    def bulbs_of(self, topic: str) -> set[str]:
        """Bulb names a /set topic reaches (a group's members, or the device)."""
        if topic in self.groups:
            return self.groups[topic]
        return {topic.removeprefix(f"{BASE}/").removesuffix("/set")}

    def topics_of(self, bulb: str) -> set[str]:
        """Every /set topic that reaches ``bulb``."""
        out = {f"{BASE}/{bulb}/set"}
        out.update(t for t, members in self.groups.items() if bulb in members)
        return out


def load(
    path: Path, config: dict[str, Any], start: float | None, end: float | None
) -> tuple[list[dict[str, Any]], House]:
    """Return the records inside ``[start, end)`` and the house model."""
    records = [json.loads(line) for line in path.read_text().splitlines()]
    names: dict[str, str] = next(
        (r["names"] for r in records if r["kind"] == "devices"), {}
    )
    groups_rec = next((r for r in records if r["kind"] == "groups"), None)
    groups = {
        f"{BASE}/{g['friendly_name']}/set": {
            names.get(m["ieee_address"], m["ieee_address"]) for m in g["members"]
        }
        for g in (groups_rec["groups"] if groups_rec else [])
    }
    switch_room: dict[str, str] = {}
    for room_key, room in config["rooms"].items():
        lights = {f"{room_key}.{light['id']}": light for light in room["lights"]}
        for switch in room.get("switches", []):
            light = lights.get(switch["governs"])
            if light is not None:
                switch_room[switch["topic"]] = light["set_topic"]
    sources = {k: s["consolidated_topic"] for k, s in config["sources"].items()}
    lo = start if start is not None else float("-inf")
    hi = end if end is not None else float("inf")
    window = [r for r in records if r["kind"] in {"set", "action", "report"}]
    window = [r for r in window if lo <= r["t"] < hi]
    return window, House(groups, switch_room, sources)


def group_window(records: list[dict[str, Any]], house: House) -> list[Row]:
    """Worst count of group sends in any rolling 10 s window."""
    worst = 0
    window: list[float] = []
    for rec in records:
        if rec["kind"] != "set" or rec["topic"] not in house.groups:
            continue
        window = [t for t in window if rec["t"] - t < 10.0] + [rec["t"]]
        worst = max(worst, len(window))
    return [(f"capture: worst 10 s group window = {worst}", f"<= {CAP}", worst <= CAP)]


def source_spacing(
    records: list[dict[str, Any]], house: House, interval: float
) -> list[Row]:
    """Each source goes out at most once per interval (would have caught 0.8.2)."""
    rows: list[Row] = []
    floor = SPACING_FRACTION * interval
    for key, topic in sorted(house.sources.items()):
        times = [r["t"] for r in records if r["kind"] == "set" and r["topic"] == topic]
        close = sum(1 for a, b in itertools.pairwise(times) if b - a < floor)
        rows.append(
            (
                f"capture: {key} sends closer than {floor:.0f} s = {close} "
                f"(of {len(times)})",
                "0",
                close == 0,
            )
        )
    return rows


def _first(
    records: list[dict[str, Any]], i: int, until: float, match: Any
) -> dict[str, Any] | None:
    for rec in records[i + 1 :]:
        if rec["t"] > until:
            return None
        if match(rec):
            return rec
    return None


def tap_latency(
    records: list[dict[str, Any]], house: House, ignored: int = 0
) -> list[Row]:
    """Tap → Light Man's send → the tapped room's bulbs report it.

    ``ignored`` is Light Man's count of config taps it dropped on purpose (light
    off or on < 0.5 s, 0.8.5); that many unanswered config taps are expected.
    """
    sends: list[float] = []
    reported: list[float] = []
    skipped = 0
    look_without_send = 0
    for i, rec in enumerate(records):
        if rec["kind"] != "action" or rec["topic"] not in house.switch_room:
            continue
        room = house.switch_room[rec["topic"]]
        next_tap = _first(
            records,
            i,
            rec["t"] + TAP_SEND_WINDOW_S,
            lambda r, sw=rec["topic"]: r["kind"] == "action" and r["topic"] == sw,
        )
        until = next_tap["t"] if next_tap else rec["t"] + TAP_SEND_WINDOW_S
        send = _first(
            records,
            i,
            until,
            lambda r, room=room: r["kind"] == "set" and r["topic"] == room,
        )
        if send is None:
            look_without_send += rec["action"] in LOOK_ACTIONS
            continue  # paddle on/off: the switch's binding acts, Light Man does not
        sends.append(send["t"] - rec["t"])
        bulbs = house.bulbs_of(room)
        recent = any(
            r["kind"] == "report"
            and r["bulb"] in bulbs
            and 0 <= send["t"] - r["t"] < REPORT_CADENCE_S
            for r in records[max(0, i - 400) : i + 1]
        )
        if recent:
            skipped += 1
            continue
        first = next(
            (
                r
                for r in records[i + 1 :]
                if r["kind"] == "report"
                and r["bulb"] in bulbs
                and send["t"] <= r["t"] <= rec["t"] + TAP_REPORT_WINDOW_S
            ),
            None,
        )
        if first is not None:
            reported.append(first["t"] - rec["t"])
    slow = [x for x in reported if x > TAP_LATENCY_S]
    worst_send = max(sends, default=0.0)
    return [
        (
            f"capture: tap → Light Man send, n={len(sends)}, "
            f"max {worst_send * 1000:.0f} ms",
            "<= 300 ms",
            worst_send <= 0.3,  # the plan's interactive target
        ),
        (
            f"capture: tap → bulbs report it, "
            f"{len(reported) - len(slow)}/{len(reported)} within 1 s "
            f"({skipped} skipped: bulb reported < 10 s before)",
            "all measurable",
            not slow,
        ),
        (
            f"capture: config taps Light Man did not answer = {look_without_send} "
            f"(ignored on purpose: {ignored})",
            "<= ignored (light off)",
            look_without_send <= ignored,
        ),
    ]


def _transition(send: dict[str, Any]) -> float:
    return float(send["payload"].get("transition", 0) or 0)


def bulb_jumps(records: list[dict[str, Any]], house: House) -> list[Row]:
    """Report-to-report rate on lit bulbs, judged by the send that caused it."""
    last_report: dict[str, dict[str, Any]] = {}
    sends_to: dict[str, list[dict[str, Any]]] = {}  # topic -> sends so far
    jumps: list[str] = []
    checked = 0
    for rec in records:
        if rec["kind"] == "set":
            sends_to.setdefault(rec["topic"], []).append(rec)
            continue
        if rec["kind"] != "report":
            continue
        prev = last_report.get(rec["bulb"])
        last_report[rec["bulb"]] = rec
        if prev is None or not (prev["on"] and rec["on"]):
            continue
        causes = [
            s
            for t in house.topics_of(rec["bulb"])
            for s in sends_to.get(t, [])[-3:]
            if s["t"] <= rec["t"] and s["t"] + _transition(s) > prev["t"]
        ]  # sends whose fade overlaps the interval between the two reports
        if not causes:
            continue  # a switch ramp or a manual dim, not Light Man
        # the fastest class among the causes sets the limit
        limit = (
            GLIDE_PCT_PER_S
            if all(_transition(c) >= GLIDE_MIN_TRANSITION_S for c in causes)
            else INTERACTIVE_PCT_PER_S
        )
        dt = max(rec["t"] - prev["t"], 0.1)
        rate = abs(rec["brightness"] - prev["brightness"]) * 100 / 254 / dt
        checked += 1
        if rate > limit and abs(rec["brightness"] - prev["brightness"]) > 2:
            jumps.append(f"{rec['bulb']} {prev['brightness']}→{rec['brightness']}")
    label = (
        f"capture: lit-bulb jumps above their send class = {len(jumps)} (of {checked})"
    )
    if jumps:
        label += f": {', '.join(jumps[:3])}"
    return [(label, "0", not jumps)]


def held_drift(records: list[dict[str, Any]], house: House) -> list[Row]:
    """Lit bulbs of a re-asserted room stay on the look until the next flood."""
    floods = {t for t in house.sources.values()}
    sends = [r for r in records if r["kind"] == "set"]
    looks: list[tuple[float, float, str, dict[str, Any]]] = []  # start, end, topic
    for i, send in enumerate(sends):
        if send["topic"] in floods or i == 0:
            continue
        before = sends[i - 1]
        transition = float(send["payload"].get("transition", 0) or 0)
        if (
            before["topic"] in floods
            and send["t"] - before["t"] <= REASSERT_AFTER_FLOOD_S
            and 0 < transition <= REASSERT_MAX_TRANSITION_S
        ):
            nxt = next(
                (
                    s["t"]
                    for s in sends[i + 1 :]
                    if s["topic"] in {before["topic"], send["topic"]}
                ),
                float("inf"),
            )
            looks.append(
                (send["t"] + DRIFT_SETTLE_S, nxt, send["topic"], send["payload"])
            )
    worst = 0
    checked = 0
    for start, end, topic, look in looks:
        bulbs = house.bulbs_of(topic)
        for rec in records:
            if not (
                rec["kind"] == "report"
                and rec["on"]
                and rec["bulb"] in bulbs
                and start <= rec["t"] < end
            ):
                continue
            checked += 1
            d_br = abs(rec["brightness"] - look.get("brightness", rec["brightness"]))
            d_mired = 0
            if "color_temp" in look and rec["mirek"] is not None:
                d_mired = abs(rec["mirek"] - look["color_temp"])
            if d_br > DRIFT_POINTS or d_mired > DRIFT_MIRED:
                worst += 1
    return [
        (
            f"capture: lit held-bulb reports off the look = {worst} "
            f"(of {checked}, {len(looks)} re-asserts)",
            "0",
            worst == 0,
        )
    ]


def all_rows(
    path: Path,
    config: dict[str, Any],
    start: float | None,
    end: float | None,
    interval: float,
    ignored_config_taps: int = 0,
) -> tuple[list[Row], int]:
    """Every capture row, and how many Light Man switch taps were in the window."""
    records, house = load(path, config, start, end)
    taps = sum(
        1 for r in records if r["kind"] == "action" and r["topic"] in house.switch_room
    )
    rows = (
        group_window(records, house)
        + source_spacing(records, house, interval)
        + tap_latency(records, house, ignored_config_taps)
        + bulb_jumps(records, house)
        + held_drift(records, house)
    )
    return rows, taps
