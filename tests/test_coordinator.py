"""Integration tests for the coordinator: push, addressing, holds, dedup."""

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
from custom_components.light_man.const import DOMAIN
from custom_components.light_man.coordinator import LightManCoordinator
from custom_components.light_man.holds import HoldManager

from .conftest import (
    AL_OVERHEAD,
    HALL_UP,
    KIT_SET,
    KIT_SWITCH,
    LEG_HALL,
    LEG_OVERHEAD,
    LR_SET,
    LR_SWITCH,
    OVERHEAD_ALL,
    SEED,
    SLEEP_SWITCH,
    FakeStore,
    published,
    seed_states,
)

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant


def _fire(hass: HomeAssistant, topic: str, payload: Any) -> None:
    """Fire an MQTT message (dict payloads are JSON-encoded)."""
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    async_fire_mqtt_message(hass, topic, payload)


async def test_startup_reconciles_legacy_on(
    hass: HomeAssistant, coordinator: LightManCoordinator
) -> None:
    """Push starts disabled, so the legacy booleans are driven ON."""
    assert coordinator.push_enabled is False
    assert hass.states.get(LEG_OVERHEAD).state == "on"
    assert hass.states.get(LEG_HALL).state == "on"


async def test_enable_push_floods_consolidated_and_legacy_off(
    hass: HomeAssistant, coordinator: LightManCoordinator, mqtt_mock: Any
) -> None:
    """Enabling pushes both sources to their consolidated groups; legacy OFF."""
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()

    pubs = published(mqtt_mock)
    assert pubs[OVERHEAD_ALL] == {
        "brightness": 127,
        "transition": 1.0,
        "color_temp": 250,
    }
    assert HALL_UP in pubs
    assert hass.states.get(LEG_OVERHEAD).state == "off"
    assert hass.states.get(LEG_HALL).state == "off"


async def test_sleep_switches_hallway_to_rgb(
    hass: HomeAssistant, coordinator: LightManCoordinator, mqtt_mock: Any
) -> None:
    """With the sleep switch on, the hallway source emits an RGB payload."""
    hass.states.async_set(SLEEP_SWITCH, "on", {})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()

    pubs = published(mqtt_mock)
    assert pubs[HALL_UP]["color"] == {"r": 255, "g": 100, "b": 50}
    assert "color_temp" not in pubs[HALL_UP]


