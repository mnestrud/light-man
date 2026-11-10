"""Drift repair: an off bulb that lost its staged look is re-staged."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.light_man.const import DRIFT_SETTLE_S
from custom_components.light_man.drift import holds, payload_xy
from custom_components.light_man.hue_frame import HueFrame, encode

from .conftest import HALL_CENTER_SET, KIT_SET, published

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.light_man.coordinator import LightManCoordinator

    from .conftest import FakeClock

CENTER = "zigbee2mqtt/Hall Center"
KIT_BULB = "zigbee2mqtt/Kitchen Bulb 1"
FACTORY = HueFrame(on=False, brightness=254, mirek=370)
BLUE_XY = (0.2198, 0.2932)  # what the bulbs report for rgb 135/206/235


def _raw(frame: HueFrame) -> str:
    return encode(frame).hex()


async def _report(hass: HomeAssistant, topic: str, frame: HueFrame) -> None:
    payload = {"state": "OFF", "philips_raw": _raw(frame), "linkquality": 80}
    async_fire_mqtt_message(hass, topic, json.dumps(payload))
    await hass.async_block_till_done()


async def _settle(hass: HomeAssistant, clock: FakeClock) -> None:
    clock.advance(DRIFT_SETTLE_S + 1)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DRIFT_SETTLE_S + 1)
    )
    await hass.async_block_till_done()


async def _ready(
    hass: HomeAssistant, coord: LightManCoordinator, clock: FakeClock, mqtt_mock: Any
) -> None:
    await coord.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    clock.advance(DRIFT_SETTLE_S + 1)  # the takeover flood has landed
    mqtt_mock.async_publish.reset_mock()


async def test_a_factory_reset_bulb_is_restaged_to_its_curve(
    hass: HomeAssistant, coordinator: Any, clock: FakeClock, mqtt_mock: Any
) -> None:
    await _ready(hass, coordinator, clock, mqtt_mock)
    await _report(hass, CENTER, FACTORY)
    assert published(mqtt_mock) == {}  # nothing before the reports settle
    await _settle(hass, clock)
    sent = published(mqtt_mock)[HALL_CENTER_SET]
    expected = coordinator._expected_look("hallway_up", "hall.center")
    assert {k: v for k, v in sent.items() if k != "transition"} == expected
    assert "state" not in sent  # staged on the off bulb, never turned on
    assert sent["transition"] >= 1.0


async def test_a_bulb_holding_its_look_is_left_alone(
    hass: HomeAssistant, coordinator: Any, clock: FakeClock, mqtt_mock: Any
) -> None:
    await _ready(hass, coordinator, clock, mqtt_mock)
    expected = coordinator._expected_look("hallway_up", "hall.center")
    held = HueFrame(
        on=False, brightness=expected["brightness"], xy=payload_xy(expected)
    )
    await _report(hass, CENTER, held)
    await _settle(hass, clock)
    assert published(mqtt_mock) == {}


async def test_a_lit_bulb_is_never_touched(
    hass: HomeAssistant, coordinator: Any, clock: FakeClock, mqtt_mock: Any
) -> None:
    await _ready(hass, coordinator, clock, mqtt_mock)
    await _report(hass, CENTER, HueFrame(on=True, brightness=254, mirek=370))
    await _settle(hass, clock)
    assert published(mqtt_mock) == {}


async def test_one_try_per_report_and_a_new_reset_is_caught_again(
    hass: HomeAssistant, coordinator: Any, clock: FakeClock, mqtt_mock: Any
) -> None:
    await _ready(hass, coordinator, clock, mqtt_mock)
    await _report(hass, CENTER, FACTORY)
    await _settle(hass, clock)
    assert HALL_CENTER_SET in published(mqtt_mock)
    mqtt_mock.async_publish.reset_mock()
    await _report(hass, CENTER, FACTORY)  # a poll republishes the same frame
    await _settle(hass, clock)
    coordinator._check_drift("Hall Center")  # even a forced check: tried already
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    expected = coordinator._expected_look("hallway_up", "hall.center")
    await _report(  # the restage landed
        hass, CENTER, HueFrame(on=False, brightness=expected["brightness"], xy=BLUE_XY)
    )
    await _settle(hass, clock)
    assert published(mqtt_mock) == {}
    await _report(hass, CENTER, FACTORY)  # a later power loss
    await _settle(hass, clock)
    assert HALL_CENTER_SET in published(mqtt_mock)


async def test_a_check_waits_while_a_send_may_still_be_landing(
    hass: HomeAssistant, coordinator: Any, clock: FakeClock, mqtt_mock: Any
) -> None:
    await _ready(hass, coordinator, clock, mqtt_mock)
    await coordinator.async_force_push()  # a flood planned just now
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await _report(hass, CENTER, FACTORY)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DRIFT_SETTLE_S + 1)
    )
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    assert "Hall Center" in coordinator._drift_checks  # checked again later
    await _settle(hass, clock)
    assert HALL_CENTER_SET in published(mqtt_mock)


async def test_a_group_member_is_restaged_to_its_held_look(
    hass: HomeAssistant, coordinator: Any, clock: FakeClock, mqtt_mock: Any
) -> None:
    devices = [
        {"ieee_address": "0x01", "friendly_name": "Kitchen Bulb 1"},
        {"ieee_address": "0x02", "friendly_name": "Kitchen Bulb 2"},
        {"friendly_name": "no address"},
    ]
    groups = [
        {
            "friendly_name": "zgb_kitchen",
            "members": [{"ieee_address": "0x01", "endpoint": 11}, {"endpoint": 11}],
        },
        {"friendly_name": "empty"},
    ]
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", json.dumps(devices))
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/groups", json.dumps(groups))
    await hass.async_block_till_done()
    assert coordinator._bulb_rooms["Kitchen Bulb 1"] == ("overhead", "kitchen.overhead")
    assert "Kitchen Bulb 2" not in coordinator._bulb_rooms  # not in the group
    await _ready(hass, coordinator, clock, mqtt_mock)
    await coordinator.async_hold("kitchen.overhead", "night")
    await hass.async_block_till_done()
    clock.advance(DRIFT_SETTLE_S + 1)
    mqtt_mock.async_publish.reset_mock()
    await _report(hass, KIT_BULB, FACTORY)
    await _settle(hass, clock)
    sent = published(mqtt_mock)[KIT_SET]
    night = coordinator._expected_look("overhead", "kitchen.overhead")
    assert {k: v for k, v in sent.items() if k != "transition"} == night
    assert night != coordinator._room_glide_target("overhead")  # the look, not curve


async def test_nothing_is_sent_when_it_cannot_or_should_not_be(
    hass: HomeAssistant, coordinator: Any, clock: FakeClock, mqtt_mock: Any
) -> None:
    await _report(hass, CENTER, FACTORY)  # push still disabled
    await _settle(hass, clock)
    await _ready(hass, coordinator, clock, mqtt_mock)
    coordinator._schedule_off_stage("hall.center")  # an off-stage will set it
    coordinator._drift_tried.clear()
    coordinator._check_drift("Hall Center")
    coordinator._cancel_off_stage("hall.center")
    with patch.object(coordinator, "_expected_look", return_value=None):
        coordinator._check_drift("Hall Center")  # look unknown
    coordinator._check_drift("Nobody")  # no report
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


async def test_reports_that_are_not_bulb_frames_are_ignored(
    hass: HomeAssistant, coordinator: Any, clock: FakeClock, mqtt_mock: Any
) -> None:
    await _ready(hass, coordinator, clock, mqtt_mock)
    for topic, payload in (
        ("zigbee2mqtt/Somebody Else", {"philips_raw": _raw(FACTORY)}),
        (CENTER, {"state": "OFF"}),
        (CENTER, {"philips_raw": 7}),
        (CENTER, {"philips_raw": "ffff"}),  # unknown flags
    ):
        async_fire_mqtt_message(hass, topic, json.dumps(payload))
    async_fire_mqtt_message(hass, CENTER, "philips_raw but not json")
    await hass.async_block_till_done()
    assert coordinator._bulb_frames == {}
    assert coordinator._drift_checks == {}


async def test_the_expected_look_follows_holds_dims_and_the_curve(
    coordinator: Any,
) -> None:
    curve = coordinator._room_glide_target("overhead")
    assert coordinator._expected_look("overhead", "kitchen.overhead") == curve
    coordinator._zone_dims["porch"] = {"porch.a1": 10.0}
    dimmed = coordinator._expected_look("overhead", "porch.a1")
    assert dimmed == {**curve, "brightness": 25}
    with patch.object(coordinator, "_engine_payload", side_effect=ValueError):
        assert coordinator._expected_look("overhead", "kitchen.overhead") is None
    with patch.object(coordinator, "_engine_payload", return_value=None):
        assert coordinator._expected_look("overhead", "kitchen.overhead") is None
    with patch.object(
        coordinator, "_held_looks", return_value={"kitchen.overhead": None}
    ):  # a dim in progress: its level is not known yet
        assert coordinator._expected_look("overhead", "kitchen.overhead") is None


async def test_unload_cancels_pending_checks(
    hass: HomeAssistant, coordinator: Any, clock: FakeClock, mqtt_mock: Any
) -> None:
    await _ready(hass, coordinator, clock, mqtt_mock)
    await _report(hass, CENTER, FACTORY)
    assert coordinator._drift_checks
    coordinator.shutdown_subscriptions()
    assert coordinator._drift_checks == {}
    coordinator._drift_check_due("Hall Center", dt_util.utcnow())  # late: no-op
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


def test_holds_compares_white_by_mired() -> None:
    white = {"brightness": 229, "color_temp": 299}
    assert holds(HueFrame(on=False, brightness=229, mirek=305), white)
    assert not holds(HueFrame(on=False, brightness=229, mirek=370), white)
    assert not holds(HueFrame(on=False, brightness=254, mirek=299), white)
    assert not holds(HueFrame(on=False, xy=(0.31, 0.32)), white)  # colour mode
    assert holds(HueFrame(on=False), white)  # nothing reported to compare


def test_holds_compares_colour_by_xy() -> None:
    blue = {"brightness": 229, "color": {"r": 135, "g": 206, "b": 235}}
    assert holds(HueFrame(on=False, brightness=229, xy=BLUE_XY), blue)
    assert not holds(HueFrame(on=False, brightness=229, mirek=370), blue)
    assert not holds(HueFrame(on=False, xy=(0.4578, 0.41)), blue)
    assert holds(HueFrame(on=False), blue)
    xy = {"color": {"x": 0.3, "y": 0.3}}
    assert holds(HueFrame(on=False, xy=(0.31, 0.3)), xy)
    assert holds(HueFrame(on=False, mirek=370), {"brightness": 254})  # no colour


def test_payload_xy_forms() -> None:
    assert payload_xy({"color": {"x": 0.3, "y": 0.4}}) == (0.3, 0.4)
    assert payload_xy({"color": {"hue": 10}}) is None
    assert payload_xy({"color_temp": 0}) is None
    assert payload_xy({"color_temp": 370}) is not None


async def test_a_newer_report_restarts_the_settle_wait(
    hass: HomeAssistant, coordinator: Any, clock: FakeClock, mqtt_mock: Any
) -> None:
    await _ready(hass, coordinator, clock, mqtt_mock)
    await _report(hass, CENTER, HueFrame(on=False, brightness=140, mirek=370))
    first = coordinator._drift_checks["Hall Center"]
    await _report(hass, CENTER, FACTORY)  # the off-fade is still reporting
    assert coordinator._drift_checks["Hall Center"] is not first
    await _settle(hass, clock)
    assert HALL_CENTER_SET in published(mqtt_mock)


async def test_lights_outside_the_z2m_base_are_not_mapped(coordinator: Any) -> None:
    rooms = coordinator._config["sources"]["overhead"]["rooms"]
    rooms["odd.light"] = {"set_topic": "elsewhere/Odd Bulb/set", "switches": []}
    rooms["blank.light"] = {"set_topic": "", "switches": []}
    coordinator._rebuild_bulb_rooms()
    assert "Odd Bulb" not in coordinator._bulb_rooms
    assert "elsewhere/Odd Bulb" not in coordinator._bulb_rooms
