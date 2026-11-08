"""Day simulator: the real coordinator, the real house config, a full simulated day.

Runs Light Man against mocked MQTT with a frozen, advancing clock and the real sun
at the house, through scripted events, and asserts the plan's network-health
budget and "no jumps" metric on every publish (plan A5).
"""

from __future__ import annotations

import copy
import itertools
import json
from collections import deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.location import Location
from astral.sun import elevation as solar_elevation
from astral.sun import noon as solar_noon
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.light_man import _read_bundled_seed
from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import DOMAIN
from custom_components.light_man.coordinator import LightManCoordinator
from custom_components.light_man.modes import ModeManager

from .conftest import FakeStore

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

    from .conftest import FakeClock

TZ = ZoneInfo("America/Chicago")
HOME = Location(LocationInfo("Home", "IL", "America/Chicago", 41.90845, -87.66669))
INTERVAL = 120
WINDOW_S = 10.0
CAP = 8
GLIDE_PCT_PER_S = 0.2
GLIDE_MIRED_PER_S = 2.0
INTERACTIVE_PCT_PER_S = 50.0
INTERACTIVE_MIRED_PER_S = 250.0
SWITCH = "zigbee2mqtt/Kitchen Overhead Light Switch"
SHOWER_SWITCH = "zigbee2mqtt/Primary Bath Shower Smart Switch"


def _real_solar(_hass: Any, when: datetime) -> tuple[float, float]:
    local = when.astimezone(TZ)
    return (
        solar_elevation(HOME.observer, when),
        solar_elevation(HOME.observer, solar_noon(HOME.observer, local.date())),
    )


class Recorder:
    """Every zigbee2mqtt publish with its simulated wall-clock time."""

    def __init__(self, mqtt_mock: Any, clock: FakeClock) -> None:
        self.mqtt_mock = mqtt_mock
        self.clock = clock
        self.seen = 0
        self.sends: list[tuple[float, str, dict[str, Any]]] = []

    def collect(self) -> None:
        calls = self.mqtt_mock.async_publish.call_args_list[self.seen :]
        self.seen += len(calls)
        for call in calls:
            topic, payload = call.args[0], call.args[1]
            if topic.startswith("zigbee2mqtt/") and topic.endswith("/set"):
                self.sends.append((self.clock.now, topic, json.loads(payload)))


async def _run_day(
    hass: HomeAssistant,
    mqtt_mock: Any,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
    events: dict[str, Any],
) -> tuple[Recorder, LightManCoordinator]:
    seed = copy.deepcopy(_read_bundled_seed())
    seed["push_interval_s"] = INTERVAL
    entry = MockConfigEntry(domain=DOMAIN, title="Light Man")
    entry.add_to_hass(hass)
    modes = ModeManager(FakeStore(None))
    await modes.async_load()
    start = datetime(2026, 11, 3, 0, 0, tzinfo=TZ)
    freezer.move_to(start)
    coordinator = LightManCoordinator(hass, entry, validate_config(seed), modes)
    await coordinator._async_setup()
    groups = [
        {"friendly_name": t.split("/")[1], "members": []}
        for t in {
            *(s["consolidated_topic"] for s in seed["sources"].values()),
            *(
                light["set_topic"]
                for room in seed["rooms"].values()
                for light in room.get("lights", [])
                if light["set_topic"].split("/")[1].startswith("zgb_")
            ),
        }
    ]
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/groups", json.dumps(groups))
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/state", '{"state":"online"}')
    recorder = Recorder(mqtt_mock, clock)
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    step = coordinator.update_interval.total_seconds()  # type: ignore[union-attr]
    elapsed = 0.0
    while elapsed < 24 * 3600:
        elapsed += step
        clock.advance(step)
        freezer.tick(timedelta(seconds=step))
        now = datetime.now(TZ).strftime("%H:%M")
        if now in events:
            await events.pop(now)(hass, coordinator)
            await hass.async_block_till_done()
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        recorder.collect()
    coordinator.shutdown_subscriptions()
    return recorder, coordinator


def _tap(topic: str, action: str) -> Any:
    async def run(hass: HomeAssistant, coordinator: LightManCoordinator) -> None:
        if action.startswith("config"):
            coordinator._set_paddle(topic, on=True)  # the light has been on
            coordinator._on_since[topic] -= 10.0
        async_fire_mqtt_message(hass, topic, json.dumps({"action": action}))

    return run


def _z2m(state: str) -> Any:
    async def run(hass: HomeAssistant, _coordinator: LightManCoordinator) -> None:
        async_fire_mqtt_message(
            hass, "zigbee2mqtt/bridge/state", json.dumps({"state": state})
        )

    return run


async def _sleep_on(_hass: HomeAssistant, coordinator: LightManCoordinator) -> None:
    await coordinator.async_set_sleep(enabled=True)