async def test_arm_via_action_addresses_per_room(
    hass: HomeAssistant, coordinator: LightManCoordinator, mqtt_mock: Any
) -> None:
    """Arming a room flips overhead to per-room floods, skipping the held room."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()

    _fire(hass, LR_SWITCH, {"action": "config_single"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 1
    assert "living_room" in coordinator.data["held"]

    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert KIT_SET in pubs
    assert LR_SET not in pubs
    assert OVERHEAD_ALL not in pubs


async def test_release_invalidates_dedup_across_addressing_flip(
    hass: HomeAssistant, coordinator: LightManCoordinator, mqtt_mock: Any
) -> None:
    """The rc30 blind-spot: releasing the last hold must re-flood consolidated.

    Even though the AL target never changed, the consolidated cache is stale
    (it was set before the hold), so a naive dedup would skip the publish and
    leave the released room on its held scene.
    """
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()  # caches the consolidated payload

    _fire(hass, LR_SWITCH, {"action": "config_single"})  # arm
    await hass.async_block_till_done()
    await coordinator.async_refresh()  # per-room flood, caches per-room
    await hass.async_block_till_done()

    mqtt_mock.async_publish.reset_mock()
    _fire(hass, LR_SWITCH, {"action": "up_single"})  # release
    await hass.async_block_till_done()

    pubs = published(mqtt_mock)
    assert OVERHEAD_ALL in pubs  # NOT deduped despite unchanged AL target
    assert coordinator.data["held_count"] == 0


async def test_off_on_release(
    hass: HomeAssistant, coordinator: LightManCoordinator
) -> None:
    """A room switch OFF->ON releases its hold."""
    _fire(hass, KIT_SWITCH, {"action": "config_single"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 1

    _fire(hass, KIT_SWITCH, {"state": "OFF"})
    await hass.async_block_till_done()
    _fire(hass, KIT_SWITCH, {"state": "ON"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0


async def test_action_topic_variant(
    hass: HomeAssistant, coordinator: LightManCoordinator
) -> None:
    """A dedicated ``.../action`` topic (raw string payload) also arms."""
    _fire(hass, LR_SWITCH + "/action", "config_single")
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 1


async def test_ignored_actions_do_not_arm(
    hass: HomeAssistant, coordinator: LightManCoordinator
) -> None:
    """Double/triple taps (shade scenes) never arm a hold."""
    _fire(hass, LR_SWITCH, {"action": "up_double"})
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0


async def test_malformed_payload_ignored(
    hass: HomeAssistant, coordinator: LightManCoordinator
) -> None:
    """A non-JSON state payload is ignored, not fatal."""
    _fire(hass, LR_SWITCH, "not-json")
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0


async def test_unknown_base_guards(
    hass: HomeAssistant, coordinator: LightManCoordinator
) -> None:
    """Action/state for an unmapped switch base are no-ops."""
    await coordinator._on_action("zigbee2mqtt/Nope", "config_single")
    await coordinator._on_state("zigbee2mqtt/Nope", "ON")
    assert coordinator.data["held_count"] == 0


async def test_ttl_sweep_clears_expired(
    hass: HomeAssistant, coordinator: LightManCoordinator, mqtt_mock: Any
) -> None:
    """An expired hold is swept and its source re-floods on the next tick."""
    now = dt_util.utcnow()
    await coordinator._holds.arm(
        "kitchen", armed_at=now, expires_at=now - timedelta(minutes=1)
    )
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()

    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_refresh()  # sweep removes kitchen
    await hass.async_block_till_done()
    assert coordinator.data["held_count"] == 0
    assert OVERHEAD_ALL in published(mqtt_mock)


async def test_dedup_skips_unchanged(
    hass: HomeAssistant, coordinator: LightManCoordinator, mqtt_mock: Any
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
    hass: HomeAssistant, coordinator: LightManCoordinator, mqtt_mock: Any
) -> None:
    """force_push republishes even when nothing changed."""
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()

    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    pubs = published(mqtt_mock)
    assert OVERHEAD_ALL in pubs
    assert HALL_UP in pubs


async def test_disabled_force_push_is_noop(
    hass: HomeAssistant, coordinator: LightManCoordinator, mqtt_mock: Any
) -> None:
    """While disabled, the legacy stack owns the push — force_push does nothing."""
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    assert published(mqtt_mock) == {}


async def test_release_unheld_room_is_noop(
    hass: HomeAssistant, coordinator: LightManCoordinator
) -> None:
    """Releasing a room that is not held does nothing."""
    await coordinator.async_release_hold("living_room")
    assert coordinator.data["held_count"] == 0


async def test_clear_holds(
    hass: HomeAssistant, coordinator: LightManCoordinator
) -> None:
    """clear_holds releases every hold."""
    await coordinator.async_arm_hold("living_room")
    await coordinator.async_arm_hold("kitchen")
    assert coordinator.data["held_count"] == 2
    await coordinator.async_clear_holds()
    assert coordinator.data["held_count"] == 0


async def test_adaptive_unavailable_skips_source(
    hass: HomeAssistant, coordinator: LightManCoordinator, mqtt_mock: Any
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
    hass: HomeAssistant, coordinator: LightManCoordinator, mqtt_mock: Any
) -> None:
    """An AL switch missing brightness is skipped (no broken payload)."""
    hass.states.async_set(AL_OVERHEAD, "on", {"color_temp_kelvin": 4000})
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    assert OVERHEAD_ALL not in published(mqtt_mock)


async def test_mqtt_disabled_skips_publish(
    hass: HomeAssistant,
    coordinator: LightManCoordinator,
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


async def test_publish_error_marks_unavailable(
    hass: HomeAssistant,
    coordinator: LightManCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publish raising HomeAssistantError flips availability off (logged once)."""

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise HomeAssistantError("broker down")

    monkeypatch.setattr("homeassistant.components.mqtt.async_publish", _boom)
    await coordinator.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    assert coordinator.mqtt_available is False

    # Broker recovers -> the next push succeeds and flips availability back on.
    monkeypatch.undo()
    await coordinator.async_force_push()
    await hass.async_block_till_done()
    assert coordinator.mqtt_available is True


async def test_invalidate_unknown_room_is_noop(
    hass: HomeAssistant, coordinator: LightManCoordinator
) -> None:
    """Invalidating a room not owned by any source does nothing."""
    coordinator._invalidate_room("ghost_room")  # defensive guard


async def test_reconcile_without_legacy_enable(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """A source with no legacy_enable still pushes; reconcile is a no-op there."""
    await seed_states(hass)
    seed = copy.deepcopy(SEED)
    for source in seed["sources"].values():
        source.pop("legacy_enable", None)
    entry = MockConfigEntry(domain=DOMAIN, title="Light Man")
    entry.add_to_hass(hass)
    holds = HoldManager(FakeStore(None))
    await holds.async_load()
    coord = LightManCoordinator(hass, entry, validate_config(seed), holds)
    await coord._async_setup()  # reconcile with no legacy entities -> early return
    await coord.async_set_push_enabled(enabled=True)
    await hass.async_block_till_done()
    assert OVERHEAD_ALL in published(mqtt_mock)
