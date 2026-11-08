"""Delivery: Z2M feedback, outage replay, held-paddle dimming, stagger, guards."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.light_man.bars import HEIGHT_TRANSITION_S
from custom_components.light_man.const import (
    BAR_HEIGHT_QUIET_S,
    DIM_FLOOR,
    DIM_LEAD_S,
    DIM_RATE,
    DIM_STOP_TRANSITION_S,
    DIM_TIMEOUT_S,
)
from custom_components.light_man.coordinator import _Dim, interactive_transition
from custom_components.light_man.models import EngineTarget
from custom_components.light_man.publisher import (
    PRIORITY_INTERACTIVE,
    PRIORITY_TICK,
    Send,
)

from .conftest import (
    KIT_SET,
    KIT_SWITCH,
    LR_SET,
    LR_SWITCH,
    OVERHEAD_ALL,
    published,
    tick,
)

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


# --- bridge/groups ----------------------------------------------------------


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
    assert [t for t in published(mqtt_mock) if not t.endswith("/get")] == []


async def test_no_send_before_the_step_is_due(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    coordinator._due = clock.now + 100  # not due
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    assert coordinator.update_interval is not None
    assert coordinator.update_interval.total_seconds() == coordinator._steps["min_s"]


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


async def test_every_source_steps_on_one_schedule(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    """All sources go out together each step; a forced push keeps the schedule."""
    await _enabled(hass, coordinator, mqtt_mock)
    due = coordinator._due
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    assert coordinator._due == due
    clock.now = due
    sent: list[str] = []
    with patch.object(
        coordinator,
        "_plan_source",
        side_effect=lambda key, *_a, **_k: sent.append(key) or [],
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert sorted(sent) == sorted(coordinator._config["sources"])
    assert coordinator._due == due + coordinator._step_s


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
    assert published(mqtt_mock) == {}  # the curve is staged later
    assert "living_room.overhead" in coordinator._off_stages
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
    assert skips == len(coordinator._config["sources"])  # one per source, once


async def test_a_step_due_within_the_timer_slack_goes_now(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    due = coordinator._due
    clock.now = due - 0.9  # HA's whole-second timer fired a little early
    sent: list[str] = []
    with patch.object(
        coordinator,
        "_plan_source",
        side_effect=lambda k, *_a, **_k: sent.append(k) or [],
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert len(sent) == len(coordinator._config["sources"])
    assert coordinator._due == due + coordinator._step_s


def _target(pct: float, kelvin: float | None = 4000.0, **kw: Any) -> EngineTarget:
    mode = kw.pop("mode", "color_temp")
    return EngineTarget(pct, mode, kelvin, kw.pop("rgb", None), kw.pop("xy", None))


def test_plan_step_is_the_interval_on_a_flat_curve(coordinator: Coord) -> None:
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    with patch.object(coordinator, "_curve_target", return_value=_target(50)):
        assert coordinator._plan_step(now) == coordinator._interval


def test_plan_step_shortens_on_a_ramp_but_not_below_the_minimum(
    coordinator: Coord,
) -> None:
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

    def ramp(rate: float) -> Any:
        return lambda _src, when: _target(50 + rate * (when - now).total_seconds())

    coordinator._interval = 240
    with patch.object(coordinator, "_curve_target", side_effect=ramp(2.5 / 60)):
        assert coordinator._plan_step(now) == 30.0  # 2.5 %/min: 2 % fits in 30 s
    with patch.object(coordinator, "_curve_target", side_effect=ramp(1 / 60)):
        assert coordinator._plan_step(now) == 120.0  # 1 %/min: 2 % in 120 s
    with patch.object(coordinator, "_curve_target", side_effect=ramp(1.0)):
        assert coordinator._plan_step(now) == coordinator._steps["min_s"]


def test_plan_step_includes_an_interval_off_the_grid(coordinator: Coord) -> None:
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    coordinator._interval = 100  # 30, 60, 90 and then 100 itself
    with patch.object(coordinator, "_curve_target", return_value=_target(50)):
        assert coordinator._plan_step(now) == 100


def test_plan_step_ignores_a_curve_without_data(coordinator: Coord) -> None:
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    with patch.object(coordinator, "_curve_target", return_value=None):
        assert coordinator._plan_step(now) == coordinator._interval
    calls = iter([_target(50), None])
    with patch.object(
        coordinator,
        "_curve_target",
        side_effect=lambda *_a: next(calls, _target(50)),
    ):
        assert coordinator._plan_step(now) == coordinator._interval


def test_close_judges_mired_xy_and_rgb(coordinator: Coord) -> None:
    steps = coordinator._steps
    assert coordinator._close(_target(50, 4000), _target(51, 4000))
    assert not coordinator._close(
        _target(50), _target(50 + steps["max_brightness_pct"] + 1)
    )
    assert not coordinator._close(_target(50, 4000), _target(50, 3700))  # ~20 mired
    rgb = {"mode": "rgb", "rgb": (135, 206, 235)}
    assert coordinator._close(_target(50, None, **rgb), _target(50, None, **rgb))
    far = {"mode": "rgb", "rgb": (255, 120, 30)}
    assert not coordinator._close(_target(50, None, **rgb), _target(50, None, **far))
    xy_a = {"mode": "xy", "xy": (0.40, 0.40)}
    xy_b = {"mode": "xy", "xy": (0.40, 0.401)}
    assert coordinator._close(_target(50, None, **xy_a), _target(50, None, **xy_b))
    blank = {"mode": "rgb"}
    assert coordinator._close(_target(50, None, **blank), _target(50, None, **blank))
    # mixed modes compare by chromaticity (CCT as xy)
    assert coordinator._close(_target(50, 4000), _target(50, 4000, mode="xy"))


def test_curve_target_is_none_without_sun_or_profile(coordinator: Coord) -> None:
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    source = coordinator._config["sources"]["overhead"]
    with patch(
        "custom_components.light_man.coordinator.solar_inputs",
        side_effect=ValueError("polar"),
    ):
        assert coordinator._curve_target(source, now) is None
    with patch(
        "custom_components.light_man.coordinator.compute_target",
        side_effect=KeyError("min_br"),
    ):
        assert coordinator._curve_target(source, now) is None


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
    assert coordinator._paddle_on[LR_SWITCH]
    clock.advance(3.0)  # after the grace window a real OFF report still counts
    _fire(hass, LR_SWITCH, {"state": "OFF"})
    await hass.async_block_till_done()
    assert not coordinator._paddle_on[LR_SWITCH]
    assert LR_SET not in published(mqtt_mock)


# --- held-paddle dimming ------------------------------------------------------

LR = "living_room.overhead"
LR_COLOUR = {"color_temp": 300}


async def _lit_at(
    hass: HomeAssistant,
    coordinator: Coord,
    mqtt_mock: Any,
    clock: FakeClock,
    level: int,
) -> None:
    """Living room on, showing ``level``; nothing published yet."""
    await _enabled(hass, coordinator, mqtt_mock)
    _fire(hass, LR_SWITCH, {"state": "ON"})
    await hass.async_block_till_done()
    coordinator._last_published[LR_SET] = {"brightness": level, **LR_COLOUR}  # type: ignore[typeddict-item]
    coordinator._marked_at[LR_SET] = clock.now
    _fire(hass, "zigbee2mqtt/bridge/devices", _DEVICES)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()


def _switch(name: str, *targets: dict[str, Any]) -> dict[str, Any]:
    bindings = [{"cluster": "genLevelCtrl", "target": t} for t in targets]
    bindings.append({"cluster": "genOnOff", "target": {"type": "group", "id": 2}})
    return {"friendly_name": name, "endpoints": {"2": {"bindings": bindings}}}


_COORDINATOR = {"type": "endpoint", "ieee_address": "0xc0", "endpoint": 1}
# The living room switch reports to the coordinator only; the kitchen one still
# has a level binding to its lights.
_DEVICES: list[Any] = [
    {"type": "Coordinator", "ieee_address": "0xc0", "endpoints": {}},
    _switch("Living Room Switch", _COORDINATOR),
    _switch("Kitchen Switch", _COORDINATOR, {"type": "group", "id": 2}),
    {"friendly_name": "A sensor"},
    "junk",
]


async def _action(hass: HomeAssistant, action: str) -> None:
    _fire(hass, LR_SWITCH, {"action": action})
    await hass.async_block_till_done()


async def test_held_paddle_fades_and_the_release_holds_the_level(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _lit_at(hass, coordinator, mqtt_mock, clock, 183)
    held_at = clock.now
    await _action(hass, "down_held")
    duration = (183 - DIM_FLOOR) / DIM_RATE  # 2.0 s
    assert published(mqtt_mock)[LR_SET] == {
        "brightness": DIM_FLOOR,
        **LR_COLOUR,
        "transition": duration,
    }
    assert coordinator.data["held"][LR]["mode"] == "manual"
    assert coordinator._modes.look_of(LR) is None
    assert coordinator._dim_pending(LR)

    # While the paddle is held the source's flood waits.
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    assert OVERHEAD_ALL not in published(mqtt_mock)
    assert coordinator.diagnostics["addressing"]["overhead"]["mode"] == "deferred"
    await coordinator._finish_stale_dims()  # not stale yet
    assert coordinator._dim_pending(LR)

    clock.advance(0.5)
    mqtt_mock.async_publish.reset_mock()
    done = (clock.now - held_at + DIM_LEAD_S) / duration
    assert 0.3 < done < 1.0  # released part-way down
    level = round(183 + (DIM_FLOOR - 183) * done)
    await _action(hass, "down_release")
    look = {"brightness": level, **LR_COLOUR}
    assert published(mqtt_mock)[LR_SET] == {
        **look,
        "transition": DIM_STOP_TRANSITION_S,
    }
    assert coordinator._modes.look_of(LR) == look
    assert not coordinator._dim_pending(LR)

    # The level is held: every flood is followed by its re-assert.
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert pubs[LR_SET] == {**look, "transition": 0.4}
    assert OVERHEAD_ALL in pubs


async def test_hold_up_runs_to_the_ceiling_and_a_new_hold_starts_from_the_level(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _lit_at(hass, coordinator, mqtt_mock, clock, 164)
    await _action(hass, "up_held")
    assert published(mqtt_mock)[LR_SET]["brightness"] == 254
    assert published(mqtt_mock)[LR_SET]["transition"] == 1.0
    clock.advance(5.0)  # held past the end of the fade
    await _action(hass, "up_release")
    assert coordinator._modes.look_of(LR) == {"brightness": 254, **LR_COLOUR}

    # A second hold with no release in between ends the first one where it is.
    held_at = clock.now
    await _action(hass, "down_held")
    clock.advance(1.0)
    done = (clock.now - held_at + DIM_LEAD_S) / (251 / DIM_RATE)
    assert done < 1.0
    first_stop = round(254 + (DIM_FLOOR - 254) * done)
    await _action(hass, "down_held")
    fade = published(mqtt_mock)[LR_SET]
    assert fade["brightness"] == DIM_FLOOR
    assert fade["transition"] == round((first_stop - DIM_FLOOR) / DIM_RATE, 2)


async def test_held_paddle_guards(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    await _action(hass, "down_held")  # the switch is not known to be on
    await _action(hass, "down_release")  # no fade to stop
    assert published(mqtt_mock) == {}
    assert not coordinator._modes.is_held(LR)

    await _lit_at(hass, coordinator, mqtt_mock, clock, DIM_FLOOR)
    await _action(hass, "down_held")  # already at the floor
    assert published(mqtt_mock) == {}
    assert not coordinator._modes.is_held(LR)

    with (
        patch.object(coordinator, "_shown", return_value=None),
        patch.object(coordinator, "_room_glide_target", return_value=None),
    ):
        await _action(hass, "up_held")  # nothing known about the room's level
    assert published(mqtt_mock) == {}
    assert not coordinator._modes.is_held(LR)


async def test_a_tap_or_a_release_service_during_a_hold_ends_the_dim(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _lit_at(hass, coordinator, mqtt_mock, clock, 183)
    await _action(hass, "down_held")
    await _action(hass, "up_single")  # back to the curve
    assert not coordinator._modes.is_held(LR)
    assert coordinator._dims == {}

    coordinator._last_published[LR_SET] = {"brightness": 183, **LR_COLOUR}  # type: ignore[typeddict-item]
    coordinator._marked_at[LR_SET] = clock.now
    await _action(hass, "down_held")
    await coordinator.async_release_hold(LR)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await _action(hass, "down_release")  # the lights are no longer held
    assert published(mqtt_mock) == {}
    assert not coordinator._modes.is_held(LR)


async def test_a_lost_release_holds_the_end_of_the_fade(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _lit_at(hass, coordinator, mqtt_mock, clock, 183)
    await _action(hass, "down_held")
    await tick(hass, coordinator, clock)
    assert coordinator._dims == {}
    assert coordinator._modes.look_of(LR) == {"brightness": DIM_FLOOR, **LR_COLOUR}


def test_a_fade_is_linear_and_stays_inside_its_ends() -> None:
    dim = _Dim("t/set", ["r"], {"color_temp": 300}, 183, 3, 2.0, 100.0)
    assert dim.level_at(99.0) == 183  # before it began
    assert dim.level_at(101.0) == 93
    assert dim.level_at(109.0) == 3  # past its end


async def test_a_manual_hold_without_a_level_is_left_alone_by_the_flood(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    now = dt_util.utcnow()
    await coordinator._modes.set_held(
        LR, kind="manual", armed_at=now, expires_at=now + timedelta(hours=1)
    )
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert LR_SET not in pubs  # addressed per room, the held room skipped
    assert KIT_SET in pubs
    assert coordinator._room_topic("no_such.light") is None


# --- the bar's height follows the lights ---------------------------------------

LR_SWITCH_SET = f"{LR_SWITCH}/set"
PADDLE_GRACE_PLUS = 3.5  # past the stale-state grace after a tap


async def _report(hass: HomeAssistant, mqtt_mock: Any, payload: Any) -> Any:
    """Fire one switch message; return what was then written to the switch."""
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, LR_SWITCH, payload)
    await hass.async_block_till_done()
    return published(mqtt_mock).get(LR_SWITCH_SET)


async def test_the_bar_follows_the_lights_on_the_switch_own_on_report(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _lit_at(hass, coordinator, mqtt_mock, clock, 76)
    on_at_229 = {"state": "ON", "brightness": 229, "defaultLevelLocal": 229}
    assert await _report(hass, mqtt_mock, on_at_229) == {
        "brightness": 76,
        "transition": HEIGHT_TRANSITION_S,
    }
    assert coordinator._bars._default[LR_SWITCH] == 229  # its own start-up height
    # The switch's reports while it ramps to our level are not answered.
    assert await _report(hass, mqtt_mock, {"state": "ON", "brightness": 150}) is None
    clock.advance(BAR_HEIGHT_QUIET_S)
    assert await _report(hass, mqtt_mock, {"state": "ON", "brightness": 77}) is None
    # A tap's message carries the switch's cached level: never answered.
    tap = {"action": "config_double", "state": "ON", "brightness": 229}
    assert await _report(hass, mqtt_mock, tap) is None

    # After a config tap the bar goes to the look the lights were sent.
    look = coordinator._last_planned[LR_SET]["brightness"]
    assert look != 76
    assert await _report(hass, mqtt_mock, {"state": "ON", "brightness": 76}) == {
        "brightness": look,
        "transition": HEIGHT_TRANSITION_S,
    }


async def test_the_bar_is_left_alone_when_writing_its_height_is_not_safe(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _lit_at(hass, coordinator, mqtt_mock, clock, 183)
    await _action(hass, "down_held")  # the switch is ramping its own bar
    assert await _report(hass, mqtt_mock, {"state": "ON", "brightness": 150}) is None
    await _action(hass, "down_release")
    await _action(hass, "up_single")

    # Off: a level written now would turn the switch on without its lights.
    assert await _report(hass, mqtt_mock, {"state": "OFF", "brightness": 40}) is None
    assert await _report(hass, mqtt_mock, {"brightness": 40}) is None

    _fire(hass, LR_SWITCH, {"state": "ON"})
    await hass.async_block_till_done()
    coordinator._last_published.clear()
    coordinator._last_planned.clear()
    coordinator._planned_at.clear()  # nothing known about the lights' level
    assert await _report(hass, mqtt_mock, {"state": "ON", "brightness": 40}) is None


async def test_a_switch_that_still_forwards_its_level_keeps_its_bar(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _enabled(hass, coordinator, mqtt_mock)
    on_at_40 = {"state": "ON", "brightness": 40}
    _fire(hass, LR_SWITCH, on_at_40)  # Z2M's device list not seen yet
    await hass.async_block_till_done()
    assert LR_SWITCH_SET not in published(mqtt_mock)

    await _lit_at(hass, coordinator, mqtt_mock, clock, 183)
    for bad in ("not json", {"devices": []}):
        _fire(hass, "zigbee2mqtt/bridge/devices", bad)
    await hass.async_block_till_done()
    assert coordinator._level_bound == {KIT_SWITCH}
    coordinator._last_published[KIT_SET] = {"brightness": 183, **LR_COLOUR}  # type: ignore[typeddict-item]
    coordinator._marked_at[KIT_SET] = clock.now
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, KIT_SWITCH, on_at_40)
    await hass.async_block_till_done()
    assert f"{KIT_SWITCH}/set" not in published(mqtt_mock)  # it would reach the lights
    assert await _report(hass, mqtt_mock, on_at_40) is not None  # living room: safe


# --- a hold must last; a hold on an off switch is undone ------------------------


class _Gate:
    """Stands in for the dim start's wait: it ends when the test opens it."""

    def __init__(self) -> None:
        self.event = asyncio.Event()

    async def sleep(self, _seconds: float) -> None:
        await self.event.wait()


