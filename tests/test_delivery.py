"""0.8.0 delivery: Z2M feedback, outage replay, manual-hold capture, stagger, guards."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.light_man import hue_frame
from custom_components.light_man.const import MANUAL_CAPTURE_REPLY_S
from custom_components.light_man.coordinator import (
    _frame_to_payload,
    interactive_transition,
)
from custom_components.light_man.publisher import (
    PRIORITY_INTERACTIVE,
    PRIORITY_TICK,
    Send,
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


async def test_busy_on_a_consolidated_topic_replans_flood_with_reasserts(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """Review blocker 3: a re-sent flood always carries the held-room re-asserts."""
    await _enabled(hass, coordinator, mqtt_mock)
    await coordinator.async_hold("kitchen.overhead", "night")
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    _fire(
        hass, "zigbee2mqtt/bridge/logging", _failure("zgb_overhead_all", "status=BUSY")
    )
    await hass.async_block_till_done()
    topics = [c.args[0] for c in mqtt_mock.async_publish.call_args_list]
    assert topics == [OVERHEAD_ALL, KIT_SET]  # one unit: flood, then re-assert
    assert published(mqtt_mock)[KIT_SET]["transition"] == 0.4
    stats = coordinator.diagnostics["delivery_failures"]
    assert stats["by_kind"] == {"busy": 1}
    assert stats["by_topic"] == {OVERHEAD_ALL: 1}
    assert stats["last"]["kind"] == "busy"


async def test_busy_on_a_room_topic_is_resent_once_without_state(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    _fire(hass, LR_SWITCH, {"state": "ON"})  # first report after start: recorded
    await hass.async_block_till_done()
    _fire(hass, LR_SWITCH, {"state": "OFF"})  # stage-off carries state: OFF
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    for _ in range(2):
        _fire(
            hass,
            "zigbee2mqtt/bridge/logging",
            _failure("zgb_living_room", "status=BUSY"),
        )
        await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert set(pubs) == {LR_SET}
    assert "state" not in pubs[LR_SET]  # a stale OFF is never re-sent
    assert len(mqtt_mock.async_publish.call_args_list) == 1  # once


async def test_stale_failure_for_an_old_room_send_is_not_resent(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    await coordinator.async_hold("living_room.overhead", "night")
    await hass.async_block_till_done()
    clock.advance(60)  # long after Light Man's send: someone else's failure
    mqtt_mock.async_publish.reset_mock()
    _fire(
        hass, "zigbee2mqtt/bridge/logging", _failure("zgb_living_room", "status=BUSY")
    )
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


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
    topics = [c.args[0] for c in mqtt_mock.async_publish.call_args_list]
    assert topics == [OVERHEAD_ALL, KIT_SET]  # the source re-planned as one unit
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
    coordinator._outage_topics.add("zigbee2mqtt/not-ours/set")  # unknown: skipped
    mqtt_mock.async_publish.reset_mock()
    coordinator._on_mqtt_status(True)
    await hass.async_block_till_done()
    assert set(published(mqtt_mock)) == {OVERHEAD_ALL, KIT_SET}
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
    assert coordinator._modes.look_of("living_room.overhead") == {
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
    assert coordinator._capture_started == {}
    # A room whose group members are unknown cannot be read.
    _fire(
        hass,
        "zigbee2mqtt/bridge/groups",
        [{"friendly_name": "zgb_living_room", "members": [{"ieee_address": "0x9"}]}],
    )
    await coordinator.async_hold("living_room.overhead", "manual")
    await hass.async_block_till_done()
    coordinator._schedule_manual_capture("living_room.overhead")
    assert coordinator._capture_started == {}
    assert coordinator._room_bulbs("ghost.room") == []
    # Messages before our /get, malformed ones, or without brightness are ignored.
    coordinator._capture_room["Lone Bulb"] = "living_room.overhead"
    coordinator._capture_asked["Lone Bulb"] = clock.now + 5  # /get not yet sent
    good = hue_frame.encode(hue_frame.HueFrame(brightness=10)).hex()
    await coordinator._handle_bulb_state(
        _msg("zigbee2mqtt/Lone Bulb", {"philips_raw": good})
    )
    assert coordinator._modes.look_of("living_room.overhead") is None
    coordinator._capture_asked["Lone Bulb"] = clock.now
    for payload in ({"no": "raw"}, {"philips_raw": "zz"}, {"philips_raw": "010001"}):
        coordinator._capture_room["Lone Bulb"] = "living_room.overhead"
        coordinator._capture_asked["Lone Bulb"] = clock.now
        await coordinator._handle_bulb_state(_msg("zigbee2mqtt/Lone Bulb", payload))
    assert coordinator._modes.look_of("living_room.overhead") is None
    # Once the room is no longer manual, a late reply is dropped.
    await coordinator.async_release_hold("living_room.overhead")
    coordinator._capture_room["Lone Bulb"] = "living_room.overhead"
    coordinator._capture_asked["Lone Bulb"] = clock.now
    await coordinator._handle_bulb_state(
        _msg("zigbee2mqtt/Lone Bulb", {"philips_raw": good})
    )
    assert coordinator._capture_room == {}


async def test_restored_manual_hold_is_read_at_startup_and_defers_its_source(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    """Review item 6: a manual hold restored without a look is read, not fanned out."""
    replied = asyncio.Event()

    async def gated_sleep(seconds: float) -> None:
        if seconds == MANUAL_CAPTURE_REPLY_S:
            await replied.wait()  # the bulb's reply lands inside the 3 s wait
        else:
            await clock.sleep(seconds)

    patcher = patch("custom_components.light_man.coordinator._pace_sleep", gated_sleep)
    patcher.start()
    await coordinator._modes.set_held(
        "living_room.overhead",
        kind="manual",
        armed_at=dt_util.utcnow(),
        expires_at=dt_util.utcnow() + timedelta(hours=4),
    )
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
    await hass.async_block_till_done()
    assert "living_room.overhead" in coordinator._capture_started
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert OVERHEAD_ALL not in pubs  # deferred while the level is read
    assert LR_SET not in pubs
    gets = [c.args[0] for c in mqtt_mock.async_publish.call_args_list]
    assert "zigbee2mqtt/LR Bulb/get" in gets
    frame = hue_frame.encode(hue_frame.HueFrame(on=True, brightness=40, mirek=420))
    _fire(hass, "zigbee2mqtt/LR Bulb", {"philips_raw": frame.hex()})
    await hass.async_block_till_done()
    replied.set()
    patcher.stop()
    assert coordinator._modes.look_of("living_room.overhead") == {
        "brightness": 40,
        "color_temp": 420,
    }


async def test_capture_gives_up_after_its_tries(
    hass: HomeAssistant,
    coordinator: Coord,
    mqtt_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await coordinator.async_hold("living_room.overhead", "manual")
    await hass.async_block_till_done()
    _fire(
        hass,
        "zigbee2mqtt/bridge/groups",
        [{"friendly_name": "zgb_living_room", "members": [{"ieee_address": "0x1"}]}],
    )
    _fire(
        hass,
        "zigbee2mqtt/bridge/devices",
        [{"ieee_address": "0x1", "friendly_name": "Silent Bulb"}],
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "could not read the dimmed level" in caplog.text
    assert not coordinator._capture_pending("living_room.overhead")


class _Msg:
    def __init__(self, topic: str, payload: Any) -> None:
        self.topic = topic
        self.payload = json.dumps(payload)


def _msg(topic: str, payload: Any) -> Any:
    return _Msg(topic, payload)


async def test_device_topic_room_reads_its_own_bulb(coordinator: Coord) -> None:
    coordinator._group_members = {}
    assert coordinator._room_bulbs("living_room.overhead") == ["zgb_living_room"]


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
    assert coordinator._capture_room == {}


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


# --- review-fix coverage ------------------------------------------------------


async def test_unload_tears_down_bulb_subscriptions_and_stops_sending(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    calls: list[str] = []
    coordinator._bulb_unsubs["LR Bulb"] = lambda: calls.append("unsub")
    coordinator.shutdown_subscriptions()
    assert calls == ["unsub"]
    assert coordinator._bulb_unsubs == {}
    mqtt_mock.async_publish.reset_mock()
    coordinator._enqueue([Send(LR_SET, {"brightness": 1})], priority=PRIORITY_TICK)
    await coordinator._resend(LR_SET)
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


async def test_per_room_fallback_is_split_into_budget_sized_units(
    coordinator: Coord,
) -> None:
    coordinator._group_topics = None  # every topic counts as a group
    sends = [Send(f"zigbee2mqtt/g{i}/set", {"brightness": i}) for i in range(9)]
    queued: list[list[str]] = []
    with patch.object(
        coordinator,
        "_enqueue",
        side_effect=lambda s, **_k: queued.append([x.topic for x in s]),
    ):
        first = coordinator._chunk_per_room(sends)
    assert len(first) == 6
    assert queued == [[f"zigbee2mqtt/g{i}/set" for i in range(6, 9)]]


def test_forced_catch_up_counts_colour_mode_changes(coordinator: Coord) -> None:
    coordinator._last_published[LR_SET] = {"brightness": 100, "color_temp": 300}
    seconds = coordinator._tick_transition(
        LR_SET, {"brightness": 100, "color": {"x": 0.3, "y": 0.3}}, True, force=True
    )
    assert seconds == 2.0
    seconds = coordinator._tick_transition(
        LR_SET, {"brightness": 100, "color_temp": 500}, True, force=True
    )
    assert seconds == 4.0  # 200 mired * 0.02 s


def test_shown_prefers_the_newer_of_room_and_flood_marks(coordinator: Coord) -> None:
    coordinator._last_published[LR_SET] = {"brightness": 10}
    coordinator._marked_at[LR_SET] = 5.0
    coordinator._last_published[OVERHEAD_ALL] = {"brightness": 200}
    coordinator._marked_at[OVERHEAD_ALL] = 9.0
    assert coordinator._shown(LR_SET) == {"brightness": 200}
    coordinator._marked_at[LR_SET] = 10.0
    assert coordinator._shown(LR_SET) == {"brightness": 10}
    del coordinator._last_published[OVERHEAD_ALL]
    assert coordinator._shown(LR_SET) == {"brightness": 10}
    assert coordinator._shown("zigbee2mqtt/not-a-room/set") is None


async def test_occupancy_resend_keeps_its_turn_on(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    topic = "zigbee2mqtt/Hall Center/set"
    coordinator._enqueue(
        [Send(topic, {"brightness": 50, "state": "ON", "transition": 1.5})],
        priority=PRIORITY_INTERACTIVE,
    )
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator._resend(topic)
    await hass.async_block_till_done()
    assert published(mqtt_mock)[topic]["state"] == "ON"


async def test_resend_after_unload_is_dropped(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    await coordinator.async_hold("living_room.overhead", "night")
    await hass.async_block_till_done()

    async def close_during_sleep(_s: float) -> None:
        coordinator._closed = True

    mqtt_mock.async_publish.reset_mock()
    with patch(
        "custom_components.light_man.coordinator._pace_sleep", close_during_sleep
    ):
        await coordinator._resend(LR_SET)
    assert published(mqtt_mock) == {}


async def test_capture_stops_when_the_hold_ends_or_a_look_arrives(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await coordinator.async_hold("living_room.overhead", "manual")
    await coordinator._modes.set_look("living_room.overhead", {"brightness": 5})
    mqtt_mock.async_publish.reset_mock()
    await coordinator._capture_manual_look("living_room.overhead", ["A", "B"])
    assert published(mqtt_mock) == {}  # a look already exists: nothing asked
    await coordinator.async_release_hold("living_room.overhead")
    await coordinator._capture_manual_look("living_room.overhead", ["A"])
    gets = [c.args[0] for c in mqtt_mock.async_publish.call_args_list]
    assert "zigbee2mqtt/A/get" not in gets
    assert await coordinator._modes.set_look("ghost", {"brightness": 1}) is False


# --- gate re-review (N1-N7) ---------------------------------------------------


async def test_reassert_busy_retries_again_after_a_new_plan(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    """N1: the one-retry limit is per plan, not once per topic for ever."""
    await _enabled(hass, coordinator, mqtt_mock)
    await coordinator.async_hold("kitchen.overhead", "night")
    await hass.async_block_till_done()
    for _ in range(2):
        coordinator._invalidate_source("overhead")
        await tick(hass, coordinator, clock)  # a new plan re-asserts the kitchen
        mqtt_mock.async_publish.reset_mock()
        _fire(
            hass,
            "zigbee2mqtt/bridge/logging",
            {**_failure("zgb_kitchen", "x"), "message": BUSY_KITCHEN},
        )
        await hass.async_block_till_done()
        assert KIT_SET in published(mqtt_mock)


async def test_zone_clear_off_is_retried_as_off(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """N2: a BUSY zone-clear OFF is re-sent with its OFF."""
    from .conftest import HALL_OFF, MMWAVE_EAST

    await _enabled(hass, coordinator, mqtt_mock)
    _fire(hass, MMWAVE_EAST, {"occupancy": True})
    await hass.async_block_till_done()
    _fire(hass, MMWAVE_EAST, {"occupancy": False})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, "zigbee2mqtt/bridge/logging", _failure("zgb_hallwayf", "status=BUSY"))
    await hass.async_block_till_done()
    assert published(mqtt_mock)[HALL_OFF]["state"] == "OFF"


async def test_force_survives_replacement_by_an_unforced_unit(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """N4: an unforced tick replacing a queued forced unit stays forced."""
    await _enabled(hass, coordinator, mqtt_mock)
    coordinator._force_pending["overhead"] = 99  # a forced unit is waiting
    mqtt_mock.async_publish.reset_mock()
    coordinator._enqueue_source("overhead", force=False)  # the timer's replacement
    await hass.async_block_till_done()
    assert OVERHEAD_ALL in published(mqtt_mock)  # unchanged curve, still sent
    assert "overhead" not in coordinator._force_pending


async def test_busy_replans_of_a_flood_are_capped_per_interval(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    """N5: at most one forced re-plan of a source per interval."""
    await _enabled(hass, coordinator, mqtt_mock)
    busy = _failure("zgb_overhead_all", "status=BUSY")
    mqtt_mock.async_publish.reset_mock()
    for _ in range(3):
        _fire(hass, "zigbee2mqtt/bridge/logging", busy)
        await hass.async_block_till_done()
    sends = [
        c for c in mqtt_mock.async_publish.call_args_list if c.args[0] == OVERHEAD_ALL
    ]
    assert len(sends) == 1
    clock.advance(coordinator._interval)
    _fire(hass, "zigbee2mqtt/bridge/logging", busy)
    await hass.async_block_till_done()
    sends = [
        c for c in mqtt_mock.async_publish.call_args_list if c.args[0] == OVERHEAD_ALL
    ]
    assert len(sends) == 2


async def test_restored_capture_waits_for_the_group_list(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """N6: bridge/devices before bridge/groups does not start a capture yet."""
    await coordinator._modes.set_held(
        "living_room.overhead",
        kind="manual",
        armed_at=dt_util.utcnow(),
        expires_at=dt_util.utcnow() + timedelta(hours=4),
    )
    _fire(
        hass,
        "zigbee2mqtt/bridge/devices",
        [{"ieee_address": "0x1", "friendly_name": "LR Bulb"}],
    )
    await hass.async_block_till_done()
    assert "living_room.overhead" not in coordinator._capture_started
    _fire(
        hass,
        "zigbee2mqtt/bridge/groups",
        [{"friendly_name": "zgb_living_room", "members": [{"ieee_address": "0x1"}]}],
    )
    await hass.async_block_till_done()
    assert "living_room.overhead" in coordinator._capture_started


async def test_unanswered_gets_are_forgotten(coordinator: Coord) -> None:
    """N7: a bulb that never answered cannot supply a look later."""
    coordinator._capture_room["Slow Bulb"] = "living_room.overhead"
    coordinator._capture_asked["Slow Bulb"] = 0.0
    coordinator._capture_room["Other Bulb"] = "kitchen.overhead"
    coordinator._forget_capture("living_room.overhead")
    assert "Slow Bulb" not in coordinator._capture_room
    assert "Slow Bulb" not in coordinator._capture_asked
    assert coordinator._capture_room == {"Other Bulb": "kitchen.overhead"}


# --- 0.8.1 ------------------------------------------------------------------


async def test_first_switch_report_after_start_sends_nothing(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """The retained switch states at startup must not re-stage every room."""
    await _enabled(hass, coordinator, mqtt_mock)
    await coordinator.async_hold("living_room.overhead", "night")
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, LR_SWITCH, {"state": "OFF"})  # first report: recorded only
    _fire(hass, "zigbee2mqtt/Kitchen Switch", {"state": "ON"})
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    assert not coordinator._modes.is_held("living_room.overhead")  # off releases


async def test_switch_off_after_the_first_report_releases_and_stages(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    _fire(hass, LR_SWITCH, {"state": "ON"})
    await hass.async_block_till_done()
    await coordinator.async_hold("living_room.overhead", "night")
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, LR_SWITCH, {"state": "OFF"})
    await hass.async_block_till_done()
    assert published(mqtt_mock)[LR_SET]["state"] == "OFF"
    assert not coordinator._modes.is_held("living_room.overhead")


async def test_state_updates_do_not_postpone_the_tick_timer(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """A tap refreshes entities without rescheduling the periodic refresh."""
    await _enabled(hass, coordinator, mqtt_mock)
    with patch.object(coordinator, "_schedule_refresh") as reschedule:
        await coordinator.async_hold("living_room.overhead", "night")
        await coordinator.async_release_hold("living_room.overhead")
        await hass.async_block_till_done()
    reschedule.assert_not_called()
    assert coordinator.data["held"] == {}


async def test_a_newer_capture_cancels_the_older_one(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    gate = asyncio.Event()

    async def blocked(_seconds: float) -> None:
        await gate.wait()

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
    await hass.async_block_till_done()
    with patch("custom_components.light_man.coordinator._pace_sleep", blocked):
        coordinator._schedule_manual_capture("living_room.overhead")
        first = coordinator._capture_tasks["living_room.overhead"]
        coordinator._schedule_manual_capture("living_room.overhead")
        second = coordinator._capture_tasks["living_room.overhead"]
        await asyncio.sleep(0)
        assert first.cancelled() or first.cancelling()
        assert second is not first
        coordinator.shutdown_subscriptions()  # unload cancels the running one
        await asyncio.sleep(0)
        assert second.cancelled() or second.cancelling()
        assert coordinator._capture_tasks == {}
        gate.set()


async def test_capture_unloaded_while_subscribing_drops_its_subscription(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    calls: list[str] = []

    async def subscribe(*_a: Any, **_k: Any) -> Any:
        coordinator._closed = True  # unload lands during the subscribe
        return lambda: calls.append("unsub")

    await coordinator._modes.set_held(
        "living_room.overhead",
        kind="manual",
        armed_at=dt_util.utcnow(),
        expires_at=dt_util.utcnow() + timedelta(hours=4),
    )
    with patch(
        "custom_components.light_man.coordinator.mqtt.async_subscribe", subscribe
    ):
        await coordinator._capture_manual_look("living_room.overhead", ["LR Bulb"])
    assert calls == ["unsub"]
    assert "LR Bulb" not in coordinator._bulb_unsubs


async def test_an_older_unit_does_not_clear_a_newer_force(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    used: list[int | None] = [1]
    coordinator._force_pending["overhead"] = 2  # a newer force request
    done: asyncio.Future[list[Send]] = hass.loop.create_future()
    done.set_result([Send(OVERHEAD_ALL, {"brightness": 1})])
    coordinator._on_source_sent("overhead", used, done)
    assert coordinator._force_pending["overhead"] == 2


async def test_a_send_reported_failed_is_not_marked_by_its_late_ack(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    coordinator._planned_at[KIT_SET] = clock.now
    coordinator._last_published.pop(KIT_SET, None)
    clock.advance(0.2)
    coordinator._failed_at[KIT_SET] = clock.now  # BUSY before the unit resolved
    done: asyncio.Future[list[Send]] = hass.loop.create_future()
    done.set_result([Send(KIT_SET, {"brightness": 9, "transition": 0.4})])
    coordinator._on_sent(done)
    assert KIT_SET not in coordinator._last_published


async def test_per_room_dedup_compares_with_what_the_room_shows(
    coordinator: Coord, clock: FakeClock
) -> None:
    """An old room mark equal to the target is not "unchanged" after a newer flood."""
    payload = {"brightness": 100, "color_temp": 300}
    coordinator._last_published[KIT_SET] = dict(payload)
    coordinator._marked_at[KIT_SET] = clock.now
    clock.advance(60)
    coordinator._last_published[OVERHEAD_ALL] = {"brightness": 200, "color_temp": 250}
    coordinator._marked_at[OVERHEAD_ALL] = clock.now
    assert not coordinator._unchanged(KIT_SET, payload)


async def test_plan_diagnostics_count_once_per_unit(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    before = coordinator.diagnostics["dedup_skips"]
    await tick(hass, coordinator, clock)  # nothing changed at a frozen sun
    skips = coordinator.diagnostics["dedup_skips"] - before
    assert skips == len(coordinator._next_due)  # one per source, not per re-plan


async def test_a_source_due_within_the_timer_slack_goes_now(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    key, due = min(coordinator._next_due.items(), key=lambda kv: kv[1])
    clock.now = due - 0.9  # HA's whole-second timer fired a little early
    sent: list[str] = []
    with patch.object(
        coordinator,
        "_plan_source",
        side_effect=lambda k, *_a, **_k: sent.append(k) or [],
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert sent == [key]
    assert coordinator._next_due[key] == due + coordinator._interval


def test_next_slot_always_moves_past_the_slot_it_is_given(coordinator: Coord) -> None:
    """0.8.3: an early send passes the slot itself; it must get the next slot.

    Float rounding of ``slot - epoch - offset`` used to return the same slot for
    some clock values, so every source was sent twice per interval.
    """
    import random

    import pytest

    rng = random.Random(20261103)  # noqa: S311  # test data, not crypto
    for _ in range(2000):
        coordinator._epoch = rng.uniform(1e3, 1e7)
        for key in coordinator._stagger:
            due = coordinator._next_slot(key, coordinator._epoch + rng.uniform(0, 1e5))
            assert coordinator._next_slot(key, due) == pytest.approx(
                due + coordinator._interval
            )


async def test_stale_off_right_after_an_up_tap_is_ignored(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    """0.8.4: Z2M's cached OFF published just after an up tap must not stage off.

    Live 2026-11-04 03:40: up tap, then 0.37 s later a front-switch message with
    the pre-tap cached state OFF; Light Man staged the room off.
    """
    await _enabled(hass, coordinator, mqtt_mock)
    _fire(hass, LR_SWITCH, {"state": "OFF"})  # first report after start
    await hass.async_block_till_done()
    _fire(hass, LR_SWITCH, {"action": "up_single"})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    clock.advance(0.4)
    _fire(hass, LR_SWITCH, {"state": "OFF", "power": 0.2})  # stale cache
    await hass.async_block_till_done()
    assert LR_SET not in published(mqtt_mock)
    clock.advance(3.0)  # after the grace window a real OFF report still counts
    _fire(hass, LR_SWITCH, {"state": "OFF"})
    await hass.async_block_till_done()
    assert published(mqtt_mock)[LR_SET]["state"] == "OFF"
