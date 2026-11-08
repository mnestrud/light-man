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
HOLD_DELAY_S = 0.4  # const.DIM_START_DELAY_S: a held dim starts this long after *_held
OFF_STAGE_DELAY_S = 1.5  # const.OFF_STAGE_DELAY_S: a released hold's send after an off
OFF_ACTIONS = {"down_single"}
REPORT_LAG_S = 1.0  # a bulb's report can trail the send that moved it
FADE_HEADROOM = 1.5  # a fade may run this much faster than linear between reports
RESTART_SEND_S = 120.0  # Light Man's start-up send lands within this of HA's start
IGNORED_TAP_WINDOW_S = 1.0  # an ignored config tap's red blink follows within this
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
    switch_topics: dict[str, set[str]] = field(default_factory=dict)  # -> governed

    def switch_bulbs(self, switch: str) -> set[str]:
        """Bulb names every light a switch governs reaches."""
        return {b for t in self.switch_topics.get(switch, ()) for b in self.bulbs_of(t)}

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
    switch_topics: dict[str, set[str]] = {}
    for room_key, room in config["rooms"].items():
        lights = {f"{room_key}.{light['id']}": light for light in room["lights"]}
        for switch in room.get("switches", []):
            governs = switch["governs"]
            refs = governs if isinstance(governs, list) else [governs]
            light = lights.get(refs[0])
            if light is not None:
                switch_room[switch["topic"]] = light["set_topic"]
            topics = switch_topics.setdefault(switch["topic"], set())
            for ref in refs:
                if ref in lights:  # a light ref
                    topics.add(lights[ref]["set_topic"])
                elif ref == room_key:  # the whole room
                    topics.update(light["set_topic"] for light in room["lights"])
    sources = {k: s["consolidated_topic"] for k, s in config["sources"].items()}
    lo = start if start is not None else float("-inf")
    hi = end if end is not None else float("inf")
    window = [r for r in records if r["kind"] in {"set", "action", "report"}]
    window = [r for r in window if lo <= r["t"] < hi]
    return window, House(groups, switch_room, sources, switch_topics)


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


def _restart_sends(times: list[float], starts: list[float]) -> set[float]:
    """Return the first send after each HA start (Light Man sends at once)."""
    out: set[float] = set()
    for start in starts:
        first = next((t for t in times if t >= start), None)
        if first is not None and first - start <= RESTART_SEND_S:
            out.add(first)
    return out


