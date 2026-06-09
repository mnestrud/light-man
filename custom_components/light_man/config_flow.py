"""Config flow for Light Man (single house-wide instance)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class LightManConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance config flow for Light Man."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup step.

        The single-instance guard is enforced by ``single_config_entry`` in the
        manifest, so this step is a trivial confirm that creates the entry.
        """
        if user_input is not None:
            return self.async_create_entry(title="Light Man", data={})
        return self.async_show_form(step_id="user")
