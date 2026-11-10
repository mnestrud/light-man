"""Light Man coordinator: the single adaptive brain for the bulb push.

Timer-driven on its own cadence. Each cycle: sweep expired held modes, then (if
Light Man owns the push) publish every source. Per room the target is its live
adaptive value, its held look (night/day, or the level a paddle was held to) —
so an off room is never re-on'd and a held look is never clobbered.

Modes are driven only by explicit Inovelli action intents (``config_*`` hold,
single taps release, a held paddle dims) — never inferred from switch on/off
bounce. The ``push_enable`` switch is a plain master on/off: ON runs the adaptive
push, OFF makes Light Man inert (it no longer touches any legacy stack).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypedDict, cast

from homeassistant.components import mqtt
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adaptive import compute_target, day_look, night_look, sleep_ramp
from .bars import (
    BAR_KEYS,
    COLOR_BAR,
    COLOR_GREEN,
    COLOR_RED,
    EFFECT_BLINK,
    EFFECT_PULSE,
    FEEDBACK_LEVEL,
    FEEDBACK_S,
    HEIGHT_STEP,
    KEY_DEFAULT_LOCAL,
    KEY_HEIGHT,
    KEY_LEVEL,
    LED_BOTTOM,
    LED_TOP,
    LOOK_COLORS,
    BarManager,
)
from .colorspace import kelvin_to_xy, rgb_to_xy
from .const import (
    BAR_HEIGHT_QUIET_S,
    BRIGHTNESS_MAX,
    COLOR_MODE_COLOR_TEMP,
    COLOR_MODE_RGB,
    CONF_CONSOLIDATED_TOPIC,
    CONF_CURVES,
    CONF_LIGHTS,
    CONF_OCCUPANCY,
    CONF_OCCUPANCY_KEY,
    CONF_OFF_LIGHTS,
    CONF_OFF_TRANSITION,
    CONF_ON_TRANSITION,
    CONF_OVERRIDE_IDLE,
    CONF_OVERRIDE_SWITCHES,
    CONF_OVERRIDE_UNTIL_OFF,
    CONF_PUSH_INTERVAL,
    CONF_ROOMS,
    CONF_SENSORS,
    CONF_SET_TOPIC,
    CONF_SOURCE,
    CONF_SOURCES,
    CONF_STAGE_BRIGHTNESS,
    CONF_STAGE_DELAY,
    CONF_STAGE_GROUP,
    CONF_STEPS,
    CONF_SWEEP,
    CONF_SYNC_SWITCHES,
    CONF_TOPIC,
    CONFIG_TAP_MIN_ON_S,
    DEFAULT_OCCUPANCY_KEY,
    DEFAULT_OCCUPANCY_TRANSITION_S,
    DEFAULT_OVERRIDE_IDLE_S,
    DEFAULT_SLEEP_RAMP_IN_S,
    DEFAULT_SLEEP_RAMP_OUT_S,
    DIM_FLOOR,
    DIM_LEAD_S,
    DIM_MIN_FADE_S,
    DIM_MIN_SPAN,
    DIM_RATE,
    DIM_START_DELAY_S,
    DIM_STOP_TRANSITION_S,
    DIM_TIMEOUT_S,
    FAILURE_LOG_INTERVAL_S,
    HELD_DAY,
    HELD_MANUAL,
    HELD_NIGHT,
    INTER_PUBLISH_DELAY_S,
    INTERACTIVE_MODE_CHANGE_MIRED,
    INTERACTIVE_RETRY_DELAY_S,
    INTERACTIVE_RETRY_WINDOW_S,
    INTERACTIVE_TRANSITION_MAX_S,
    INTERACTIVE_TRANSITION_MIN_S,
    LOOK_KINDS,
    MODE_ADAPTIVE,
    MQTT_SLOW_PUBLISH_S,
    OFF_STAGE_DELAY_S,
    OVERRIDE_UNTIL_OFF_DAYS,
    PADDLE_STATE_GRACE_S,
    RELEASE_ACTIONS,
    STATE_OFF,
    STATE_ON,
    SWITCH_ECHO_S,
    SWITCH_READ_GAP_S,
    TRANSITION_CATCHUP_UNKNOWN_S,
    TRANSITION_REASSERT,
    ZONE_DIM_RELEASE_TRANSITION_S,
)
from .modes import action_to_mode
from .publisher import (
    PRIORITY_INTERACTIVE,
    PRIORITY_SWEEP,
    PRIORITY_TICK,
    TICK_BUDGET,
    PacedPublisher,
    Send,
)
from .push import (
    build_payload,
    pct_to_brightness,
    plan_source,
)
from .solar import day_profile, solar_inputs
from .z2m import RESEND_KINDS, parse_publish_failure, set_topic_for, z2m_base_topic

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .config_loader import ValidatedConfig
    from .models import (
        EngineTarget,
        LightManConfig,
        OccupancyLight,
        OccupancyStage,
        OccupancyZone,
        PushPayload,
        SourceConfig,
        SourceProfile,
    )
    from .modes import ModeManager

_SLOT_EPSILON = 1e-6  # slots: a clock within 1 µs of a boundary counts as on it
_LOGGER = logging.getLogger(__name__)


def _monotonic() -> float:
    """Pacing clock (patched in tests)."""
    return time.monotonic()


# Pacing sleep, looked up at call time so tests can patch it.
_pace_sleep = asyncio.sleep
_bar_sleep = asyncio.sleep  # the LED bars' own pacing (separate so tests can idle it)
_dim_sleep = asyncio.sleep  # the wait before a held paddle dims (tests idle it)


@dataclass(slots=True)
class _Dim:
    """One fade a held paddle is running on a light topic."""

    topic: str
    refs: list[str]  # the light refs held at the level when the paddle lets go
    colour: PushPayload  # what the lights showed when the hold began
    start: int
    target: int
    duration: float
    began: float

    def level_at(self, clock: float) -> int:
        """Return the level the fade has reached at ``clock``."""
        done = min(1.0, max(0.0, (clock - self.began) / self.duration))
        return round(self.start + (self.target - self.start) * done)


@dataclass(slots=True)
class _Switch:
    """One Inovelli switch: the lights it governs and what is known of it."""

    lights: list[tuple[str, str]]  # every (source, light ref) it governs
    room: str | None = None  # the room it governs whole (the hallway pair)
    on: bool | None = None  # its paddle state; None until it reports
    on_since: float | None = None  # when it last turned on (config-tap rule)
    height: int | None = None  # its own level (the bar's height), last reported
    bar_quiet_until: float = 0.0  # its level reports are ignored until then
    echo: tuple[str, float] | None = None  # a state write of ours coming back
    ignore_until: float = 0.0  # state reports before then are not presses
    held_off: bool = False  # its paddle is held while it is off
    dims: list[_Dim] = field(default_factory=list)  # fades its held paddle runs
    dim_start: asyncio.Task[None] | None = None  # a hold not dimming yet


class _Binding(NamedTuple):
    """One occupancy trigger: a named sensor's ``(zone, topic, key, sweep)``.

    ``zone``/``name`` identify it; ``topic`` is the mmwave device topic and
    ``key`` the JSON field watched on it (``occupancy`` aggregate or a specific
    ``areaNoccupancy``). Multiple bindings can share a ``topic`` with different
    ``key``s, each tracking its own edge + sweep — so a second mmwave area is
    just another binding, not a refactor.
    """

    zone: str
    name: str
    topic: str
    key: str
    sweep: list[OccupancyStage]


class CoordinatorData(TypedDict):
    """Snapshot consumed by the entities."""

    held: dict[str, dict[str, str]]
    held_count: int
    push_enabled: bool


class LightManCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Drive the adaptive push and own per-room mode state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        validated: ValidatedConfig,
        modes: ModeManager,
    ) -> None:
        """Wire config, mode manager, and the push timer."""
        config: LightManConfig = validated.config
        interval = config[CONF_PUSH_INTERVAL]
        steps = config[CONF_STEPS]
        super().__init__(
            hass,
            _LOGGER,
            name="Light Man",
            config_entry=entry,
            # One timer at the shortest step: every source shares one schedule.
            update_interval=timedelta(seconds=steps["min_s"]),
            always_update=False,
        )
        self._interval = interval
        self._steps = steps
        self.entry_id = entry.entry_id
        self._entry = entry
        self._config = config
        self.stored = validated.stored  # the data-model shape (read by the panel)
        self._modes = modes
        self._switch_map = validated.switch_map
        self._switches: dict[str, _Switch] = {
            base: _Switch(lights) for base, lights in validated.switch_lights.items()
        }
        for base, (room, lights) in validated.room_switch_map.items():
            self._switches[base] = _Switch(lights, room=room)
        self._bar_lights = validated.bar_lights  # base -> the light its bar follows
        self._override_timers: dict[str, Callable[[], None]] = {}
        self._off_stages: dict[str, Callable[[], None]] = {}  # light ref -> cancel
        self._bar_pct: dict[str, float] = {}  # source -> curve % of its last send
        self._zone_lit: dict[str, set[str]] = {}  # zone -> topics lit this episode
        # zone -> {light ref: brightness %} shown dimmed this episode (night light)
        self._zone_dims: dict[str, dict[str, float]] = {}
        self._room_source: dict[str, str] = {
            room: source_key
            for source_key, source in config[CONF_SOURCES].items()
            for room in source.get(CONF_ROOMS, {})
        }
        # Dedup: what each topic last showed (colour + brightness, no transition),
        # set only on broker ack; cleared by Z2M failures and outages.
        self._last_published: dict[str, PushPayload] = {}
        self._last_planned: dict[str, dict[str, Any]] = {}  # for re-sends/replay
        self._unsubs: list[Any] = []
        self._clock: Callable[[], float] = lambda: _monotonic()
        topics = [s[CONF_CONSOLIDATED_TOPIC] for s in config[CONF_SOURCES].values()]
        self._z2m_base, _disagree = z2m_base_topic(topics)
        self._group_topics: set[str] | None = None
        # source -> generation of the latest force request (cleared when a unit
        # planned with that generation is actually sent)
        self._force_pending: dict[str, int] = {}
        self._force_seq = 0
        self._failed_at: dict[str, float] = {}  # topic -> last Z2M failure
        # switches whose level binding still reaches a light or another switch;
        # None until Z2M's device list has been seen (then nothing is written)
        self._level_bound: set[str] | None = None
        self._plan_diag: dict[str, tuple[dict[str, Any], int]] = {}
        self._replanned_at: dict[str, float] = {}  # source -> last BUSY re-plan
        self._publisher = PacedPublisher(
            self._send,
            # A tracked task: exits when its queue is empty; unload cancels it.
            lambda coro: hass.async_create_task(coro, "lm_publisher", eager_start=True),
            is_group=self._is_group,
            min_gap_s=INTER_PUBLISH_DELAY_S,
            clock=lambda: _monotonic(),
            sleep=lambda seconds: _pace_sleep(seconds),
        )
        # LED bars: status lights, tap/shade animations, curve-following level.
        self._animations = validated.switch_animations
        self._bars = BarManager(
            self._send,
            lambda coro: hass.async_create_task(coro, "lm_bars", eager_start=True),
            sleep=lambda seconds: _bar_sleep(seconds),
        )
        self._due = 0.0  # when every source's next step is due (shared schedule)
        self._step_s = float(interval)  # the current step: its length is its fade
        self._step_end: datetime | None = None  # where every curve send aims
        self._pushed_once = False
        self._closed = False
        self._marked_at: dict[str, float] = {}
        self._planned_at: dict[str, float] = {}
        self._consolidated_source = {
            s[CONF_CONSOLIDATED_TOPIC]: k for k, s in config[CONF_SOURCES].items()
        }
        self._topic_owner: dict[str, str] = {}
        self._topic_source_consolidated: dict[str, str] = {}
        for k, src in config[CONF_SOURCES].items():
            self._topic_owner[src[CONF_CONSOLIDATED_TOPIC]] = k
            for room_cfg in src.get(CONF_ROOMS, {}).values():
                if room_cfg.get(CONF_SET_TOPIC):
                    self._topic_owner[room_cfg[CONF_SET_TOPIC]] = k
                    self._topic_source_consolidated[room_cfg[CONF_SET_TOPIC]] = src[
                        CONF_CONSOLIDATED_TOPIC
                    ]
        self._interactive_recent: dict[str, tuple[float, int]] = {}
        self._failure_log: dict[str, tuple[float | None, int]] = {}
        self._z2m_online: bool | None = None
        self._outage = False
        self._outage_topics: set[str] = set()
        self.push_enabled = False
        self.mqtt_available = True
        # Global sleep overlay: target on/off, the ramp value held at the last
        # toggle (ramp start), and when it flipped. None changed_at = snapped.
        self.sleep_on = False
        self._sleep_start = 0.0
        self._sleep_changed_at: datetime | None = None
        # Occupancy: flatten zones into named (zone, sensor) bindings, indexed by
        # topic (for the message handler) and by zone (for the all-clear check).
        # Edge state + in-flight sweeps are keyed per binding so two areas of one
        # switch (same topic, different key) trigger independently.
        self._occupancy = config[CONF_OCCUPANCY]
        self._bindings: list[_Binding] = []
        for zone_key, zone in self._occupancy.items():
            for name, sensor in zone.get(CONF_SENSORS, {}).items():
                self._bindings.append(
                    _Binding(
                        zone=zone_key,
                        name=name,
                        topic=sensor[CONF_TOPIC],
                        key=sensor.get(CONF_OCCUPANCY_KEY, DEFAULT_OCCUPANCY_KEY),
                        sweep=sensor[CONF_SWEEP],
                    )
                )
        self._bindings_by_topic: dict[str, list[_Binding]] = {}
        self._bindings_by_zone: dict[str, list[_Binding]] = {}
        for binding in self._bindings:
            self._bindings_by_topic.setdefault(binding.topic, []).append(binding)
            self._bindings_by_zone.setdefault(binding.zone, []).append(binding)
        self._sensor_occupied: dict[tuple[str, str], bool] = {}
        # Zones not yet settled after a start (see _settle_start), and the
        # zones each sync switch's first report can settle.
        self._start_pending: set[str] = set(self._bindings_by_zone)
        self._sync_zones = _switch_zones(self._occupancy, CONF_SYNC_SWITCHES)
        # The rooms each zone's sweeps light: a config-tap look on any of them
        # holds the zone on (no all-clear OFF; sweeps show the look).
        self._topic_room: dict[str, str] = {
            room_cfg[CONF_SET_TOPIC]: room
            for source in config[CONF_SOURCES].values()
            for room, room_cfg in source.get(CONF_ROOMS, {}).items()
            if room_cfg.get(CONF_SET_TOPIC)
        }
        self._zone_rooms: dict[str, set[str]] = {
            zone_key: {
                self._topic_room[light[CONF_SET_TOPIC]]
                for binding in bindings
                for stage in binding.sweep
                for light in stage.get(CONF_LIGHTS, [])
                if light[CONF_SET_TOPIC] in self._topic_room
            }
            for zone_key, bindings in self._bindings_by_zone.items()
        }
        # switch -> zones it holds
        self._override_zones = _switch_zones(self._occupancy, CONF_OVERRIDE_SWITCHES)
        self._sweep_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self.diagnostics: dict[str, Any] = {
            "issues": validated.issues,
            "dedup_skips": 0,
            "addressing": {},
            "last_publish": {},
            "engine": {},
            "sleep": {"on": False, "s": 0.0},
            "occupancy": {},
            "delivery_failures": {"total": 0, "by_kind": {}, "by_topic": {}},
            "forced_floods": 0,
            "ignored_config_taps": 0,
        }

    # --- lifecycle ----------------------------------------------------------

    async def _async_setup(self) -> None:
        """Prune stale holds, subscribe to switch, occupancy and Z2M bridge topics."""
        # Drop holds orphaned by a topology change (keys no longer addressable).
        pruned = await self._modes.prune(set(self._room_source))
        if pruned:
            _LOGGER.info("pruned orphaned holds: %s", ", ".join(sorted(pruned)))
        # A dim cut short by a restart has no level: the room goes back to its curve.
        for room in sorted(self._modes.held_rooms()):
            if (
                self._modes.mode_of(room) == HELD_MANUAL
                and self._modes.look_of(room) is None
            ):
                await self._modes.release(room)
                _LOGGER.info("dropped the unfinished dim of %s", room)
        # One subscription per switch: the device's JSON publish. It always
        # carries `action` on a tap (Z2M output: json), so the separate
        # `<base>/action` topic would only duplicate every tap's handling
        # (observed live as double force-floods per tap).
        for base in self._all_switches():
            self._unsubs.append(
                await mqtt.async_subscribe(self.hass, base, self._handle_message)
            )
        for zone in self._modes.overrides():
            if zone in self._occupancy:
                self._restart_idle_timer(zone)  # presence unknown until reported
            else:
                await self._modes.end_override(zone)
        for topic in self._bindings_by_topic:
            self._unsubs.append(
                await mqtt.async_subscribe(self.hass, topic, self._handle_occupancy)
            )
        bridge = f"{self._z2m_base}/bridge"
        for suffix, handler in (
            ("logging", self._handle_bridge_logging),
            ("state", self._handle_bridge_state),
            ("groups", self._handle_bridge_groups),
            ("devices", self._handle_bridge_devices),
        ):
            self._unsubs.append(
                await mqtt.async_subscribe(self.hass, f"{bridge}/{suffix}", handler)
            )
        self._unsubs.append(
            mqtt.async_subscribe_connection_status(self.hass, self._on_mqtt_status)
        )
        # Defer the first push until HA has fully started.
        self._unsubs.append(async_at_started(self.hass, self._takeover_on_start))

    async def _takeover_on_start(self, _hass: HomeAssistant) -> None:
        """Push once HA is fully up — unless the first refresh already did."""
        if self.push_enabled and not self._pushed_once:
            await self._run_push(force=True)
        self._publish_snapshot()
        # Switch states are not retained: read each once so a config tap after a
        # restart is judged on the switch's real state (unknown counts as off).
        self._entry.async_create_background_task(
            self.hass, self._read_switches(), name="lm_read_switches"
        )

    def _all_switches(self) -> list[str]:
        return list(self._switches)

    async def _read_switches(self) -> None:
        """Ask each switch for its state once (unicasts, spaced)."""
        for base in self._all_switches():
            if self._closed:
                return
            if self._switches[base].on is not None:
                continue  # it already reported
            await mqtt.async_publish(
                self.hass, f"{base}/get", json.dumps({"state": ""})
            )
            await _pace_sleep(SWITCH_READ_GAP_S)

    @callback
    def _publish_snapshot(self) -> None:
        """Refresh entities without restarting the tick timer.

        ``async_set_updated_data`` reschedules the refresh, so a stream of taps or
        occupancy messages would keep postponing the periodic ticks.
        """
        self.data = self._snapshot()
        self.async_update_listeners()
        self._sync_bars()

    def shutdown_subscriptions(self) -> None:
        """Unsubscribe all MQTT subscriptions and stop the publisher (unload)."""
        self._closed = True
        while self._unsubs:
            self._unsubs.pop()()
        for switch in self._switches.values():
            switch.dims = []
            if switch.dim_start is not None:
                switch.dim_start.cancel()
                switch.dim_start = None
        for cancel in [*self._override_timers.values(), *self._off_stages.values()]:
            cancel()
        self._override_timers.clear()
        self._off_stages.clear()
        self._publisher.cancel()
        self._bars.cancel()

    def is_known_room(self, room: str) -> bool:
        """Return True if ``room`` is a light ref, or a zone that can be overridden."""
        return room in self._room_source or any(
            self._occupancy[zone].get(CONF_OVERRIDE_SWITCHES)
            for zone in self._occupancy
            if zone == room
        )

    def config_summary(self) -> dict[str, Any]:
        """Non-secret config shape for diagnostics."""
        return {
            "push_interval_s": self._config[CONF_PUSH_INTERVAL],
            "sources": {
                key: {
                    "consolidated_topic": source.get(CONF_CONSOLIDATED_TOPIC),
                    "rooms": sorted(source.get(CONF_ROOMS, {})),
                }
                for key, source in self._config[CONF_SOURCES].items()
            },
        }

    def panel_config(self) -> dict[str, Any]:
        """Return the stored topology + today's curve previews for the panel."""
        return {
            "config": self.stored,
            "switch_map": dict(self._switch_map),
            "preview": self.curve_previews(),
        }

    def curve_previews(self) -> dict[str, Any] | None:
        """Per-curve brightness/color across *today*, for the panel's time viz.

        Reuses the engine (``compute_target``) over today's sampled sun path so
        the visualization matches what the engine actually publishes — including
        the day-window wind-down at "sunset". Returns ``None`` if the sun path is
        unavailable (a polar edge), and skips any structurally broken curve.
        """
        try:
            profile = day_profile(self.hass, dt_util.utcnow())
        except ValueError:
            return None
        samples = profile["samples"]
        noon = profile["noon_elevation"]
        curves = self.stored.get(CONF_CURVES, {})
        preview: dict[str, Any] = {}
        noon_minute = profile["events"].get("noon")
        for name, curve in curves.items():
            try:
                preview[name] = self._curve_series(curve, samples, noon, noon_minute)
            except KeyError, TypeError, ValueError:
                continue  # a malformed curve is skipped, not fatal to the panel
        return {
            "times": [minutes for minutes, _e in samples],
            "events": profile["events"],
            "noon_elevation": noon,
            "curves": preview,
        }

    @staticmethod
    def _curve_series(
        curve: SourceProfile,
        samples: list[tuple[int, float]],
        noon: float,
        noon_minute: int | None,
    ) -> dict[str, Any]:
        """Build one curve's day series: awake brightness/color + the sleep target."""
        brightness: list[float] = []
        kelvin: list[int] = []
        for minutes, elevation in samples:
            target = compute_target(
                elevation, noon, curve, sleep_s=0.0, now_minutes=float(minutes)
            )
            brightness.append(round(target.brightness_pct, 1))
            kelvin.append(round(target.color_temp_kelvin or 0))
        mode = curve.get("base_color_mode", COLOR_MODE_COLOR_TEMP)
        sleep = curve.get("sleep", {})
        series: dict[str, Any] = {
            "br": brightness,
            "mode": mode,
            "sleep_br": sleep.get("br"),
            "sleep_mode": sleep.get("color_mode", COLOR_MODE_COLOR_TEMP),
            "inflections": _curve_inflections(curve, samples, brightness, noon_minute),
        }
        if mode == COLOR_MODE_RGB:
            series["rgb"] = curve.get("base_rgb")
        else:
            series["ct"] = kelvin
        if sleep.get("color_mode") == COLOR_MODE_RGB:
            series["sleep_rgb"] = sleep.get("rgb")
        else:
            series["sleep_ct"] = sleep.get("ct")
        return series

    def panel_state(self) -> dict[str, Any]:
        """Live, JSON-able read model for the panel's subscription stream.

        A flat snapshot of everything the read-only dashboard shows: master
        on/off, sleep ramp, hold state, the engine's per-source-group targets,
        the latest addressing/publish/occupancy diagnostics, delivery feedback
        from Z2M, publisher pacing stats, and config issues. No secrets; never
        mutates anything.
        """
        return {
            "push_enabled": self.push_enabled,
            "mqtt_available": self.mqtt_available,
            "z2m_online": self._z2m_online,
            "sleep": self.diagnostics.get("sleep", {}),
            "held": self._modes.as_attributes(),
            "engine": self.diagnostics.get("engine", {}),
            "addressing": self.diagnostics.get("addressing", {}),
            "last_publish": self.diagnostics.get("last_publish", {}),
            "occupancy": self.diagnostics.get("occupancy", {}),
            "issues": self.diagnostics.get("issues", []),
            "dedup_skips": self.diagnostics.get("dedup_skips", 0),
            "delivery_failures": self.diagnostics.get("delivery_failures", {}),
            "forced_floods": self.diagnostics.get("forced_floods", 0),
            "ignored_config_taps": self.diagnostics.get("ignored_config_taps", 0),
            "publisher": self._publisher.stats.as_dict(),
        }

    async def _async_update_data(self) -> CoordinatorData:
        """Periodic tick: sweep expired holds, then push the sources that are due.

        The timer runs at the shortest step; all sources step together on one
        schedule (:meth:`_plan_step`). Expiry only invalidates — the next due
        tick sends what changed (no forced flood at solar midnight).
        """
        now = dt_util.utcnow()
        looks = self._look_rooms()
        expired = await self._modes.sweep_expired(now)
        for room in expired:
            self._invalidate_room(room)
            _LOGGER.info("hold expired for room %s", room)
        self._resume_motion(looks.intersection(expired))
        for zone in await self._modes.sweep_expired_overrides(now):
            _LOGGER.info("override expired for zone %s", zone)
            self._cancel_idle_timer(zone)
            if zone in self._occupancy and self._zone_vacant(zone):
                self._clear_zone(zone)
        await self._finish_stale_dims()
        await self._run_push(force=False, due_only=True)
        self._sync_bars()
        return self._snapshot()

    # --- push (engine-driven adaptive value) --------------------------------

    async def _run_push(
        self,
        *,
        force: bool,
        due_only: bool = False,
    ) -> None:
        """Plan each source's unit and hand it to the paced publisher.

        Every step publishes ``target(t_end)`` with ``transition`` = the step
        (A6 glide), where ``t_end`` is one step ahead; the bulb fades
        continuously between steps. All sources share the schedule, so lights
        on different curves change at the same moments.
        """
        if not self.push_enabled:
            return
        now = dt_util.utcnow()
        clock = self._clock()
        if force:
            self.diagnostics["forced_floods"] = (
                self.diagnostics.get("forced_floods", 0) + 1
            )
        # HA rounds the timer to whole seconds, so it fires up to a second either
        # side of the due time; a step due within half a timer period goes now
        # rather than one whole period late (review nit 12).
        slack = self._steps["min_s"] / 2
        if clock >= self._due - slack:  # this send starts the next step
            self._step_s = self._plan_step(now)
            self._due = max(clock, self._due) + self._step_s
            self._step_end = now + timedelta(seconds=self._step_s)
        elif due_only:
            return  # not due yet; a forced push leaves the schedule unchanged
        for key in self._config[CONF_SOURCES]:
            self._enqueue_source(key, force=force)
        self._pushed_once = True

    def _step_target(self) -> datetime:
        """Return the current step's end: where every curve send aims.

        Every source aims at the same moment, so a source sent later in the
        step (paced behind the others) fades faster and lands with them. Past
        the step's end (a backlog), a send aims one step ahead of now.
        """
        now = dt_util.utcnow()
        if self._step_end is None or self._step_end <= now:
            return now + timedelta(seconds=self._step_s)
        return self._step_end

    def _plan_step(self, now: datetime) -> float:
        """Return the next step's length: the longest that keeps every curve close.

        Steps are whole multiples of the shortest step, up to the push interval.
        A step ends early when any curve, at any point in it, would move more than
        ``max_brightness_pct`` / ``max_mired`` / ``max_xy`` from where it starts —
        so a light that turns on mid-step (it comes up at the step's end value)
        is never far from the lights already on, which reach it gradually.
        """
        shortest = self._steps["min_s"]
        count = max(1, math.floor(self._interval / shortest + _SLOT_EPSILON))
        offsets = [shortest * k for k in range(1, count + 1)]
        if self._interval - offsets[-1] > _SLOT_EPSILON:
            offsets.append(float(self._interval))
        allowed = len(offsets)  # how many offsets every curve stays within
        for source in self._config[CONF_SOURCES].values():
            start = self._curve_target(source, now)
            if start is None:
                continue
            for index, offset in enumerate(offsets[:allowed]):
                later = self._curve_target(source, now + timedelta(seconds=offset))
                if later is not None and not self._close(start, later):
                    allowed = index
                    break
        return offsets[max(allowed, 1) - 1]

    def _close(self, start: EngineTarget, end: EngineTarget) -> bool:
        """Return True when ``end`` is within one step's change of ``start``."""
        if (
            abs(end.brightness_pct - start.brightness_pct)
            > self._steps["max_brightness_pct"]
        ):
            return False
        if (
            start.color_mode == end.color_mode == COLOR_MODE_COLOR_TEMP
            and start.color_temp_kelvin
            and end.color_temp_kelvin
        ):
            mired = abs(1e6 / end.color_temp_kelvin - 1e6 / start.color_temp_kelvin)
            return mired <= self._steps["max_mired"]
        a, b = _target_xy(start), _target_xy(end)
        if a is None or b is None:
            return True
        return math.dist(a, b) <= self._steps["max_xy"]

    def _enqueue_source(self, key: str, *, force: bool) -> None:
        """Queue a source's tick as a *planned* unit (planned when it is sent).

        Planning at send time means a hold armed while the unit waited for budget
        is re-asserted in it, and a re-send/replay of the source always carries
        the held-room re-asserts with the flood (review blockers 2 and 3).
        """
        source = self._config[CONF_SOURCES][key]
        if force:
            # A later unforced unit replaces this one by key; the force survives.
            self._force_seq += 1
            self._force_pending[key] = self._force_seq
        used: list[int | None] = [None]  # the force generation this unit planned with

        def planner() -> list[Send]:
            t_end = self._step_target()
            used[0] = self._force_pending.get(key)
            sends = self._plan_source(key, source, t_end, force=used[0] is not None)
            self._note_planned(sends, PRIORITY_TICK)
            engine = self.diagnostics["engine"].get(key)
            if sends and engine:
                self._bar_pct[key] = float(engine["brightness_pct"])  # bars follow
            return sends

        future = self._publisher.enqueue_planned(
            f"source:{key}", planner, priority=PRIORITY_TICK
        )
        future.add_done_callback(self._on_sent)
        future.add_done_callback(partial(self._on_source_sent, key, used))

    def _on_source_sent(
        self, key: str, used: list[int | None], future: asyncio.Future[list[Send]]
    ) -> None:
        """Record the unit's plan diagnostics; clear the force it was planned with.

        A force stays pending until a unit planned with *that* request is sent,
        so neither re-planning, replacement by key nor an unrelated unit already
        on the air can drop it. Diagnostics count once per sent/resolved unit,
        not once per re-plan.
        """
        diag = self._plan_diag.pop(key, None)
        if diag is not None:
            self.diagnostics["addressing"][key] = diag[0]
            self.diagnostics["dedup_skips"] += diag[1]
        if (
            future.result()
            and used[0] is not None
            and self._force_pending.get(key) == used[0]
        ):
            del self._force_pending[key]

    def _plan_source(
        self, key: str, source: SourceConfig, t_end: datetime, *, force: bool
    ) -> list[Send]:
        """Plan one source's unit (consolidated + re-asserts, or per-room)."""
        try:
            adaptive = self._engine_payload(key, source, t_end)
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("skipping source %s this tick: %s", key, err)
            return []
        if adaptive is None:
            return []
        looks = self._held_looks(key, source, adaptive)
        if any(looks.get(room) is None and self._dim_pending(room) for room in looks):
            self._plan_diag[key] = ({"mode": "deferred", "topics": []}, 0)
            return []  # a paddle is being held; this source waits one tick
        addressing, planned = plan_source(
            source, adaptive_payload=adaptive, looks=looks, max_unit=TICK_BUDGET
        )
        diag = {"mode": addressing, "topics": [p.topic for p in planned]}
        consolidated = source[CONF_CONSOLIDATED_TOPIC]
        if (
            addressing == "reassert"
            and not force
            and self._unchanged(consolidated, adaptive)
        ):
            self._plan_diag[key] = (diag, 1)
            return []
        skips = 0
        sends: list[Send] = []
        for item in planned:
            if item.dedup and not force and self._unchanged(item.topic, item.payload):
                skips += 1
                continue
            transition = self._tick_transition(
                item.topic, item.payload, item.dedup, t_end, force=force
            )
            sends.append(Send(item.topic, {**item.payload, "transition": transition}))
        self._plan_diag[key] = (diag, skips)
        if addressing == "per_room":
            sends = self._chunk_per_room(sends)
        return sends

    def _chunk_per_room(self, sends: list[Send]) -> list[Send]:
        """Send a per-room fallback in budget-sized pieces (no contiguity needed)."""
        first: list[Send] = []
        rest: list[Send] = []
        groups = 0
        for send in sends:
            is_group = self._is_group(send.topic)
            if is_group and groups >= TICK_BUDGET:
                rest.append(send)
                continue
            groups += is_group
            first.append(send)
        for start in range(0, len(rest), TICK_BUDGET):
            self._enqueue(rest[start : start + TICK_BUDGET], priority=PRIORITY_TICK)
        return first

    def _curve_target(
        self, source: SourceConfig, when: datetime
    ) -> EngineTarget | None:
        """Return the curve's target for ``source`` at ``when`` (None: no sun data).

        A broken profile yields None here too: step planning skips that curve
        and its own send reports the error.
        """
        try:
            elevation, noon = solar_inputs(self.hass, when)
            return self._target_at(source, when, elevation, noon)
        except KeyError, TypeError, ValueError:
            return None

    def _target_at(
        self, source: SourceConfig, when: datetime, elevation: float, noon: float
    ) -> EngineTarget:
        local = dt_util.as_local(when)
        minutes = local.hour * 60 + local.minute + local.second / 60
        return compute_target(
            elevation,
            noon,
            source["profile"],
            sleep_s=self._compute_sleep_s(when),
            now_minutes=minutes,
        )

    def _engine_payload(
        self, key: str, source: SourceConfig, when: datetime
    ) -> PushPayload | None:
        """Return the engine's colour/brightness for ``source`` at ``when``.

        Every input — sun elevation, clock minutes, and the sleep ramp — is taken
        at ``when``, so a glide aims exactly where the curve will be (U5).
        """
        try:
            elevation, noon = solar_inputs(self.hass, when)
        except ValueError:
            return None
        target = self._target_at(source, when, elevation, noon)
        sleep_s = self._compute_sleep_s(when)
        self.diagnostics["sleep"] = {"on": self.sleep_on, "s": round(sleep_s, 3)}
        self.diagnostics["engine"][key] = {
            "elevation": round(elevation, 2),
            "brightness_pct": round(target.brightness_pct, 1),
            "color_mode": target.color_mode,
            "color_temp_kelvin": target.color_temp_kelvin,
            "rgb_color": list(target.rgb_color) if target.rgb_color else None,
            "xy": list(target.xy) if target.xy else None,
            "at": when.isoformat(),
        }
        return _strip(_target_to_payload(target, 0.0))

    def _held_looks(
        self, key: str, source: SourceConfig, adaptive: PushPayload | None = None
    ) -> dict[str, PushPayload | None]:
        """Each held room's look: night/day from the curve, manual as dimmed to.

        With the curve value (``adaptive``), a zone's dimmed night light that is
        lit and not overridden counts as held too: the curve colour at its
        fixed level, so each flood is followed by its re-assert.
        """
        looks: dict[str, PushPayload | None] = {}
        profile = source["profile"]
        for room in source.get(CONF_ROOMS, {}):
            if not self._modes.is_held(room):
                continue
            mode = self._modes.mode_of(room)
            if mode == HELD_NIGHT:
                looks[room] = _strip(_target_to_payload(night_look(profile), 0.0))
            elif mode == HELD_DAY:
                looks[room] = _strip(_target_to_payload(day_look(profile), 0.0))
            else:
                look = self._modes.look_of(room)
                looks[room] = cast("PushPayload", look) if look else None
        if adaptive is not None:
            rooms = source.get(CONF_ROOMS, {})
            for room, level in self._active_dims().items():
                if room in rooms and room not in looks:
                    looks[room] = _dimmed(_strip(adaptive), level)
        del key
        return looks

    def _active_dims(self) -> dict[str, float]:
        """Dimmed zone lights to keep at their level (zones not overridden)."""
        return {
            room: level
            for zone_key, dims in self._zone_dims.items()
            if self._modes.override_until(zone_key) is None
            for room, level in dims.items()
        }

    def _until_off(self, zone_key: str) -> bool:
        return bool(self._occupancy[zone_key].get(CONF_OVERRIDE_UNTIL_OFF, False))

    def _unchanged(self, topic: str, payload: PushPayload) -> bool:
        """Return True when ``topic`` already shows this colour and brightness.

        A room topic is compared with what it last showed — its own mark or a
        newer flood of its source — never with an hours-old room mark alone.
        """
        return self._shown(topic) == _strip(payload)

    def _tick_transition(
        self,
        topic: str,
        payload: PushPayload,
        dedup: bool,
        t_end: datetime,
        *,
        force: bool = False,
    ) -> float:
        """Glide to ``t_end`` for curve sends, short for re-asserts, catch-up.

        Catch-up (U7): ``0.1 s`` per brightness point of change, clamped to 2-10 s,
        so a stale bulb converges without a visible jump; 5 s when the last value
        is unknown (boot, after a dedup clear).
        """
        if not dedup:
            return TRANSITION_REASSERT
        last = self._shown(topic)
        if last is None:
            return TRANSITION_CATCHUP_UNKNOWN_S
        if force:
            delta = abs(payload.get("brightness", 0) - last.get("brightness", 0))
            delta_pct = delta * 100 / 254
            d_mired = abs(payload.get("color_temp", 0) - last.get("color_temp", 0))
            if ("color_temp" in payload) != ("color_temp" in last):
                d_mired = INTERACTIVE_MODE_CHANGE_MIRED
            seconds = 0.1 * delta_pct + 0.02 * d_mired
            return round(min(10.0, max(2.0, seconds)), 1)
        remaining = (t_end - dt_util.utcnow()).total_seconds()
        return round(max(1.0, remaining), 1)

    def _enqueue(
        self,
        sends: list[Send],
        *,
        priority: int,
        origin_at: float | None = None,
        together: bool = False,
    ) -> None:
        """Queue a concrete unit; dedup marks are set from the acked sends only."""
        if self._closed:
            return
        self._note_planned(sends, priority)
        future = self._publisher.enqueue(
            sends, priority=priority, origin_at=origin_at, together=together
        )
        future.add_done_callback(self._on_sent)

    def _note_planned(self, sends: list[Send], priority: int) -> None:
        now = self._clock()
        for send in sends:
            self._last_planned[send.topic] = send.payload
            self._planned_at[send.topic] = now
            if self._outage:
                self._outage_topics.add(send.topic)
            self._interactive_recent[send.topic] = (now, 0)  # retries per plan

    def _on_sent(self, future: asyncio.Future[list[Send]]) -> None:
        """Mark exactly what the broker acked (coalesced sends are not marked).

        The publisher always resolves its futures (never cancels them).
        """
        now = self._clock()
        for send in future.result():
            failed = self._failed_at.get(send.topic)
            if failed is not None and failed >= self._planned_at.get(send.topic, 0.0):
                continue  # Z2M reported this send failed before its unit resolved
            payload = cast("PushPayload", send.payload)
            self._last_published[send.topic] = _strip(payload)
            self._marked_at[send.topic] = now
            self.diagnostics["last_publish"][send.topic] = dict(send.payload)

    async def _send(self, topic: str, payload: dict[str, Any]) -> bool:
        """Publish once (the publisher's send), guarded on broker availability."""
        if not mqtt.mqtt_config_entry_enabled(self.hass):
            self._set_mqtt_available(available=False)
            return False
        started = self._clock()
        try:
            await mqtt.async_publish(self.hass, topic, json.dumps(payload))
        except HomeAssistantError:
            self._set_mqtt_available(available=False)
            return False
        if self._clock() - started >= MQTT_SLOW_PUBLISH_S:
            _LOGGER.warning(
                "MQTT publish to %s took %.0f s", topic, self._clock() - started
            )
        self._set_mqtt_available(available=True)
        return True

    def _set_mqtt_available(self, *, available: bool) -> None:
        """Track MQTT connectivity, logging only on transitions."""
        if available == self.mqtt_available:
            return
        self.mqtt_available = available
        if available:
            _LOGGER.info("MQTT reconnected; resuming push")
        else:
            _LOGGER.warning("MQTT unavailable; skipping push until reconnected")

    # --- interactive sends (taps, holds, stage-off, occupancy) --------------

    def _room_glide_target(self, source_key: str) -> PushPayload | None:
        """Return the source's curve value at its current glide end.

        One-shot sends aim where the rest of the house is gliding to, so a room
        arrives slightly early and waits rather than stepping backwards (U3).
        """
        source = self._config[CONF_SOURCES][source_key]  # loader-validated
        try:
            return self._engine_payload(source_key, source, self._step_target())
        except KeyError, TypeError, ValueError:
            return None

    def _shown(self, topic: str) -> PushPayload | None:
        """Return what a room topic last showed: its own mark or its source flood.

        Floods never mark room topics, so the source's consolidated mark is the
        better baseline whenever it is newer (review item 4).
        """
        own = self._last_published.get(topic)
        own_at = self._marked_at.get(topic, float("-inf"))
        consolidated = self._topic_source_consolidated.get(topic)
        if consolidated is None:
            return own
        flood = self._last_published.get(consolidated)
        if (
            flood is not None
            and self._marked_at.get(consolidated, float("-inf")) > own_at
        ):
            return flood
        return own if own is not None else flood

    def _interactive_send(
        self,
        topic: str,
        payload: PushPayload,
        *,
        transition: float | None = None,
        origin_at: float | None = None,
    ) -> None:
        """Queue one interactive send with a change-scaled transition (U2)."""
        body: dict[str, Any] = dict(payload)
        if transition is None:
            transition = interactive_transition(self._shown(topic), payload)
        body["transition"] = transition
        self._enqueue(
            [Send(topic, body)], priority=PRIORITY_INTERACTIVE, origin_at=origin_at
        )

    def _compute_sleep_s(self, now: datetime) -> float:
        """Return the global sleep-ramp value in [0, 1] (0 awake, 1 asleep)."""
        target = 1.0 if self.sleep_on else 0.0
        if self._sleep_changed_at is None:
            return target
        duration = (
            DEFAULT_SLEEP_RAMP_IN_S if self.sleep_on else DEFAULT_SLEEP_RAMP_OUT_S
        )
        elapsed = (now - self._sleep_changed_at).total_seconds()
        return sleep_ramp(elapsed, duration, start=self._sleep_start, target=target)

    async def async_set_sleep(self, *, enabled: bool, ramp: bool = True) -> None:
        """Engage/release the sleep overlay — ramped, or snapped on restore.

        No push here: the ramp starts at the next due tick, and every tick
        targets the ramp at its glide end (no forced flood per toggle).
        """
        now = dt_util.utcnow()
        if ramp:
            self._sleep_start = self._compute_sleep_s(now)
            self._sleep_changed_at = now
        else:
            self._sleep_changed_at = None  # snap straight to the target
        self.sleep_on = enabled
        self._publish_snapshot()

    # --- LED bars (status for the switch's own room, animations, level) -----

    def _governed(self, base: str) -> list[tuple[str, str]]:
        """Return every (source, light ref) a switch governs."""
        return self._switches[base].lights

    def _bar_source(self, base: str) -> str:
        """Return the source whose curve a switch's bar follows."""
        return self._bar_lights[base][0]

    def _bar_fraction(self, source_key: str) -> float | None:
        """Where the source's last curve send sits, 0 (dark) to 1 (its brightest).

        Follows the curve sends only: the bar level changes when a tick sends,
        never between sends. None until the source has sent.
        """
        peak = self._config[CONF_SOURCES][source_key]["profile"].get("max_br")
        sent = self._bar_pct.get(source_key)
        if sent is None or not peak:
            return None
        return max(0.0, min(1.0, sent / peak))

    def _bar_leds(self, base: str) -> dict[str, int | None]:
        """Status lights of a switch: a held look on top, the override below."""
        refs = [ref for _source, ref in self._governed(base)]
        kinds = {self._modes.mode_of(ref) for ref in refs}
        top = next((c for kind, c in LOOK_COLORS.items() if kind in kinds), None)
        leds: dict[str, int | None] = {LED_TOP: top}
        zones = [
            z for z in self._override_zones.get(base, []) if not self._until_off(z)
        ]
        if zones:
            held_on = any(self._modes.override_until(z) is not None for z in zones)
            leds[LED_BOTTOM] = COLOR_GREEN if held_on else None
        return leds

    def _sync_bars(self) -> None:
        """Bring every switch's bar to its room's status and curve level."""
        if self._closed or not self.push_enabled:
            return
        for base in self._all_switches():
            fraction = self._bar_fraction(self._bar_source(base))
            if fraction is not None:
                self._bars.sync(base, self._bar_leds(base), fraction)
            staged = self._staged_level(base)
            if staged is not None:
                self._bars.default_level(base, staged)

    def _staged_level(self, base: str) -> int | None:
        """Return the level a switch's lights wake at: its curve's last flood."""
        source = self._config[CONF_SOURCES][self._bar_source(base)]
        flood = self._last_published.get(source[CONF_CONSOLIDATED_TOPIC])
        level = flood.get("brightness") if flood else None
        return int(level) if level is not None else None

    def _lights_level(self, base: str) -> int | None:
        """Return the level a switch's bar light shows (or is being sent to)."""
        bar_ref = self._bar_lights[base][1]
        groups = self._dim_groups(base)
        topic = next((t for _s, t, refs in groups if bar_ref in refs), groups[0][1])
        payload: Mapping[str, Any] | None = self._shown(topic)
        consolidated = self._topic_source_consolidated.get(topic, topic)
        marked = max(
            self._marked_at.get(topic, float("-inf")),
            self._marked_at.get(consolidated, float("-inf")),
        )
        if self._planned_at.get(topic, float("-inf")) >= marked:
            payload = self._last_planned.get(topic, payload)
        level = payload.get("brightness") if payload else None
        return int(level) if level is not None else None

    def _follow_bar(self, base: str, data: dict[str, Any]) -> None:
        """Bring a switch's own level (its bar's height) to what its lights show.

        Runs only on the switch's own report that it is on at another height
        (after a paddle-on, a held dim, a config tap) — never on a timer: a level
        written to an off switch turns the switch on without its lights. A switch
        that still has a level binding is skipped: it would forward the level.
        """
        level = data.get(KEY_HEIGHT)
        if not isinstance(level, int) or data.get("state") != STATE_ON:
            return
        self._switches[base].height = level
        self._write_height(base, level)

    def _follow_partners(self, base: str) -> None:
        """Bring the bars of switches sharing a light with ``base`` to its lights.

        A switch bound to another (the primary bath pair, the hallway pair) does
        not report when its partner dims or holds a look, so its bar is set here,
        from its last report — only while that report has it on.
        """
        refs = {ref for _source, ref in self._governed(base)}
        for other in self._all_switches():
            if other == base or not refs & {r for _s, r in self._governed(other)}:
                continue
            level = self._switches[other].height
            if level is not None:
                self._write_height(other, level)

    def _write_height(self, base: str, level: int) -> None:
        """Set a switch's own level to its lights' when it is off by a step."""
        switch = self._switches[base]
        if (
            not switch.on
            or not self.push_enabled
            or switch.dims
            or self._level_bound is None
            or base in self._level_bound
            or self._clock() < switch.bar_quiet_until
        ):
            return
        target = self._lights_level(base)
        if target is None or abs(target - level) < HEIGHT_STEP:
            return
        switch.bar_quiet_until = self._clock() + BAR_HEIGHT_QUIET_S
        switch.height = target
        self._bars.height(base, target)

    def _bar_play(
        self,
        base: str,
        effect: str,
        color: int,
        *,
        level: int = FEEDBACK_LEVEL,
        duration: int = FEEDBACK_S,
    ) -> None:
        """Play a whole-bar animation on one switch, dimmed with its curve."""
        fraction = self._bar_fraction(self._bar_source(base))
        self._bars.play(
            base, effect, color, level, duration, 1.0 if fraction is None else fraction
        )

    # --- occupancy (mmwave presence -> directional sweep, MQTT-driven) ------

    async def _handle_occupancy(self, msg: mqtt.ReceiveMessage) -> None:
        """Run each binding's sweep on its occupied edge; clear the zone on all-off.

        A topic may carry several bindings (e.g. ``mmwave_area1_occupancy`` and
        ``mmwave_area2_occupancy`` on one switch); each reads its own field, tracks its
        own edge, and drives its own sweep independently. A binding's first report
        after a start is no edge: it is recorded and the zone settles once
        (:meth:`_settle_start`).
        """
        bindings = self._bindings_by_topic.get(msg.topic)
        if not bindings or not self.push_enabled:
            return
        data = _parse_json(str(msg.payload))
        if data is None:
            return
        for binding in bindings:
            value = data.get(binding.key)
            if value is None:
                continue  # this binding's field is absent from this message
            occupied = _truthy(value)
            bid = (binding.zone, binding.name)
            first = bid not in self._sensor_occupied
            if not first and occupied == self._sensor_occupied[bid]:
                continue  # no edge for this binding
            self._sensor_occupied[bid] = occupied
            if first:
                self._settle_start(binding.zone)
            if self._modes.override_until(binding.zone) is not None:
                # Held on by a tap: no sweep (the lights are on), no clear; any
                # presence restarts the idle clock.
                self.diagnostics["occupancy"][binding.zone] = not self._zone_vacant(
                    binding.zone
                )
                self._restart_idle_timer(binding.zone)
                continue
            if first:
                continue  # a baseline, not an edge
            if occupied:
                self._start_sweep(binding)
            else:
                await self._maybe_clear_zone(binding.zone)

    def _settle_start(self, zone_key: str) -> None:
        """Bring a zone in line once after a start, sending only what is needed.

        Waits until every sensor binding and sync switch of the zone has
        reported. The sync switch shows whether the zone's lights are on: a
        vacant zone whose switch shows ON is cleared (through the switch); an
        occupied zone whose switch shows OFF is swept on. Otherwise nothing is
        sent. A zone held by a tap override is left to its idle timer, and a
        zone without a sync switch to its next edge.
        """
        if zone_key not in self._start_pending:
            return
        bindings = self._bindings_by_zone[zone_key]
        if any((b.zone, b.name) not in self._sensor_occupied for b in bindings):
            return
        sync = self._occupancy[zone_key].get(CONF_SYNC_SWITCHES, [])
        if any(self._paddle(base) is None for base in sync):
            return
        self._start_pending.discard(zone_key)
        if self._modes.override_until(zone_key) is not None:
            return
        vacant = self._zone_vacant(zone_key)
        self.diagnostics["occupancy"][zone_key] = not vacant
        if not sync:
            return
        lit = any(self._paddle(base) for base in sync)
        if vacant and lit:
            _LOGGER.info(
                "zone %s vacant at start with its lights on: clearing", zone_key
            )
            self._clear_zone(zone_key)
        elif not vacant and not lit:
            _LOGGER.info(
                "zone %s occupied at start with its lights off: sweeping", zone_key
            )
            for binding in bindings:
                if self._sensor_occupied[(binding.zone, binding.name)]:
                    self._start_sweep(binding)

    def _start_sweep(self, binding: _Binding) -> None:
        """Launch a binding's sweep as a task, restarting any in-flight run."""
        task_key = (binding.zone, binding.name)
        running = self._sweep_tasks.get(task_key)
        if running is not None and not running.done():
            running.cancel()
        self.diagnostics["occupancy"][binding.zone] = True
        self._sweep_tasks[task_key] = self._entry.async_create_background_task(
            self.hass,
            self._run_sweep(binding.zone, binding.sweep),
            name=f"lm_sweep_{binding.zone}_{binding.name}",
        )

    async def _run_sweep(self, zone_key: str, sweep: list[OccupancyStage]) -> None:
        """Light a sensor's stages in order, pausing ``delay_s`` before each.

        A light already lit in this occupancy episode (by the other sensor's
        sweep) is skipped (#10a). After the last stage, the zone's sync switches
        are told ON so their state and LED bar follow the lights (F).
        """
        lit = self._zone_lit.setdefault(zone_key, set())
        fade = self._occupancy[zone_key].get(
            CONF_ON_TRANSITION, DEFAULT_OCCUPANCY_TRANSITION_S
        )
        # Curve ticks wait while the sweep runs (at most TICK_HOLD_MAX_S each).
        self._publisher.begin_sweep()
        try:
            for stage in sweep:
                delay = stage.get(CONF_STAGE_DELAY, 0.0)
                if delay:
                    await asyncio.sleep(delay)
                self._sweep_stage(zone_key, stage, lit, fade)
        finally:
            self._publisher.end_sweep()
        self._write_switches(
            self._occupancy[zone_key].get(CONF_SYNC_SWITCHES, []), STATE_ON
        )

    def _sweep_stage(
        self, zone_key: str, stage: OccupancyStage, lit: set[str], fade: float
    ) -> None:
        """Turn one stage on: its pair group if a group slot is free, else lamps.

        Lamp sends are unicasts (no broadcast-table slot), so a stage never
        waits for the group budget; they go back to back to keep the pair close.
        """
        lights = stage.get(CONF_LIGHTS, [])
        group_body = self._stage_group_body(stage, lit, fade)
        if group_body is not None and self._publisher.group_slot_free():
            # One groupcast: the stage's lamps start their fade together.
            lit.update(light[CONF_SET_TOPIC] for light in lights)
            self._enqueue(
                [Send(stage[CONF_STAGE_GROUP], group_body)], priority=PRIORITY_SWEEP
            )
            return
        sends: list[Send] = []
        for light in lights:
            if light[CONF_SET_TOPIC] in lit:
                continue
            payload = self._sweep_payload(light)
            if payload is None:
                continue
            level = stage.get(CONF_STAGE_BRIGHTNESS)
            room = self._topic_room.get(light[CONF_SET_TOPIC], "")
            if level is not None and self._modes.mode_of(room) not in LOOK_KINDS:
                payload = _dimmed(payload, level)
                self._zone_dims.setdefault(zone_key, {})[room] = level
            body = {**payload, "transition": fade, "state": STATE_ON}
            self._cancel_off_stage(room)  # a pending curve stage must not land on it
            sends.append(Send(light[CONF_SET_TOPIC], body))
            lit.add(light[CONF_SET_TOPIC])
        if sends:
            self._enqueue(sends, priority=PRIORITY_SWEEP, together=True)

    def _stage_group_body(
        self, stage: OccupancyStage, lit: set[str], fade: float
    ) -> dict[str, Any] | None:
        """Return the stage's groupcast body, or None to send lamp by lamp.

        The group carries only ON + fade: each lamp's brightness and colour are
        already staged by its source's flood (``hue_native_control`` stages
        while off). So it is used only when no stage lamp is lit yet and none
        is held at a look (a look is not staged).
        """
        group = stage.get(CONF_STAGE_GROUP)
        lights = stage.get(CONF_LIGHTS, [])
        if not group or not lights or CONF_STAGE_BRIGHTNESS in stage:
            return None  # a fixed level is not staged: those lights go one by one
        for light in lights:
            topic = light[CONF_SET_TOPIC]
            room = self._topic_room.get(topic, "")
            if topic in lit or self._modes.mode_of(room) in LOOK_KINDS:
                return None
        return {"transition": fade, "state": STATE_ON}

    def _sweep_payload(self, light: OccupancyLight) -> PushPayload | None:
        """Return what a sweep shows on a light: its held look, else the curve."""
        source_key = light[CONF_SOURCE]
        room = self._topic_room.get(light[CONF_SET_TOPIC], "")
        if self._modes.mode_of(room) in LOOK_KINDS:
            source = self._config[CONF_SOURCES][source_key]
            return self._held_looks(source_key, source).get(room)
        return self._room_glide_target(source_key)

    def _look_rooms(self) -> set[str]:
        """Rooms held at a config-tap look (night / day)."""
        return {
            room
            for room in self._modes.held_rooms()
            if self._modes.mode_of(room) in LOOK_KINDS
        }

    def _zone_held(self, zone_key: str) -> bool:
        """Return True while a config-tap look holds any of the zone's lights.

        A look also keeps its zone on (user 2026-11-05): the all-clear does not
        turn it off until a tap off, a release, or 00:00 ends the look.
        """
        return not self._look_rooms().isdisjoint(self._zone_rooms.get(zone_key, ()))

    def _resume_motion(self, rooms: Iterable[str]) -> None:
        """Clear each vacant zone that an ended look on ``rooms`` was keeping on."""
        ended = set(rooms)
        for zone_key, zone_rooms in self._zone_rooms.items():
            if (
                not ended.isdisjoint(zone_rooms)
                and self._zone_vacant(zone_key)
                and self._modes.override_until(zone_key) is None
            ):
                self.diagnostics["occupancy"][zone_key] = False
                self._clear_zone(zone_key)

    def _zone_vacant(self, zone_key: str) -> bool:
        return not any(
            self._sensor_occupied.get((b.zone, b.name))
            for b in self._bindings_by_zone.get(zone_key, [])
        )

    async def _maybe_clear_zone(self, zone_key: str) -> None:
        """Turn the zone's off_lights off once all its bindings report no presence."""
        if not self._zone_vacant(zone_key):
            return
        self.diagnostics["occupancy"][zone_key] = False
        self._clear_zone(zone_key)

    def _clear_zone(self, zone_key: str) -> None:
        """Turn a vacant zone off: through its sync switch, else its off topics.

        A sync switch that shows ON is told OFF and its own binding switches the
        lights (one unicast + the binding's groupcast, and the bar follows). A
        switch that does not show ON may not forward an OFF, so the group gets
        Light Man's OFF as before. A zone held on by a config-tap look is left
        on; the next occupancy episode's sweep shows the look again.
        """
        zone = self._occupancy[zone_key]
        self._zone_lit.pop(zone_key, None)
        dimmed = self._zone_dims.pop(zone_key, {})
        if self._zone_held(zone_key):
            return
        for room in dimmed:
            self._schedule_off_stage(room)  # stage the curve, not the night level
        sync = zone.get(CONF_SYNC_SWITCHES, [])
        if sync and all(self._paddle(base) for base in sync):
            self._write_switches(sync, STATE_OFF)
            return
        transition = zone.get(CONF_OFF_TRANSITION, DEFAULT_OCCUPANCY_TRANSITION_S)
        off = {"state": STATE_OFF, "transition": transition}
        sends = [Send(topic, off) for topic in zone.get(CONF_OFF_LIGHTS, [])]
        if sends:
            self._enqueue(sends, priority=PRIORITY_INTERACTIVE)
        self._write_switches(sync, STATE_OFF)  # bars follow (lights already off)

    def _paddle(self, base: str) -> bool | None:
        """Return a switch's paddle state (None: unknown, or not a switch here)."""
        switch = self._switches.get(base)
        return None if switch is None else switch.on

    def _write_switches(self, bases: Iterable[str], state: str) -> None:
        """Set switches' on/off state (unicasts); their echo is not a paddle press.

        A switch already showing ``state`` is skipped.
        """
        want = state == STATE_ON
        sends = [
            Send(f"{base}/set", {"state": state})
            for base in bases
            if self._paddle(base) != want
        ]
        if not sends:
            return
        now = self._clock()
        for send in sends:
            switch = self._switches.get(send.topic.removesuffix("/set"))
            if switch is not None:
                switch.echo = (state, now)
        self._enqueue(sends, priority=PRIORITY_INTERACTIVE)

    # --- zone override (a tap holds the zone on; motion ignored) ------------

    async def _arm_override(self, zone_key: str) -> None:
        now = dt_util.utcnow()
        expires = (
            now + timedelta(days=OVERRIDE_UNTIL_OFF_DAYS)
            if self._until_off(zone_key)
            else self._next_midnight(now)
        )
        await self._modes.set_override(zone_key, expires_at=expires)
        _LOGGER.info("override armed for zone %s", zone_key)
        self._restart_idle_timer(zone_key)

    async def _end_override(self, zone_key: str) -> None:
        self._cancel_idle_timer(zone_key)
        if await self._modes.end_override(zone_key):
            _LOGGER.info("override ended for zone %s", zone_key)

    def _cancel_idle_timer(self, zone_key: str) -> None:
        cancel = self._override_timers.pop(zone_key, None)
        if cancel is not None:
            cancel()

    def _restart_idle_timer(self, zone_key: str) -> None:
        """End a vacant zone's override ``override_idle_s`` after its last presence."""
        self._cancel_idle_timer(zone_key)
        if not self._zone_vacant(zone_key) or self._until_off(zone_key):
            return
        zone = self._occupancy[zone_key]
        idle = zone.get(CONF_OVERRIDE_IDLE, DEFAULT_OVERRIDE_IDLE_S)
        self._override_timers[zone_key] = async_call_later(
            self.hass, idle, partial(self._override_idle, zone_key)
        )

    async def _override_idle(self, zone_key: str, _now: datetime) -> None:
        """No presence for the idle time: end the override and clear the zone."""
        self._override_timers.pop(zone_key, None)
        if self._closed or not await self._modes.end_override(zone_key):
            return
        _LOGGER.info("override ended for zone %s (no presence)", zone_key)
        if self._zone_vacant(zone_key):
            self.diagnostics["occupancy"][zone_key] = False
            self._clear_zone(zone_key)
        self._publish_snapshot()

    # --- modes --------------------------------------------------------------

    def _next_midnight(self, now: datetime) -> datetime:
        """Next local 00:00 (hold and override TTL), correct across DST."""
        tomorrow = dt_util.as_local(now).date() + timedelta(days=1)
        return dt_util.start_of_local_day(tomorrow)

    def _invalidate_source(self, source_key: str) -> None:
        """Forget what a source's topics last showed (next due tick re-sends)."""
        source = self._config[CONF_SOURCES][source_key]  # keys come from the config
        topics = {source[CONF_CONSOLIDATED_TOPIC]} | {
            room[CONF_SET_TOPIC]
            for room in source.get(CONF_ROOMS, {}).values()
            if room.get(CONF_SET_TOPIC)
        }
        for topic in topics:
            self._last_published.pop(topic, None)

    def _invalidate_room(self, room: str) -> None:
        """Forget the source that owns ``room`` (its addressing changed)."""
        source_key = self._room_source.get(room)
        if source_key is not None:
            self._invalidate_source(source_key)

    def _room_topic(self, room: str) -> str | None:
        source_key = self._room_source.get(room)
        if source_key is None:
            return None
        room_cfg = self._config[CONF_SOURCES][source_key][CONF_ROOMS][room]
        return room_cfg.get(CONF_SET_TOPIC) or None

    async def async_hold(
        self, room: str, kind: str, *, origin_at: float | None = None
    ) -> None:
        """Hold a room at a look and show it now — one interactive send (#3)."""
        source_key = self._room_source.get(room, "")
        await self._hold_lights([(source_key, room)], kind, origin_at=origin_at)

    async def async_release_hold(
        self, room: str, *, origin_at: float | None = None
    ) -> None:
        """Resume adaptive for a room: one send to the curve at the glide end (#4).

        ``room`` may also be a zone: that ends its override (motion resumes).
        """
        if room in self._occupancy and room not in self._room_source:
            await self._end_override(room)
            if self._zone_vacant(room):
                self._clear_zone(room)
            self._publish_snapshot()
            return
        was_look = room in self._look_rooms()
        if not await self._modes.release(room):
            return
        self._release_room(room, origin_at=origin_at)
        if was_look:
            self._resume_motion([room])
        self._publish_snapshot()

    def _release_room(self, room: str, *, origin_at: float | None = None) -> None:
        source_key = self._room_source.get(room)
        topic = self._room_topic(room)
        if source_key is None or topic is None or not self.push_enabled:
            return
        payload = self._room_glide_target(source_key)
        if payload is not None:
            self._interactive_send(topic, payload, origin_at=origin_at)

    def _schedule_off_stage(self, light_ref: str) -> None:
        """Stage a released light's curve value once its bulbs are off.

        A switch-off sends nothing: the switch's binding turns the bulbs off.
        Only a light that was held (a look or a held dim) still carries that
        value; ``OFF_STAGE_DELAY_S`` after the off, one state-less send stages
        the curve so the next turn-on comes up right. Sent earlier, it would
        land on bulbs that are still on.
        """
        self._cancel_off_stage(light_ref)
        self._off_stages[light_ref] = async_call_later(
            self.hass, OFF_STAGE_DELAY_S, partial(self._off_stage_due, light_ref)
        )

    def _cancel_off_stage(self, light_ref: str) -> bool:
        """Drop a pending off-stage; True if one was pending."""
        cancel = self._off_stages.pop(light_ref, None)
        if cancel is None:
            return False
        cancel()
        return True

    @callback
    def _off_stage_due(self, light_ref: str, _now: datetime) -> None:
        self._off_stages.pop(light_ref, None)
        if not self._closed:
            self._release_room(light_ref)

    async def async_clear_holds(self) -> None:
        """Resume adaptive everywhere: one release send per held room (#5)."""
        looks = self._look_rooms()
        for room in await self._modes.clear():
            self._release_room(room)
        self._resume_motion(looks)
        for zone in self._modes.overrides():
            await self._end_override(zone)
        self._publish_snapshot()

    async def async_force_push(self) -> None:
        """Re-publish every source now, bypassing dedup (operator action)."""
        await self._run_push(force=True)

    # --- master on/off switch ----------------------------------------------

    async def async_set_push_enabled(self, *, enabled: bool) -> None:
        """Master on/off. ON runs the adaptive push; OFF makes Light Man inert."""
        self.push_enabled = enabled
        if enabled:
            await self._run_push(force=True)
        self._publish_snapshot()

    # --- MQTT message handling ---------------------------------------------

    async def _handle_message(self, msg: mqtt.ReceiveMessage) -> None:
        """Route an incoming switch message to action OR state handling.

        An action-bearing message is handled as the action **only**: Z2M bundles
        the device's *cached* attributes with the action, so its ``state`` field
        is the pre-tap value (observed live: ``{"action": "up_single",
        "state": "OFF"}`` right after a turn-off — reading that ``OFF`` as a
        fresh paddle-off staged the room OFF and killed the light the user just
        turned on). Paddle state is tracked solely from action-less attribute
        reports, which carry the switch's real reported state.
        """
        data = _parse_json(str(msg.payload))
        if data is None:
            return
        level = data.get(KEY_LEVEL)
        if isinstance(level, int):
            self._bars.seed_level(msg.topic, level)
        default = data.get(KEY_DEFAULT_LOCAL)
        if isinstance(default, int):
            self._bars.seed_default(msg.topic, default)
        action = data.get("action")
        if isinstance(action, str) and action:
            await self._on_action(msg.topic, action)
            return
        state = data.get("state")
        if isinstance(state, str):
            await self._on_state(msg.topic, state)
        self._follow_bar(msg.topic, data)

    async def _on_action(self, base: str, action: str) -> None:
        """Apply a tap: a config hold, a held-paddle dim, or an on/off release.

        Every switch handles taps the same way, on the lights it governs. On a
        Smart-Bulb-Mode switch a single tap is also the on/off intent (up = on,
        down = off). Paddle-on publishes nothing: the bulbs wake at the value
        staged by the last flood (verified on every model 2026-11-03). A switch
        that also holds a zone on (the hallway pair) arms that override with
        its up tap and ends it with its down tap.
        """
        if not self.push_enabled:
            return  # inert when Light Man is off — the blueprint owns taps
        self._play_tap_animation(base, action)
        switch = self._switches.get(base)
        if switch is None:
            return
        if action in RELEASE_ACTIONS:
            await self._end_dim(base)
            return
        mode = action_to_mode(action)
        if mode is None:
            return
        origin_at = self._clock()
        if mode == MODE_ADAPTIVE:
            await self._on_paddle_tap(base, switch, action, origin_at)
        elif mode == HELD_MANUAL:
            await self._start_dim(base, action)
        elif self._look_tap_accepted(base, action):
            self._bars.announce(base, LED_TOP)  # the top LED pulses, then stays
            await self._hold_lights(
                switch.lights,
                mode,
                origin_at=origin_at,
                whole=switch.room is not None,
            )
            self._bars.unannounce(base)
            self._follow_partners(base)

    async def _on_paddle_tap(
        self, base: str, switch: _Switch, action: str, origin_at: float
    ) -> None:
        """Handle a single up/down tap: track the paddle and release any hold.

        A release is answered by a bar pulse (back to automatic); a newly
        armed zone override by the bottom LED instead.
        """
        off = action.startswith("down")
        self._drop_dim(base)
        self._set_paddle(base, on=not off)
        switch.ignore_until = origin_at + PADDLE_STATE_GRACE_S
        released = [
            ref for _src, ref in switch.lights if await self._modes.release(ref)
        ]
        zones = self._override_zones.get(base, [])
        feedback: int | None = None
        if off:
            await self._paddle_off(switch, zones, released)
        else:
            feedback = await self._paddle_up(base, switch, zones, released, origin_at)
        _LOGGER.debug("%s on %s", action, base)
        self._publish_snapshot()
        self._bars.unannounce(base)
        if feedback is not None:
            self._bar_play(base, EFFECT_PULSE, feedback)

    async def _paddle_off(
        self, switch: _Switch, zones: list[str], released: list[str]
    ) -> None:
        """End a switch-off's overrides; stage released holds' curves.

        The switch's binding turns the bulbs off. A light that was held still
        carries its look; it gets its curve staged once the bulbs are off.
        """
        for zone in zones:
            await self._end_override(zone)
            self._zone_lit.pop(zone, None)
            for room in self._zone_dims.pop(zone, {}):
                self._schedule_off_stage(room)  # off at the night level: stage curve
        for _source, ref in switch.lights:
            self._forget_lit(ref)
        for source_key, topic, _refs in self._light_topics(
            switch.lights, whole=switch.room is not None
        ):
            if topic == self._config[CONF_SOURCES][source_key][CONF_CONSOLIDATED_TOPIC]:
                self._invalidate_source(source_key)
        for ref in released:
            self._schedule_off_stage(ref)

    async def _paddle_up(
        self,
        base: str,
        switch: _Switch,
        zones: list[str],
        released: list[str],
        origin_at: float,
    ) -> int | None:
        """Show released lights their curve, arm overrides; return the bar colour.

        A newly armed override is answered on the bottom LED of the tapped
        switch (it pulses, then stays); only a bare release pulses the bar.
        """
        pending = [ref for _src, ref in switch.lights if self._cancel_off_stage(ref)]
        shown = [
            (src, ref)
            for src, ref in switch.lights
            if ref in released or ref in pending
        ]
        feedback: int | None = None
        if shown:
            self._show_lights(
                shown,
                self._room_glide_target,
                origin_at=origin_at,
                whole=switch.room is not None,
            )
            feedback = COLOR_BAR  # back to automatic
        for zone in zones:
            self._release_dims(zone)
            if self._modes.override_until(zone) is None and not self._until_off(zone):
                self._bars.announce(base, LED_BOTTOM)
                feedback = None
            await self._arm_override(zone)
        return feedback

    def _release_dims(self, zone_key: str) -> None:
        """Bring a zone's lit night lights to their curve (a paddle-on).

        The binding's ON does not change a bulb that is already on, so each
        dimmed light gets its curve value, faded like the bulbs' own wake.
        """
        for room in self._zone_dims.pop(zone_key, {}):
            self._cancel_off_stage(room)
            source_key = self._room_source.get(room)
            topic = self._room_topic(room)
            payload = (
                self._room_glide_target(source_key) if source_key is not None else None
            )
            if topic is not None and payload is not None:
                self._interactive_send(
                    topic, payload, transition=ZONE_DIM_RELEASE_TRANSITION_S
                )

    def _play_tap_animation(self, base: str, action: str) -> None:
        """Play the bar animation configured for a tap (the shade taps)."""
        animation = self._animations.get(base, {}).get(action)
        if animation is not None:
            self._bar_play(
                base,
                animation["effect"],
                animation["color"],
                level=animation["level"],
                duration=animation["duration"],
            )

    def _look_tap_accepted(self, base: str, action: str) -> bool:
        """Apply the config-tap rule; an ignored tap blinks the top LED red."""
        if self._on_long_enough(base, action):
            return True
        fraction = self._bar_fraction(self._bar_source(base))
        self._bars.flash(
            base,
            LED_TOP,
            EFFECT_BLINK,
            COLOR_RED,
            1.0 if fraction is None else fraction,
        )
        return False

    def _set_paddle(self, base: str, *, on: bool) -> None:
        """Record a switch's on/off and when it turned on (config-tap rule)."""
        switch = self._switches[base]
        was = switch.on
        switch.on = on
        if not on:
            switch.on_since = None
        elif not was:
            switch.on_since = self._clock()

    def _on_long_enough(self, base: str, action: str) -> bool:
        """Return True once the switch has been on for 0.5 s (config-tap rule).

        Family members hit config by accident while pressing the paddle on; an
        unknown switch state counts as off. A config tap never turns lights on.
        """
        since = self._switches[base].on_since
        if since is not None and self._clock() - since >= CONFIG_TAP_MIN_ON_S:
            return True
        self.diagnostics["ignored_config_taps"] += 1
        _LOGGER.debug("%s on %s ignored: the light is not on", action, base)
        return False

    def _forget_lit(self, light_ref: str) -> None:
        """Let this episode's sweep light again a light switched off by hand."""
        topic = self._room_topic(light_ref)
        for lit in self._zone_lit.values():
            lit.discard(topic or "")

    async def _hold_lights(
        self,
        lights: list[tuple[str, str]],
        kind: str,
        *,
        origin_at: float | None,
        whole: bool = False,
    ) -> None:
        """Hold lights at a look; show it with one send per light (or source)."""
        now = dt_util.utcnow()
        for _source, ref in lights:
            await self._modes.set_held(
                ref, kind=kind, armed_at=now, expires_at=self._next_midnight(now)
            )

        def look(source_key: str) -> PushPayload | None:
            profile = self._config[CONF_SOURCES][source_key]["profile"]
            target = night_look(profile) if kind == HELD_NIGHT else day_look(profile)
            return _strip(_target_to_payload(target, 0.0))

        if self.push_enabled:
            self._show_lights(lights, look, origin_at=origin_at, whole=whole)
        self._publish_snapshot()

    def _light_topics(
        self, lights: list[tuple[str, str]], *, whole: bool
    ) -> list[tuple[str, str, list[str]]]:
        """Return (source, topic, light refs) for each send that reaches ``lights``.

        With ``whole`` (a switch governing a whole room), a source whose every
        light is in ``lights`` is reached on its consolidated topic; any other
        light on its own topic.
        """
        by_source: dict[str, list[str]] = {}
        for source_key, ref in lights:
            by_source.setdefault(source_key, []).append(ref)
        topics: list[tuple[str, str, list[str]]] = []
        for source_key, refs in by_source.items():
            source = self._config[CONF_SOURCES].get(source_key)
            if source is None:
                continue  # not a light here (a hold on an unknown ref)
            rooms = source.get(CONF_ROOMS, {})
            if whole and set(rooms) <= set(refs):
                topics.append((source_key, source[CONF_CONSOLIDATED_TOPIC], refs))
                continue
            topics.extend(
                (source_key, rooms[ref][CONF_SET_TOPIC], [ref])
                for ref in refs
                if ref in rooms and rooms[ref].get(CONF_SET_TOPIC)
            )
        return topics

    def _show_lights(
        self,
        lights: list[tuple[str, str]],
        payload_of: Callable[[str], PushPayload | None],
        *,
        origin_at: float | None,
        whole: bool,
    ) -> None:
        """Send each source's payload to its lights (see :meth:`_light_topics`)."""
        payloads: dict[str, PushPayload | None] = {}
        for source_key, topic, _refs in self._light_topics(lights, whole=whole):
            if source_key not in payloads:
                payloads[source_key] = payload_of(source_key)
            payload = payloads[source_key]
            if payload is not None:
                self._interactive_send(topic, payload, origin_at=origin_at)

    async def _on_state(self, base: str, state: str) -> None:
        """Track the paddle; off **releases any hold** and sends nothing then.

        The switch's binding turns the bulbs off. A light that was held (a look
        or a held dim) gets its curve value staged once the bulbs are off
        (:meth:`_schedule_off_stage`); otherwise the regular sends keep it staged.
        """
        switch = self._switches.get(base)
        if switch is None:
            return
        on = state == STATE_ON
        previous = switch.on
        if previous == on:
            return  # unchanged, or the matching action already handled it
        now = self._clock()
        if (
            switch.echo is not None
            and switch.echo[0] == state
            and now - switch.echo[1] < SWITCH_ECHO_S
        ):
            # Our own sync write coming back: record it, it is not a press.
            switch.echo = None
            self._set_paddle(base, on=on)
            return
        if now < switch.ignore_until:
            # Not a press: either Z2M republishing the switch's *cached* state
            # after a tap, before the switch reports its new one (~1.5 s; live
            # 2026-11-04 03:40 a bath light turned on, then staged off 0.37 s
            # later), or the switch turning itself on under a paddle held while
            # it was off. A real paddle change arrives as an action.
            return
        self._set_paddle(base, on=on)
        if previous is None:
            for zone_key in self._sync_zones.get(base, ()):
                self._settle_start(zone_key)
        if switch.room is not None or not self.push_enabled:
            return  # a whole-room switch: its binding acts, the state is tracked
        self._drop_dim(base)
        for source_key, light_ref in switch.lights:
            await self._follow_paddle(
                source_key, light_ref, on=on, boot=previous is None
            )
        self._publish_snapshot()

    async def _follow_paddle(
        self, source_key: str, light_ref: str, *, on: bool, boot: bool
    ) -> None:
        """Apply a switch's reported on/off to one light it governs."""
        if boot:
            # The first report after startup (retained): record it only. The
            # boot flood already staged every bulb, so re-staging each switched
            # off room would be a burst of sends for nothing.
            if not on and await self._modes.release(light_ref):
                _LOGGER.info("hold released by switch-off for %s", light_ref)
            return
        if on:
            pending = self._cancel_off_stage(light_ref)
            if await self._modes.release(light_ref) or pending:
                self._release_room(light_ref)
            return
        self._forget_lit(light_ref)
        if await self._modes.release(light_ref):
            _LOGGER.info("hold released by switch-off for %s", light_ref)
            self._schedule_off_stage(light_ref)

    # --- held-paddle dimming: Light Man fades, so it knows the level ---------

    def _dim_groups(self, base: str) -> list[tuple[str, str, list[str]]]:
        """Return (source, topic, light refs) for each fade a switch's hold runs.

        One per curve: the consolidated topic when a room switch covers every
        light of the source, else each light's own topic.
        """
        switch = self._switches[base]
        return self._light_topics(switch.lights, whole=switch.room is not None)

    async def _start_dim(self, base: str, action: str) -> None:
        """Fade a held paddle's lights toward the floor or the ceiling.

        The switches have no level binding, so this fade is the dim. It needs
        the switch on (a held paddle never turns lights on) and holds the lights
        as ``manual``; the level is set when the paddle is released.
        """
        switch = self._switches[base]
        if switch.on is False:
            switch.held_off = True
            self._turn_back_off(base)
            # The switch turns itself on under the held paddle: not a press.
            switch.ignore_until = self._clock() + DIM_TIMEOUT_S
            return
        if not switch.on:
            return
        self._cancel_dim_start(base)
        switch.dim_start = self.hass.async_create_task(
            self._dim_after_delay(base, action), "lm_dim_start", eager_start=True
        )

    async def _dim_after_delay(self, base: str, action: str) -> None:
        """Start the dim once the paddle has stayed held past the delay."""
        await _dim_sleep(DIM_START_DELAY_S)
        self._switches[base].dim_start = None
        await self._begin_dim(base, action)

    async def _begin_dim(self, base: str, action: str) -> None:
        """Send the fades of a held paddle and hold its lights as ``manual``."""
        ended = await self._end_dim(base)  # a release that never arrived
        target = DIM_FLOOR if action.startswith("down") else BRIGHTNESS_MAX
        clock = self._clock()
        now = dt_util.utcnow()
        dims: list[_Dim] = []
        for source_key, topic, refs in self._dim_groups(base):
            shown = (
                ended.get(topic)
                or self._shown(topic)
                or self._room_glide_target(source_key)
            )
            if shown is None or "brightness" not in shown:
                continue
            start = int(shown["brightness"])
            span = abs(start - target)
            if span < DIM_MIN_SPAN:
                continue
            duration = max(DIM_MIN_FADE_S, span / DIM_RATE)
            dims.append(
                _Dim(topic, refs, _strip(shown), start, target, duration, clock)
            )
            payload = cast("PushPayload", {**_strip(shown), "brightness": target})
            self._interactive_send(
                topic, payload, transition=round(duration, 2), origin_at=clock
            )
        for dim in dims:
            for ref in dim.refs:
                await self._modes.set_held(
                    ref,
                    kind=HELD_MANUAL,
                    armed_at=now,
                    expires_at=self._next_midnight(now),
                )
        self._switches[base].dims = dims
        self._publish_snapshot()

    async def _end_dim(self, base: str) -> dict[str, PushPayload]:
        """Stop each fade where it is (paddle released) and hold that level.

        Returns what each stopped topic now shows.
        """
        stopped: dict[str, PushPayload] = {}
        switch = self._switches[base]
        if switch.held_off:
            switch.held_off = False
            self._turn_back_off(base)
            return stopped
        self._cancel_dim_start(base)  # released before the dim began: nothing
        dims, switch.dims = switch.dims, []
        if not dims:
            return stopped
        clock = self._clock()
        for dim in dims:
            held = [r for r in dim.refs if self._modes.mode_of(r) == HELD_MANUAL]
            if not held:
                continue  # a tap released the lights meanwhile
            level = dim.level_at(clock + DIM_LEAD_S)
            look = cast("PushPayload", {**dim.colour, "brightness": level})
            stopped[dim.topic] = look
            self._interactive_send(
                dim.topic, look, transition=DIM_STOP_TRANSITION_S, origin_at=clock
            )
            for ref in held:
                await self._modes.set_look(ref, dict(look))
        self._publish_snapshot()
        self._follow_partners(base)
        return stopped

    def _cancel_dim_start(self, base: str) -> None:
        """Forget a hold that has not started dimming yet."""
        switch = self._switches[base]
        task, switch.dim_start = switch.dim_start, None
        if task is not None:
            task.cancel()

    def _drop_dim(self, base: str) -> None:
        """Forget a switch's hold (a tap or a paddle change ended it)."""
        self._cancel_dim_start(base)
        switch = self._switches[base]
        switch.dims = []
        switch.held_off = False

    def _turn_back_off(self, base: str) -> None:
        """Turn off a switch whose paddle was held while it was off.

        A hold on an off switch turns the switch itself on and ramps its bar,
        while its lights stay off (no level binding; seen 2026-11-06, primary
        bath). Sent on the hold and again on the release; the switch's state
        reports in between are not paddle presses.
        """
        now = self._clock()
        switch = self._switches[base]
        switch.ignore_until = now + PADDLE_STATE_GRACE_S
        switch.echo = (STATE_OFF, now)
        self._enqueue(
            [Send(f"{base}/set", {"state": STATE_OFF})], priority=PRIORITY_INTERACTIVE
        )

    def _dim_pending(self, room: str) -> bool:
        """Return True while a held paddle is fading ``room``."""
        clock = self._clock()
        return any(
            room in dim.refs and clock - dim.began < DIM_TIMEOUT_S
            for switch in self._switches.values()
            for dim in switch.dims
        )

    async def _finish_stale_dims(self) -> None:
        """End fades whose release never arrived (they have reached their end)."""
        clock = self._clock()
        for base, switch in self._switches.items():
            if any(clock - dim.began >= DIM_TIMEOUT_S for dim in switch.dims):
                await self._end_dim(base)

    # --- Z2M feedback (A2) --------------------------------------------------

    async def _handle_bridge_logging(self, msg: mqtt.ReceiveMessage) -> None:
        """Count Z2M publish failures; re-send what really failed (A2)."""
        data = _parse_json(str(msg.payload))
        if data is None or data.get("level") != "error":
            return
        if data.get("namespace") not in (None, "z2m"):
            return
        failure = parse_publish_failure(str(data.get("message", "")))
        if failure is None:
            return
        topic = set_topic_for(self._z2m_base, failure.name)
        stats = self.diagnostics.setdefault(
            "delivery_failures", {"total": 0, "by_kind": {}, "by_topic": {}}
        )
        stats["total"] += 1
        stats["by_kind"][failure.kind] = stats["by_kind"].get(failure.kind, 0) + 1
        stats["by_topic"][topic] = stats["by_topic"].get(topic, 0) + 1
        stats["last"] = {"topic": topic, "kind": failure.kind, "error": failure.error}
        if failure.attr in BAR_KEYS:
            # A bar command: forget what that bar shows; the next sync re-sends it.
            self._bars.forget(topic.removesuffix("/set"))
            return
        if failure.kind not in RESEND_KINDS:
            return
        self._failed_at[topic] = self._clock()
        self._last_published.pop(topic, None)
        self._warn_failure(topic, failure.kind)
        await self._resend(topic)

    async def _resend(self, topic: str) -> None:
        """Re-send a topic Z2M could not deliver.

        - A source's consolidated topic: re-plan the whole source (flood plus
          held-room re-asserts) as one unit — never the flood alone.
        - Any other topic: only if Light Man itself published it in the last
          ``INTERACTIVE_RETRY_WINDOW_S`` and only once per plan, unchanged.
        """
        source_key = self._consolidated_source.get(topic)
        if source_key is not None:
            now = self._clock()
            last = self._replanned_at.get(source_key)
            if last is not None and now - last < self._interval:
                return  # mark cleared; the next due tick re-sends the source
            self._replanned_at[source_key] = now
            self._enqueue_source(source_key, force=True)
            return
        payload = self._last_planned.get(topic)
        planned_at = self._planned_at.get(topic)
        if payload is None or planned_at is None:
            return
        if self._clock() - planned_at > INTERACTIVE_RETRY_WINDOW_S:
            return
        recent = self._interactive_recent.get(topic, (planned_at, 0))
        if recent[1] >= 1:
            return  # already retried once
        self._interactive_recent[topic] = (recent[0], recent[1] + 1)
        await _pace_sleep(INTERACTIVE_RETRY_DELAY_S)
        if self._closed:
            return
        future = self._publisher.enqueue(
            [Send(topic, dict(payload))], priority=PRIORITY_INTERACTIVE
        )
        future.add_done_callback(self._on_sent)

    def _warn_failure(self, topic: str, kind: str) -> None:
        now = self._clock()
        last, suppressed = self._failure_log.get(topic, (None, 0))
        if last is not None and now - last < FAILURE_LOG_INTERVAL_S:
            self._failure_log[topic] = (last, suppressed + 1)
            return
        _LOGGER.warning(
            "Z2M could not deliver to %s (%s)%s",
            topic,
            kind,
            f"; {suppressed} more since last report" if suppressed else "",
        )
        self._failure_log[topic] = (now, 0)

    async def _handle_bridge_state(self, msg: mqtt.ReceiveMessage) -> None:
        """Track Z2M online/offline; replay what was sent during an outage."""
        data = _parse_json(str(msg.payload))
        state = data.get("state") if data is not None else str(msg.payload)
        online = state == "online"
        previous = self._z2m_online
        self._z2m_online = online
        if previous is None:
            return  # the retained value at startup — nothing to recover
        if previous and not online:
            self._begin_outage()
        elif not previous and online:
            self.diagnostics["z2m_restarts"] = (
                self.diagnostics.get("z2m_restarts", 0) + 1
            )
            self._end_outage()

    @callback
    def _on_mqtt_status(self, connected: bool) -> None:  # HA callback signature
        """HA's own MQTT connection: an outage window like a Z2M restart."""
        if not connected:
            self._begin_outage()
        elif self._outage:
            self.diagnostics["mqtt_reconnects"] = (
                self.diagnostics.get("mqtt_reconnects", 0) + 1
            )
            self._end_outage()

    def _begin_outage(self) -> None:
        self._outage = True
        self._outage_topics.clear()

    def _end_outage(self) -> None:
        """Re-plan every source whose topics were published while Z2M/MQTT was down.

        Replay is per source (flood plus re-asserts in one unit), never per topic,
        so a held room is never overwritten by a replayed flood (review blocker 3).
        """
        self._outage = False
        topics, self._outage_topics = self._outage_topics, set()
        sources = {
            self._topic_owner[topic] for topic in topics if topic in self._topic_owner
        }
        for key in sorted(sources):
            self._invalidate_source(key)
            self._enqueue_source(key, force=True)

    async def _handle_bridge_groups(self, msg: mqtt.ReceiveMessage) -> None:
        """Learn which topics are groups (broadcasts)."""
        try:
            groups = json.loads(str(msg.payload))
        except json.JSONDecodeError, ValueError:
            return
        if not isinstance(groups, list):
            return
        self._group_topics = {
            set_topic_for(self._z2m_base, group["friendly_name"])
            for group in groups
            if isinstance(group, dict) and "friendly_name" in group
        }

    async def _handle_bridge_devices(self, msg: mqtt.ReceiveMessage) -> None:
        """Learn which switches still forward their level (a level binding)."""
        try:
            devices = json.loads(str(msg.payload))
        except json.JSONDecodeError, ValueError:
            return
        if not isinstance(devices, list):
            return
        devices = [d for d in devices if isinstance(d, dict)]
        coordinators = {
            d.get("ieee_address") for d in devices if d.get("type") == "Coordinator"
        }
        bound: set[str] = set()
        for device in devices:
            for endpoint in (device.get("endpoints") or {}).values():
                for binding in endpoint.get("bindings", []):
                    target = binding.get("target", {})
                    if (
                        binding.get("cluster") == "genLevelCtrl"
                        and target.get("ieee_address") not in coordinators
                    ):
                        name = device.get("friendly_name", "")
                        bound.add(f"{self._z2m_base}/{name}")
        self._level_bound = bound

    def _is_group(self, topic: str) -> bool:
        """Return True for group (broadcast) topics; all of them until groups load."""
        if self._group_topics is None:
            return True
        return topic in self._group_topics

    # --- snapshot -----------------------------------------------------------

    def _snapshot(self) -> CoordinatorData:
        """Build the entity-facing snapshot from current mode state."""
        held = self._modes.as_attributes()
        return CoordinatorData(
            held=held, held_count=len(held), push_enabled=self.push_enabled
        )


