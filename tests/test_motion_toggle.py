"""Config hold: a zone's motion switched off (dumb switches) and back on."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.light_man.bars import COLOR_GREEN, COLOR_RED, LED_BOTTOM
from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import DOMAIN
from custom_components.light_man.coordinator import LightManCoordinator
from custom_components.light_man.modes import ModeManager

from .conftest import FakeStore, published, seed_states
from .test_night_light import (
    FRONT,
    TOILET,
    TOILET_SW,
    VANITY,
    _later,
    _radar,
    _seed,
    _tap,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant

HELD = "config_held"


def _seed_b() -> dict[str, Any]:
    """The bath zone with the shared paddle-on rule (00:00 / 60 min idle)."""
    seed = copy.deepcopy(_seed())
    del seed["occupancy_zones"]["bath"]["override_until_off"]
    return seed


@pytest.fixture
async def zone(hass: HomeAssistant, mqtt_mock: Any) -> AsyncIterator[Any]:
    await seed_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="Light Man")
    entry.add_to_hass(hass)
    modes = ModeManager(FakeStore(None))
    await modes.async_load()
    coord = LightManCoordinator(hass, entry, validate_config(_seed_b()), modes)
    await coord._async_setup()
    await coord.async_refresh()
    await hass.async_block_till_done()
    for binding in coord._bindings:
        coord._sensor_occupied[(binding.zone, binding.name)] = False
    coord._start_pending.clear()
    for base in (FRONT, TOILET_SW):
        coord._set_paddle(base, on=False)
    await coord.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    yield coord
    coord.shutdown_subscriptions()


async def test_hold_turns_motion_lit_lights_off_and_shows_red(
    hass: HomeAssistant, zone: Any, mqtt_mock: Any
) -> None:
    await _radar(hass, zone, occupied=True)
    mqtt_mock.async_publish.reset_mock()
    await _tap(hass, FRONT, HELD)
    pubs = published(mqtt_mock)
    assert pubs[TOILET]["state"] == "OFF"
    assert pubs[VANITY]["state"] == "OFF"
    assert zone._modes.motion_off("bath")
    assert zone._bar_leds(FRONT)[LED_BOTTOM] == COLOR_RED
    assert zone._bar_leds(TOILET_SW)[LED_BOTTOM] == COLOR_RED  # every zone switch


async def test_hold_leaves_paddle_on_lights_on(
    hass: HomeAssistant, zone: Any, mqtt_mock: Any
) -> None:
    await _radar(hass, zone, occupied=True)
    await _tap(hass, FRONT, "up_single")  # paddle-on: override armed
    mqtt_mock.async_publish.reset_mock()
    await _tap(hass, FRONT, HELD)
    pubs = published(mqtt_mock)
    assert TOILET not in pubs
    assert VANITY not in pubs
    assert zone._modes.motion_off("bath")


async def test_motion_off_is_dumb_no_sweeps_no_turn_offs_no_resets(
    hass: HomeAssistant, zone: Any, mqtt_mock: Any
) -> None:
    await _tap(hass, FRONT, "up_single")
    await _tap(hass, FRONT, HELD)
    mqtt_mock.async_publish.reset_mock()
    await _radar(hass, zone, occupied=True)
    await _radar(hass, zone, occupied=False)
    await _later(hass, 3700)  # the 60-min idle passes
    zone._clear_zone("bath")  # a 00:00 expiry or idle end would call this
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert TOILET not in pubs
    assert VANITY not in pubs
    await _tap(hass, FRONT, "down_single")  # paddles still work
    assert zone._modes.motion_off("bath")  # and do not reset motion-off


async def test_hold_again_sweeps_an_occupied_dark_zone(
    hass: HomeAssistant, zone: Any, mqtt_mock: Any
) -> None:
    await _tap(hass, FRONT, HELD)
    await _radar(hass, zone, occupied=True)  # tracked, not acted on
    mqtt_mock.async_publish.reset_mock()
    await _tap(hass, TOILET_SW, HELD)
    pubs = published(mqtt_mock)
    assert pubs[TOILET]["state"] == "ON"
    assert not zone._modes.motion_off("bath")
    assert LED_BOTTOM not in zone._bar_leds(FRONT) or (
        zone._bar_leds(FRONT)[LED_BOTTOM] != COLOR_RED
    )
    mqtt_mock.async_publish.reset_mock()
    await _radar(hass, zone, occupied=False)  # then follows motion
    assert published(mqtt_mock)[TOILET]["state"] == "OFF"


async def test_hold_again_with_nobody_there_sends_nothing(
    hass: HomeAssistant, zone: Any, mqtt_mock: Any
) -> None:
    await _tap(hass, FRONT, HELD)
    mqtt_mock.async_publish.reset_mock()
    await _tap(hass, FRONT, HELD)
    assert TOILET not in published(mqtt_mock)
    assert VANITY not in published(mqtt_mock)


async def test_hold_again_with_paddle_on_lights_arms_a_fresh_override(
    hass: HomeAssistant, zone: Any, mqtt_mock: Any
) -> None:
    await _tap(hass, FRONT, HELD)
    async_fire_mqtt_message(hass, FRONT, json.dumps({"state": "ON"}))
    await hass.async_block_till_done()
    zone._set_paddle(FRONT, on=True)
    await zone._modes.end_override("bath")
    await _tap(hass, FRONT, HELD)
    until = zone._modes.override_until("bath")
    assert until is not None
    assert until < dt_util.utcnow() + timedelta(days=2)  # the 00:00 rule
    assert zone._bar_leds(FRONT)[LED_BOTTOM] == COLOR_GREEN


async def test_hold_on_a_switch_with_no_zone_does_nothing(
    hass: HomeAssistant, zone: Any, mqtt_mock: Any
) -> None:
    from .conftest import LR_SWITCH

    mqtt_mock.async_publish.reset_mock()
    await _tap(hass, LR_SWITCH, HELD)
    assert not zone._modes.motion_off("bath")


async def test_sweeps_in_flight_stop_on_the_hold(
    hass: HomeAssistant, zone: Any
) -> None:
    import asyncio

    never = asyncio.get_running_loop().create_future()
    task = hass.async_create_task(asyncio.wait_for(never, 60))
    zone._sweep_tasks[("bath", "front")] = task
    await zone._motion_off("bath")
    await hass.async_block_till_done()
    assert task.cancelled() or task.done()