async def test_a_hold_released_before_the_delay_does_not_dim(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _lit_at(hass, coordinator, mqtt_mock, clock, 183)
    gate = _Gate()
    with patch("custom_components.light_man.coordinator._dim_sleep", gate.sleep):
        _fire(hass, LR_SWITCH, {"action": "down_held"})
        await asyncio.sleep(0)
        assert LR_SWITCH in coordinator._dim_starts
        await _action(hass, "down_release")  # a press that lingered
        assert coordinator._dim_starts == {}
        assert published(mqtt_mock) == {}
        assert not coordinator._modes.is_held(LR)

        # A tap also ends a hold that has not begun; so does unloading.
        _fire(hass, LR_SWITCH, {"action": "down_held"})
        await asyncio.sleep(0)
        await _action(hass, "up_single")
        assert coordinator._dim_starts == {}
        _fire(hass, LR_SWITCH, {"action": "up_held"})
        await asyncio.sleep(0)
        task = coordinator._dim_starts[LR_SWITCH]
        coordinator.shutdown_subscriptions()
        await hass.async_block_till_done()
        assert task.cancelled()
        assert coordinator._dim_starts == {}


async def test_a_hold_past_the_delay_dims(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _lit_at(hass, coordinator, mqtt_mock, clock, 183)
    gate = _Gate()
    with patch("custom_components.light_man.coordinator._dim_sleep", gate.sleep):
        _fire(hass, LR_SWITCH, {"action": "down_held"})
        await asyncio.sleep(0)
        assert published(mqtt_mock) == {}
        gate.event.set()
        await hass.async_block_till_done()
    assert published(mqtt_mock)[LR_SET]["brightness"] == DIM_FLOOR
    assert coordinator._dim_starts == {}


async def test_a_hold_on_an_off_switch_turns_the_switch_back_off(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _lit_at(hass, coordinator, mqtt_mock, clock, 183)
    await _action(hass, "down_single")
    clock.advance(5.0)
    mqtt_mock.async_publish.reset_mock()

    await _action(hass, "up_held")
    assert published(mqtt_mock) == {LR_SWITCH_SET: {"state": "OFF"}}
    # The switch reports itself on under the held paddle: not a press.
    clock.advance(PADDLE_GRACE_PLUS)
    mqtt_mock.async_publish.reset_mock()
    assert await _report(hass, mqtt_mock, {"state": "ON", "brightness": 200}) is None
    assert coordinator._paddle_on[LR_SWITCH] is False
    assert LR_SET not in published(mqtt_mock)

    mqtt_mock.async_publish.reset_mock()
    await _action(hass, "up_release")
    assert published(mqtt_mock) == {LR_SWITCH_SET: {"state": "OFF"}}
    assert coordinator._off_holds == {}
    assert not coordinator._modes.is_held(LR)
    await _report(hass, mqtt_mock, {"state": "OFF"})  # the write's echo
    assert coordinator._paddle_on[LR_SWITCH] is False


async def test_an_off_hold_whose_release_is_lost_stops_hiding_reports(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any, clock: FakeClock
) -> None:
    await _lit_at(hass, coordinator, mqtt_mock, clock, 183)
    await _action(hass, "down_single")
    clock.advance(5.0)
    await _action(hass, "down_held")
    assert LR_SWITCH in coordinator._off_holds
    clock.advance(DIM_TIMEOUT_S)
    await _report(hass, mqtt_mock, {"state": "ON"})  # a real paddle-on, later
    assert coordinator._paddle_on[LR_SWITCH] is True
    assert coordinator._off_holds == {}


async def test_unload_stops_sending(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    coordinator.shutdown_subscriptions()
    mqtt_mock.async_publish.reset_mock()
    coordinator._enqueue([Send(LR_SET, {"brightness": 1})], priority=PRIORITY_TICK)
    await coordinator._resend(LR_SET)
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
