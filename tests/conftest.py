"""Shared fixtures and seed data for Light Man tests."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import CONFIG_STORE_KEY, DOMAIN
from custom_components.light_man.coordinator import LightManCoordinator
from custom_components.light_man.modes import ModeManager

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant

# --- Entity ids / topics used across the suite ------------------------------
AL_OVERHEAD = "switch.al_overhead"
AL_HALL = "switch.al_hall"
SLEEP_SWITCH = "switch.sleep_mode"
LEG_OVERHEAD = "input_boolean.legacy_overhead"
LEG_HALL = "input_boolean.legacy_hallway"

# Recorded (service, data) for automation.turn_on/off — the tick toggle target.
TICK_CALLS_KEY = "lm_tick_calls"

OVERHEAD_ALL = "zigbee2mqtt/zgb_overhead_all/set"
HALL_UP = "zigbee2mqtt/zgb_hallway_up/set"
LR_SET = "zigbee2mqtt/zgb_living_room/set"
KIT_SET = "zigbee2mqtt/zgb_kitchen/set"
LR_SWITCH = "zigbee2mqtt/Living Room Switch"
KIT_SWITCH = "zigbee2mqtt/Kitchen Switch"

SEED: dict[str, Any] = {
    "seed_version": 2,
    "push_interval_s": 30,
    "sources": {
        "overhead": {
            "al_switch": AL_OVERHEAD,
            "consolidated_topic": OVERHEAD_ALL,
            "legacy_enable": LEG_OVERHEAD,
            "day_color_mode": "color_temp",
            "night_color_mode": "color_temp",
            "night_brightness_pct": 20,
            "night_color_temp_kelvin": 2700,
            "profile": {
                "min_br": 30,
                "max_br": 90,
                "min_ct": 2700,
                "max_ct": 6500,
                "sat": 0.5,
                "base_color_mode": "color_temp",
                "base_rgb": None,
                "dusk_floor_ct": 2200,
                "night_floor_br": 30,
                "sleep": {"br": 30, "color_mode": "color_temp", "ct": 2700},
                "day_window": {"enabled": False, "start": "08:00", "end": "17:00"},
            },
            "rooms": {
                "living_room": {"set_topic": LR_SET, "switches": [LR_SWITCH]},
                "kitchen": {"set_topic": KIT_SET, "switches": [KIT_SWITCH]},
            },
        },
        "hallway_up": {
            "al_switch": AL_HALL,
            "consolidated_topic": HALL_UP,
            "legacy_enable": LEG_HALL,
            "day_color_mode": "color_temp",
            "night_color_mode": "rgb",
            "sleep_switch": SLEEP_SWITCH,
            "profile": {
                "min_br": 30,
                "max_br": 90,
                "min_ct": 2700,
                "max_ct": 6500,
                "sat": 0.5,
                "base_color_mode": "rgb",
                "base_rgb": [135, 206, 235],
                "dusk_floor_ct": 2200,
                "night_floor_br": 30,
                "sleep": {"br": 40, "color_mode": "rgb", "rgb": [255, 126, 30]},
                "day_window": {"enabled": False, "start": "08:00", "end": "17:00"},
            },
            "rooms": {},
        },
    },
}


class FakeStore:
    """In-memory stand-in for ``helpers.storage.Store``."""

    def __init__(self, data: Any = None) -> None:
        """Start with optional preloaded data."""
        self._data = data

    async def async_load(self) -> Any:
        """Return the stored data."""
        return self._data

    async def async_save(self, data: Any) -> None:
        """Persist the data."""
        self._data = data


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Iterator[None]:
    """Allow HA to load the custom integration during tests."""
    yield


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """Use the selector loop on Windows (ProactorEventLoop breaks PHCC)."""
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(autouse=True)
def _fixed_solar() -> Iterator[None]:
    """Pin solar elevation so the engine's adaptive payload is deterministic.

    The push reads real solar elevation; tests freeze it (45 deg now, 71.5 deg
    noon) so the computed payload is reproducible regardless of wall clock.
    """
    with patch(
        "custom_components.light_man.coordinator.solar_inputs",
        return_value=(45.0, 71.5),
    ):
        yield


async def seed_states(hass: HomeAssistant) -> None:
    """Set up the legacy booleans, AL dummy switches, and stub tick services."""
    await async_setup_component(
        hass,
        "input_boolean",
        {"input_boolean": {"legacy_overhead": None, "legacy_hallway": None}},
    )
    # Stub + record automation.turn_on/off so the toggle's tick reconcile both
    # succeeds and is observable (real ServiceRegistry is slotted, can't patch).
    tick_calls: list[tuple[str, dict[str, Any]]] = []

    def _record_tick(call: Any) -> None:
        tick_calls.append((call.service, dict(call.data)))

    hass.services.async_register("automation", "turn_on", _record_tick)
    hass.services.async_register("automation", "turn_off", _record_tick)
    hass.data[TICK_CALLS_KEY] = tick_calls
    hass.states.async_set(
        AL_OVERHEAD, "on", {"brightness_pct": 50, "color_temp_kelvin": 4000}
    )
    hass.states.async_set(
        AL_HALL,
        "on",
        {"brightness_pct": 30, "color_temp_kelvin": 2700, "rgb_color": [255, 100, 50]},
    )
    hass.states.async_set(SLEEP_SWITCH, "off", {})
    await hass.async_block_till_done()


@pytest.fixture
async def coordinator(hass: HomeAssistant, mqtt_mock: Any) -> LightManCoordinator:
    """A constructed + set-up coordinator (push starts disabled)."""
    await seed_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="Light Man")
    entry.add_to_hass(hass)
    modes = ModeManager(FakeStore(None))
    await modes.async_load()
    coord = LightManCoordinator(hass, entry, validate_config(SEED), modes)
    await coord._async_setup()  # exercise subscribe + reconcile
    await coord.async_refresh()
    await hass.async_block_till_done()
    return coord


async def setup_lightman(hass: HomeAssistant, seed: Any = SEED) -> MockConfigEntry:
    """Set up the integration through the full config-entry path (Store patched)."""
    await seed_states(hass)

    def _factory(
        _hass: HomeAssistant, _version: int, key: str, **_kw: Any
    ) -> FakeStore:
        data = copy.deepcopy(seed) if key == CONFIG_STORE_KEY else None
        return FakeStore(data)

    entry = MockConfigEntry(domain=DOMAIN, title="Light Man", unique_id=DOMAIN)
    entry.add_to_hass(hass)
    with patch("custom_components.light_man.Store", side_effect=_factory):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def published(mqtt_mock: Any) -> dict[str, dict[str, Any]]:
    """Map each published zigbee2mqtt topic to its decoded JSON payload."""
    out: dict[str, dict[str, Any]] = {}
    for call in mqtt_mock.async_publish.call_args_list:
        topic, payload = call.args[0], call.args[1]
        if topic.startswith("zigbee2mqtt"):
            out[topic] = json.loads(payload)
    return out
