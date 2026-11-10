"""Primary-bath style night light: a dimmed sweep light held by re-asserts."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.light_man.bars import LED_BOTTOM
from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import (
    DOMAIN,
    OFF_STAGE_DELAY_S,
    ZONE_DIM_RELEASE_TRANSITION_S,
)
from custom_components.light_man.coordinator import LightManCoordinator
from custom_components.light_man.modes import ModeManager

from .conftest import OVERHEAD_ALL, SEED, FakeStore, published, seed_states

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant

TOILET = "zigbee2mqtt/Bath Toilet Light/set"
VANITY = "zigbee2mqtt/Bath Vanity/set"
FRONT = "zigbee2mqtt/Bath Front Switch"
TOILET_SW = "zigbee2mqtt/Bath Toilet Switch"
RADAR = "zigbee2mqtt/Bath Front Switch"  # the front switch's own mmWave
NIGHT = 25  # 10 % of 254


def _seed() -> dict[str, Any]:
    seed = copy.deepcopy(SEED)
    seed["rooms"]["bath"] = {
        "name": "Bath",
        "lights": [
            {"id": "toilet", "set_topic": TOILET, "source": "overhead"},
            {"id": "vanity", "set_topic": VANITY, "source": "overhead"},
        ],
        "switches": [
            {"topic": FRONT, "governs": ["bath.toilet", "bath.vanity"]},
            {"topic": TOILET_SW, "governs": ["bath.toilet", "bath.vanity"]},
        ],
        "sensors": {
            "front": {"topic": RADAR, "occupancy_key": "mmwave_area1_occupancy"}
        },
    }
    seed["occupancy_zones"]["bath"] = {
        "off_lights": [TOILET, VANITY],
        "sensors": {
            "bath.front": {
                "sweep": [
                    {"lights": ["bath.toilet"], "brightness_pct": 10},
                    {"lights": ["bath.vanity"]},
                ]
            }
        },
        "override_switches": [FRONT, TOILET_SW],
        "override_until_off": True,
        "on_transition_s": 1.0,
    }
    return seed


@pytest.fixture
async def bath(hass: HomeAssistant, mqtt_mock: Any) -> AsyncIterator[Any]:
    await seed_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="Light Man")
    entry.add_to_hass(hass)
    modes = ModeManager(FakeStore(None))
    await modes.async_load()
    validated = validate_config(_seed())
    assert validated.issues == []
    coord = LightManCoordinator(hass, entry, validated, modes)
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


async def _instant(*_a: Any, **_k: Any) -> None:
    """Sweep stage delays run instantly."""


async def _radar(hass: HomeAssistant, coord: Any, *, occupied: bool) -> None:
    with patch("custom_components.light_man.coordinator.asyncio.sleep", _instant):
        async_fire_mqtt_message(
            hass, RADAR, json.dumps({"mmwave_area1_occupancy": occupied})
        )
        await hass.async_block_till_done()
        pending = [t for t in coord._sweep_tasks.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await hass.async_block_till_done()


async def _tap(hass: HomeAssistant, base: str, action: str) -> None:
    async_fire_mqtt_message(hass, base, json.dumps({"action": action}))
    await hass.async_block_till_done()


async def _later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_presence_lights_toilet_dimmed_and_vanity_at_curve(
    hass: HomeAssistant, bath: Any, mqtt_mock: Any
) -> None:
    mqtt_mock.async_publish.reset_mock()
    await _radar(hass, bath, occupied=True)
    pubs = published(mqtt_mock)
    curve = pubs[VANITY]
    assert pubs[TOILET] == {**curve, "brightness": NIGHT}  # curve colour, 10 %
    assert pubs[TOILET]["state"] == "ON"
    assert pubs[TOILET]["transition"] == 1.0
    assert curve["brightness"] > NIGHT
    assert FRONT + "/set" not in pubs  # no sync switch: the paddles stay off
    assert bath._active_dims() == {"bath.toilet": 10.0}


async def test_a_flood_is_followed_by_the_night_level(
    hass: HomeAssistant, bath: Any, mqtt_mock: Any
) -> None:
    await _radar(hass, bath, occupied=True)
    mqtt_mock.async_publish.reset_mock()
    await bath.async_force_push()
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert OVERHEAD_ALL in pubs
    assert pubs[TOILET]["brightness"] == NIGHT  # re-asserted after the flood
    assert pubs[TOILET]["color_temp"] == pubs[OVERHEAD_ALL]["color_temp"]
    assert "state" not in pubs[TOILET]


async def test_vacancy_turns_both_off_then_stages_the_curve(
    hass: HomeAssistant, bath: Any, mqtt_mock: Any
) -> None:
    await _radar(hass, bath, occupied=True)
    mqtt_mock.async_publish.reset_mock()
    await _radar(hass, bath, occupied=False)
    pubs = published(mqtt_mock)
    assert pubs[TOILET] == {"state": "OFF", "transition": 1.5}
    assert pubs[VANITY] == {"state": "OFF", "transition": 1.5}
    assert bath._zone_dims == {}
    mqtt_mock.async_publish.reset_mock()
    await _later(hass, OFF_STAGE_DELAY_S + 1)
    staged = published(mqtt_mock)[TOILET]
    assert "state" not in staged  # stored on the off bulb, not turned on
    assert staged["brightness"] > NIGHT


async def test_paddle_on_brings_the_night_light_to_curve_and_cancels_motion(
    hass: HomeAssistant, bath: Any, mqtt_mock: Any
) -> None:
    await _radar(hass, bath, occupied=True)
    mqtt_mock.async_publish.reset_mock()
    await _tap(hass, FRONT, "up_single")
    pubs = published(mqtt_mock)
    assert pubs[TOILET]["brightness"] > NIGHT
    assert pubs[TOILET]["transition"] == ZONE_DIM_RELEASE_TRANSITION_S
    until = bath._modes.override_until("bath")
    assert until is not None
    assert until > dt_util.utcnow() + timedelta(days=365)  # until a paddle-off
    assert "bath" not in bath._override_timers  # no idle timer
    assert LED_BOTTOM not in bath._bar_leds(FRONT)  # no override LED
    assert bath._active_dims() == {}
    mqtt_mock.async_publish.reset_mock()
    await _radar(hass, bath, occupied=False)  # leaving changes nothing
    assert published(mqtt_mock) == {}
    assert "bath" not in bath._override_timers


async def test_paddle_off_ends_the_override_and_motion_resumes(
    hass: HomeAssistant, bath: Any, mqtt_mock: Any
) -> None:
    await _tap(hass, FRONT, "up_single")
    assert bath._modes.override_until("bath") is not None
    await _tap(hass, FRONT, "down_single")
    assert bath._modes.override_until("bath") is None
    mqtt_mock.async_publish.reset_mock()
    await _radar(hass, bath, occupied=True)
    assert published(mqtt_mock)[TOILET]["brightness"] == NIGHT


async def test_paddle_off_during_the_night_light_stages_the_curve(
    hass: HomeAssistant, bath: Any, mqtt_mock: Any
) -> None:
    await _radar(hass, bath, occupied=True)
    await _tap(hass, TOILET_SW, "down_single")
    assert bath._zone_dims == {}
    mqtt_mock.async_publish.reset_mock()
    await _later(hass, OFF_STAGE_DELAY_S + 1)
    staged = published(mqtt_mock)[TOILET]
    assert "state" not in staged
    assert staged["brightness"] > NIGHT


async def test_a_held_look_wins_over_the_night_level(
    hass: HomeAssistant, bath: Any, mqtt_mock: Any
) -> None:
    await bath.async_hold("bath.toilet", "night")
    mqtt_mock.async_publish.reset_mock()
    await _radar(hass, bath, occupied=True)
    assert published(mqtt_mock)[TOILET]["brightness"] != NIGHT
    assert bath._active_dims() == {}


async def test_release_skips_a_light_with_no_topic_or_curve(
    hass: HomeAssistant, bath: Any, mqtt_mock: Any
) -> None:
    bath._zone_dims["bath"] = {"nowhere.light": 10.0, "bath.toilet": 10.0}
    mqtt_mock.async_publish.reset_mock()
    with patch.object(bath, "_room_glide_target", return_value=None):
        bath._release_dims("bath")
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    assert bath._zone_dims == {}


async def test_a_fixed_level_stage_never_uses_its_group(bath: Any) -> None:
    stage = {
        "lights": [{"set_topic": TOILET, "source": "overhead"}],
        "group": "zigbee2mqtt/g/set",
        "brightness_pct": 10.0,
    }
    assert bath._stage_group_body(stage, set(), 1.0) is None


async def test_a_quick_redetect_cancels_the_pending_curve_stage(
    hass: HomeAssistant, bath: Any, mqtt_mock: Any
) -> None:
    await _radar(hass, bath, occupied=True)
    await _radar(hass, bath, occupied=False)  # off; curve stage due in 1.5 s
    assert "bath.toilet" in bath._off_stages
    await _radar(hass, bath, occupied=True)  # back within the 1.5 s
    assert "bath.toilet" not in bath._off_stages
    mqtt_mock.async_publish.reset_mock()
    await _later(hass, OFF_STAGE_DELAY_S + 1)
    assert TOILET not in published(mqtt_mock)  # stays at the night level