def _interp_at(times: list[int], values: list[float], minute: float) -> float:
    """Linear-interpolate a uniformly-sampled day series at an arbitrary minute."""
    if minute <= times[0]:
        return values[0]
    if minute >= times[-1]:
        return values[-1]
    # minute now lies strictly inside the range, so a segment always matches.
    i = next(i for i in range(len(times) - 1) if times[i] <= minute <= times[i + 1])
    span = times[i + 1] - times[i] or 1
    frac = (minute - times[i]) / span
    return values[i] + (values[i + 1] - values[i]) * frac


def _curve_inflections(
    curve: SourceProfile,
    samples: list[tuple[int, float]],
    brightness: list[float],
    noon_minute: int | None,
) -> dict[str, Any] | None:
    """Locate a curve's day-shape for the viz, read from the sampled brightness.

    Returns the **ramp-up span** (when brightness leaves the night floor → when it
    reaches its daytime peak), the **ramp-down span** (when it leaves the peak →
    when it returns to the floor — i.e. its end is the minimum), and **solar
    noon**. Each point is ``{t, br}``. ``None`` for a flat curve. The ``curve`` arg
    is unused (kept for signature symmetry); the series already encodes the
    day-window shape.
    """
    times = [minute for minute, _e in samples]
    floor = min(brightness)
    peak = max(brightness)
    if peak - floor < 1.0:
        return None  # a flat curve has no inflections worth marking
    near_floor = floor + 0.02 * (peak - floor)
    near_peak = peak - 0.02 * (peak - floor)
    count = len(brightness)
    up_end = next(i for i in range(count) if brightness[i] >= near_peak)
    up_start = max((i for i in range(up_end) if brightness[i] <= near_floor), default=0)
    down_start = max(i for i in range(count) if brightness[i] >= near_peak)
    down_end = next(
        (i for i in range(down_start + 1, count) if brightness[i] <= near_floor),
        count - 1,
    )

    def point(index: int) -> dict[str, float]:
        return {"t": times[index], "br": round(brightness[index], 1)}

    if noon_minute is not None:
        noon = {
            "t": round(noon_minute),
            "br": round(_interp_at(times, brightness, noon_minute), 1),
        }
    else:
        noon = point(brightness.index(peak))
    return {
        "ramp_up": {"start": point(up_start), "end": point(up_end)},
        "ramp_down": {"start": point(down_start), "end": point(down_end)},
        "solar_noon": noon,
    }


