"""Constants for the Light Man integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "light_man"

# Logger for hold lifecycle events (arm / release / TTL-expiry info lines).
LOGGER_HOLD: Final = "light_man.hold"

# Platforms loaded by the entry.
PLATFORMS: Final = ["sensor", "switch"]

# --- Store keys (HA helpers.storage) ---------------------------------------
# Seed topology, written/edited out of band (live enumeration — see §1.2).
CONFIG_STORE_KEY: Final = "light_man_config"
CONFIG_STORE_VERSION: Final = 1
# Hold intent; must survive restarts so a held scene is not re-clobbered.
HOLDS_STORE_KEY: Final = "light_man_holds"
HOLDS_STORE_VERSION: Final = 1

# --- Seed-config JSON keys --------------------------------------------------
CONF_PUSH_INTERVAL: Final = "push_interval_s"
CONF_SOURCES: Final = "sources"
CONF_AL_SWITCH: Final = "al_switch"
CONF_CONSOLIDATED_TOPIC: Final = "consolidated_topic"
CONF_LEGACY_ENABLE: Final = "legacy_enable"
CONF_DAY_COLOR_MODE: Final = "day_color_mode"
CONF_NIGHT_COLOR_MODE: Final = "night_color_mode"
CONF_SLEEP_SWITCH: Final = "sleep_switch"
CONF_TRANSITION: Final = "transition_s"
CONF_ROOMS: Final = "rooms"
CONF_SET_TOPIC: Final = "set_topic"
CONF_SWITCHES: Final = "switches"

# --- Color modes ------------------------------------------------------------
COLOR_MODE_COLOR_TEMP: Final = "color_temp"
COLOR_MODE_RGB: Final = "rgb"
COLOR_MODES: Final = frozenset({COLOR_MODE_COLOR_TEMP, COLOR_MODE_RGB})

# --- Defaults ---------------------------------------------------------------
DEFAULT_PUSH_INTERVAL_S: Final = 30
DEFAULT_TRANSITION_S: Final = 1.0
DEFAULT_COLOR_MODE: Final = COLOR_MODE_COLOR_TEMP

# --- Device value ranges (Z2M) ---------------------------------------------
BRIGHTNESS_MAX: Final = 254
MIRED_MIN: Final = 153  # ~6500 K
MIRED_MAX: Final = 500  # 2000 K

# --- AL dummy-switch attributes (read from HA state, not Zigbee) ------------
ATTR_BRIGHTNESS_PCT: Final = "brightness_pct"
ATTR_COLOR_TEMP_KELVIN: Final = "color_temp_kelvin"
ATTR_RGB_COLOR: Final = "rgb_color"

# --- MQTT topic suffixes ----------------------------------------------------
ACTION_SUFFIX: Final = "/action"

# --- Inovelli action strings -> hold ops ------------------------------------
# Arm a hold: Day/Night config taps and the held-dim ramp. Never the
# double/triple taps (PowerView shade scenes) or accent toggles.
ACTIONS_ARM: Final = frozenset(
    {"config_single", "config_double", "up_held", "down_held"}
)
# Release a hold: a tap-on (paddle up).
ACTIONS_RELEASE: Final = frozenset({"up_single"})

# Z2M device-state values (aggregate topic) used for off->on release.
STATE_ON: Final = "ON"
STATE_OFF: Final = "OFF"

# --- Hold ops ---------------------------------------------------------------
OP_ARM: Final = "arm"
OP_RELEASE: Final = "release"

# --- Services ---------------------------------------------------------------
SERVICE_RELEASE_HOLD: Final = "release_hold"
SERVICE_CLEAR_HOLDS: Final = "clear_holds"
SERVICE_FORCE_PUSH: Final = "force_push"
ATTR_ROOM: Final = "room"

# --- Entity unique_id kinds ({entry_id}_{kind}) -----------------------------
UID_ACTIVE_HOLDS: Final = "active_holds"
UID_PUSH_ENABLE: Final = "push_enable"

# --- Solar event for hold TTL (astral "midnight" = solar midnight/nadir) -----
SOLAR_MIDNIGHT_EVENT: Final = "midnight"
