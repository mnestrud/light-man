"""0.8.10: a switch governing several lights; bound switch pairs share bars."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.light_man.bars import HEIGHT_TRANSITION_S
from custom_components.light_man.const import CONFIG_TAP_MIN_ON_S, DIM_FLOOR

from .conftest import SEED, published
from .test_switches import _coordinator

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant

    from custom_components.light_man.coordinator import LightManCoordinator

    from .conftest import FakeClock

FRONT = "zigbee2mqtt/Porch Front Switch"
SIDE = "zigbee2mqtt/Porch Side Switch"
A1_SET = "zigbee2mqtt/zgb_porch_a1/set"
STRIP_SET = "zigbee2mqtt/Porch Strip/set"
LIGHTS = ["porch.a1", "porch.strip"]
COLOUR = {"color_temp": 300}
_COORD = {"type": "endpoint", "ieee_address": "0xc0", "endpoint": 1}


def _pair_seed() -> dict[str, Any]:
    seed = copy.deepcopy(SEED)
    porch = seed["rooms"]["porch"]
    porch["lights"].append(
        {"id": "strip", "set_topic": STRIP_SET, "source": "hallway_up"}
    )
    porch["switches"] = [
        {"topic": FRONT, "governs": list(LIGHTS)},
        {"topic": SIDE, "governs": list(LIGHTS), "bar_follows": "porch.strip"},
    ]
    return seed


def _fire(hass: HomeAssistant, topic: str, payload: Any) -> None:
    async_fire_mqtt_message(hass, topic, json.dumps(payload))


def _bound_to_coordinator(name: str) -> dict[str, Any]:
    bindings = [{"cluster": "genLevelCtrl", "target": _COORD}]
    return {"friendly_name": name, "endpoints": {"2": {"bindings": bindings}}}


@pytest.fixture
async def pair(
    hass: HomeAssistant, mqtt_mock: Any, clock: FakeClock
) -> AsyncIterator[LightManCoordinator]:
    """Both switches on (bound to each other), a1 at 180, the strip at 90."""
    coord = await _coordinator(hass, _pair_seed())
    await coord.async_set_push_enabled(enabled=True)
    devices = [
        {"type": "Coordinator", "ieee_address": "0xc0", "endpoints": {}},
        _bound_to_coordinator("Porch Front Switch"),
        _bound_to_coordinator("Porch Side Switch"),
    ]
    _fire(hass, "zigbee2mqtt/bridge/devices", devices)
    for base in (FRONT, SIDE):
        _fire(hass, base, {"state": "ON", "brightness": 200})
    await hass.async_block_till_done()
    _show(coord, clock, a1=180, strip=90)
    clock.advance(CONFIG_TAP_MIN_ON_S + 3.0)  # on long enough, bars not quiet
    mqtt_mock.async_publish.reset_mock()
    yield coord


def _show(coord: LightManCoordinator, clock: FakeClock, *, a1: int, strip: int) -> None:
    for topic, level in ((A1_SET, a1), (STRIP_SET, strip)):
        coord._last_published[topic] = {"brightness": level, **COLOUR}  # type: ignore[typeddict-item]
        coord._marked_at[topic] = clock.now


async def _press(hass: HomeAssistant, base: str, payload: dict[str, Any]) -> None:
    _fire(hass, base, payload)
    await hass.async_block_till_done()


def _height(mqtt_mock: Any, base: str) -> int | None:
    payload = published(mqtt_mock).get(f"{base}/set")
    if payload is None or "brightness" not in payload:
        return None
    assert payload["transition"] == HEIGHT_TRANSITION_S
    return int(payload["brightness"])


async def test_a_held_paddle_dims_every_light_and_the_partner_bar_follows(
    hass: HomeAssistant, pair: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _press(hass, FRONT, {"action": "down_held"})
    pubs = published(mqtt_mock)
    assert pubs[A1_SET]["brightness"] == DIM_FLOOR
    assert pubs[STRIP_SET]["brightness"] == DIM_FLOOR
    assert pair._modes.held_rooms() == set(LIGHTS)

    clock.advance(0.5)
    mqtt_mock.async_publish.reset_mock()
    await _press(hass, FRONT, {"action": "down_release"})
    strip = pair._modes.look_of("porch.strip")
    a1 = pair._modes.look_of("porch.a1")
    assert strip is not None
    assert a1 is not None
    assert DIM_FLOOR < strip["brightness"] < 90
    assert DIM_FLOOR < a1["brightness"] < 180
    # The side switch is not told by its binding: its bar is set to its light.
    assert _height(mqtt_mock, SIDE) == strip["brightness"]
    assert _height(mqtt_mock, FRONT) is None  # it answers its own report
    # Its own later report at that level is not answered again.
    mqtt_mock.async_publish.reset_mock()
    level = {"state": "ON", "brightness": strip["brightness"]}
    await _press(hass, SIDE, level)
    assert _height(mqtt_mock, SIDE) is None


async def test_a_config_tap_holds_every_light_and_moves_the_partner_bar(
    hass: HomeAssistant, pair: LightManCoordinator, mqtt_mock: Any
) -> None:
    await _press(hass, SIDE, {"action": "config_double"})
    assert pair._modes.held_rooms() == set(LIGHTS)
    pubs = published(mqtt_mock)
    assert A1_SET in pubs
    assert STRIP_SET in pubs
    # The front bar follows a1 (its first light), now at the look.
    assert _height(mqtt_mock, FRONT) == pubs[A1_SET]["brightness"]


async def test_a_partner_that_is_off_or_unknown_keeps_its_bar(
    hass: HomeAssistant, pair: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    pair._paddle_on[SIDE] = False
    await _press(hass, FRONT, {"action": "config_double"})
    assert _height(mqtt_mock, SIDE) is None

    pair._paddle_on[SIDE] = True
    pair._switch_height.pop(SIDE)
    pair._follow_partners(FRONT)
    await hass.async_block_till_done()
    assert _height(mqtt_mock, SIDE) is None


async def test_paddle_taps_and_reports_reach_every_light(
    hass: HomeAssistant, pair: LightManCoordinator, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _press(hass, FRONT, {"action": "config_double"})
    mqtt_mock.async_publish.reset_mock()
    await _press(hass, FRONT, {"action": "up_single"})  # back to the curve
    assert pair._modes.held_rooms() == set()
    assert {A1_SET, STRIP_SET} <= set(published(mqtt_mock))

    mqtt_mock.async_publish.reset_mock()
    await _press(hass, FRONT, {"action": "down_single"})
    pubs = published(mqtt_mock)
    assert pubs[A1_SET]["state"] == "OFF"
    assert pubs[STRIP_SET]["state"] == "OFF"

    # The partner turns off through its binding: its report stages its lights.
    mqtt_mock.async_publish.reset_mock()
    await _press(hass, SIDE, {"state": "OFF"})
    assert published(mqtt_mock)[STRIP_SET]["state"] == "OFF"
    clock.advance(3.0)
    await _press(hass, SIDE, {"action": "config_double"})
    mqtt_mock.async_publish.reset_mock()
    await _press(hass, SIDE, {"state": "ON"})  # the look is released
    assert pair._modes.held_rooms() == set()


async def test_the_bar_follows_the_named_light(
    hass: HomeAssistant, pair: LightManCoordinator, mqtt_mock: Any
) -> None:
    assert pair._bar_source(SIDE) == "hallway_up"
    assert pair._bar_source(FRONT) == "overhead"
    assert pair._lights_level(SIDE) == 90
    assert pair._lights_level(FRONT) == 180
    await _press(hass, SIDE, {"state": "ON", "brightness": 30})
    assert _height(mqtt_mock, SIDE) == 90
