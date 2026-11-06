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
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict, cast

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
    CONF_OVERRIDE_IDLE,
    CONF_OVERRIDE_SWITCHES,
    CONF_PUSH_INTERVAL,
    CONF_ROOMS,
    CONF_SENSORS,
    CONF_SET_TOPIC,
    CONF_SOURCE,
    CONF_SOURCES,
    CONF_STAGE_DELAY,
    CONF_SWEEP,
    CONF_SYNC_SWITCHES,
    CONF_TOPIC,
    CONFIG_TAP_MIN_ON_S,
    DEFAULT_OCCUPANCY_KEY,
    DEFAULT_OCCUPANCY_TRANSITION_S,
    DEFAULT_OVERRIDE_IDLE_S,
    DEFAULT_SLEEP_RAMP_IN_S,
    DEFAULT_SLEEP_RAMP_OUT_S,
    DEFAULT_TRANSITION_S,
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
    PADDLE_STATE_GRACE_S,
    RELEASE_ACTIONS,
    STATE_OFF,
    STATE_ON,
    SWITCH_ECHO_S,
    SWITCH_READ_GAP_S,
    TRANSITION_CATCHUP_UNKNOWN_S,
    TRANSITION_REASSERT,
)
from .modes import action_to_mode
from .publisher import (
    PRIORITY_INTERACTIVE,
    PRIORITY_TICK,
    TICK_BUDGET,
    PacedPublisher,
    Send,
)
from .push import (
    build_payload,
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
        n_sources = max(1, len(config[CONF_SOURCES]))
        super().__init__(
            hass,
            _LOGGER,
            name="Light Man",
            config_entry=entry,
            # One timer, sources staggered across the interval (A7).
            update_interval=timedelta(seconds=interval / n_sources),
            always_update=False,
        )
        self._interval = interval
        self.entry_id = entry.entry_id
        self._entry = entry
        self._config = config
        self.stored = validated.stored  # the data-model shape (read by the panel)
        self._modes = modes
        self._switch_map = validated.switch_map
        # light-level switches: base -> every (source, ref) it governs
        self._switch_lights = validated.switch_lights
        # room-level switches (the hallway pair): base -> (room, [(source, ref)])
        self._room_switches = validated.room_switch_map
        self._bar_lights = validated.bar_lights  # base -> the light its bar follows
        self._switch_height: dict[str, int] = {}  # base -> its last reported level
        self._on_since: dict[str, float] = {}  # switch -> when it last turned on
        self._expect_echo: dict[str, tuple[str, float]] = {}  # our switch writes
        self._override_timers: dict[str, Callable[[], None]] = {}
        self._zone_lit: dict[str, set[str]] = {}  # zone -> topics lit this episode
        self._room_source: dict[str, str] = {
            room: source_key
            for source_key, source in config[CONF_SOURCES].items()
            for room in source.get(CONF_ROOMS, {})
        }
        # Dedup: what each topic last showed (colour + brightness, no transition),
        # set only on broker ack; cleared by Z2M failures and outages.
        self._last_published: dict[str, PushPayload] = {}
        self._last_planned: dict[str, dict[str, Any]] = {}  # for re-sends/replay
        self._paddle_on: dict[str, bool] = {}
        self._paddle_action_at: dict[str, float] = {}  # switch -> last up/down tap
        self._unsubs: list[Any] = []
        self._clock: Callable[[], float] = lambda: _monotonic()
        topics = [s[CONF_CONSOLIDATED_TOPIC] for s in config[CONF_SOURCES].values()]
        self._z2m_base, _disagree = z2m_base_topic(topics)
        self._group_topics: set[str] | None = None
        self._state_resend: set[str] = set()  # topics whose OFF a retry keeps
        # source -> generation of the latest force request (cleared when a unit
        # planned with that generation is actually sent)
        self._force_pending: dict[str, int] = {}
        self._force_seq = 0
        self._failed_at: dict[str, float] = {}  # topic -> last Z2M failure
        self._dims: dict[str, list[_Dim]] = {}  # switch -> fades it is running
        self._dim_starts: dict[str, asyncio.Task[None]] = {}  # holds not yet dimming
        self._off_holds: dict[str, float] = {}  # switch held while off -> since
        self._bar_quiet: dict[str, float] = {}  # switch -> ignore its level until
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
        offsets = {key: i / n_sources for i, key in enumerate(config[CONF_SOURCES])}
        self._stagger = offsets
        self._epoch: float | None = None
        self._next_due: dict[str, float] = {}
        self._glide_end: dict[str, datetime] = {}
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
        self._override_zones: dict[str, list[str]] = {}  # switch -> zones it holds
        for zone_key, zone in self._occupancy.items():
            for base in zone.get(CONF_OVERRIDE_SWITCHES, []):
                self._override_zones.setdefault(base, []).append(zone_key)
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
        return [*self._switch_map, *self._room_switches]

    async def _read_switches(self) -> None:
        """Ask each switch for its state once (unicasts, spaced)."""
        for base in self._all_switches():
            if self._closed:
                return
            if base in self._paddle_on:
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
        self._dims.clear()
        for task in self._dim_starts.values():
            task.cancel()
        self._dim_starts.clear()
        for cancel in self._override_timers.values():
            cancel()
        self._override_timers.clear()
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

        The timer runs at ``interval / n_sources`` and each source is due once per
        interval at its own offset (A7 stagger), so floods are spread across the
        interval instead of landing together. Expiry only invalidates — the next
        due tick sends what changed (no forced flood at solar midnight).
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

        Every tick publishes ``target(t_end)`` with ``transition = interval``
        (A6 glide), where ``t_end`` is one interval ahead; the bulb fades
        continuously between ticks. Light Man never publishes to an Inovelli
        switch topic — switches are read-only inputs.
        """
        if not self.push_enabled:
            return
        now = dt_util.utcnow()
        clock = self._clock()
        if force:
            self.diagnostics["forced_floods"] = (
                self.diagnostics.get("forced_floods", 0) + 1
            )
        del now
        # HA rounds the timer to whole seconds, so it fires up to a second either
        # side of a slot; a source due within half a timer period goes now
        # rather than one whole period late (review nit 12).
        slack = self._interval / max(1, len(self._config[CONF_SOURCES])) / 2
        for key in self._config[CONF_SOURCES]:
            due = self._next_due.get(key, 0.0)
            if clock >= due - slack:  # this send is the source's slot
                self._next_due[key] = self._next_slot(key, max(clock, due))
            elif due_only:
                continue  # not due yet; a forced push leaves the slot unchanged
            self._enqueue_source(key, force=force)
        self._pushed_once = True

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
            t_end = dt_util.utcnow() + timedelta(seconds=self._interval)
            self._glide_end[key] = t_end
            used[0] = self._force_pending.get(key)
            sends = self._plan_source(key, source, t_end, force=used[0] is not None)
            self._note_planned(sends, PRIORITY_TICK)
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

    def _next_slot(self, key: str, clock: float) -> float:
        """Next due time on this source's own slot (A7 stagger).

        Slots are ``epoch + (k + offset) * interval``, where ``offset`` is
        ``i / n_sources``; a forced push therefore never re-synchronises the
        sources onto the same instant.
        """
        if self._epoch is None:
            self._epoch = clock
        offset = self._stagger[key] * self._interval
        elapsed = clock - self._epoch - offset
        # A clock exactly on a slot (an early send passes the slot itself) must
        # move to the *next* slot; float rounding of ``slot - epoch - offset``
        # can land a hair below the boundary and return the same slot, which
        # sent every source twice per interval (live 2026-11-03).
        slots = math.floor(elapsed / self._interval + _SLOT_EPSILON) + 1
        return self._epoch + offset + slots * self._interval

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
        looks = self._held_looks(key, source)
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
                item.topic, item.payload, item.dedup, force=force
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
        local = dt_util.as_local(when)
        minutes = local.hour * 60 + local.minute + local.second / 60
        sleep_s = self._compute_sleep_s(when)
        target = compute_target(
            elevation, noon, source["profile"], sleep_s=sleep_s, now_minutes=minutes
        )
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
        self, key: str, source: SourceConfig
    ) -> dict[str, PushPayload | None]:
        """Each held room's look: night/day from the curve, manual as dimmed to."""
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
        del key
        return looks

    def _unchanged(self, topic: str, payload: PushPayload) -> bool:
        """Return True when ``topic`` already shows this colour and brightness.

        A room topic is compared with what it last showed — its own mark or a
        newer flood of its source — never with an hours-old room mark alone.
        """
        return self._shown(topic) == _strip(payload)

    def _tick_transition(
        self, topic: str, payload: PushPayload, dedup: bool, *, force: bool = False
    ) -> float:
        """Glide for curve sends, short for re-asserts, catch-up when forced/unknown.

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
        return float(self._interval)

    def _enqueue(
        self,
        sends: list[Send],
        *,
        priority: int,
        origin_at: float | None = None,
    ) -> None:
        """Queue a concrete unit; dedup marks are set from the acked sends only."""
        if self._closed:
            return
        self._note_planned(sends, priority)
        future = self._publisher.enqueue(sends, priority=priority, origin_at=origin_at)
        future.add_done_callback(self._on_sent)

    def _note_planned(self, sends: list[Send], priority: int) -> None:
        now = self._clock()
        for send in sends:
            self._last_planned[send.topic] = send.payload
            self._planned_at[send.topic] = now
            if self._outage:
                self._outage_topics.add(send.topic)
            self._interactive_recent[send.topic] = (now, 0)  # retries per plan
            self._state_resend.discard(send.topic)

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
        t_end = self._glide_end.get(source_key) or (
            dt_util.utcnow() + timedelta(seconds=self._interval)
        )
        try:
            return self._engine_payload(source_key, source, t_end)
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
        state: str | None = None,
        transition: float | None = None,
        origin_at: float | None = None,
    ) -> None:
        """Queue one interactive send with a change-scaled transition (U2)."""
        body: dict[str, Any] = dict(payload)
        if transition is None:
            transition = interactive_transition(self._shown(topic), payload)
        body["transition"] = transition
        if state is not None:
            body["state"] = state
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
        if base in self._room_switches:
            return self._room_switches[base][1]
        return self._switch_lights[base]

    def _bar_source(self, base: str) -> str:
        """Return the source whose curve a switch's bar follows."""
        return self._bar_lights[base][0]

    def _bar_fraction(self, source_key: str) -> float | None:
        """Where the source's curve sits, 0 (dark) to 1 (its brightest), if known."""
        engine = self.diagnostics["engine"].get(source_key)
        peak = self._config[CONF_SOURCES][source_key]["profile"].get("max_br")
        if not engine or not peak:
            return None
        return max(0.0, min(1.0, float(engine["brightness_pct"]) / peak))

    def _bar_leds(self, base: str) -> dict[str, int | None]:
        """Status lights of a switch: a held look on top, the override below."""
        refs = [ref for _source, ref in self._governed(base)]
        kinds = {self._modes.mode_of(ref) for ref in refs}
        top = next((c for kind, c in LOOK_COLORS.items() if kind in kinds), None)
        leds: dict[str, int | None] = {LED_TOP: top}
        zones = self._override_zones.get(base)
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
        self._switch_height[base] = level
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
            level = self._switch_height.get(other)
            if level is not None:
                self._write_height(other, level)

    def _write_height(self, base: str, level: int) -> None:
        """Set a switch's own level to its lights' when it is off by a step."""
        if (
            not self._paddle_on.get(base)
            or not self.push_enabled
            or base in self._dims
            or self._level_bound is None
            or base in self._level_bound
            or self._clock() < self._bar_quiet.get(base, 0.0)
        ):
            return
        target = self._lights_level(base)
        if target is None or abs(target - level) < HEIGHT_STEP:
            return
        self._bar_quiet[base] = self._clock() + BAR_HEIGHT_QUIET_S
        self._switch_height[base] = target
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
        own edge, and drives its own sweep independently.
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
            if occupied == self._sensor_occupied.get(bid):
                continue  # no edge for this binding
            self._sensor_occupied[bid] = occupied
            if self._modes.override_until(binding.zone) is not None:
                # Held on by a tap: no sweep (the lights are on), no clear; any
                # presence restarts the idle clock.
                self.diagnostics["occupancy"][binding.zone] = not self._zone_vacant(
                    binding.zone
                )
                self._restart_idle_timer(binding.zone)
                continue
            if occupied:
                self._start_sweep(binding)
            else:
                await self._maybe_clear_zone(binding.zone)

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
        for stage in sweep:
            delay = stage.get(CONF_STAGE_DELAY, 0.0)
            if delay:
                await asyncio.sleep(delay)
            sends: list[Send] = []
            for light in stage.get(CONF_LIGHTS, []):
                if light[CONF_SET_TOPIC] in lit:
                    continue
                payload = self._sweep_payload(light)
                if payload is None:
                    continue
                body = {
                    **payload,
                    "transition": DEFAULT_OCCUPANCY_TRANSITION_S,
                    "state": STATE_ON,
                }
                sends.append(Send(light[CONF_SET_TOPIC], body))
                lit.add(light[CONF_SET_TOPIC])
            if sends:
                self._enqueue(sends, priority=PRIORITY_INTERACTIVE)
        self._write_switches(
            self._occupancy[zone_key].get(CONF_SYNC_SWITCHES, []), STATE_ON
        )

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
        if self._zone_held(zone_key):
            return
        sync = zone.get(CONF_SYNC_SWITCHES, [])
        if sync and all(self._paddle_on.get(base) for base in sync):
            self._write_switches(sync, STATE_OFF)
            return
        transition = zone.get(CONF_OFF_TRANSITION, DEFAULT_OCCUPANCY_TRANSITION_S)
        off = {"state": STATE_OFF, "transition": transition}
        sends = [Send(topic, off) for topic in zone.get(CONF_OFF_LIGHTS, [])]
        if sends:
            self._enqueue(sends, priority=PRIORITY_INTERACTIVE)
            self._state_resend.update(send.topic for send in sends)
        self._write_switches(sync, STATE_OFF)  # bars follow (lights already off)

    def _write_switches(self, bases: Iterable[str], state: str) -> None:
        """Set switches' on/off state (unicasts); their echo is not a paddle press.

        A switch already showing ``state`` is skipped.
        """
        want = state == STATE_ON
        sends = [
            Send(f"{base}/set", {"state": state})
            for base in bases
            if self._paddle_on.get(base) != want
        ]
        if not sends:
            return
        now = self._clock()
        for send in sends:
            self._expect_echo[send.topic.removesuffix("/set")] = (state, now)
        self._enqueue(sends, priority=PRIORITY_INTERACTIVE)

    # --- zone override (a tap holds the zone on; motion ignored) ------------

    async def _arm_override(self, zone_key: str) -> None:
        await self._modes.set_override(
            zone_key, expires_at=self._next_midnight(dt_util.utcnow())
        )
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
        if not self._zone_vacant(zone_key):
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

    async def _stage_off(self, source_key: str, light_ref: str) -> None:
        """Turn a room off + stage its adaptive value in one deterministic command.

        A paddle-off must NOT flood the consolidated group state-less: the MQTT
        round-trip beats the switch's local Zigbee binding OFF, so the brightness
        lands while the bulb is still on and re-brightens it (the multi-tap bug).
        Instead, address the room with an explicit ``state: OFF`` carrying the
        staged adaptive value (at the glide end) — it goes off regardless of the
        race and is pre-loaded for the next turn-on.
        """
        rooms = self._config[CONF_SOURCES][source_key].get(CONF_ROOMS, {})
        room = rooms.get(light_ref)
        if room is None or not room.get(CONF_SET_TOPIC):
            return
        payload = self._room_glide_target(source_key)
        if payload is None:
            return
        self._interactive_send(
            room[CONF_SET_TOPIC],
            payload,
            state=STATE_OFF,
            transition=DEFAULT_TRANSITION_S,
        )

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
        now = dt_util.utcnow()
        await self._modes.set_held(
            room, kind=kind, armed_at=now, expires_at=self._next_midnight(now)
        )
        source_key = self._room_source.get(room)
        topic = self._room_topic(room)
        if source_key is not None and topic is not None:
            look = self._held_looks(source_key, self._config[CONF_SOURCES][source_key])
            payload = look.get(room)
            if payload is not None and self.push_enabled:
                self._interactive_send(topic, payload, origin_at=origin_at)
        self._publish_snapshot()

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
        """Apply a tap: a config hold, a held-dim freeze, or an on/off release.

        On a Smart-Bulb-Mode switch a single tap is also the on/off intent
        (up = on, down = off). Paddle-on publishes nothing: the bulbs wake at the
        value staged by the last flood (verified on every model 2026-11-03); the
        source is invalidated so its next due tick re-sends it.
        """
        if not self.push_enabled:
            return  # inert when Light Man is off — the blueprint owns taps
        self._play_tap_animation(base, action)
        if base in self._room_switches:
            await self._on_room_action(base, action)
            return
        lights = self._switch_lights.get(base)
        if lights is None:
            return
        origin_at = self._clock()
        if action in RELEASE_ACTIONS:
            await self._end_dim(base)
            return
        mode = action_to_mode(action)
        if mode is None:
            return
        if mode == MODE_ADAPTIVE:
            await self._on_paddle_tap(base, action, lights, origin_at)
        elif mode not in LOOK_KINDS:
            await self._start_dim(base, action)
        elif self._look_tap_accepted(base, action):
            self._bars.announce(base, LED_TOP)  # the top LED pulses, then stays
            for _source, light_ref in lights:
                await self.async_hold(light_ref, mode, origin_at=origin_at)
            self._bars.unannounce(base)
            self._follow_partners(base)

    async def _on_paddle_tap(
        self,
        base: str,
        action: str,
        lights: list[tuple[str, str]],
        origin_at: float,
    ) -> None:
        """Handle a single up/down tap: track the paddle and release any hold."""
        off = action.startswith("down")
        self._drop_dim(base)
        self._set_paddle(base, on=not off)
        self._paddle_action_at[base] = self._clock()
        released = False
        for source_key, light_ref in lights:
            freed = await self._modes.release(light_ref)
            released = released or freed
            if off:
                self._forget_lit(light_ref)
                await self._stage_off(source_key, light_ref)
            elif freed:
                self._release_room(light_ref, origin_at=origin_at)
        self._publish_snapshot()
        if released and not off:
            self._bar_play(base, EFFECT_PULSE, COLOR_BAR)  # back to automatic

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
        was = self._paddle_on.get(base)
        self._paddle_on[base] = on
        if not on:
            self._on_since.pop(base, None)
        elif not was:
            self._on_since[base] = self._clock()

    def _on_long_enough(self, base: str, action: str) -> bool:
        """Return True once the switch has been on for 0.5 s (config-tap rule).

        Family members hit config by accident while pressing the paddle on; an
        unknown switch state counts as off. A config tap never turns lights on.
        """
        since = self._on_since.get(base)
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

    async def _on_room_action(self, base: str, action: str) -> None:
        """Handle a tap on a switch that governs a whole room (the hallway pair).

        Paddle on/off is the switch's own binding. Up arms the zone's motion
        override (and releases a look); down ends it. A config tap holds every
        light of the room at the look while the lights are on; a held paddle
        dims each of the room's curves and holds the level.
        """
        room, lights = self._room_switches[base]
        origin_at = self._clock()
        if action in RELEASE_ACTIONS:
            await self._end_dim(base)
            return
        mode = action_to_mode(action)
        if mode is None:
            return
        if mode == HELD_MANUAL:
            await self._start_dim(base, action)
            return
        if mode in LOOK_KINDS:
            if self._look_tap_accepted(base, action):
                self._bars.announce(base, LED_TOP)  # the top LED pulses, then stays
                await self._hold_lights(lights, mode, origin_at=origin_at)
                self._bars.unannounce(base)
                self._follow_partners(base)
            return
        off = action.startswith("down")
        self._drop_dim(base)
        self._set_paddle(base, on=not off)
        self._paddle_action_at[base] = origin_at
        released = [ref for _src, ref in lights if await self._modes.release(ref)]
        zones = self._override_zones.get(base, [])
        feedback: int | None = None
        if off:
            for zone in zones:
                await self._end_override(zone)
                self._zone_lit.pop(zone, None)
            for source_key in {src for src, _ref in lights}:
                self._invalidate_source(source_key)
        else:
            shown = [(src, ref) for src, ref in lights if ref in released]
            feedback = await self._room_tap_up(base, zones, shown, origin_at)
        _LOGGER.debug("%s on %s (room %s)", action, base, room)
        self._publish_snapshot()
        self._bars.unannounce(base)
        if feedback is not None:
            self._bar_play(base, EFFECT_PULSE, feedback)

    async def _room_tap_up(
        self,
        base: str,
        zones: list[str],
        released: list[tuple[str, str]],
        origin_at: float,
    ) -> int | None:
        """Release looks to the curve and arm the override; return the bar colour.

        A newly armed override is answered on the bottom LED of the tapped
        switch (it pulses, then stays); only a bare release pulses the bar.
        """
        feedback: int | None = None
        if released:
            self._show_lights(released, self._room_glide_target, origin_at=origin_at)
            feedback = COLOR_BAR  # back to automatic
        for zone in zones:
            if self._modes.override_until(zone) is None:
                self._bars.announce(base, LED_BOTTOM)
                feedback = None
            await self._arm_override(zone)
        return feedback

    async def _hold_lights(
        self, lights: list[tuple[str, str]], kind: str, *, origin_at: float
    ) -> None:
        """Hold several lights at a look; show it with one send per source."""
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
            self._show_lights(lights, look, origin_at=origin_at)
        self._publish_snapshot()

    def _show_lights(
        self,
        lights: list[tuple[str, str]],
        payload_of: Callable[[str], PushPayload | None],
        *,
        origin_at: float,
    ) -> None:
        """Send each source's payload to its lights.

        The consolidated topic when every light of the source is in ``lights``,
        else each light's own topic.
        """
        by_source: dict[str, list[str]] = {}
        for source_key, ref in lights:
            by_source.setdefault(source_key, []).append(ref)
        for source_key, refs in by_source.items():
            payload = payload_of(source_key)
            if payload is None:
                continue
            source = self._config[CONF_SOURCES][source_key]
            rooms = source.get(CONF_ROOMS, {})
            if set(rooms) <= set(refs):
                topics = [source[CONF_CONSOLIDATED_TOPIC]]
            else:
                topics = [
                    rooms[ref][CONF_SET_TOPIC]
                    for ref in refs
                    if ref in rooms and rooms[ref].get(CONF_SET_TOPIC)
                ]
            for topic in topics:
                self._interactive_send(topic, payload, origin_at=origin_at)

    async def _on_state(self, base: str, state: str) -> None:
        """Track the paddle: on invalidates the source; off stages the room off.

        Off **releases any hold** (a look must not linger to next turn-on) and
        stages the room with an explicit ``state: OFF`` rather than the state-less
        flood — the flood would race the local binding and re-brighten the bulb
        (see :meth:`_stage_off`).
        """
        on = state == STATE_ON
        previous = self._paddle_on.get(base)
        if previous == on:
            return  # unchanged, or the matching action already handled it
        echo = self._expect_echo.get(base)
        if (
            echo is not None
            and echo[0] == state
            and self._clock() - echo[1] < SWITCH_ECHO_S
        ):
            # Our own sync write coming back: record it, it is not a press.
            del self._expect_echo[base]
            self._set_paddle(base, on=on)
            return
        held_off = self._off_holds.get(base)
        if held_off is not None and self._clock() - held_off < DIM_TIMEOUT_S:
            return  # the switch turned itself on under a held paddle: not a press
        tapped = self._paddle_action_at.get(base)
        if tapped is not None and self._clock() - tapped < PADDLE_STATE_GRACE_S:
            # Z2M republishes the switch's *cached* state with any attribute report
            # until the switch reports its new state (~1.5 s after a tap). A
            # contradicting state this soon after an up/down tap is that stale
            # cache (live 2026-11-04 03:40: a bath light turned on, then staged
            # off 0.37 s later); a real paddle change arrives as an action.
            return
        self._set_paddle(base, on=on)
        lights = self._switch_lights.get(base)
        if lights is None or not self.push_enabled:
            return  # a room-level switch: its binding acts, the state is tracked
        self._drop_dim(base)
        for source_key, light_ref in lights:
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
            if await self._modes.release(light_ref):
                self._release_room(light_ref)
            return
        if await self._modes.release(light_ref):
            _LOGGER.info("hold released by switch-off for %s", light_ref)
        self._forget_lit(light_ref)
        await self._stage_off(source_key, light_ref)

    # --- held-paddle dimming: Light Man fades, so it knows the level ---------

    def _dim_groups(self, base: str) -> list[tuple[str, str, list[str]]]:
        """Return (source, topic, light refs) for each fade a switch's hold runs.

        One per curve: the consolidated topic when a room switch covers every
        light of the source, else each light's own topic.
        """
        by_source: dict[str, list[str]] = {}
        for source_key, ref in self._governed(base):
            by_source.setdefault(source_key, []).append(ref)
        groups: list[tuple[str, str, list[str]]] = []
        for source_key, refs in by_source.items():
            source = self._config[CONF_SOURCES][source_key]
            rooms = source.get(CONF_ROOMS, {})
            if base in self._room_switches and set(rooms) <= set(refs):
                groups.append((source_key, source[CONF_CONSOLIDATED_TOPIC], refs))
                continue
            groups.extend(
                (source_key, rooms[ref][CONF_SET_TOPIC], [ref])
                for ref in refs
                if ref in rooms and rooms[ref].get(CONF_SET_TOPIC)
            )
        return groups

    async def _start_dim(self, base: str, action: str) -> None:
        """Fade a held paddle's lights toward the floor or the ceiling.

        The switches have no level binding, so this fade is the dim. It needs
        the switch on (a held paddle never turns lights on) and holds the lights
        as ``manual``; the level is set when the paddle is released.
        """
        on = self._paddle_on.get(base)
        if on is False:
            self._off_holds[base] = self._clock()
            self._turn_back_off(base)
            return
        if not on:
            return
        self._cancel_dim_start(base)
        self._dim_starts[base] = self.hass.async_create_task(
            self._dim_after_delay(base, action), "lm_dim_start", eager_start=True
        )

    async def _dim_after_delay(self, base: str, action: str) -> None:
        """Start the dim once the paddle has stayed held past the delay."""
        await _dim_sleep(DIM_START_DELAY_S)
        self._dim_starts.pop(base, None)
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
        if dims:
            self._dims[base] = dims
        self._publish_snapshot()

    async def _end_dim(self, base: str) -> dict[str, PushPayload]:
        """Stop each fade where it is (paddle released) and hold that level.

        Returns what each stopped topic now shows.
        """
        stopped: dict[str, PushPayload] = {}
        if self._off_holds.pop(base, None) is not None:
            self._turn_back_off(base)
            return stopped
        self._cancel_dim_start(base)  # released before the dim began: nothing
        dims = self._dims.pop(base, None)
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
        task = self._dim_starts.pop(base, None)
        if task is not None:
            task.cancel()

    def _drop_dim(self, base: str) -> None:
        """Forget a switch's hold (a tap or a paddle change ended it)."""
        self._cancel_dim_start(base)
        self._dims.pop(base, None)
        self._off_holds.pop(base, None)

    def _turn_back_off(self, base: str) -> None:
        """Turn off a switch whose paddle was held while it was off.

        A hold on an off switch turns the switch itself on and ramps its bar,
        while its lights stay off (no level binding; seen 2026-11-06, primary
        bath). Sent on the hold and again on the release; the switch's state
        reports in between are not paddle presses.
        """
        now = self._clock()
        self._paddle_action_at[base] = now
        self._expect_echo[base] = (STATE_OFF, now)
        self._enqueue(
            [Send(f"{base}/set", {"state": STATE_OFF})], priority=PRIORITY_INTERACTIVE
        )

    def _dim_pending(self, room: str) -> bool:
        """Return True while a held paddle is fading ``room``."""
        clock = self._clock()
        return any(
            room in dim.refs and clock - dim.began < DIM_TIMEOUT_S
            for dims in self._dims.values()
            for dim in dims
        )

    async def _finish_stale_dims(self) -> None:
        """End fades whose release never arrived (they have reached their end)."""
        clock = self._clock()
        for base, dims in list(self._dims.items()):
            if any(clock - dim.began >= DIM_TIMEOUT_S for dim in dims):
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
          ``INTERACTIVE_RETRY_WINDOW_S`` and only once per plan. A zone-clear
          OFF is re-sent as an OFF; a paddle-off's staging without ``state``
          (the paddle may already be back on).
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
        body = dict(payload)
        if body.get("state") == STATE_OFF and topic not in self._state_resend:
            body.pop("state")  # a paddle-off's staging: the paddle owns on/off
        future = self._publisher.enqueue(
            [Send(topic, body)], priority=PRIORITY_INTERACTIVE
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


def _truthy(value: object) -> bool:
    """Coerce an mmwave occupancy field (bool / "ON" / 1 / ...) to a bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "on", "1", "occupied", "detected")
    if isinstance(value, (int, float)):
        return value != 0
    return False


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
