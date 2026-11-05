"""LED bars: per-room status lights, tap and shade animations, curve level."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.light_man.bars import (
    BAR_PEAK_LEVEL,
    COLOR_AMBER,
    COLOR_BAR,
    COLOR_GREEN,
    COLOR_RED,
    INDEFINITE,
    KEY_EFFECT,
    KEY_LED,
    KEY_LEVEL,
    LED_BOTTOM,
    LED_TOP,
    BarManager,
    bar_level,
    led_command,
    scaled,
)
from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import BUNDLED_SEED_VERSION
from custom_components.light_man.migrations import V12_ANIMATIONS, migrate
from custom_components.light_man.z2m import parse_publish_failure

from .conftest import LR_SWITCH, MMWAVE_EAST, MMWAVE_WEST, bar_published
from .test_switches import _coordinator, _fire, _hall_seed, _send

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant

    from custom_components.light_man.coordinator import LightManCoordinator

    from .conftest import FakeClock

FIXTURES = Path(__file__).parent / "fixtures"
BUNDLE = Path("custom_components/light_man/light_man_config.json")
LR_SET_TOPIC = f"{LR_SWITCH}/set"
EAST_SET = f"{MMWAVE_EAST}/set"
WEST_SET = f"{MMWAVE_WEST}/set"
RISING = {"effect": "slow_rising", "color": 220, "level": 50, "duration": 10}


# --- pure helpers ------------------------------------------------------------


def test_levels_follow_the_curve_fraction() -> None:
    assert bar_level(1.0) == BAR_PEAK_LEVEL
    assert bar_level(1 / 3) == 8
    assert bar_level(0.0) == 1  # never fully dark
    assert scaled(60, 0.5, 10) == 30
    assert scaled(60, 0.05, 10) == 10
    assert scaled(100, 1.0, 5) == 100


def test_led_command_lights_or_clears_one_led() -> None:
    lit = led_command(LED_TOP, COLOR_AMBER, 40)[KEY_LED]
    assert lit == {
        "led": "7",
        "effect": "solid",
        "color": COLOR_AMBER,
        "level": 40,
        "duration": INDEFINITE,
    }
    assert led_command(LED_TOP, None, 40)[KEY_LED]["effect"] == "clear_effect"


# --- BarManager --------------------------------------------------------------


class _Bars:
    """A BarManager with a recording send and instant pacing."""

    def __init__(self, *, ok: bool = True) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.ok = ok
        self.manager = BarManager(self._send, asyncio.ensure_future, sleep=self._sleep)

    async def _send(self, topic: str, payload: dict[str, Any]) -> bool:
        self.sent.append((topic, payload))
        return self.ok

    async def _sleep(self, _seconds: float) -> None:
        await asyncio.sleep(0)

    async def drain(self) -> list[tuple[str, dict[str, Any]]]:
        worker = self.manager._worker
        if worker is not None:
            await worker
        sent, self.sent = self.sent, []
        return sent


async def test_first_sync_sets_the_level_and_clears_the_status_leds() -> None:
    bars = _Bars()
    bars.manager.sync("sw", {LED_TOP: None, LED_BOTTOM: None}, 1.0)
    sent = await bars.drain()
    assert sent[0] == ("sw/set", {KEY_LEVEL: BAR_PEAK_LEVEL})
    assert [p[KEY_LED]["led"] for _t, p in sent[1:]] == ["7", "1"]
    assert all(p[KEY_LED]["effect"] == "clear_effect" for _t, p in sent[1:])
    bars.manager.sync("sw", {LED_TOP: None, LED_BOTTOM: None}, 1.0)
    assert await bars.drain() == []  # nothing changed


async def test_status_led_is_sent_on_change_only() -> None:
    bars = _Bars()
    bars.manager.sync("sw", {LED_TOP: None}, 1.0)
    await bars.drain()
    bars.manager.sync("sw", {LED_TOP: COLOR_AMBER}, 1.0)
    assert await bars.drain() == [("sw/set", led_command(LED_TOP, COLOR_AMBER, 60))]
    bars.manager.sync("sw", {LED_TOP: COLOR_AMBER}, 1.0)
    assert await bars.drain() == []
    bars.manager.sync("sw", {LED_TOP: None}, 1.0)
    assert await bars.drain() == [("sw/set", led_command(LED_TOP, None, 60))]


async def test_level_moves_in_steps_and_relevels_a_lit_status() -> None:
    bars = _Bars()
    bars.manager.sync("sw", {LED_TOP: COLOR_AMBER}, 1.0)
    await bars.drain()
    bars.manager.sync("sw", {LED_TOP: COLOR_AMBER}, 0.97)  # 24: under one step
    assert await bars.drain() == []
    bars.manager.sync("sw", {LED_TOP: COLOR_AMBER}, 0.5)
    assert await bars.drain() == [
        ("sw/set", {KEY_LEVEL: 12}),
        ("sw/set", led_command(LED_TOP, COLOR_AMBER, 30)),
    ]
    # an unlit status LED is not re-sent when only the level moves
    bars.manager.sync("other", {LED_TOP: None}, 1.0)
    await bars.drain()
    bars.manager.sync("other", {LED_TOP: None}, 0.5)
    assert await bars.drain() == [("other/set", {KEY_LEVEL: 12})]


async def test_the_switch_own_level_seeds_the_start_once() -> None:
    bars = _Bars()
    bars.manager.seed_level("sw", 12)
    bars.manager.seed_level("sw", 25)  # only the first report counts
    bars.manager.sync("sw", {LED_TOP: None}, 0.5)
    assert await bars.drain() == [("sw/set", led_command(LED_TOP, None, 30))]


async def test_forget_makes_the_next_sync_send_everything_again() -> None:
    bars = _Bars()
    bars.manager.sync("sw", {LED_TOP: COLOR_AMBER}, 1.0)
    await bars.drain()
    bars.manager.forget("sw")
    bars.manager.sync("sw", {LED_TOP: COLOR_AMBER}, 1.0)
    assert len(await bars.drain()) == 2


async def test_a_refused_send_forgets_the_bar() -> None:
    bars = _Bars(ok=False)
    bars.manager.sync("sw", {LED_TOP: None}, 1.0)
    await bars.drain()
    assert bars.manager._level == {}
    assert bars.manager._leds == {}


async def test_play_scales_the_level_and_a_newer_animation_replaces_a_waiting_one() -> (
    None
):
    bars = _Bars()
    bars.manager.play("a", "pulse", COLOR_RED, 60, 1, 1.0)  # starts the worker
    bars.manager.play("sw", "slow_rising", 220, 50, 10, 0.5)
    bars.manager.play("sw", "pulse", COLOR_BAR, 60, 1, 0.5)
    sent = await bars.drain()
    assert [topic for topic, _p in sent] == ["a/set", "sw/set"]
    assert sent[1][1][KEY_EFFECT] == {
        "effect": "pulse",
        "color": COLOR_BAR,
        "level": 30,
        "duration": 1,
    }


async def test_cancel_drops_pending_and_refuses_new_commands() -> None:
    bars = _Bars()
    bars.manager.play("a", "pulse", COLOR_RED, 60, 1, 1.0)
    bars.manager.play("b", "pulse", COLOR_RED, 60, 1, 1.0)
    bars.manager.cancel()
    bars.manager.play("c", "pulse", COLOR_RED, 60, 1, 1.0)
    await asyncio.sleep(0)
    assert bars.manager._pending == {}
    assert bars.sent == []
    bars.manager.cancel()  # idempotent


# --- coordinator: status, feedback, shades, level ------------------------------


def _bar_seed() -> dict[str, Any]:
    seed = _hall_seed()
    seed["rooms"]["living_room"]["switches"][0]["animations"] = {"up_double": RISING}
    seed["rooms"]["hall"]["switches"][0]["animations"] = {"down_double": RISING}
    return seed


@pytest.fixture
async def house(
    hass: HomeAssistant, mqtt_mock: Any
) -> AsyncIterator[LightManCoordinator]:
    coord = await _coordinator(hass, _bar_seed())
    await coord.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    # A source's bars sync once its curve has been computed: the first flood of
    # the hallway source lands after the enabling snapshot, so its bars follow
    # at the next snapshot or tick.
    coord._publish_snapshot()
    await hass.async_block_till_done()
    yield coord
    for cancel in coord._override_timers.values():
        cancel()


def _effects(mqtt_mock: Any, topic: str) -> list[dict[str, Any]]:
    return [
        p[KEY_EFFECT]
        for t, p in bar_published(mqtt_mock)
        if t == topic and KEY_EFFECT in p
    ]


def _leds(mqtt_mock: Any, topic: str) -> list[tuple[str, str, int]]:
    return [
        (p[KEY_LED]["led"], p[KEY_LED]["effect"], p[KEY_LED]["color"])
        for t, p in bar_published(mqtt_mock)
        if t == topic and KEY_LED in p
    ]


async def test_startup_sets_each_bar_level_and_clears_its_status(
    hass: HomeAssistant, house: LightManCoordinator, mqtt_mock: Any
) -> None:
    fraction = house._bar_fraction("overhead")
    assert fraction is not None
    sent = bar_published(mqtt_mock)
    assert (LR_SET_TOPIC, {KEY_LEVEL: bar_level(fraction)}) in sent
    assert _leds(mqtt_mock, LR_SET_TOPIC) == [("7", "clear_effect", 0)]
    # an override switch also owns the bottom LED
    assert _leds(mqtt_mock, EAST_SET) == [
        ("7", "clear_effect", 0),
        ("1", "clear_effect", 0),
    ]


async def test_config_tap_lights_the_top_led_and_pulses_then_up_tap_clears(
    hass: HomeAssistant, house: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _send(hass, house, LR_SWITCH, {"action": "up_single"})
    clock.advance(1.0)
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, house, LR_SWITCH, {"action": "config_single"})
    sent = [p for t, p in bar_published(mqtt_mock) if t == LR_SET_TOPIC]
    assert [next(iter(p)) for p in sent] == [KEY_LED, KEY_EFFECT]  # status, then pulse
    assert sent[0][KEY_LED]["color"] == COLOR_AMBER
    assert sent[1][KEY_EFFECT]["effect"] == "pulse"
    assert sent[1][KEY_EFFECT]["color"] == COLOR_AMBER
    assert sent[1][KEY_EFFECT]["duration"] == 1
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, house, LR_SWITCH, {"action": "up_single"})
    assert _leds(mqtt_mock, LR_SET_TOPIC) == [("7", "clear_effect", 0)]
    assert [e["color"] for e in _effects(mqtt_mock, LR_SET_TOPIC)] == [COLOR_BAR]


async def test_ignored_config_tap_blinks_red_and_lights_nothing(
    hass: HomeAssistant, house: LightManCoordinator, mqtt_mock: Any
) -> None:
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, house, LR_SWITCH, {"action": "config_single"})  # light off
    await _send(hass, house, MMWAVE_EAST, {"action": "config_double"})
    for topic in (LR_SET_TOPIC, EAST_SET):
        assert [(e["effect"], e["color"]) for e in _effects(mqtt_mock, topic)] == [
            ("fast_blink", COLOR_RED)
        ]
        assert _leds(mqtt_mock, topic) == []


async def test_hallway_pair_shows_override_and_look_on_both_switches(
    hass: HomeAssistant, house: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, house, MMWAVE_EAST, {"action": "up_single"})
    for topic in (EAST_SET, WEST_SET):
        assert _leds(mqtt_mock, topic) == [("1", "solid", COLOR_GREEN)]
    assert [e["color"] for e in _effects(mqtt_mock, EAST_SET)] == [COLOR_GREEN]
    assert _effects(mqtt_mock, WEST_SET) == []  # feedback only where it was tapped
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, house, MMWAVE_EAST, {"action": "up_single"})
    assert bar_published(mqtt_mock) == []  # already armed, nothing held
    clock.advance(1.0)
    await _send(hass, house, MMWAVE_EAST, {"action": "config_single"})
    for topic in (EAST_SET, WEST_SET):
        assert _leds(mqtt_mock, topic) == [("7", "solid", COLOR_AMBER)]
    assert [e["color"] for e in _effects(mqtt_mock, EAST_SET)] == [COLOR_AMBER]
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, house, MMWAVE_WEST, {"action": "up_single"})  # look released
    assert [e["color"] for e in _effects(mqtt_mock, WEST_SET)] == [COLOR_BAR]
    assert _leds(mqtt_mock, EAST_SET) == [("7", "clear_effect", 0)]
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, house, MMWAVE_EAST, {"action": "down_single"})
    for topic in (EAST_SET, WEST_SET):
        assert _leds(mqtt_mock, topic) == [("1", "clear_effect", 0)]
    assert _effects(mqtt_mock, EAST_SET) == []


async def test_shade_taps_play_their_animation_dimmed_with_the_curve(
    hass: HomeAssistant, house: LightManCoordinator, mqtt_mock: Any
) -> None:
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, house, LR_SWITCH, {"action": "up_double"})
    await _send(hass, house, MMWAVE_EAST, {"action": "down_double"})
    await _send(hass, house, LR_SWITCH, {"action": "down_double"})  # none configured
    fraction = house._bar_fraction("overhead")
    assert fraction is not None
    assert _effects(mqtt_mock, LR_SET_TOPIC) == [
        {**RISING, "level": scaled(50, fraction, 5)}
    ]
    assert len(_effects(mqtt_mock, EAST_SET)) == 1
    # before the engine has run, an animation plays at its configured level
    house.diagnostics["engine"].clear()
    mqtt_mock.async_publish.reset_mock()
    await _send(hass, house, LR_SWITCH, {"action": "up_double"})
    assert _effects(mqtt_mock, LR_SET_TOPIC) == [RISING]
    assert [p for _t, p in bar_published(mqtt_mock) if KEY_LEVEL in p] == []


async def test_bar_level_follows_the_curve_at_the_tick(
    hass: HomeAssistant, house: LightManCoordinator, mqtt_mock: Any
) -> None:
    mqtt_mock.async_publish.reset_mock()
    peak = house._config["sources"]["overhead"]["profile"]["max_br"]
    house.diagnostics["engine"]["overhead"]["brightness_pct"] = peak / 5
    house._sync_bars()
    await hass.async_block_till_done()
    assert (LR_SET_TOPIC, {KEY_LEVEL: 5}) in bar_published(mqtt_mock)


async def test_the_switch_report_seeds_the_level_so_it_is_not_rewritten(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    coord = await _coordinator(hass, _bar_seed())
    _fire(hass, LR_SWITCH, {"state": "OFF", KEY_LEVEL: 25})
    await hass.async_block_till_done()
    coord.diagnostics["engine"]["overhead"] = {"brightness_pct": 1000.0}
    coord.push_enabled = True
    coord._sync_bars()
    await hass.async_block_till_done()
    lr = [p for t, p in bar_published(mqtt_mock) if t == LR_SET_TOPIC]
    assert [next(iter(p)) for p in lr] == [KEY_LED]  # no level write


async def test_nothing_is_written_while_light_man_is_off(
    hass: HomeAssistant, house: LightManCoordinator, mqtt_mock: Any
) -> None:
    await house.async_set_push_enabled(enabled=False)
    house._bars.forget(LR_SWITCH)
    mqtt_mock.async_publish.reset_mock()
    house._publish_snapshot()
    await _send(hass, house, LR_SWITCH, {"action": "up_double"})
    assert bar_published(mqtt_mock) == []


async def test_a_failed_bar_command_is_re_synced_and_resends_no_light(
    hass: HomeAssistant, house: LightManCoordinator, mqtt_mock: Any
) -> None:
    mqtt_mock.async_publish.reset_mock()
    name = LR_SWITCH.removeprefix("zigbee2mqtt/")
    message = f"Publish 'set' 'led_effect' to '{name}' failed: 'Delivery failed'"
    _fire(
        hass,
        "zigbee2mqtt/bridge/logging",
        {"level": "error", "namespace": "z2m", "message": message},
    )
    await hass.async_block_till_done()
    assert mqtt_mock.async_publish.call_count == 0  # no light or switch re-send
    assert house.diagnostics["delivery_failures"]["total"] == 1
    house._publish_snapshot()
    await hass.async_block_till_done()
    assert {t for t, _p in bar_published(mqtt_mock)} == {LR_SET_TOPIC}


def test_publish_failure_carries_the_attribute() -> None:
    failure = parse_publish_failure(
        "Publish 'set' 'individual_led_effect' to 'Office' failed: 'timed out after 1'"
    )
    assert failure is not None
    assert failure.attr == "individual_led_effect"


# --- config: loader + migration -----------------------------------------------


def test_loader_keeps_valid_animations_and_flags_bad_ones() -> None:
    seed = _bar_seed()
    switch = seed["rooms"]["living_room"]["switches"][0]
    switch["animations"]["down_double"] = {"effect": "pulse", "color": 300}
    switch["animations"]["up_triple"] = "nope"
    seed["rooms"]["hall"]["switches"][1]["animations"] = ["not", "a", "mapping"]
    result = validate_config(seed)
    assert result.switch_animations[LR_SWITCH] == {"up_double": RISING}
    assert MMWAVE_WEST not in result.switch_animations
    stored = result.stored["rooms"]["living_room"]["switches"][0]
    assert stored["animations"] == {"up_double": RISING}
    assert "animations" not in result.stored["rooms"]["hall"]["switches"][1]
    text = "\n".join(result.issues)
    assert "animation 'down_double' needs" in text
    assert "animation 'up_triple' needs" in text
    assert "'animations' must be a mapping" in text


def test_v11_fixture_migrates_to_the_shipped_bundle() -> None:
    raw = json.loads((FIXTURES / "seed_v11.json").read_text())
    migrated, steps = migrate(raw)
    assert steps == [11]
    assert migrated == json.loads(BUNDLE.read_text())
    assert migrated["seed_version"] == BUNDLED_SEED_VERSION
    result = validate_config(migrated)
    assert result.issues == []
    assert set(result.switch_animations) == set(V12_ANIMATIONS)
    office = result.switch_animations["zigbee2mqtt/Office Smart Switch"]
    assert office["up_double"] == RISING
    assert office["down_triple"]["effect"] == "pulse"


def test_v11_to_v12_leaves_edited_and_odd_switches_alone() -> None:
    raw = json.loads((FIXTURES / "seed_v11.json").read_text())
    office = raw["rooms"]["office"]["switches"][0]
    office["animations"] = {}  # the user removed them
    raw["rooms"]["odd"] = "nope"
    raw["rooms"]["no_list"] = {"switches": "nope"}
    raw["rooms"]["kitchen"]["switches"].append("nope")
    before = copy.deepcopy(raw)
    migrated, _steps = migrate(raw)
    assert raw == before
    assert migrated["rooms"]["office"]["switches"][0]["animations"] == {}
    assert "animations" in migrated["rooms"]["kitchen"]["switches"][0]
    # a switch the blueprint had no animation for gets none
    assert "animations" not in migrated["rooms"]["stairwell"]["switches"][0]
