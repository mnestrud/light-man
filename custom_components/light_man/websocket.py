"""Read-only websocket_api commands backing the Light Man panel.

Two commands, both admin-gated and side-effect-free:

- ``light_man/config`` — request/response: the stored topology (data model) the
  panel renders (rooms, curves, source groups, zones, switch map).
- ``light_man/subscribe`` — subscription: pushes the live panel state (engine
  targets, holds, sleep ramp, addressing/publish diagnostics) on every
  coordinator update, plus an initial snapshot.

No write path exists yet (Phase 2 is read-only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.decorators import (
    async_response,
    require_admin,
    websocket_command,
)
from homeassistant.components.websocket_api.messages import event_message
from homeassistant.core import callback

from .const import DOMAIN, WS_CONFIG, WS_SUBSCRIBE

if TYPE_CHECKING:
    from homeassistant.components.websocket_api.connection import ActiveConnection
    from homeassistant.core import HomeAssistant

    from .coordinator import LightManCoordinator

# Registered once per process; survives a config-entry reload (never popped).
_WS_REGISTERED = "light_man_ws_registered"


def _coordinator(hass: HomeAssistant) -> LightManCoordinator | None:
    """Return the single loaded coordinator, or None if not set up."""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        data = getattr(entry, "runtime_data", None)
        if data is not None:
            coordinator: LightManCoordinator = data.coordinator
            return coordinator
    return None


@websocket_command({vol.Required("type"): WS_CONFIG})
@require_admin
@callback
def _ws_config(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the stored topology the panel renders."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_loaded", "Light Man is not loaded")
        return
    connection.send_result(msg["id"], coordinator.panel_config())


@websocket_command({vol.Required("type"): WS_SUBSCRIBE})
@require_admin
@async_response
async def _ws_subscribe(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Stream live panel state on every coordinator update (+ initial snapshot)."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_loaded", "Light Man is not loaded")
        return

    @callback
    def _forward() -> None:
        connection.send_message(event_message(msg["id"], coordinator.panel_state()))

    connection.subscriptions[msg["id"]] = coordinator.async_add_listener(_forward)
    connection.send_result(msg["id"])
    _forward()  # initial push so the panel renders immediately


@callback
def async_register_ws(hass: HomeAssistant) -> None:
    """Register the read-only websocket commands (idempotent, once per process).

    No-ops when ``websocket_api`` is unavailable (a minimal install / test env) —
    the panel is optional; the engine does not depend on it.
    """
    if hass.data.get(_WS_REGISTERED) or "websocket_api" not in hass.config.components:
        return
    async_register_command(hass, _ws_config)
    async_register_command(hass, _ws_subscribe)
    hass.data[_WS_REGISTERED] = True
