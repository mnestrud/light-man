"""Integration tests for the coordinator: modes, off-respect, addressing, toggle."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import DOMAIN, TICK_AUTOMATION
from custom_components.light_man.coordinator import LightManCoordinator
from custom_components.light_man.modes import ModeManager

from .conftest import (
    AL_OVERHEAD,
    HALL_UP,
    KIT_SET,
    LEG_HALL,
    LEG_OVERHEAD,
    LR_SET,
    LR_SWITCH,
    OVERHEAD_ALL,
    SEED,
    SLEEP_SWITCH,
    TICK_CALLS_KEY,
    FakeStore,
    published,
    seed_states,
)

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant

    from custom_components.light_man.coordinator import LightManCoordinator as Coord

# Expected payloads for the test seed (AL overhead 50%/4000K; night 20%/2700K).
DAY = {"brightness": 127, "transition": 1.0, "color_temp": 250}
NIGHT = {"brightness": 51, "transition": 1.0, "color_temp": 370}


def _fire(hass: HomeAssistant, topic: str, payload: Any) -> None:
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    async_fire_mqtt_message(hass, topic, payload)


async def test_startup_reconciles_legacy_on(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """Push starts disabled, so the legacy booleans are driven ON."""
    assert coordinator.push_enabled is False
    assert hass.states.get(LEG_OVERHEAD).state == "on"
    assert hass.states.get(LEG_HALL).state == "on"


async def test_enable_floods_and_legacy_off(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """Enabling floods both consolidated groups and drives legacy booleans off."""
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert pubs[OVERHEAD_ALL] == DAY
    assert HALL_UP in pubs
    assert hass.states.get(LEG_OVERHEAD).state == "off"
    assert hass.states.get(LEG_HALL).state == "off"


async def test_toggle_drives_tick_automation(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """The toggle disables the master tick on, and re-enables it off."""
    calls = hass.data[TICK_CALLS_KEY]
    tick = {"entity_id": TICK_AUTOMATION}

    calls.clear()
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    assert ("turn_off", tick) in calls

    calls.clear()
    await coordinator.async_set_push_enabled(enabled=False)
    await hass.async_block_till_done()
    assert ("turn_on", tick) in calls


async def test_unload_restore_calls_tick_on(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """async_restore_legacy re-enables the tick (removal-safe fallback)."""
    calls = hass.data[TICK_CALLS_KEY]
    calls.clear()
    await coordinator.async_restore_legacy()
    await hass.async_block_till_done()
    assert ("turn_on", {"entity_id": TICK_AUTOMATION}) in calls


async def test_sleep_switches_hallway_to_rgb(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """The hallway source emits rgb when its sleep switch is on."""
    hass.states.async_set(SLEEP_SWITCH, "on", {})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    assert published(mqtt_mock)[HALL_UP]["color"] == {"r": 255, "g": 100, "b": 50}


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
    assert coordinator.data["held"]["living_room"]["mode"] == "night"

    pubs = published(mqtt_mock)
    assert pubs[LR_SET] == NIGHT
    assert pubs[KIT_SET] == DAY
    assert OVERHEAD_ALL not in pubs


async def test_config_double_holds_day_consolidated(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """config_double holds at the day value -> still uniform -> consolidated."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    _fire(hass, LR_SWITCH, {"action": "config_double"})
    await hass.async_block_till_done()
    assert coordinator.data["held"]["living_room"]["mode"] == "day"


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


async def test_paddle_off_still_staged(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """An off room is NOT skipped — it stages color-while-off via the flood."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    _fire(hass, LR_SWITCH, {"state": "OFF"})
    await hass.async_block_till_done()
    # All rooms still adaptive -> consolidated flood includes the off room.
    assert OVERHEAD_ALL in published(mqtt_mock)


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


async def test_action_topic_variant(hass: HomeAssistant, coordinator: Coord) -> None:
    """A dedicated .../action topic (raw string) also holds."""
    await coordinator.async_set_push_enabled(enabled=True)
    _fire(hass, LR_SWITCH + "/action", "config_single")
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 1


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
        "living_room", kind="night", armed_at=now, expires_at=now - timedelta(minutes=1)
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
    await coordinator.async_hold("living_room", "night")
    await coordinator.async_hold("kitchen", "day")
    assert coordinator.data["held_count"] == 2
    await coordinator.async_clear_holds()
    assert coordinator.data["held_count"] == 0


async def test_release_unheld_is_noop(hass: HomeAssistant, coordinator: Coord) -> None:
    """Resuming a room that is already adaptive does nothing."""
    await coordinator.async_release_hold("living_room")
    assert coordinator.data["held_count"] == 0


async def test_invalidate_unknown_room_is_noop(
    hass: HomeAssistant, coordinator: Coord
) -> None:
    """Invalidating a room not owned by any source does nothing."""
    coordinator._invalidate_room("ghost")


async def test_adaptive_unavailable_skips_source(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """An unavailable AL switch skips that source, not the whole push."""
    hass.states.async_set(AL_OVERHEAD, "unavailable", {})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert OVERHEAD_ALL not in pubs
    assert HALL_UP in pubs


async def test_adaptive_missing_attributes_skips_source(
    hass: HomeAssistant, coordinator: Coord, mqtt_mock: Any
) -> None:
    """An AL switch missing brightness is skipped."""
    hass.states.async_set(AL_OVERHEAD, "on", {"color_temp_kelvin": 4000})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    assert OVERHEAD_ALL not in published(mqtt_mock)


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


async def test_reconcile_without_legacy_enable(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """A source with no legacy_enable still pushes; reconcile skips booleans."""
    await seed_states(hass)
    seed = copy.deepcopy(SEED)
    for source in seed["sources"].values():
        source.pop("legacy_enable", None)
    entry = MockConfigEntry(domain=DOMAIN, title="Light Man")
    entry.add_to_hass(hass)
    modes = ModeManager(FakeStore(None))
    await modes.async_load()
    coord = LightManCoordinator(hass, entry, validate_config(seed), modes)
    await coord._async_setup()
    await coord.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    assert OVERHEAD_ALL in published(mqtt_mock)
