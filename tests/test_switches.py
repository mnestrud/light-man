"""0.8.5: config-tap rule, 00:00 expiry, hallway pair, switch sync, sweep dedup."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.light_man.adaptive import night_look
from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import CONFIG_TAP_MIN_ON_S, DOMAIN
from custom_components.light_man.coordinator import (
    LightManCoordinator,
    _strip,
    _target_to_payload,
)
from custom_components.light_man.modes import OVERRIDES_KEY, ModeManager
from custom_components.light_man.push import plan_source

from .conftest import (
    HALL_CENTER_SET,
    HALL_OFF,
    HALL_UP,
    LR_SET,
    LR_SWITCH,
    MMWAVE_EAST,
    MMWAVE_WEST,
    SEED,
    FakeStore,
    published,
    tick,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant

    from .conftest import FakeClock

EAST_SET = f"{MMWAVE_EAST}/set"


def _hall_seed() -> dict[str, Any]:
    seed = copy.deepcopy(SEED)
    seed["rooms"]["hall"]["switches"] = [
        {"topic": MMWAVE_EAST, "governs": "hall"},
        {"topic": MMWAVE_WEST, "governs": "hall"},
    ]
    zone = seed["occupancy_zones"]["hallway"]
    zone["sync_switches"] = [MMWAVE_EAST]
    zone["override_switches"] = [MMWAVE_EAST, MMWAVE_WEST]
    zone["override_idle_s"] = 600
    return seed


async def _coordinator(
    hass: HomeAssistant, seed: dict[str, Any], store: FakeStore | None = None
) -> LightManCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, title="Light Man")
    entry.add_to_hass(hass)
    modes = ModeManager(store or FakeStore(None))
    await modes.async_load()
    coord = LightManCoordinator(hass, entry, validate_config(seed), modes)
    await coord._async_setup()
    await coord.async_refresh()
    await hass.async_block_till_done()
    return coord


@pytest.fixture
async def hall(
    hass: HomeAssistant, mqtt_mock: Any
) -> AsyncIterator[LightManCoordinator]:
    coord = await _coordinator(hass, _hall_seed())
    await coord.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    yield coord
    for cancel in coord._override_timers.values():
        cancel()  # idle clocks a test left running


def _fire(hass: HomeAssistant, topic: str, payload: dict[str, Any]) -> None:
    async_fire_mqtt_message(hass, topic, json.dumps(payload))


async def _instant(*_a: Any, **_k: Any) -> None:
    """Sweep stage delays run instantly."""


async def _send(
    hass: HomeAssistant, coord: LightManCoordinator, topic: str, payload: Any
) -> None:
    with patch("custom_components.light_man.coordinator.asyncio.sleep", _instant):
        _fire(hass, topic, payload)
        await hass.async_block_till_done()
        pending = [t for t in coord._sweep_tasks.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await hass.async_block_till_done()


def _night(coord: LightManCoordinator, source: str) -> dict[str, Any]:
    profile = coord._config["sources"][source]["profile"]
    return dict(_strip(_target_to_payload(night_look(profile), 0.0)))


# --- config tap needs the light on (house-wide) ------------------------------


async def test_config_tap_on_an_off_or_unknown_switch_is_ignored(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    await _send(hass, hall, LR_SWITCH, {"action": "config_single"})  # unknown
    _fire(hass, LR_SWITCH, {"state": "OFF"})
    await hass.async_block_till_done()
    await _send(hass, hall, LR_SWITCH, {"action": "config_double"})  # off
    assert not hall._modes.is_held("living_room.overhead")
    assert published(mqtt_mock) == {}
    assert hall.diagnostics["ignored_config_taps"] == 2


async def test_config_tap_right_after_paddle_on_is_ignored_then_accepted(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _send(hass, hall, LR_SWITCH, {"action": "up_single"})
    clock.advance(CONFIG_TAP_MIN_ON_S - 0.1)
    await _send(hass, hall, LR_SWITCH, {"action": "config_single"})
    assert not hall._modes.is_held("living_room.overhead")
    clock.advance(0.2)
    # the switch's own ON report while already on keeps the original on-time
    _fire(hass, LR_SWITCH, {"state": "ON"})
    await hass.async_block_till_done()
    await _send(hass, hall, LR_SWITCH, {"action": "config_single"})
    assert hall._modes.mode_of("living_room.overhead") == "night"
    assert LR_SET in published(mqtt_mock)


# --- midnight = 00:00 local ----------------------------------------------------


async def test_holds_expire_at_local_midnight_across_dst(
    hass: HomeAssistant, hall: LightManCoordinator
) -> None:
    await hass.config.async_set_time_zone("America/Chicago")
    tz = dt_util.get_default_time_zone()
    late = datetime(2026, 10, 31, 23, 59, tzinfo=tz)
    assert hall._next_midnight(late) == datetime(2026, 11, 1, 0, 0, tzinfo=tz)
    # 1 Nov 2026 is 25 h long (CDT -> CST): midnight is still 00:00 local
    nxt = hall._next_midnight(datetime(2026, 11, 1, 12, 0, tzinfo=tz))
    assert dt_util.as_local(nxt).hour == 0
    assert nxt.utcoffset() == timedelta(hours=-6)


# --- hallway pair: one look for the whole room ------------------------------


async def test_room_switch_config_tap_holds_the_room_on_its_source_topic(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    _fire(hass, MMWAVE_WEST, {"state": "ON"})
    await hass.async_block_till_done()
    clock.advance(1.0)
    await _send(hass, hall, MMWAVE_WEST, {"action": "config_single"})
    assert hall._modes.mode_of("hall.up_main") == "night"
    assert hall._modes.mode_of("hall.center") == "night"
    pubs = published(mqtt_mock)
    assert set(pubs) == {HALL_UP}  # one groupcast, not one per light
    sent = dict(pubs[HALL_UP])
    sent.pop("transition")
    assert sent == _night(hall, "hallway_up")
    # every tick after that: the look on the consolidated topic, deduped
    mqtt_mock.async_publish.reset_mock()
    for _ in range(3):
        await tick(hass, hall, clock)
    assert HALL_UP not in published(mqtt_mock)
    assert hall.diagnostics["addressing"]["hallway_up"]["mode"] == "held"


async def test_room_switch_ignores_unknown_taps_and_holds_while_off(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    for action in ("up_held", "up_release", "down_double"):
        await _send(hass, hall, MMWAVE_EAST, {"action": action})
    assert hall._modes.held_rooms() == set()
    assert hall._modes.overrides() == {}
    assert published(mqtt_mock) == {}


async def test_room_switch_hold_dims_the_whole_curve_and_holds_every_light(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _send(hass, hall, MMWAVE_EAST, {"state": "ON"})
    start = hall._shown(HALL_UP)
    assert start is not None
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, hall, MMWAVE_EAST, {"action": "down_held"})
    pubs = published(mqtt_mock)
    assert pubs[HALL_UP]["brightness"] == 3  # one fade on the curve's own group
    assert HALL_CENTER_SET not in pubs
    assert hall._modes.held_rooms() == {"hall.up_main", "hall.center"}
    clock.advance(0.5)
    await _send(hass, hall, MMWAVE_EAST, {"action": "down_release"})
    look = hall._modes.look_of("hall.center")
    assert look is not None
    assert 3 < look["brightness"] < start["brightness"]
    assert hall._modes.look_of("hall.up_main") == look
    assert published(mqtt_mock)[HALL_UP]["brightness"] == look["brightness"]


def test_plan_source_sends_a_uniform_whole_source_hold_on_the_group() -> None:
    source: Any = {
        "consolidated_topic": "g/set",
        "rooms": {"a": {"set_topic": "a/set"}, "b": {"set_topic": "b/set"}},
    }
    look: Any = {"brightness": 10}
    other: Any = {"brightness": 20}
    curve: Any = {"brightness": 99}
    mode, sends = plan_source(
        source, adaptive_payload=curve, looks={"a": look, "b": look}
    )
    assert (mode, [s.topic for s in sends]) == ("held", ["g/set"])
    mode, _ = plan_source(source, adaptive_payload=curve, looks={"a": look, "b": other})
    assert mode == "reassert"
    mode, _ = plan_source(source, adaptive_payload=curve, looks={"a": look})
    assert mode == "reassert"


# --- hallway pair: motion override ------------------------------------------


async def test_up_tap_overrides_motion_until_an_hour_without_presence(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": True})
    await _send(hass, hall, MMWAVE_EAST, {"action": "up_single"})
    assert "hallway" in hall._modes.overrides()
    assert hall.data["held"]["hallway"]["mode"] == "override"
    mqtt_mock.async_publish.reset_mock()
    # motion comes and goes: no sweep, no clear
    await _send(hass, hall, MMWAVE_WEST, {"occupancy": True})
    await _send(hass, hall, MMWAVE_WEST, {"occupancy": False})
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    assert published(mqtt_mock) == {}
    # presence again before the idle time restarts the clock
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=300))
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": True})
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=500))
    await hass.async_block_till_done()
    assert "hallway" in hall._modes.overrides()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1300))
    await hass.async_block_till_done()
    assert hall._modes.overrides() == {}
    pubs = published(mqtt_mock)
    # the east switch showed ON (its up tap): it is told OFF, its binding acts
    assert pubs == {EAST_SET: {"state": "OFF"}}


async def test_override_end_with_presence_waits_for_the_normal_clear(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    await _send(hass, hall, MMWAVE_EAST, {"action": "up_single"})
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": True})
    await hall.async_release_hold("hallway")  # the service, on a zone
    await hass.async_block_till_done()
    assert hall._modes.overrides() == {}
    assert published(mqtt_mock) == {}  # someone is there: nothing turns off
    assert hall.is_known_room("hallway")
    assert not hall.is_known_room("porch")  # a zone without override switches


async def test_down_tap_ends_the_override_and_the_hold(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _send(hass, hall, MMWAVE_EAST, {"action": "up_single"})
    clock.advance(1.0)
    await _send(hass, hall, MMWAVE_EAST, {"action": "config_single"})
    assert hall._modes.is_held("hall.center")
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, hall, MMWAVE_EAST, {"action": "down_single"})
    assert hall._modes.overrides() == {}
    assert hall._modes.held_rooms() == set()
    assert published(mqtt_mock) == {}  # the binding turned the lights off
    await tick(hass, hall, clock)
    assert HALL_UP in published(mqtt_mock)  # the curve is staged again
    # a person still standing there does not re-trigger the sweep
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    assert HALL_OFF in published(mqtt_mock)  # the normal clear (switch is off)


async def test_up_tap_releases_a_room_look_back_to_the_curve(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _send(hass, hall, MMWAVE_EAST, {"action": "up_single"})
    clock.advance(1.0)
    await _send(hass, hall, MMWAVE_EAST, {"action": "config_single"})
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, hall, MMWAVE_WEST, {"action": "up_single"})
    assert hall._modes.held_rooms() == set()
    assert published(mqtt_mock)[HALL_UP]["brightness"] == 229


async def test_override_expires_at_midnight_and_clears_a_vacant_zone(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _send(hass, hall, MMWAVE_EAST, {"action": "up_single"})
    await hall._modes.set_override("hallway", expires_at=dt_util.utcnow())
    mqtt_mock.async_publish.reset_mock()
    await tick(hass, hall, clock)
    assert hall._modes.overrides() == {}
    assert published(mqtt_mock)[EAST_SET] == {"state": "OFF"}


async def test_clear_holds_ends_overrides(
    hass: HomeAssistant, hall: LightManCoordinator
) -> None:
    await _send(hass, hall, MMWAVE_EAST, {"action": "up_single"})
    await hall.async_clear_holds()
    assert hall._modes.overrides() == {}
    assert hall._override_timers == {}


async def test_restored_override_restarts_its_idle_clock_and_unknown_is_dropped(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    later = (dt_util.utcnow() + timedelta(hours=3)).isoformat()
    store = FakeStore({OVERRIDES_KEY: {"hallway": later, "gone": later}})
    coord = await _coordinator(hass, _hall_seed(), store)
    assert set(coord._modes.overrides()) == {"hallway"}
    assert "hallway" in coord._override_timers
    coord.shutdown_subscriptions()
    assert coord._override_timers == {}


async def test_idle_timer_after_unload_does_nothing(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    await _send(hass, hall, MMWAVE_EAST, {"action": "up_single"})
    hall._cancel_idle_timer("hallway")
    hall._closed = True
    await hall._override_idle("hallway", dt_util.utcnow())
    assert "hallway" in hall._modes.overrides()
    assert published(mqtt_mock) == {}


# --- switch sync (F) and sweep dedup (#10a) ----------------------------------


async def test_sweep_turns_the_sync_switch_on_and_its_echo_is_not_a_press(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    _fire(hass, MMWAVE_EAST, {"state": "OFF"})
    await hass.async_block_till_done()
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": True})
    pubs = published(mqtt_mock)
    assert pubs[EAST_SET] == {"state": "ON"}
    assert HALL_UP in pubs and HALL_CENTER_SET in pubs
    _fire(hass, MMWAVE_EAST, {"state": "ON"})  # the echo
    await hass.async_block_till_done()
    assert hall._modes.overrides() == {}  # a sync write is not a tap
    assert hall._paddle_on[MMWAVE_EAST] is True
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    assert published(mqtt_mock) == {EAST_SET: {"state": "OFF"}}


async def test_clear_with_an_unknown_switch_state_turns_the_group_off(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    hall._expect_echo.clear()
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": True})
    _fire(hass, MMWAVE_EAST, {"state": "OFF"})  # the ON write never landed
    await hass.async_block_till_done()
    hall._expect_echo.clear()
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    pubs = published(mqtt_mock)
    assert pubs[HALL_OFF] == {"state": "OFF", "transition": 1.5}
    assert EAST_SET not in pubs  # already showing OFF


async def test_second_sensor_does_not_re_light_what_the_first_lit(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": True})
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, hall, MMWAVE_WEST, {"occupancy": True})  # sweeps hall.center
    assert HALL_CENTER_SET not in published(mqtt_mock)
    # after the zone clears, the next episode lights again
    await _send(hass, hall, MMWAVE_WEST, {"occupancy": False})
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, hall, MMWAVE_WEST, {"occupancy": True})
    assert HALL_CENTER_SET in published(mqtt_mock)


async def test_paddle_off_lets_the_sweep_light_that_room_again(
    hass: HomeAssistant, coordinator: LightManCoordinator
) -> None:
    coordinator._zone_lit["hallway"] = {LR_SET, HALL_UP}
    coordinator._forget_lit("living_room.overhead")
    assert coordinator._zone_lit["hallway"] == {HALL_UP}


# --- startup reads -------------------------------------------------------------


async def test_startup_reads_each_switch_that_has_not_reported(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    hall._paddle_on[LR_SWITCH] = True
    await hall._read_switches()
    gets = [
        c.args[0]
        for c in mqtt_mock.async_publish.call_args_list
        if c.args[0].endswith("/get")
    ]
    assert f"{LR_SWITCH}/get" not in gets
    assert f"{MMWAVE_EAST}/get" in gets and f"{MMWAVE_WEST}/get" in gets
    mqtt_mock.async_publish.reset_mock()
    hall._closed = True
    await hall._read_switches()
    assert mqtt_mock.async_publish.call_count == 0


# --- loader + modes ---------------------------------------------------------------


def test_loader_room_switch_and_zone_lists() -> None:
    validated = validate_config(_hall_seed())
    assert validated.issues == []
    room, lights = validated.room_switch_map[MMWAVE_EAST]
    assert room == "hall"
    assert {ref for _src, ref in lights} == {"hall.up_main", "hall.center"}
    zone = validated.config["occupancy"]["hallway"]
    assert zone["override_idle_s"] == 600.0
    assert validated.stored["occupancy_zones"]["hallway"]["sync_switches"] == [
        MMWAVE_EAST
    ]


def test_loader_flags_bad_room_switches_and_zone_lists() -> None:
    seed = _hall_seed()
    seed["rooms"]["empty"] = {
        "lights": [],
        "switches": [{"topic": "zigbee2mqtt/E", "governs": "empty"}],
    }
    seed["rooms"]["hall"]["switches"].append({"topic": MMWAVE_EAST, "governs": "hall"})
    seed["rooms"]["kitchen"]["switches"].append(
        {"topic": MMWAVE_WEST, "governs": "kitchen.overhead"}
    )
    zone = seed["occupancy_zones"]["hallway"]
    zone["sync_switches"] = [MMWAVE_EAST, "zigbee2mqtt/Nope", 5]
    zone["override_idle_s"] = -1
    seed["occupancy_zones"]["porch"]["sync_switches"] = "not-a-list"
    issues = " | ".join(validate_config(seed).issues)
    assert "room 'empty' has no lights" in issues
    assert "listed twice" in issues
    assert "maps to both" not in issues or MMWAVE_WEST in issues
    assert "unknown switch ['zigbee2mqtt/Nope']" in issues
    assert "bad override_idle_s" in issues
    validated = validate_config(seed)
    assert validated.config["occupancy"]["hallway"]["sync_switches"] == [MMWAVE_EAST]
    assert validated.config["occupancy"]["hallway"]["override_idle_s"] == 3600.0
    assert validated.config["occupancy"]["porch"]["sync_switches"] == []


async def test_modes_persist_and_expire_overrides() -> None:
    store = FakeStore(None)
    modes = ModeManager(store)
    soon = dt_util.utcnow() + timedelta(minutes=5)
    await modes.set_override("hallway", expires_at=soon)
    assert modes.override_until("hallway") == soon
    saved = store._data
    assert saved[OVERRIDES_KEY] == {"hallway": soon.isoformat()}
    reloaded = ModeManager(
        FakeStore(
            {
                **saved,
                "junk": "x",
                OVERRIDES_KEY: {"hallway": soon.isoformat(), "bad": "not-a-date"},
            }
        )
    )
    await reloaded.async_load()
    assert reloaded.overrides() == {"hallway": soon}
    assert await reloaded.sweep_expired_overrides(dt_util.utcnow()) == []
    assert await reloaded.sweep_expired_overrides(soon) == ["hallway"]
    assert not await reloaded.end_override("hallway")


async def test_releasing_the_override_of_a_vacant_zone_clears_it(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    await _send(hass, hall, MMWAVE_EAST, {"action": "up_single"})
    mqtt_mock.async_publish.reset_mock()
    await hall.async_release_hold("hallway")
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {EAST_SET: {"state": "OFF"}}


async def test_show_lights_uses_light_topics_for_part_of_a_source(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any
) -> None:
    look: Any = {"brightness": 50}
    hall._show_lights([("hallway_up", "hall.center")], lambda _s: look, origin_at=0)
    hall._show_lights([("hallway_up", "hall.center")], lambda _s: None, origin_at=0)
    await hass.async_block_till_done()
    assert set(published(mqtt_mock)) == {HALL_CENTER_SET}


# --- a config-tap look keeps its zone on (user 2026-11-05) --------------------


async def _hold_hall_at_night(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    """Motion lights the hall, then a config tap holds both lights at night."""
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": True})
    _fire(hass, MMWAVE_EAST, {"state": "ON"})  # the sync write's echo
    await hass.async_block_till_done()
    clock.advance(1.0)
    await _send(hass, hall, MMWAVE_EAST, {"action": "config_single"})
    assert hall._modes.held_rooms() == {"hall.up_main", "hall.center"}
    mqtt_mock.async_publish.reset_mock()


async def test_config_look_keeps_the_zone_on_and_sweeps_show_the_look(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _hold_hall_at_night(hass, hall, mqtt_mock, clock)
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    assert published(mqtt_mock) == {}  # vacant, but held: nothing turns off
    assert hall.diagnostics["occupancy"]["hallway"] is False
    # the next episode's sweep shows the look, not the curve
    await _send(hass, hall, MMWAVE_WEST, {"occupancy": True})
    night = _night(hall, "hallway_up")
    assert published(mqtt_mock) == {
        HALL_CENTER_SET: {**night, "transition": 1.5, "state": "ON"}
    }
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, hall, MMWAVE_WEST, {"occupancy": False})
    assert published(mqtt_mock) == {}


async def test_tap_off_returns_a_held_zone_to_motion(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _hold_hall_at_night(hass, hall, mqtt_mock, clock)
    await _send(hass, hall, MMWAVE_EAST, {"action": "down_single"})
    assert hall._modes.held_rooms() == set()
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    assert HALL_OFF in published(mqtt_mock)  # the normal clear again


async def test_look_expiring_at_midnight_clears_a_vacant_zone(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _hold_hall_at_night(hass, hall, mqtt_mock, clock)
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    now = dt_util.utcnow()
    for room in ("hall.up_main", "hall.center"):
        await hall._modes.set_held(room, kind="night", armed_at=now, expires_at=now)
    await tick(hass, hall, clock)
    assert hall._modes.held_rooms() == set()
    assert published(mqtt_mock)[EAST_SET] == {"state": "OFF"}


async def test_look_expiring_with_presence_waits_for_the_normal_clear(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _hold_hall_at_night(hass, hall, mqtt_mock, clock)
    now = dt_util.utcnow()
    for room in ("hall.up_main", "hall.center"):
        await hall._modes.set_held(room, kind="night", armed_at=now, expires_at=now)
    await tick(hass, hall, clock)
    assert EAST_SET not in published(mqtt_mock)  # someone is there
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    assert published(mqtt_mock)[EAST_SET] == {"state": "OFF"}


async def test_releasing_the_last_look_clears_a_vacant_zone(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _hold_hall_at_night(hass, hall, mqtt_mock, clock)
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    await hall.async_release_hold("hall.center")
    await hass.async_block_till_done()
    assert EAST_SET not in published(mqtt_mock)  # hall.up_main still holds it
    await hall.async_release_hold("hall.up_main")
    await hass.async_block_till_done()
    assert published(mqtt_mock)[EAST_SET] == {"state": "OFF"}


async def test_clear_holds_clears_a_vacant_held_zone(
    hass: HomeAssistant, hall: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _hold_hall_at_night(hass, hall, mqtt_mock, clock)
    await _send(hass, hall, MMWAVE_EAST, {"occupancy": False})
    await hall.async_clear_holds()
    await hass.async_block_till_done()
    assert published(mqtt_mock)[EAST_SET] == {"state": "OFF"}