def _switch_zones(
    occupancy: Mapping[str, OccupancyZone],
    key: Literal["sync_switches", "override_switches"],
) -> dict[str, list[str]]:
    """Map each switch listed under ``key`` to the zones that list it."""
    zones: dict[str, list[str]] = {}
    for zone_key, zone in occupancy.items():
        for base in zone.get(key, []):
            zones.setdefault(base, []).append(zone_key)
    return zones


def _truthy(value: object) -> bool:
    """Coerce an mmwave occupancy field (bool / "ON" / 1 / ...) to a bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "on", "1", "occupied", "detected")
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _target_xy(target: EngineTarget) -> tuple[float, float] | None:
    """Return a target's chromaticity: its xy, its rgb as xy, or its CCT as xy."""
    if target.xy:
        return (target.xy[0], target.xy[1])
    if target.rgb_color:
        return rgb_to_xy(
            (target.rgb_color[0], target.rgb_color[1], target.rgb_color[2])
        )
    if target.color_temp_kelvin:
        return kelvin_to_xy(target.color_temp_kelvin)
    return None


def _target_to_payload(target: EngineTarget, transition: float) -> PushPayload:
    """Convert an engine target to a stateless Z2M ``/set`` payload."""
    return build_payload(
        brightness_pct=target.brightness_pct,
        color_temp_kelvin=target.color_temp_kelvin or 0.0,
        rgb_color=list(target.rgb_color) if target.rgb_color else None,
        mode=target.color_mode,
        transition=transition,
        xy=target.xy,
    )