def source_spacing(
    records: list[dict[str, Any]],
    house: House,
    interval: float,
    starts: list[float] | None = None,
) -> list[Row]:
    """Each source goes out at most once per interval (would have caught 0.8.2).

    A regular send's fade is the interval it was sent with (120 s until 0.8.13,
    240 s since), so each gap is judged by the earlier send's own fade; a short
    send falls back to ``interval``. The first send after each HA start is
    off-cycle (Light Man sends every source at once on start): the gaps on
    either side of it are left out and counted.
    """
    rows: list[Row] = []
    for key, topic in sorted(house.sources.items()):
        sends = [r for r in records if r["kind"] == "set" and r["topic"] == topic]
        restart = _restart_sends([r["t"] for r in sends], starts or [])
        close = excused = 0
        for a, b in itertools.pairwise(sends):
            fade = _transition(a)
            floor = SPACING_FRACTION * (
                fade if fade >= GLIDE_MIN_TRANSITION_S else interval
            )
            if b["t"] - a["t"] >= floor:
                continue
            if a["t"] in restart or b["t"] in restart:
                excused += 1
            else:
                close += 1
        rows.append(
            (
                f"capture: {key} sends closer than their interval = {close} "
                f"(of {len(sends)}; {excused} next to a restart send left out)",
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


def _red_blink(records: list[dict[str, Any]], i: int, switch: str) -> bool:
    """Return True if Light Man blinked a switch's top LED red right after tap ``i``.

    That blink is how Light Man shows a config tap it ignores on purpose (light
    off or on < 0.5 s, 0.8.5).
    """
    tap = records[i]
    blink = _first(
        records,
        i,
        tap["t"] + IGNORED_TAP_WINDOW_S,
        lambda r: (
            r["kind"] == "set"
            and r["topic"] == f"{switch}/set"
            and any(
                (r["payload"].get(key) or {}).get("effect") == "fast_blink"
                and r["payload"][key].get("color") == 0
                for key in ("individual_led_effect", "led_effect")  # top LED / bar
            )
        ),
    )
    return blink is not None


def _lights_off(records: list[dict[str, Any]], i: int, bulbs: set[str]) -> bool:
    """Return True if every governed bulb's last report before tap ``i`` was off.

    Before 0.8.7 an ignored config tap had no red blink; the light being off is
    then the only sign it was ignored on purpose.
    """
    seen: dict[str, bool] = {}
    for rec in reversed(records[max(0, i - 2000) : i]):
        if rec["kind"] == "report" and rec["bulb"] in bulbs:
            seen.setdefault(rec["bulb"], rec["on"])
    return bool(seen) and not any(seen.values())


def _taps(
    records: list[dict[str, Any]], house: House
) -> list[tuple[int, dict[str, Any], dict[str, Any] | None]]:
    """Each Light Man switch tap with Light Man's send to its room (None: no send)."""
    out: list[tuple[int, dict[str, Any], dict[str, Any] | None]] = []
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
        out.append((i, rec, send))
    return out


def tap_latency(records: list[dict[str, Any]], house: House) -> list[Row]:
    """Tap → Light Man's send → the tapped room's bulbs report it.

    A down tap that released a hold is answered by the curve send 1.5 s later
    (0.8.12); that delay is taken off, and the bulbs are off then, so no report
    is measured for it.
    """
    sends: list[float] = []
    reported: list[float] = []
    skipped = 0
    off_stages = 0
    look_without_send = 0
    ignored = 0
    for i, rec, send in _taps(records, house):
        room = house.switch_room[rec["topic"]]
        if send is None:
            if rec["action"] in LOOK_ACTIONS:
                if _red_blink(records, i, rec["topic"]) or _lights_off(
                    records, i, house.switch_bulbs(rec["topic"])
                ):
                    ignored += 1
                else:
                    look_without_send += 1
            continue  # paddle on/off: the switch's binding acts, Light Man does not
        delay = HOLD_DELAY_S if rec["action"].endswith("_held") else 0.0
        if rec["action"] in OFF_ACTIONS:
            sends.append(send["t"] - rec["t"] - OFF_STAGE_DELAY_S)
            off_stages += 1
            continue  # the bulbs are off: nothing to see in their reports
        sends.append(send["t"] - rec["t"] - delay)
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
            reported.append(first["t"] - rec["t"] - delay)
    slow = [x for x in reported if x > TAP_LATENCY_S]
    worst_send = max(sends, default=0.0)
    return [
        (
            f"capture: tap → Light Man send, n={len(sends)}, "
            f"max {worst_send * 1000:.0f} ms ({off_stages} after an off, "
            f"less the {OFF_STAGE_DELAY_S} s delay)",
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
            f"(ignored on purpose, red blink or light off: {ignored})",
            "0",
            look_without_send == 0,
        ),
    ]


def _transition(send: dict[str, Any]) -> float:
    return float(send["payload"].get("transition", 0) or 0)


def _paddle_times(
    records: list[dict[str, Any]], house: House
) -> dict[str, list[float]]:
    """Bulb name -> the times a switch governing it acted."""
    times: dict[str, list[float]] = {}
    for rec in records:
        if rec["kind"] == "action":
            for bulb in house.switch_bulbs(rec["topic"]):
                times.setdefault(bulb, []).append(rec["t"])
    return times


def _levels_at_sends(
    records: list[dict[str, Any]], house: House
) -> dict[str, dict[float, int]]:
    """Bulb name -> {send time: the bulb's last reported level when it went out}."""
    last: dict[str, int] = {}
    out: dict[str, dict[float, int]] = {}
    for rec in records:
        if rec["kind"] == "report":
            last[rec["bulb"]] = rec["brightness"]
        elif rec["kind"] == "set":
            for bulb in house.bulbs_of(rec["topic"]):
                if bulb in last:
                    out.setdefault(bulb, {})[rec["t"]] = last[bulb]
    return out


def _rate_limit(causes: list[dict[str, Any]], level_at: dict[float, int]) -> float:
    """Return the fastest rate the causing sends allow, in percent per second.

    A send fades from the bulb's level at the time it went out to its target
    over its transition, so its own pace is allowed (with headroom), and never
    less than its class: a glide 0.2 %/s, an interactive send 50 %/s.
    """
    allowed = 0.0
    for send in causes:
        fade = _transition(send)
        floor = (
            GLIDE_PCT_PER_S if fade >= GLIDE_MIN_TRANSITION_S else INTERACTIVE_PCT_PER_S
        )
        target = send["payload"].get("brightness")
        start = level_at.get(send["t"])
        pace = 0.0
        if target is not None and start is not None and fade > 0:
            pace = abs(target - start) * 100 / 254 / fade * FADE_HEADROOM
        allowed = max(allowed, floor, pace)
    return allowed


def bulb_jumps(records: list[dict[str, Any]], house: House) -> list[Row]:
    """Report-to-report rate on lit bulbs, judged by the send that caused it."""
    last_report: dict[str, dict[str, Any]] = {}
    first_lit: set[str] = set()  # bulbs whose last report was the first after off
    sends_to: dict[str, list[dict[str, Any]]] = {}  # topic -> sends so far
    jumps: list[str] = []
    checked = 0
    paddled = _paddle_times(records, house)
    level_at = _levels_at_sends(records, house)
    for rec in records:
        if rec["kind"] == "set":
            sends_to.setdefault(rec["topic"], []).append(rec)
            continue
        if rec["kind"] != "report":
            continue
        prev = last_report.get(rec["bulb"])
        last_report[rec["bulb"]] = rec
        after_on = rec["bulb"] in first_lit
        first_lit.discard(rec["bulb"])
        if rec["on"] and (prev is None or not prev["on"]):
            first_lit.add(rec["bulb"])
        if prev is None or not (prev["on"] and rec["on"]) or after_on:
            continue  # the first lit report can land mid power-on fade
        if any(prev["t"] < t <= rec["t"] for t in paddled.get(rec["bulb"], [])):
            continue  # a paddle acted in between: its binding moved the bulb
        causes = [
            s
            for t in house.topics_of(rec["bulb"])
            for s in sends_to.get(t, [])[-3:]
            if s["t"] <= rec["t"] and s["t"] + _transition(s) > prev["t"] - REPORT_LAG_S
        ]  # sends whose fade overlaps the interval between the two reports
        if not causes:
            continue  # a switch ramp or a manual dim, not Light Man
        limit = _rate_limit(causes, level_at.get(rec["bulb"], {}))
        dt = max(rec["t"] - prev["t"], 0.1)
        rate = abs(rec["brightness"] - prev["brightness"]) * 100 / 254 / dt
        checked += 1
        if rate > limit and abs(rec["brightness"] - prev["brightness"]) > 2:
            jumps.append(f"{rec['bulb']} {prev['brightness']}→{rec['brightness']}")
    label = (
        f"capture: lit-bulb jumps faster than the causing send = {len(jumps)} "
        f"(of {checked})"
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
    starts: list[float] | None = None,
    skip: list[tuple[float, float]] | None = None,
) -> tuple[list[Row], list[float]]:
    """Every capture row, and the times of the taps Light Man answered with a send.

    ``skip`` windows (test sessions) are left out of every row.
    """
    records, house = load(path, config, start, end)
    records = [
        r for r in records if not any(lo <= r["t"] < hi for lo, hi in skip or [])
    ]
    taps = [rec["t"] for _i, rec, send in _taps(records, house) if send is not None]
    rows = (
        group_window(records, house)
        + source_spacing(records, house, interval, starts)
        + tap_latency(records, house)
        + bulb_jumps(records, house)
        + held_drift(records, house)
    )
    return rows, taps
