"""0.8.0 delivery: Z2M feedback, outage replay, manual-hold capture, stagger, guards."""

from __future__ import annotations

import itertools
import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.light_man import hue_frame
from custom_components.light_man.coordinator import (
    _frame_to_payload,
    interactive_transition,
)

from .conftest import KIT_SET, LR_SET, LR_SWITCH, OVERHEAD_ALL, published, tick

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant

    from custom_components.light_man.coordinator import LightManCoordinator as Coord

    from .conftest import FakeClock

BUSY_KITCHEN = (
    "Publish 'set' 'brightness' to 'zgb_kitchen' failed: 'Error: Command 2 "
    "manuSpecificPhilips2.multiColor(...) failed (~x~> [ZCL GROUP groupId=2] "
    "Failed to send with status=BUSY.)'"
)


def _fire(hass: HomeAssistant, topic: str, payload: Any) -> None:
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    async_fire_mqtt_message(hass, topic, payload)


def _failure(name: str, error: str) -> dict[str, str]:
    return {
        "level": "error",
        "namespace": "z2m",
        "message": f"Publish 'set' 'brightness' to '{name}' failed: '{error}'",
    }


async def _enabled(hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any) -> None:
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()


# --- bridge/groups + bridge/devices ----------------------------------------


async def test_group_topics_learned_from_bridge_groups(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    assert coordinator._is_group("zigbee2mqtt/anything/set")  # unknown: assume group
    _fire(
        hass,
        "zigbee2mqtt/bridge/groups",
        [
            {"friendly_name": "zgb_living_room", "members": [{"ieee_address": "0x1"}]},
            "junk",
        ],
    )
    await hass.async_block_till_done()
    assert coordinator._is_group(LR_SET)
    assert not coordinator._is_group("zigbee2mqtt/Some Bulb/set")
    _fire(hass, "zigbee2mqtt/bridge/groups", "not json")
    _fire(hass, "zigbee2mqtt/bridge/groups", {"not": "a list"})
    await hass.async_block_till_done()
    assert coordinator._is_group(LR_SET)  # unchanged by bad payloads


async def test_device_names_learned_from_bridge_devices(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    _fire(
        hass,
        "zigbee2mqtt/bridge/devices",
        [{"ieee_address": "0x1", "friendly_name": "LR Bulb 1"}, "junk", {"x": 1}],
    )
    _fire(hass, "zigbee2mqtt/bridge/devices", "not json")
    await hass.async_block_till_done()
    assert coordinator._device_names == {"0x1": "LR Bulb 1"}


# --- bridge/logging feedback ----------------------------------------------


async def test_busy_on_a_tick_topic_clears_mark_and_resends_now(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    await coordinator.async_hold("kitchen.overhead", "night")
    await hass.async_block_till_done()
    coordinator._interactive_recent.clear()  # treat the kitchen send as tick traffic
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, "zigbee2mqtt/bridge/logging", _failure("zgb_kitchen", "status=BUSY"))
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert set(pubs) == {KIT_SET}
    assert pubs[KIT_SET]["transition"] == 5.0  # catch-up re-send
    stats = coordinator.diagnostics["delivery_failures"]
    assert stats["by_kind"] == {"busy": 1}
    assert stats["by_topic"] == {KIT_SET: 1}
    assert stats["last"]["kind"] == "busy"


async def test_busy_on_an_interactive_send_retries_once(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    await coordinator.async_hold("living_room.overhead", "night")
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    _fire(
        hass,
        "zigbee2mqtt/bridge/logging",
        _failure("zgb_living_room", "status=BUSY"),
    )
    await hass.async_block_till_done()
    assert set(published(mqtt_mock)) == {LR_SET}
    mqtt_mock.async_publish.reset_mock()
    _fire(
        hass,
        "zigbee2mqtt/bridge/logging",
        _failure("zgb_living_room", "status=BUSY"),
    )
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}  # only one retry


async def test_unconfirmed_keeps_mark_and_does_not_resend(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    _fire(
        hass,
        "zigbee2mqtt/bridge/logging",
        _failure("zgb_overhead_all", "x timed out after 10000ms"),
    )
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    assert coordinator.diagnostics["delivery_failures"]["by_kind"] == {"unconfirmed": 1}
    await tick(hass, coordinator, clock)
    assert OVERHEAD_ALL not in published(mqtt_mock)  # mark kept: dedup still skips


async def test_failure_for_an_unknown_topic_is_only_counted(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    _fire(hass, "zigbee2mqtt/bridge/logging", _failure("Nobody", "status=BUSY"))
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    assert coordinator.diagnostics["delivery_failures"]["total"] == 1


async def test_irrelevant_logging_is_ignored(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    for payload in (
        "not json",
        {"level": "warning", "message": BUSY_KITCHEN},
        {"level": "error", "namespace": "zh:ember", "message": BUSY_KITCHEN},
        {"level": "error", "namespace": "z2m", "message": "MQTT failed to connect"},
    ):
        _fire(hass, "zigbee2mqtt/bridge/logging", payload)
    await hass.async_block_till_done()
    assert coordinator.diagnostics["delivery_failures"]["total"] == 0


async def test_failure_warning_is_rate_limited(
    hass: HomeAssistant,
    coordinator: Coord,
    caplog: pytest.LogCaptureFixture,
    clock: FakeClock,
) -> None:
    for _ in range(3):
        _fire(
            hass,
            "zigbee2mqtt/bridge/logging",
            {"level": "error", "message": BUSY_KITCHEN},
        )
        await hass.async_block_till_done()
    assert caplog.text.count("Z2M could not deliver") == 1
    clock.advance(601)
    _fire(
        hass, "zigbee2mqtt/bridge/logging", {"level": "error", "message": BUSY_KITCHEN}
    )
    await hass.async_block_till_done()
    assert "2 more since last report" in caplog.text


# --- outages ----------------------------------------------------------------


async def test_z2m_restart_replays_only_topics_sent_while_down(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    _fire(hass, "zigbee2mqtt/bridge/state", {"state": "online"})  # retained: no-op
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    _fire(hass, "zigbee2mqtt/bridge/state", {"state": "offline"})
    await hass.async_block_till_done()
    await coordinator.async_hold("kitchen.overhead", "night")  # sent while Z2M down
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, "zigbee2mqtt/bridge/state", "online")  # plain-string form
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert set(pubs) == {KIT_SET}
    assert pubs[KIT_SET]["transition"] == 5.0
    assert coordinator.diagnostics["z2m_restarts"] == 1
    assert coordinator.panel_state()["z2m_online"] is True


async def test_mqtt_reconnect_replays_and_plain_connect_does_not(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    coordinator._on_mqtt_status(True)  # connected with no outage: nothing
    coordinator._on_mqtt_status(False)
    await coordinator.async_hold("kitchen.overhead", "day")
    await hass.async_block_till_done()
    coordinator._last_planned.pop(LR_SET, None)
    coordinator._outage_topics.add(LR_SET)  # nothing planned for it: skipped
    mqtt_mock.async_publish.reset_mock()
    coordinator._on_mqtt_status(True)
    await hass.async_block_till_done()
    assert set(published(mqtt_mock)) == {KIT_SET}
    assert coordinator.diagnostics["mqtt_reconnects"] == 1


async def test_slow_publish_is_logged(
    hass: HomeAssistant,
    coordinator: Coord,
    mqtt_mock: Any,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def slow(*_args: Any, **_kw: Any) -> None:
        clock.advance(11)

    with patch("custom_components.light_man.coordinator.mqtt.async_publish", slow):
        assert await coordinator._send(LR_SET, {"brightness": 1}) is True
    assert "took 11 s" in caplog.text


# --- manual holds -------------------------------------------------------------


async def test_manual_hold_captures_the_dimmed_level_and_reasserts_it(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    _fire(
        hass,
        "zigbee2mqtt/bridge/groups",
        [{"friendly_name": "zgb_living_room", "members": [{"ieee_address": "0x1"}]}],
    )
    _fire(
        hass,
        "zigbee2mqtt/bridge/devices",
        [{"ieee_address": "0x1", "friendly_name": "LR Bulb"}],
    )
    _fire(hass, LR_SWITCH, {"action": "up_held"})
    await hass.async_block_till_done()
    assert coordinator.data["held"]["living_room.overhead"]["mode"] == "manual"
    assert published(mqtt_mock) == {}  # the binding moves the bulbs, not Light Man

    # Before the level is known the source is addressed per-room, room untouched.
    coordinator._invalidate_source("overhead")
    await tick(hass, coordinator, clock)
    assert LR_SET not in published(mqtt_mock)
    assert KIT_SET in published(mqtt_mock)

    _fire(hass, LR_SWITCH, {"action": "up_release"})
    await hass.async_block_till_done()
    gets = [c.args[0] for c in mqtt_mock.async_publish.call_args_list]
    assert "zigbee2mqtt/LR Bulb/get" in gets
    frame = hue_frame.encode(hue_frame.HueFrame(on=True, brightness=90, mirek=400))
    _fire(hass, "zigbee2mqtt/LR Bulb", {"philips_raw": frame.hex()})
    await hass.async_block_till_done()
    assert coordinator._manual_looks["living_room.overhead"] == {
        "brightness": 90,
        "color_temp": 400,
    }
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert pubs[LR_SET] == {"brightness": 90, "color_temp": 400, "transition": 0.4}
    assert OVERHEAD_ALL in pubs


async def test_manual_capture_guards(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    # Release while not manually held: nothing scheduled.
    _fire(hass, LR_SWITCH, {"action": "down_release"})
    await hass.async_block_till_done()
    assert coordinator._capture_room == {}
    # A room whose group members are unknown cannot be read.
    _fire(
        hass,
        "zigbee2mqtt/bridge/groups",
        [{"friendly_name": "zgb_living_room", "members": [{"ieee_address": "0x9"}]}],
    )
    await coordinator.async_hold("living_room.overhead", "manual")
    await hass.async_block_till_done()
    coordinator._schedule_manual_capture("living_room.overhead")
    assert coordinator._capture_room == {}
    assert coordinator._room_bulb("ghost.room") is None
    # A bulb report that is not for a pending capture, or is malformed, is ignored.
    coordinator._capture_room["Lone Bulb"] = "living_room.overhead"
    for payload in ("not json", {"no": "raw"}, {"philips_raw": "zz"}):
        _fire(hass, "zigbee2mqtt/Lone Bulb", payload)
    await coordinator._handle_bulb_state(
        _msg("zigbee2mqtt/Lone Bulb", {"philips_raw": "zz"})
    )
    await coordinator._handle_bulb_state(
        _msg("zigbee2mqtt/Lone Bulb", {"philips_raw": "0100" + "01"})
    )  # no brightness
    assert "living_room.overhead" not in coordinator._manual_looks
    # Once the room is no longer manual, a late report drops the pending capture.
    await coordinator.async_release_hold("living_room.overhead")
    frame = hue_frame.encode(hue_frame.HueFrame(brightness=10))
    await coordinator._handle_bulb_state(
        _msg("zigbee2mqtt/Lone Bulb", {"philips_raw": frame.hex()})
    )
    assert coordinator._capture_room == {}


class _Msg:
    def __init__(self, topic: str, payload: Any) -> None:
        self.topic = topic
        self.payload = json.dumps(payload)


def _msg(topic: str, payload: Any) -> Any:
    return _Msg(topic, payload)


async def test_device_topic_room_reads_its_own_bulb(coordinator: Coord) -> None:
    coordinator._group_members = {}
    assert coordinator._room_bulb("living_room.overhead") == "zgb_living_room"


# --- engine guards, stagger, helpers ----------------------------------------


async def test_a_broken_curve_skips_only_its_source(
    hass: HomeAssistant,
    coordinator: Coord,
    mqtt_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    with patch(
        "custom_components.light_man.coordinator.compute_target",
        side_effect=KeyError("min_br"),
    ):
        await coordinator.async_set_push_enabled(enabled=True)
        await hass.async_block_till_done()
    assert "skipping source" in caplog.text
    assert published(mqtt_mock) == {}


async def test_sources_are_staggered_across_the_interval(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    for key in coordinator._next_due:
        coordinator._next_due[key] = clock.now + 1  # not due
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    assert coordinator.update_interval is not None
    assert coordinator.update_interval.total_seconds() < coordinator._interval


async def test_room_glide_target_without_solar(coordinator: Coord) -> None:
    with patch(
        "custom_components.light_man.coordinator.solar_inputs",
        side_effect=ValueError("polar"),
    ):
        assert coordinator._room_glide_target("overhead") is None
    with patch.object(coordinator, "_engine_payload", side_effect=KeyError("x")):
        assert coordinator._room_glide_target("overhead") is None


async def test_reassert_unit_is_skipped_when_the_curve_is_unchanged(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    await coordinator.async_hold("kitchen.overhead", "night")
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await tick(hass, coordinator, clock)
    assert published(mqtt_mock) == {}
    assert coordinator.panel_state()["addressing"]["overhead"]["mode"] == "reassert"


def test_interactive_transition_scales_with_the_change() -> None:
    assert interactive_transition(None, {"brightness": 200}) == 0.4
    small = interactive_transition(
        {"brightness": 100, "color_temp": 300}, {"brightness": 105, "color_temp": 305}
    )
    big = interactive_transition(
        {"brightness": 76, "color_temp": 370}, {"brightness": 229, "color_temp": 286}
    )
    assert 0.4 <= small < 0.6
    assert 1.9 <= big <= 2.0
    mode_change = interactive_transition(
        {"brightness": 100, "color_temp": 300}, {"brightness": 100, "color": {"x": 0.3}}
    )
    assert mode_change == 0.8


def test_frame_to_payload() -> None:
    assert _frame_to_payload(hue_frame.HueFrame(on=True)) is None
    assert _frame_to_payload(hue_frame.HueFrame(brightness=5, xy=(0.3, 0.2))) == {
        "brightness": 5,
        "color": {"x": 0.3, "y": 0.2},
    }
    assert _frame_to_payload(hue_frame.HueFrame(brightness=5)) == {"brightness": 5}


async def test_bulb_report_without_a_pending_capture_is_ignored(
    coordinator: Coord,
) -> None:
    frame = hue_frame.encode(hue_frame.HueFrame(brightness=10))
    await coordinator._handle_bulb_state(
        _msg("zigbee2mqtt/Unasked Bulb", {"philips_raw": frame.hex()})
    )
    assert coordinator._manual_looks == {}


async def test_stagger_spreads_sources_over_the_interval(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    """After the boot push each source is due on its own slot, ~interval/n apart."""
    await _enabled(hass, coordinator, mqtt_mock)
    due = sorted(coordinator._next_due.values())
    gaps = [b - a for a, b in itertools.pairwise(due)]
    step = coordinator._interval / len(coordinator._next_due)
    assert all(abs(gap - step) < 1e-6 for gap in gaps)
    # A forced push keeps the slots: no re-synchronisation.
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    assert sorted(coordinator._next_due.values()) == due
    # Advancing one slot makes exactly one more source due.
    clock.advance(step)
    sent: list[str] = []
    with patch.object(
        coordinator,
        "_plan_source",
        side_effect=lambda key, *_a, **_k: sent.append(key) or [],
    ):
        await coordinator.async_refresh()
    assert len(sent) == 1
