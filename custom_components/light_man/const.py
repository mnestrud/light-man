"""Constants for the Light Man integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "light_man"

# Platforms loaded by the entry.
PLATFORMS: Final = ["sensor", "switch"]

# --- Store keys (HA helpers.storage) ---------------------------------------
# Seed topology, written/edited out of band (live enumeration — see §1.2).
CONFIG_STORE_KEY: Final = "light_man_config"
CONFIG_STORE_VERSION: Final = 1
# Content version of the bundled seed. Bump when the shipped light_man_config.json
# changes structure so installs whose Store predates it re-seed from the bundle
# instead of running a stale config. v7 dropped the vestigial AL-fallback fields
# (al_switch, day/night_color_mode, night_* targets, sleep_switch) and the legacy
# cutover (legacy_enable + the master-tick disable) — the engine is the sole source.
MODES_STORE_KEY: Final = "light_man_modes"
MODES_STORE_VERSION: Final = 1
# v8 reconciled the topology to the dashboard data model (docs/reference/data-model.md):
# curves became a top-level named library; "sources" became curve-bearing *groups*
# (carrying curve_ref, not an inline profile); physical rooms became first-class
# top-level objects merged across groups; occupancy moved to `occupancy_zones`
# referencing room-owned sensors; sleep ramp became a top-level block. The loader
# derives the unchanged runtime view from this shape, so addressing is identical.
BUNDLED_SEED_VERSION: Final = 8

# --- Seed-config JSON keys --------------------------------------------------
CONF_PUSH_INTERVAL: Final = "push_interval_s"
# Stored shape (data model): a top-level curve library + curve-bearing source
# groups + first-class rooms + zones referencing room sensors + a sleep block.
CONF_CURVES: Final = "curves"
CONF_CURVE_REF: Final = "curve_ref"
CONF_SOURCES: Final = "sources"
CONF_CONSOLIDATED_TOPIC: Final = "consolidated_topic"
CONF_TRANSITION: Final = "transition_s"
# `profile` is the *runtime* key the engine reads (the curve resolved from
# curve_ref); it is not present in the stored seed any more.
CONF_PROFILE: Final = "profile"
CONF_ROOMS: Final = "rooms"
CONF_NAME: Final = "name"
CONF_ID: Final = "id"
CONF_GOVERNS: Final = "governs"
CONF_SET_TOPIC: Final = "set_topic"
CONF_SWITCHES: Final = "switches"
CONF_OCCUPANCY_ZONES: Final = "occupancy_zones"
CONF_SLEEP: Final = "sleep"
CONF_RAMP_IN: Final = "ramp_in_s"
CONF_RAMP_OUT: Final = "ramp_out_s"

# --- Color modes ------------------------------------------------------------
COLOR_MODE_COLOR_TEMP: Final = "color_temp"
COLOR_MODE_RGB: Final = "rgb"

# --- Defaults ---------------------------------------------------------------
DEFAULT_PUSH_INTERVAL_S: Final = 30
DEFAULT_TRANSITION_S: Final = 1.0
# A small gap between per-group publishes so the Zigbee multicasts (network
# broadcasts) don't all land in the same instant — gentle-on-mesh hygiene, not
# correctness (the color-mode split was a stale bulb group membership, fixed at
# the device; see docs/reference/bulb-split-investigation.md).
INTER_PUBLISH_DELAY_S: Final = 0.15

# --- Device value ranges (Z2M) ---------------------------------------------
BRIGHTNESS_MAX: Final = 254
MIRED_MIN: Final = 153  # ~6500 K
MIRED_MAX: Final = 500  # 2000 K

# --- Adaptive engine --------------------------------------------------------
# Real-solar-elevation target engine (the sole adaptive source). These are
# *engine* constants (not per-source); the per-source profile lives in the seed
# config / OptionsFlow. Design: docs/reference/adaptive-algorithm.md.
# Fixed color reference = summer solar-noon elevation at the house lat/long, so
# winter daylight stays honestly warmer (the curve is not renormalized per day).
REF_ELEVATION_DEG: Final = 71.5
# Astronomical-twilight depth: the dusk/dawn band over which lights wind down.
TWILIGHT_BAND_DEG: Final = 18.0
# Brightness interpolates in perceptual (gamma) space, not raw %. ~display gamma.
PERCEPTUAL_GAMMA: Final = 2.2
# Brightness saturates at this fraction of today's noon elevation (full bright by
# mid-morning every season — no dim winter middays). Per-source override allowed.
DEFAULT_SAT: Final = 0.5
# Optional forced day-window edge ramps (seconds), used only when a source's
# day_window is enabled — the window gates the edges without remapping midday.
DEFAULT_EDGE_TRANSITION_S: Final = 1800.0  # 30 min ramp into the live curve
DEFAULT_WIND_DOWN_S: Final = 5400.0  # 90 min wind-down to the night floor


# --- Room modes -------------------------------------------------------------
# A room is either `adaptive` (the default — no hold) or held at a look.
MODE_ADAPTIVE: Final = "adaptive"
HELD_NIGHT: Final = "night"  # config_single -> hold at Light Man's night target
HELD_DAY: Final = "day"  # config_double -> hold at the day value
HELD_MANUAL: Final = "manual"  # up/down_held -> freeze (blueprint owns the level)
HELD_KINDS: Final = frozenset({HELD_NIGHT, HELD_DAY, HELD_MANUAL})

# Inovelli action string -> resulting room mode. Single taps release to
# `adaptive` (down differs only in the resulting paddle state). Double/triple
# taps (PowerView shade scenes) and everything else map to nothing (ignored).
ACTION_MODE: Final = {
    "config_single": HELD_NIGHT,
    "config_double": HELD_DAY,
    "up_held": HELD_MANUAL,
    "down_held": HELD_MANUAL,
    "up_single": MODE_ADAPTIVE,
    "down_single": MODE_ADAPTIVE,
}

# Z2M paddle state values (aggregate topic) for on/off respect.
STATE_ON: Final = "ON"
STATE_OFF: Final = "OFF"

# --- Occupancy (mmwave presence -> directional sweep, MQTT-driven) -----------
CONF_OCCUPANCY: Final = "occupancy"
CONF_OCCUPANCY_KEY: Final = "occupancy_key"
CONF_SENSORS: Final = "sensors"
CONF_TOPIC: Final = "topic"
CONF_SWEEP: Final = "sweep"
CONF_STAGE_DELAY: Final = "delay_s"
CONF_LIGHTS: Final = "lights"
CONF_SOURCE: Final = "source"
CONF_OFF_LIGHTS: Final = "off_lights"
CONF_OFF_TRANSITION: Final = "off_transition_s"
# Default JSON field in an Inovelli mmwave payload that reports overall presence.
DEFAULT_OCCUPANCY_KEY: Final = "occupancy"
DEFAULT_OCCUPANCY_TRANSITION_S: Final = 1.5

# --- Services ---------------------------------------------------------------
SERVICE_RELEASE_HOLD: Final = "release_hold"
SERVICE_CLEAR_HOLDS: Final = "clear_holds"
SERVICE_FORCE_PUSH: Final = "force_push"
ATTR_ROOM: Final = "room"

# --- Entity unique_id kinds ({entry_id}_{kind}) -----------------------------
UID_ACTIVE_HOLDS: Final = "active_holds"
UID_PUSH_ENABLE: Final = "push_enable"
UID_SLEEP_ENABLE: Final = "sleep_enable"

# --- Sleep overlay (Light-Man-owned global toggle + ramp) -------------------
# The user owns the schedule (automate switch.light_man_sleep); Light Man owns
# the ramp. Global durations — a per-source override can come later if needed.
DEFAULT_SLEEP_RAMP_IN_S: Final = 5400.0  # 90 min ease into the sleep target
DEFAULT_SLEEP_RAMP_OUT_S: Final = 1800.0  # 30 min ease back out on wake

# --- Solar event for hold TTL (astral "midnight" = solar midnight/nadir) -----
SOLAR_MIDNIGHT_EVENT: Final = "midnight"

# --- Web control panel (sidebar SPA) ----------------------------------------
# A self-hosted read-only dashboard (Phase 2): a custom panel mounts the SPA,
# served from the package `panel/` dir; the frontend reads live state over the
# websocket_api commands below (no write API yet).
PANEL_URL_PATH: Final = "light-man"  # sidebar route (/light-man)
PANEL_TITLE: Final = "Light Man"
PANEL_ICON: Final = "mdi:white-balance-sunny"
PANEL_WEBCOMPONENT: Final = "light-man-panel"
PANEL_STATIC_URL: Final = "/light_man_panel"  # served bundle root
PANEL_DIR: Final = "panel"  # package subdir holding index.js
PANEL_JS: Final = "index.js"
# websocket_api command types (read-only).
WS_CONFIG: Final = "light_man/config"  # request/response: stored topology
WS_SUBSCRIBE: Final = "light_man/subscribe"  # subscription: live panel state