def _dimmed(payload: PushPayload, level: float) -> PushPayload:
    """Return the payload's colour at a fixed brightness percentage."""
    return cast("PushPayload", {**payload, "brightness": pct_to_brightness(level)})


def _strip(payload: PushPayload) -> PushPayload:
    """Return the payload without transition/state: the dedup key (U1)."""
    stripped = dict(payload)
    stripped.pop("transition", None)
    stripped.pop("state", None)
    return cast("PushPayload", stripped)


def interactive_transition(last: PushPayload | None, new: PushPayload) -> float:
    """Transition for a user-triggered change, scaled by how far it moves (U2).

    ``0.4 s + 0.02 s per brightness percentage point + 0.004 s per mired``,
    clamped to 0.4-2.0 s: a small look change feels instant, a release from a
    dim warm look back to a bright day curve fades in about 2 s instead of
    jumping.
    """
    if last is None:
        return INTERACTIVE_TRANSITION_MIN_S
    d_pct = abs(new.get("brightness", 0) - last.get("brightness", 0)) * 100 / 254
    d_mired = 0
    if "color_temp" in new and "color_temp" in last:
        d_mired = abs(new["color_temp"] - last["color_temp"])
    elif ("color_temp" in new) != ("color_temp" in last):
        d_mired = INTERACTIVE_MODE_CHANGE_MIRED
    seconds = INTERACTIVE_TRANSITION_MIN_S + 0.02 * d_pct + 0.004 * d_mired
    return round(min(INTERACTIVE_TRANSITION_MAX_S, seconds), 2)


def _parse_json(payload: str) -> dict[str, Any] | None:
    """Parse a Z2M JSON state payload, returning None if it is not an object."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError, ValueError:
        return None
    return data if isinstance(data, dict) else None
