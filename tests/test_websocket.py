"""Tests for the read-only panel websocket commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.setup import async_setup_component

from .conftest import setup_lightman

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant


async def _setup(hass: HomeAssistant) -> Any:
    """Set up websocket_api + the integration (so the WS commands register)."""
    assert await async_setup_component(hass, "websocket_api", {})
    return await setup_lightman(hass)


async def test_ws_config_returns_stored_topology(
    hass: HomeAssistant,
    hass_ws_client: Callable[..., Awaitable[Any]],
    mqtt_mock: Any,
) -> None:
    """light_man/config returns the stored data model + switch map."""
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 5, "type": "light_man/config"})
    msg = await client.receive_json()
    assert msg["success"]
    config = msg["result"]["config"]
    assert set(config["curves"]) == {"standard", "sky"}
    assert config["sources"]["overhead"]["curve_ref"] == "standard"
    assert msg["result"]["switch_map"]["zigbee2mqtt/Living Room Switch"] == [
        "overhead",
        "living_room.overhead",
    ]


async def test_ws_subscribe_pushes_live_state(
    hass: HomeAssistant,
    hass_ws_client: Callable[..., Awaitable[Any]],
    mqtt_mock: Any,
) -> None:
    """light_man/subscribe acks, pushes initial state, then re-pushes on update."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    client = await hass_ws_client(hass)
    await client.send_json({"id": 6, "type": "light_man/subscribe"})

    ack = await client.receive_json()
    assert ack["success"]
    initial = await client.receive_json()
    assert initial["event"]["push_enabled"] is True
    assert "engine" in initial["event"]

    # A coordinator update (a hold) pushes a fresh snapshot to the subscriber.
    await coordinator.async_hold("kitchen.overhead", "night")
    update = await client.receive_json()
    assert "kitchen.overhead" in update["event"]["held"]


async def test_ws_not_loaded_errors(
    hass: HomeAssistant,
    hass_ws_client: Callable[..., Awaitable[Any]],
    mqtt_mock: Any,
) -> None:
    """With the entry unloaded, the commands error rather than crash."""
    entry = await _setup(hass)
    client = await hass_ws_client(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    await client.send_json({"id": 7, "type": "light_man/config"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_loaded"

    await client.send_json({"id": 8, "type": "light_man/subscribe"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_loaded"