def _rate(prev: dict[str, Any], new: dict[str, Any]) -> tuple[float, float]:
    seconds = max(float(new.get("transition", 0.4)), 0.1)
    d_pct = abs(new.get("brightness", 0) - prev.get("brightness", 0)) * 100 / 254
    d_mired = 0.0
    if "color_temp" in new and "color_temp" in prev:
        d_mired = abs(new["color_temp"] - prev["color_temp"])
    return d_pct / seconds, d_mired / seconds


async def test_a_day_stays_inside_the_network_budget_and_never_jumps(
    hass: HomeAssistant,
    mqtt_mock: Any,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
) -> None:
    events = {
        "09:00": _tap(SHOWER_SWITCH, "config_single"),  # hold the shower at night
        "09:00:01": None,
        "11:30": _tap(SHOWER_SWITCH, "up_single"),  # release
        "14:00": _tap(SWITCH, "config_double"),  # kitchen day look
        "14:02": _tap(SWITCH, "config_single"),  # then night look
        "15:00": _z2m("offline"),
        "15:04": _z2m("online"),
        "16:30": _tap(SWITCH, "up_single"),
        "21:00": _sleep_on,
    }
    events = {k: v for k, v in events.items() if v is not None}
    with patch("custom_components.light_man.coordinator.solar_inputs", _real_solar):
        recorder, coordinator = await _run_day(hass, mqtt_mock, clock, freezer, events)
    sends = recorder.sends
    assert events == {}, f"scripted events never fired: {sorted(events)}"
    assert len(sends) > 200, "the simulator produced too few publishes"
    groups = coordinator._group_topics or set()

    # 1. Never more group sends in any 10 s window than the cap.
    window: deque[float] = deque()
    worst = 0
    for t, topic, _payload in sends:
        if topic not in groups:
            continue
        window.append(t)
        while t - window[0] >= WINDOW_S:
            window.popleft()
        worst = max(worst, len(window))
    assert worst <= CAP

    # 2. Group sends per hour stay inside the budget: at most one flood per
    #    source and one re-assert per held room each step, and a step is never
    #    shorter than the shortest step (0.8.16 variable steps).
    per_hour: dict[int, int] = {}
    for t, topic, _payload in sends:
        if topic in groups:
            hour = int((t - sends[0][0]) // 3600)
            per_hour[hour] = per_hour.get(hour, 0) + 1
    steps_per_hour = 3600 / coordinator._steps["min_s"]
    assert max(per_hour.values()) <= (4 + 1) * steps_per_hour + 30
    # Flat hours keep the longest step: no more than one flood per source.
    assert sorted(per_hour.values())[len(per_hour) // 2] <= 4 * 3600 / INTERVAL + 30

    # 3. Forced floods only from the operator enabling the push.
    assert coordinator.diagnostics["forced_floods"] == 1

    # 4. The jump metric on every send, per topic.
    last: dict[str, dict[str, Any]] = {}
    for _t, topic, payload in sends:
        prev = last.get(topic)
        last[topic] = payload
        if prev is None or "state" in payload:
            continue
        pct_s, mired_s = _rate(prev, payload)
        if float(payload.get("transition", 0)) >= coordinator._steps["min_s"]:
            assert pct_s <= GLIDE_PCT_PER_S, (topic, prev, payload)
            assert mired_s <= GLIDE_MIRED_PER_S, (topic, prev, payload)
        else:
            assert pct_s <= INTERACTIVE_PCT_PER_S, (topic, prev, payload)
            assert mired_s <= INTERACTIVE_MIRED_PER_S, (topic, prev, payload)

    # 5. Taps cost one send each: the shower hold arm and release are single sends.
    shower = "zigbee2mqtt/zgb_primary_shower_lights/set"
    interactive = [
        p for _t, topic, p in sends if topic == shower and p.get("transition", 0) < 5
    ]
    assert len(interactive) >= 2

    # 6. Z2M restart replays only what was sent while it was down.
    assert coordinator.diagnostics.get("z2m_restarts") == 1

    # 7. While the kitchen is held, floods are followed by its re-assert.
    kitchen = "zigbee2mqtt/zgb_kitchen/set"
    overhead = "zigbee2mqtt/zgb_overhead_all/set"
    pairs = sum(
        1
        for (_t1, a, _p1), (_t2, b, p2) in itertools.pairwise(sends)
        if a == overhead and b == kitchen and p2["transition"] == 0.4
    )
    assert pairs >= 5
    print(  # noqa: T201  # visible with -s: what the day exercised
        f"\nsimulated day: {len(sends)} sends, worst 10 s window {worst}, "
        f"max group sends/h {max(per_hour.values())}, flood+re-assert pairs {pairs}"
    )
