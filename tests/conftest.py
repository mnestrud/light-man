"""Shared fixtures and seed data for Light Man tests."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import CONFIG_STORE_KEY, DOMAIN
from custom_components.light_man.coordinator import LightManCoordinator
from custom_components.light_man.modes import ModeManager

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant

# --- Entity ids / topics used across the suite ------------------------------
OVERHEAD_ALL = "zigbee2mqtt/zgb_overhead_all/set"
HALL_UP = "zigbee2mqtt/zgb_hallway_up/set"
LR_SET = "zigbee2mqtt/zgb_living_room/set"
KIT_SET = "zigbee2mqtt/zgb_kitchen/set"
LR_SWITCH = "zigbee2mqtt/Living Room Switch"
KIT_SWITCH = "zigbee2mqtt/Kitchen Switch"
MMWAVE_EAST = "zigbee2mqtt/Hall East mmwave"
MMWAVE_WEST = "zigbee2mqtt/Hall West mmwave"
HALL_CENTER_SET = "zigbee2mqtt/Hall Center/set"
HALL_OFF = "zigbee2mqtt/zgb_hallwayf/set"
# A single mmwave switch whose two detection areas drive independent triggers.
MMWAVE_PORCH = "zigbee2mqtt/Porch mmwave"
PORCH_A1_SET = "zigbee2mqtt/zgb_porch_a1/set"
PORCH_A2_SET = "zigbee2mqtt/zgb_porch_a2/set"
PORCH_OFF = "zigbee2mqtt/zgb_porch/set"

# The test seed in the reconciled data model: a named curve library, curve-bearing
# source groups, first-class rooms (lights/switches/sensors), and cross-room zones
# referencing room sensors. The loader derives the runtime view (light-ref-keyed
# source groups) — see config_loader.py.
SEED: dict[str, Any] = {
    "seed_version": 8,
    "push_interval_s": 30,
    "curves": {
        "standard": {
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
        "sky": {
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
    },
    "sources": {
        "overhead": {"consolidated_topic": OVERHEAD_ALL, "curve_ref": "standard"},
        "hallway_up": {"consolidated_topic": HALL_UP, "curve_ref": "sky"},
    },
    "rooms": {
        "living_room": {
            "name": "Living Room",
            "lights": [{"id": "overhead", "set_topic": LR_SET, "source": "overhead"}],
            "switches": [{"topic": LR_SWITCH, "governs": "living_room.overhead"}],
            "sensors": {},
        },
        "kitchen": {
            "name": "Kitchen",
            "lights": [{"id": "overhead", "set_topic": KIT_SET, "source": "overhead"}],
            "switches": [{"topic": KIT_SWITCH, "governs": "kitchen.overhead"}],
            "sensors": {},
        },
        "hall": {
            "name": "Hall",
            "lights": [
                {"id": "up_main", "set_topic": HALL_UP, "source": "hallway_up"},
                {"id": "center", "set_topic": HALL_CENTER_SET, "source": "hallway_up"},
            ],
            "switches": [],
            "sensors": {
                "east": {"topic": MMWAVE_EAST},
                "west": {"topic": MMWAVE_WEST},
            },
        },
        # One physical switch, two detection areas wired as independent sensors.
        "porch": {
            "name": "Porch",
            "lights": [
                {"id": "a1", "set_topic": PORCH_A1_SET, "source": "overhead"},
                {"id": "a2", "set_topic": PORCH_A2_SET, "source": "overhead"},
            ],
            "switches": [],
            "sensors": {
                "porch_a1": {
                    "topic": MMWAVE_PORCH,
                    "occupancy_key": "mmwave_area1_occupancy",
                },
                "porch_a2": {
                    "topic": MMWAVE_PORCH,
                    "occupancy_key": "mmwave_area2_occupancy",
                },
            },
        },
    },
    "occupancy_zones": {
        "hallway": {
            "off_lights": [HALL_OFF],
            "sensors": {
                "hall.east": {
                    "sweep": [
                        {"lights": ["hall.up_main"]},
                        {"delay_s": 1.0, "lights": ["hall.center"]},
                    ],
                },
                "hall.west": {"sweep": [{"lights": ["hall.center"]}]},
            },
        },
        "porch": {
            "off_lights": [PORCH_OFF],
            "sensors": {
                "porch.porch_a1": {"sweep": [{"lights": ["porch.a1"]}]},
                "porch.porch_a2": {"sweep": [{"lights": ["porch.a2"]}]},
            },
        },
    },
    "sleep": {"ramp_in_s": 5400, "ramp_out_s": 1800},
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


@pytest.fixture
def socket_enabled() -> Iterator[None]:
    """Satisfy ``hass_ws_client``'s ``socket_enabled`` dependency.

    ``pyproject.toml`` disables pytest-socket (``-p no:socket``) so missed MQTT
    mocks surface — but that also drops the ``socket_enabled`` fixture the HA
    websocket test client requires. Sockets aren't blocked (the plugin is off),
    so a no-op shim is all the client needs.
    """
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
    """No external HA state is needed — the engine is the sole value source."""
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
