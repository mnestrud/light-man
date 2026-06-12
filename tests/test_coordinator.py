"""Integration tests for the coordinator: modes, off-respect, addressing, toggle."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import DOMAIN
from custom_components.light_man.coordinator import (
    LightManCoordinator,
    _curve_inflections,
    _interp_at,
    _truthy,
)
from custom_components.light_man.modes import ModeManager

from .conftest import (
    HALL_CENTER_SET,
    HALL_OFF,
    HALL_UP,
    KIT_SET,
    KIT_SWITCH,
    LR_SET,
    LR_SWITCH,
    MMWAVE_EAST,
    MMWAVE_PORCH,
    MMWAVE_WEST,
    OVERHEAD_ALL,
    PORCH_A1_SET,
    PORCH_A2_SET,
    PORCH_OFF,
    SEED,
    FakeStore,
    published,
)

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant

    from custom_components.light_man.coordinator import LightManCoordinator as Coord

# Expected payloads for the test seed (overhead profile).
# DAY: live adaptive at the pinned elevation (e=45, day_window off, awake) ->
# 90% / ct_pct 0.629 -> 229 / 234 mired.
# NIGHT: the forced night look (config_single) = engine night/sleep target,
# 30% / 2700K -> 76 / 370 mired.
# DAY_LOOK: the forced day look (config_double) = peak sun, 90% / 6500K -> 229 / 154.
DAY = {"brightness": 229, "transition": 1.0, "color_temp": 234}
NIGHT = {"brightness": 76, "transition": 1.0, "color_temp": 370}
DAY_LOOK = {"brightness": 229, "transition": 1.0, "color_temp": 154}


def _fire(hass: HomeAssistant, topic: str, payload: Any) -> None:
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    async_fire_mqtt_message(hass, topic, payload)


async def test_push_starts_disabled(hass: HomeAssistant, coordinator: Coord) -> None:
    """The coordinator constructs with the push disabled (the switch enables it)."""
    assert coordinator.push_enabled is False


async def test_enable_floods(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """Enabling floods both consolidated groups."""
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert pubs[OVERHEAD_ALL] == DAY
    assert HALL_UP in pubs


async def test_start_hook_takes_over_when_enabled(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """The async_at_started hook floods on takeover when push is already enabled.

    Mirrors a cold boot where the switch restored ON before HA finished starting:
    the switch sets the flag, and the deferred hook runs the first push.
    """
    coordinator.push_enabled = True
    mqtt_mock.async_publish.reset_mock()
    await coordinator._takeover_on_start(hass)
    await hass.async_block_till_done()
    assert published(mqtt_mock)[OVERHEAD_ALL] == DAY


async def test_sleep_snap_applies_engine_sleep_target(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """Snapping sleep on drives every source to its profile's sleep target."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_set_sleep(enabled=True, ramp=False)
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert pubs[HALL_UP]["color"] == {"r": 255, "g": 126, "b": 30}  # hallway sleep rgb
    # overhead sleep target: 30% -> 76, 2700K -> 370 mired.
    assert pubs[OVERHEAD_ALL] == {
        "brightness": 76,
        "transition": 1.0,
        "color_temp": 370,
    }
    assert coordinator.diagnostics["sleep"] == {"on": True, "s": 1.0}


async def test_sleep_ramp_interpolates(hass: HomeAssistant, coordinator: Coord) -> None:
    """The sleep ramp eases between awake and asleep over its duration."""
    now = dt_util.utcnow()
    coordinator.sleep_on = True
    coordinator._sleep_start = 0.0
    coordinator._sleep_changed_at = now - timedelta(seconds=2700)  # half of 5400s in
    assert abs(coordinator._compute_sleep_s(now) - 0.5) < 0.01
    coordinator._sleep_changed_at = None  # snapped -> straight to target
    assert coordinator._compute_sleep_s(now) == 1.0


