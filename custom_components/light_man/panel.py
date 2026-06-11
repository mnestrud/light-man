"""Register the Light Man sidebar panel and serve its SPA bundle.

A ``panel_custom`` web component (HA injects ``hass`` → free auth + the live
websocket) mounting the dependency-free SPA shipped in the package ``panel/``
dir. Read-only (Phase 2): the frontend reads state over the ``websocket_api``
commands in :mod:`.websocket`; there is no write path yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig

from .const import (
    PANEL_DIR,
    PANEL_ICON,
    PANEL_JS,
    PANEL_STATIC_URL,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PANEL_WEBCOMPONENT,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# HA static paths cannot be unregistered, so the bundle is served once per
# process; this flag survives a config-entry reload (it is never popped).
_STATIC_REGISTERED = "light_man_panel_static"
# Tracks whether the sidebar panel is currently registered (toggled on
# register/unregister); independent of DOMAIN data so it survives a reload.
_PANEL_REGISTERED = "light_man_panel_registered"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve the SPA bundle (once) and register the sidebar panel (idempotent).

    The panel is optional: without the ``frontend`` integration (e.g. a minimal
    install or test env) there is no sidebar to mount it on, so this no-ops and
    the adaptive engine runs unaffected.
    """
    if "frontend" not in hass.config.components:
        return
    if not hass.data.get(_STATIC_REGISTERED):
        panel_dir = Path(__file__).parent / PANEL_DIR
        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_STATIC_URL, str(panel_dir), cache_headers=False)]
        )
        hass.data[_STATIC_REGISTERED] = True
    if hass.data.get(_PANEL_REGISTERED):
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_WEBCOMPONENT,
        module_url=f"{PANEL_STATIC_URL}/{PANEL_JS}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=True,
        embed_iframe=False,
    )
    hass.data[_PANEL_REGISTERED] = True


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar panel on unload (static paths persist for the process)."""
    if not hass.data.get(_PANEL_REGISTERED):
        return
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    hass.data[_PANEL_REGISTERED] = False