async def test_config_single_holds_night_per_room(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """config_single holds the room at the night target; others stay day."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    _fire(hass, LR_SWITCH, {"action": "config_single"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 1
    assert coordinator.data["held"]["living_room.overhead"]["mode"] == "night"

    pubs = published(mqtt_mock)
    assert pubs[LR_SET] == NIGHT
    assert pubs[KIT_SET] == DAY
    assert OVERHEAD_ALL not in pubs


async def test_config_double_holds_day_per_room(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """config_double applies a distinct forced day look per-room (not a no-op)."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    _fire(hass, LR_SWITCH, {"action": "config_double"})
    await hass.async_block_till_done()
    assert coordinator.data["held"]["living_room.overhead"]["mode"] == "day"

    pubs = published(mqtt_mock)
    assert pubs[LR_SET] == DAY_LOOK  # forced peak-day look, distinct from adaptive
    assert pubs[KIT_SET] == DAY
    assert OVERHEAD_ALL not in pubs


async def test_up_single_releases(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """up_single resumes adaptive and re-floods consolidated."""
    await coordinator.async_set_push_enabled(enabled=True)
    _fire(hass, LR_SWITCH, {"action": "config_single"})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    _fire(hass, LR_SWITCH, {"action": "up_single"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0
    assert OVERHEAD_ALL in published(mqtt_mock)


async def test_down_single_also_releases(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """down_single also resumes adaptive (matches the old re-engage)."""
    await coordinator.async_set_push_enabled(enabled=True)
    _fire(hass, LR_SWITCH, {"action": "config_single"})
    await hass.async_block_till_done()
    _fire(hass, LR_SWITCH, {"action": "down_single"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0


async def test_paddle_off_stages_room_off(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """Switching a room off stages it with an explicit state:OFF, not the flood.

    A state-less consolidated flood here would race the local binding (MQTT beats
    the bind) and re-brighten the still-on bulb; the explicit off + staged value
    is deterministic and pre-loads the next turn-on.
    """
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    _fire(hass, LR_SWITCH, {"state": "OFF"})
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert OVERHEAD_ALL not in pubs  # no racing state-less flood
    assert pubs[LR_SET]["state"] == "OFF"  # explicit off
    assert pubs[LR_SET]["brightness"] == 229  # staged adaptive value for next on


async def test_stage_off_guards(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """_stage_off no-ops for an unknown room and when solar is unavailable."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator._stage_off("overhead", "ghost.room")  # unknown ref → no publish
    with patch(
        "custom_components.light_man.coordinator.solar_inputs",
        side_effect=ValueError("polar night"),
    ):
        await coordinator._stage_off("overhead", "living_room.overhead")  # no payload
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


async def test_down_single_stages_off(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """A down-tap (off intent) releases the hold and stages the room off."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    _fire(hass, LR_SWITCH, {"action": "down_single"})
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert OVERHEAD_ALL not in pubs
    assert pubs[LR_SET]["state"] == "OFF"
    assert pubs[LR_SET]["brightness"] == 229


async def test_setup_prunes_orphaned_holds(hass: HomeAssistant, mqtt_mock: Any) -> None:
    """Setup drops holds whose key no longer maps to any light (post-migration)."""
    entry = MockConfigEntry(domain=DOMAIN, title="Light Man")
    entry.add_to_hass(hass)
    modes = ModeManager(FakeStore(None))
    await modes.async_load()
    now = dt_util.utcnow()
    later = now + timedelta(hours=8)
    await modes.set_held("overhead_bath", kind="night", armed_at=now, expires_at=later)
    await modes.set_held(
        "living_room.overhead", kind="night", armed_at=now, expires_at=later
    )
    coord = LightManCoordinator(hass, entry, validate_config(SEED), modes)
    await coord._async_setup()
    assert "overhead_bath" not in modes.held_rooms()  # orphaned key pruned
    assert "living_room.overhead" in modes.held_rooms()  # valid key kept
    coord.shutdown_subscriptions()


async def test_paddle_off_releases_hold(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """Switching a held light off resumes adaptive (the hold does not linger)."""
    await coordinator.async_set_push_enabled(enabled=True)
    _fire(hass, LR_SWITCH, {"action": "config_single"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 1

    _fire(hass, LR_SWITCH, {"state": "OFF"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0  # off cleared the hold


async def test_paddle_on_repushes_room(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """Turning the paddle back on returns the source to the consolidated flood."""
    await coordinator.async_set_push_enabled(enabled=True)
    _fire(hass, LR_SWITCH, {"state": "OFF"})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, LR_SWITCH, {"state": "ON"})
    await hass.async_block_till_done()
    assert OVERHEAD_ALL in published(mqtt_mock)


async def test_paddle_state_unchanged_is_noop(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """A repeated paddle state does not trigger another push."""
    await coordinator.async_set_push_enabled(enabled=True)
    _fire(hass, LR_SWITCH, {"state": "ON"})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, LR_SWITCH, {"state": "ON"})  # unchanged
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


async def test_paddle_state_tracked_while_disabled(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """While disabled, paddle state is tracked but no push fires."""
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, LR_SWITCH, {"state": "OFF"})
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


async def test_ignored_actions_no_mode_change(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """Double/triple taps (shade scenes) never change a room's mode."""
    await coordinator.async_set_push_enabled(enabled=True)
    _fire(hass, LR_SWITCH, {"action": "up_double"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0


async def test_inert_when_disabled(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """In legacy mode Light Man ignores taps and publishes nothing."""
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, LR_SWITCH, {"action": "config_single"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0
    assert published(mqtt_mock) == {}


async def test_stale_bundled_state_ignored(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """An action message's bundled (cached) `state` field is never processed.

    Z2M attaches the pre-tap cached attributes to the action publish; reading
    `{"action": "up_single", "state": "OFF"}` as a paddle-off staged the room
    OFF right after the user turned it on (live office bug, 2026-06-12).
    """
    await coordinator.async_set_push_enabled(enabled=True)
    _fire(hass, LR_SWITCH, {"action": "down_single", "state": "ON"})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    # The up tap arrives with the stale OFF still cached in the payload.
    _fire(hass, LR_SWITCH, {"action": "up_single", "state": "OFF"})
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert OVERHEAD_ALL in pubs  # the turn-on flood happened
    assert LR_SET not in pubs  # no stage-off fired against the fresh turn-on


async def test_malformed_payload_ignored(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """A non-JSON state payload is ignored, not fatal."""
    await coordinator.async_set_push_enabled(enabled=True)
    _fire(hass, LR_SWITCH, "not-json")
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0


async def test_unknown_base_guards(hass: HomeAssistant, coordinator: Coord) -> None:
    """Action/state for an unmapped switch base are no-ops."""
    coordinator.push_enabled = True
    await coordinator._on_action("zigbee2mqtt/Nope", "config_single")
    await coordinator._on_state("zigbee2mqtt/Nope", "ON")
    assert coordinator.data["held_count"] == 0


async def test_ttl_sweep_clears_expired(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """An expired held mode is swept and the source re-floods consolidated."""
    now = dt_util.utcnow()
    await coordinator._modes.set_held(
        "living_room.overhead",
        kind="night",
        armed_at=now,
        expires_at=now - timedelta(minutes=1),
    )
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0
    assert OVERHEAD_ALL in published(mqtt_mock)


async def test_dedup_skips_unchanged(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """A second cycle with unchanged values publishes nothing."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    assert coordinator.diagnostics["dedup_skips"] > 0


async def test_force_push_bypasses_dedup(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """force_push republishes even when nothing changed."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    assert OVERHEAD_ALL in published(mqtt_mock)


async def test_disabled_force_push_is_noop(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """While disabled, force_push does nothing."""
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


async def test_clear_holds(hass: HomeAssistant, coordinator: Coord) -> None:
    """clear_holds resumes every room."""
    await coordinator.async_hold("living_room.overhead", "night")
    await coordinator.async_hold("kitchen.overhead", "day")
    assert coordinator.data["held_count"] == 2
    await coordinator.async_clear_holds()
    assert coordinator.data["held_count"] == 0


async def test_release_unheld_is_noop(hass: HomeAssistant, coordinator: Coord) -> None:
    """Resuming a room that is already adaptive does nothing."""
    await coordinator.async_release_hold("living_room.overhead")
    assert coordinator.data["held_count"] == 0


async def test_invalidate_unknown_room_is_noop(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """Invalidating a room not owned by any source does nothing."""
    coordinator._invalidate_room("ghost")


async def test_mqtt_disabled_skips_publish(
    hass: HomeAssistant,
    coordinator: Coord,
    mqtt_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the MQTT entry is disabled, the push skips and marks unavailable."""
    monkeypatch.setattr(
        "homeassistant.components.mqtt.mqtt_config_entry_enabled", lambda _hass: False
    )
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}
    assert coordinator.mqtt_available is False


async def test_publish_error_marks_then_recovers(
    hass: HomeAssistant,
    coordinator: Coord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publish error flips availability off; recovery flips it back on."""

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise HomeAssistantError("broker down")

    monkeypatch.setattr("homeassistant.components.mqtt.async_publish", _boom)
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    assert coordinator.mqtt_available is False

    monkeypatch.undo()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    assert coordinator.mqtt_available is True


async def test_engine_drives_the_push_and_records_diagnostics(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """The push value comes from the engine; rgb sources publish a color."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert pubs[OVERHEAD_ALL] == DAY  # engine-derived, not the AL read
    assert pubs[HALL_UP]["color"] == {"r": 135, "g": 206, "b": 235}  # rgb base
    engine = coordinator.diagnostics["engine"]
    assert engine["overhead"]["color_mode"] == "color_temp"
    assert engine["hallway_up"]["rgb_color"] == [135, 206, 235]


async def test_solar_unavailable_skips_push(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """A solar ValueError (polar edge) skips the cycle instead of pushing junk."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    with patch(
        "custom_components.light_man.coordinator.solar_inputs",
        side_effect=ValueError("polar night"),
    ):
        await coordinator.async_force_push()
        await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


# --- occupancy (mmwave presence -> directional sweep) ----------------------

# Engine value for hallway_up (rgb sky-blue, 90% -> 229) + occupancy state/transition.
SWEEP_ON = {
    "brightness": 229,
    "transition": 1.5,
    "color": {"r": 135, "g": 206, "b": 235},
    "state": "ON",
}


async def _instant(*_a: Any, **_k: Any) -> None:
    """Replace asyncio.sleep so sweep stage delays don't slow the tests."""


async def _walk(
    hass: HomeAssistant, coordinator: Coord, topic: str, *, occupied: bool
) -> None:
    """Fire an mmwave message, running sweep delays instantly + draining tasks."""
    with patch("custom_components.light_man.coordinator.asyncio.sleep", _instant):
        _fire(hass, topic, {"occupancy": occupied})
        await hass.async_block_till_done()
        pending = [t for t in coordinator._sweep_tasks.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await hass.async_block_till_done()


async def test_occupancy_sweep_lights_each_stage(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """The east sensor sweeps each stage on at the engine value + state ON."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await _walk(hass, coordinator, MMWAVE_EAST, occupied=True)
    pubs = published(mqtt_mock)
    assert pubs[HALL_UP] == SWEEP_ON  # stage 1
    assert pubs[HALL_CENTER_SET] == SWEEP_ON  # stage 2 (after the delay)
    assert coordinator.diagnostics["occupancy"]["hallway"] is True


async def test_occupancy_clears_when_all_sensors_off(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """When the last sensor clears, the zone's off_lights turn off."""
    await coordinator.async_set_push_enabled(enabled=True)
    await _walk(hass, coordinator, MMWAVE_EAST, occupied=True)
    mqtt_mock.async_publish.reset_mock()
    await _walk(hass, coordinator, MMWAVE_EAST, occupied=False)
    assert published(mqtt_mock)[HALL_OFF] == {"state": "OFF", "transition": 1.5}
    assert coordinator.diagnostics["occupancy"]["hallway"] is False


async def test_occupancy_stays_on_until_all_clear(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """One sensor clearing while another is occupied does not turn the zone off."""
    await coordinator.async_set_push_enabled(enabled=True)
    await _walk(hass, coordinator, MMWAVE_EAST, occupied=True)
    await _walk(hass, coordinator, MMWAVE_WEST, occupied=True)
    mqtt_mock.async_publish.reset_mock()
    await _walk(hass, coordinator, MMWAVE_EAST, occupied=False)  # west still occupied
    assert HALL_OFF not in published(mqtt_mock)


async def test_occupancy_areas_on_one_switch_are_independent(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """Two mmwave areas on one topic trigger and clear as independent sensors."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    async def _fire_payload(payload: dict[str, Any]) -> None:
        with patch("custom_components.light_man.coordinator.asyncio.sleep", _instant):
            _fire(hass, MMWAVE_PORCH, payload)
            await hass.async_block_till_done()
            pending = [t for t in coordinator._sweep_tasks.values() if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await hass.async_block_till_done()

    # area1 alone -> only area1's light; the zone is not cleared.
    await _fire_payload({"mmwave_area1_occupancy": True})
    pubs = published(mqtt_mock)
    assert PORCH_A1_SET in pubs
    assert PORCH_A2_SET not in pubs
    assert PORCH_OFF not in pubs

    # area2 turns on -> its own light fires independently.
    mqtt_mock.async_publish.reset_mock()
    await _fire_payload({"mmwave_area2_occupancy": True})
    assert PORCH_A2_SET in published(mqtt_mock)

    # area1 clears but area2 still occupied -> zone stays on.
    mqtt_mock.async_publish.reset_mock()
    await _fire_payload({"mmwave_area1_occupancy": False})
    assert PORCH_OFF not in published(mqtt_mock)

    # area2 clears too -> now every binding is clear, so the zone turns off.
    mqtt_mock.async_publish.reset_mock()
    await _fire_payload({"mmwave_area2_occupancy": False})
    assert published(mqtt_mock)[PORCH_OFF] == {"state": "OFF", "transition": 1.5}


async def test_occupancy_sweep_restarts_on_retrigger(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """A new occupied edge cancels the in-flight sweep before starting a fresh one."""
    await coordinator.async_set_push_enabled(enabled=True)
    task_key = ("hallway", "east")
    stuck = hass.async_create_task(asyncio.Event().wait())
    coordinator._sweep_tasks[task_key] = stuck
    coordinator._sensor_occupied[task_key] = False
    await _walk(hass, coordinator, MMWAVE_EAST, occupied=True)
    assert stuck.cancelled()
    assert coordinator._sweep_tasks[task_key] is not stuck


async def test_occupancy_repeat_message_is_no_edge(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """A repeated occupancy value (no transition) does not re-run the sweep."""
    await coordinator.async_set_push_enabled(enabled=True)
    await _walk(hass, coordinator, MMWAVE_EAST, occupied=True)
    mqtt_mock.async_publish.reset_mock()
    await _walk(hass, coordinator, MMWAVE_EAST, occupied=True)  # same value
    assert published(mqtt_mock) == {}


async def test_occupancy_inert_when_push_disabled(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """In legacy mode mmwave presence is ignored."""
    mqtt_mock.async_publish.reset_mock()
    await _walk(hass, coordinator, MMWAVE_EAST, occupied=True)
    assert published(mqtt_mock) == {}


async def test_occupancy_ignores_non_occupancy_message(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """A message on the topic without the occupancy field is ignored."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, MMWAVE_EAST, {"linkquality": 80})
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


async def test_occupancy_malformed_payload_ignored(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """A non-JSON mmwave payload is ignored, not fatal."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    _fire(hass, MMWAVE_EAST, "not-json")
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


async def test_occupancy_sweep_skips_when_solar_unavailable(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """If solar is unavailable, the sweep has no engine value and publishes nothing."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    with patch(
        "custom_components.light_man.coordinator.solar_inputs",
        side_effect=ValueError("polar night"),
    ):
        await _walk(hass, coordinator, MMWAVE_EAST, occupied=True)
    assert HALL_UP not in published(mqtt_mock)


def test_interp_at_clamps_and_interpolates() -> None:
    times = [0, 60, 120]
    vals = [10.0, 30.0, 10.0]
    assert _interp_at(times, vals, -5) == 10.0  # before the first sample
    assert _interp_at(times, vals, 200) == 10.0  # after the last sample
    assert _interp_at(times, vals, 30) == 20.0  # halfway up the first segment


def test_curve_inflections_spans_from_series() -> None:
    # A trapezoid day shape: floor 30% -> rise -> plateau 90% -> fall -> floor.
    samples = [(m, 0.0) for m in range(0, 1440, 30)]

    def br_at(m: int) -> float:
        if m <= 480 or m >= 1080:
            return 30.0
        if m < 600:
            return 30.0 + 60.0 * (m - 480) / 120  # ramp up 8:00 -> 10:00
        if m <= 900:
            return 90.0  # plateau
        return 90.0 - 60.0 * (m - 900) / 180  # ramp down 15:00 -> 18:00

    br = [br_at(m) for m, _ in samples]
    inf = _curve_inflections({}, samples, br, 750)  # solar noon 12:30
    assert inf is not None
    assert inf["ramp_up"]["start"] == {"t": 480, "br": 30.0}  # leaves the floor
    assert inf["ramp_up"]["end"] == {"t": 600, "br": 90.0}  # reaches the peak
    assert inf["ramp_down"]["start"]["t"] == 900  # leaves the peak
    assert inf["ramp_down"]["end"] == {"t": 1080, "br": 30.0}  # back to the minimum
    assert inf["solar_noon"] == {"t": 750, "br": 90.0}


def test_curve_inflections_smooth_peak_without_noon() -> None:
    # A single-peak curve + no solar-noon time -> solar_noon falls back to the peak.
    samples = [(m, 0.0) for m in range(0, 1440, 60)]
    br = [10.0 + max(0.0, 70.0 - abs(720 - m) / 6) for m, _ in samples]
    inf = _curve_inflections({}, samples, br, None)
    assert inf is not None
    assert inf["solar_noon"]["t"] == 720  # brightness peak
    assert inf["ramp_up"]["start"]["t"] < inf["ramp_up"]["end"]["t"]
    assert inf["ramp_down"]["start"]["t"] < inf["ramp_down"]["end"]["t"]


def test_curve_inflections_flat_curve_is_none() -> None:
    samples = [(m, 0.0) for m in range(0, 1440, 60)]
    assert _curve_inflections({}, samples, [50.0] * len(samples), None) is None


def test_truthy_coercions() -> None:
    """Occupancy fields parse from bool / string / number forms."""
    assert _truthy(True) is True
    assert _truthy(False) is False
    assert _truthy("ON") is True
    assert _truthy("off") is False
    assert _truthy(1) is True
    assert _truthy(0.0) is False
    assert _truthy(None) is False
    assert _truthy(["x"]) is False


# --- switches are never written (v0.7.6) ------------------------------------

SWITCH_BASES = (LR_SWITCH, KIT_SWITCH)


def _assert_no_switch_writes(mqtt_mock: Any) -> None:
    """No published topic may target an Inovelli switch.

    On a Smart-Bulb-Mode VZM31 any level-type write — `brightness` or even the
    "passive" `defaultLevelLocal/Remote` prestage — can drive the local Zigbee
    bind and turn the bound bulb on. Switch topics are read-only inputs.
    """
    hit = [t for t in published(mqtt_mock) if t.startswith(SWITCH_BASES)]
    assert not hit, f"published to switch topics: {hit}"


async def test_no_switch_writes_on_push_and_force(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """Neither the enable flood nor a force push writes to any switch."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    _assert_no_switch_writes(mqtt_mock)
    assert published(mqtt_mock)[OVERHEAD_ALL] == DAY  # bulbs still flooded


async def test_no_switch_writes_on_paddle_and_holds(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """Paddle on/off, config-tap holds, and release never write to a switch."""
    await coordinator.async_set_push_enabled(enabled=True)
    for event in (
        {"state": "ON"},
        {"action": "config_single"},  # -> held night
        {"action": "config_double"},  # -> held day
        {"action": "up_held"},  # -> held manual
        {"action": "up_single"},  # release -> on
        {"action": "down_single"},  # release -> off (stage_off path)
        {"state": "OFF"},
    ):
        _fire(hass, LR_SWITCH, event)
        await hass.async_block_till_done()
    _assert_no_switch_writes(mqtt_mock)


# --- curve previews (panel time-of-day visualization) ----------------------


async def test_curve_previews_shape(hass: HomeAssistant, coordinator: Coord) -> None:
    """Each curve gets a day-sampled brightness/color series + sleep target."""
    preview = coordinator.curve_previews()
    assert preview is not None
    assert len(preview["times"]) > 10
    standard = preview["curves"]["standard"]
    assert standard["mode"] == "color_temp"
    assert len(standard["br"]) == len(preview["times"])
    assert len(standard["ct"]) == len(preview["times"])
    assert standard["sleep_br"] == 30
    inf = standard["inflections"]
    assert set(inf) == {"ramp_up", "ramp_down", "solar_noon"}
    assert inf["ramp_up"]["start"]["t"] <= inf["ramp_up"]["end"]["t"]
    assert inf["ramp_down"]["start"]["t"] <= inf["ramp_down"]["end"]["t"]
    sky = preview["curves"]["sky"]  # an rgb curve
    assert sky["mode"] == "rgb"
    assert sky["rgb"] == [135, 206, 235]
    assert sky["sleep_rgb"] == [255, 126, 30]


async def test_panel_config_includes_preview(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """panel_config bundles the stored topology, switch map, and curve previews."""
    cfg = coordinator.panel_config()
    assert set(cfg["config"]["curves"]) == {"standard", "sky"}
    assert cfg["switch_map"][LR_SWITCH] == ("overhead", "living_room.overhead")
    assert cfg["preview"]["curves"]["standard"]["mode"] == "color_temp"


async def test_curve_previews_none_when_sun_unavailable(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """A polar-edge ValueError from the sun path yields no preview (not a crash)."""
    with patch(
        "custom_components.light_man.coordinator.day_profile",
        side_effect=ValueError("polar night"),
    ):
        assert coordinator.curve_previews() is None


async def test_curve_previews_skip_malformed_curve(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """A structurally broken curve is skipped, not fatal to the preview."""
    coordinator.stored["curves"]["broken"] = {}  # missing min_br etc.
    preview = coordinator.curve_previews()
    assert preview is not None
    assert "broken" not in preview["curves"]
    assert "standard" in preview["curves"]
