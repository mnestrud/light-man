"""Light Man coordinator: the single adaptive brain for the bulb push.

Timer-driven on its own cadence. Each cycle: sweep expired held modes, then (if
Light Man owns the push) publish every source. Per room the target is its live
adaptive value, its held look (night/day), or nothing (off / manually frozen) —
so an off room is never re-on'd and a held look is never clobbered.

Modes are driven only by explicit Inovelli action intents (``config_*`` hold,
single taps release, held-dim freezes) — never inferred from switch on/off
bounce. The ``push_enable`` switch is a plain master on/off: ON runs the adaptive
push, OFF makes Light Man inert (it no longer touches any legacy stack).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

from homeassistant.components import mqtt
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.sun import get_astral_event_next
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adaptive import compute_target, day_look, night_look, sleep_ramp
from .const import (
    ACTION_SUFFIX,
    COLOR_MODE_COLOR_TEMP,
    COLOR_MODE_RGB,
    CONF_CONSOLIDATED_TOPIC,
    CONF_CURVES,
    CONF_LIGHTS,
    CONF_OCCUPANCY,
    CONF_OCCUPANCY_KEY,
    CONF_OFF_LIGHTS,
    CONF_OFF_TRANSITION,
    CONF_PUSH_INTERVAL,
    CONF_ROOMS,
    CONF_SENSORS,
    CONF_SET_TOPIC,
    CONF_SOURCE,
    CONF_SOURCES,
    CONF_STAGE_DELAY,
    CONF_SWEEP,
    CONF_TOPIC,
    CONF_TRANSITION,
    DEFAULT_OCCUPANCY_KEY,
    DEFAULT_OCCUPANCY_TRANSITION_S,
    DEFAULT_SLEEP_RAMP_IN_S,
    DEFAULT_SLEEP_RAMP_OUT_S,
    DEFAULT_TRANSITION_S,
    DEFAULT_WIND_DOWN_S,
    INTER_PUBLISH_DELAY_S,
    MODE_ADAPTIVE,
    SET_SUFFIX,
    SOLAR_MIDNIGHT_EVENT,
    STATE_OFF,
    STATE_ON,
)
from .modes import action_to_mode
from .push import _room_target, build_payload, plan_publishes
from .solar import day_profile, solar_inputs

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .config_loader import ValidatedConfig
    from .models import (
        EngineTarget,
        LightManConfig,
        OccupancyStage,
        PushPayload,
        SourceConfig,
        SourceProfile,
    )
    from .modes import ModeManager

_LOGGER = logging.getLogger(__name__)


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
        super().__init__(
            hass,
            _LOGGER,
            name="Light Man",
            config_entry=entry,
            update_interval=timedelta(seconds=interval),
            always_update=False,
        )
        self.entry_id = entry.entry_id
        self._entry = entry
        self._config = config
        self.stored = validated.stored  # the data-model shape (read by the panel)
        self._modes = modes
        self._switch_map = validated.switch_map
        self._room_source: dict[str, str] = {
            room: source_key
            for source_key, source in config[CONF_SOURCES].items()
            for room in source.get(CONF_ROOMS, {})
        }
        self._last_published: dict[tuple[str, str], PushPayload] = {}
        self._last_inovelli: dict[str, dict[str, Any]] = {}
        self._paddle_on: dict[str, bool] = {}
        self._unsubs: list[Any] = []
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
        self._sweep_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self.diagnostics: dict[str, Any] = {
            "issues": validated.issues,
            "dedup_skips": 0,
            "addressing": {},
            "last_publish": {},
            "engine": {},
            "sleep": {"on": False, "s": 0.0},
            "occupancy": {},
            "inovelli": {},
        }

    # --- lifecycle ----------------------------------------------------------

    async def _async_setup(self) -> None:
        """Prune stale holds, subscribe to switch topics, push once HA is up."""
        # Drop holds orphaned by a topology change (keys no longer addressable).
        pruned = await self._modes.prune(set(self._room_source))
        if pruned:
            _LOGGER.info("pruned orphaned holds: %s", ", ".join(sorted(pruned)))
        for base in self._switch_map:
            self._unsubs.append(
                await mqtt.async_subscribe(self.hass, base, self._handle_message)
            )
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass, base + ACTION_SUFFIX, self._handle_message
                )
            )
        for topic in self._bindings_by_topic:
            self._unsubs.append(
                await mqtt.async_subscribe(self.hass, topic, self._handle_occupancy)
            )
        # Defer the first push until HA has fully started.
        self._unsubs.append(async_at_started(self.hass, self._takeover_on_start))

    async def _takeover_on_start(self, _hass: HomeAssistant) -> None:
        """Run the first push once HA is fully up (if Light Man is enabled)."""
        if self.push_enabled:
            await self._run_push(force=True)
            self.async_set_updated_data(self._snapshot())

    def shutdown_subscriptions(self) -> None:
        """Unsubscribe all MQTT subscriptions (called on unload)."""
        while self._unsubs:
            self._unsubs.pop()()

    def is_known_room(self, room: str) -> bool:
        """Return True if ``room`` exists in the seed config (service validation)."""
        return room in self._room_source

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
            except (KeyError, TypeError, ValueError):
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
        the latest addressing/publish/occupancy diagnostics, and config issues.
        No secrets; never mutates anything.
        """
        return {
            "push_enabled": self.push_enabled,
            "mqtt_available": self.mqtt_available,
            "sleep": self.diagnostics.get("sleep", {}),
            "held": self._modes.as_attributes(),
            "engine": self.diagnostics.get("engine", {}),
            "addressing": self.diagnostics.get("addressing", {}),
            "last_publish": self.diagnostics.get("last_publish", {}),
            "inovelli": self.diagnostics.get("inovelli", {}),
            "occupancy": self.diagnostics.get("occupancy", {}),
            "issues": self.diagnostics.get("issues", []),
            "dedup_skips": self.diagnostics.get("dedup_skips", 0),
        }

    async def _async_update_data(self) -> CoordinatorData:
        """Periodic tick: sweep expired held modes, then push."""
        now = dt_util.utcnow()
        expired = await self._modes.sweep_expired(now)
        for room in expired:
            self._invalidate_room(room)
            _LOGGER.info("hold expired for room %s", room)
        await self._run_push(force=bool(expired))
        return self._snapshot()

    # --- push (engine-driven adaptive value) --------------------------------

    async def _run_push(self, *, force: bool, only_source: str | None = None) -> None:
        """Plan every source (or one) and publish with a small inter-send gap.

        The adaptive value comes from the real-elevation engine (per-source
        profile); a source with no profile falls back to its AL dummy switch.
        Solar elevation is read once per cycle; if it is unavailable (a polar
        edge) the cycle is skipped rather than publishing a bad value. Group
        commands are Zigbee multicasts — a short inter-send gap keeps them from
        all landing in the same instant.
        """
        if not self.push_enabled:
            return
        now = dt_util.utcnow()
        try:
            elevation, noon = solar_inputs(self.hass, now)
        except ValueError:
            _LOGGER.warning("solar elevation unavailable; skipping push cycle")
            return
        local = dt_util.as_local(now)
        now_minutes = local.hour * 60 + local.minute + local.second / 60
        sleep_s = self._compute_sleep_s(now)
        self.diagnostics["sleep"] = {"on": self.sleep_on, "s": round(sleep_s, 3)}
        plan: list[tuple[str, str, PushPayload]] = []
        inovelli: list[tuple[str, dict[str, Any]]] = []
        for key, source in self._config[CONF_SOURCES].items():
            if only_source is not None and key != only_source:
                continue
            bulb, switches = self._plan_source(
                key, source, elevation, noon, now_minutes, sleep_s
            )
            plan.extend(bulb)
            inovelli.extend(switches)
        await self._publish_spaced(plan, force=force)
        await self._publish_inovelli(inovelli, force=force)

    def _plan_source(
        self,
        key: str,
        source: SourceConfig,
        elevation: float,
        noon: float,
        now_minutes: float,
        sleep_s: float,
    ) -> tuple[list[tuple[str, str, PushPayload]], list[tuple[str, dict[str, Any]]]]:
        """Plan a source's bulb publishes + its rooms' Inovelli switch publishes."""
        transition = float(source.get(CONF_TRANSITION, DEFAULT_TRANSITION_S))
        adaptive_payload = self._adaptive_payload(
            key, source, elevation, noon, now_minutes, sleep_s, transition
        )
        day_payload, night_payload = self._look_payloads(source, transition)
        rooms = source.get(CONF_ROOMS, {})
        modes = {r: self._modes.mode_of(r) for r in rooms if self._modes.is_held(r)}
        plan = plan_publishes(
            source,
            adaptive_payload=adaptive_payload,
            day_payload=day_payload,
            night_payload=night_payload,
            modes=modes,
        )
        self.diagnostics["addressing"][key] = [topic for topic, _ in plan]
        bulb = [(key, topic, payload) for topic, payload in plan]
        switches = self._inovelli_plan(
            source, adaptive_payload, day_payload, night_payload, modes
        )
        return bulb, switches

    def _inovelli_plan(
        self,
        source: SourceConfig,
        adaptive_payload: PushPayload,
        day_payload: PushPayload,
        night_payload: PushPayload,
        modes: dict[str, str],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Plan per-room Inovelli switch writes (absorbs the tick's a1-a15).

        Each room's switch gets the room's target brightness as ``defaultLevel``
        (tap-on prestage) plus the LED-bar ``brightness`` while its paddle is on.
        A manually-frozen room is skipped (its switch is left as-is).
        """
        plan: list[tuple[str, dict[str, Any]]] = []
        for room, room_cfg in source.get(CONF_ROOMS, {}).items():
            target = _room_target(
                modes.get(room, MODE_ADAPTIVE),
                adaptive_payload=adaptive_payload,
                day_payload=day_payload,
                night_payload=night_payload,
            )
            if target is None:
                continue
            bri = target["brightness"]  # build_payload always sets it
            for base in room_cfg.get("switches", []):
                payload: dict[str, Any] = {
                    "defaultLevelLocal": bri,
                    "defaultLevelRemote": bri,
                }
                if self._paddle_on.get(base):
                    payload["brightness"] = bri  # LED bar, only while the paddle is on
                plan.append((base + SET_SUFFIX, payload))
        return plan

    def _source_payload(
        self,
        source: SourceConfig,
        elevation: float,
        noon: float,
        now_minutes: float,
        sleep_s: float,
        transition: float,
    ) -> tuple[PushPayload, EngineTarget]:
        """Return ``(payload, engine_target)`` for a source via the engine.

        The real-elevation engine is the sole value source (the loader requires a
        ``profile`` on every source). Shared by the periodic push and occupancy
        turn-on.
        """
        target = compute_target(
            elevation, noon, source["profile"], sleep_s=sleep_s, now_minutes=now_minutes
        )
        return _target_to_payload(target, transition), target

    def _adaptive_payload(
        self,
        key: str,
        source: SourceConfig,
        elevation: float,
        noon: float,
        now_minutes: float,
        sleep_s: float,
        transition: float,
    ) -> PushPayload:
        """Return the live adaptive payload for a source, recording diagnostics."""
        payload, target = self._source_payload(
            source, elevation, noon, now_minutes, sleep_s, transition
        )
        self.diagnostics["engine"][key] = {
            "elevation": round(elevation, 2),
            "brightness_pct": round(target.brightness_pct, 1),
            "color_mode": target.color_mode,
            "color_temp_kelvin": target.color_temp_kelvin,
            "rgb_color": list(target.rgb_color) if target.rgb_color else None,
        }
        return payload

    def _look_payloads(
        self, source: SourceConfig, transition: float
    ) -> tuple[PushPayload, PushPayload]:
        """Return the engine's ``(day, night)`` forced-hold looks for a source.

        Day = the curve at peak sun; night = the curve at sun-down + sleep — both
        distinct from the live adaptive value, so a config-tap hold is visible.
        """
        profile = source["profile"]
        return (
            _target_to_payload(day_look(profile), transition),
            _target_to_payload(night_look(profile), transition),
        )

    async def _publish_spaced(
        self, plan: list[tuple[str, str, PushPayload]], *, force: bool
    ) -> None:
        """Publish a plan with a small gap between real sends.

        The gap only follows a real publish — deduped no-ops don't pace.
        """
        awaiting_gap = False
        for source_key, topic, payload in plan:
            if awaiting_gap:
                await asyncio.sleep(INTER_PUBLISH_DELAY_S)
            awaiting_gap = await self._publish(source_key, topic, payload, force=force)

    async def _publish_inovelli(
        self, plan: list[tuple[str, dict[str, Any]]], *, force: bool
    ) -> None:
        """Publish per-room Inovelli switch writes with write-on-change dedup.

        These are unicasts to individual switches (no mesh-multicast spacing).
        """
        for topic, payload in plan:
            if not force and self._last_inovelli.get(topic) == payload:
                continue
            if await self._raw_publish(topic, payload):
                self._last_inovelli[topic] = payload
                self.diagnostics["inovelli"][topic] = payload

    async def _publish(
        self, source_key: str, topic: str, payload: PushPayload, *, force: bool
    ) -> bool:
        """Publish to a topic with write-on-change dedup; True if it published."""
        dedup_key = (source_key, topic)
        if not force and self._last_published.get(dedup_key) == payload:
            self.diagnostics["dedup_skips"] += 1
            return False
        if await self._mqtt_publish(topic, payload):
            self._last_published[dedup_key] = payload
            self.diagnostics["last_publish"][topic] = payload
            return True
        return False

    async def _mqtt_publish(self, topic: str, payload: PushPayload) -> bool:
        """Publish a push payload (delegates to the raw publisher)."""
        return await self._raw_publish(topic, dict(payload))

    async def _raw_publish(self, topic: str, payload: dict[str, Any]) -> bool:
        """Publish JSON to MQTT, guarding on broker availability."""
        if not mqtt.mqtt_config_entry_enabled(self.hass):
            self._set_mqtt_available(available=False)
            return False
        try:
            await mqtt.async_publish(self.hass, topic, json.dumps(payload))
        except HomeAssistantError:
            self._set_mqtt_available(available=False)
            return False
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

    # --- sleep overlay (Light-Man-owned global toggle + ramp) ---------------

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
        """Engage/release the sleep overlay — ramped, or snapped on restore."""
        now = dt_util.utcnow()
        if ramp:
            self._sleep_start = self._compute_sleep_s(now)
            self._sleep_changed_at = now
        else:
            self._sleep_changed_at = None  # snap straight to the target
        self.sleep_on = enabled
        await self._run_push(force=True)
        self.async_set_updated_data(self._snapshot())

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
            self._run_sweep(binding.sweep),
            name=f"lm_sweep_{binding.zone}_{binding.name}",
        )

    async def _run_sweep(self, sweep: list[OccupancyStage]) -> None:
        """Light a sensor's stages in order, pausing ``delay_s`` before each."""
        for stage in sweep:
            delay = stage.get(CONF_STAGE_DELAY, 0.0)
            if delay:
                await asyncio.sleep(delay)
            for light in stage.get(CONF_LIGHTS, []):
                payload = self._engine_payload_for(
                    light[CONF_SOURCE], DEFAULT_OCCUPANCY_TRANSITION_S
                )
                if payload is None:
                    continue
                await self._raw_publish(
                    light[CONF_SET_TOPIC], {**payload, "state": STATE_ON}
                )

    async def _maybe_clear_zone(self, zone_key: str) -> None:
        """Turn the zone's off_lights off once all its bindings report no presence."""
        zone = self._occupancy[zone_key]
        if any(
            self._sensor_occupied.get((b.zone, b.name))
            for b in self._bindings_by_zone.get(zone_key, [])
        ):
            return
        self.diagnostics["occupancy"][zone_key] = False
        transition = zone.get(CONF_OFF_TRANSITION, DEFAULT_OCCUPANCY_TRANSITION_S)
        off = {"state": STATE_OFF, "transition": transition}
        for topic in zone.get(CONF_OFF_LIGHTS, []):
            await self._raw_publish(topic, off)

    def _engine_payload_for(
        self, source_key: str, transition: float
    ) -> PushPayload | None:
        """Return the live engine payload for a source (occupancy turn-on)."""
        source = self._config[CONF_SOURCES][source_key]  # loader-validated
        now = dt_util.utcnow()
        try:
            elevation, noon = solar_inputs(self.hass, now)
        except ValueError:
            return None
        local = dt_util.as_local(now)
        now_minutes = local.hour * 60 + local.minute + local.second / 60
        payload, _target = self._source_payload(
            source, elevation, noon, now_minutes, self._compute_sleep_s(now), transition
        )
        return payload

    # --- modes --------------------------------------------------------------

    def _next_solar_midnight(self, now: datetime) -> datetime:
        """Next solar midnight (mode TTL — held looks clear overnight)."""
        return get_astral_event_next(self.hass, SOLAR_MIDNIGHT_EVENT, now)

    def _invalidate_source(self, source_key: str) -> None:
        """Drop the dedup cache for a source (its addressing may have changed)."""
        for dedup_key in [k for k in self._last_published if k[0] == source_key]:
            del self._last_published[dedup_key]

    def _invalidate_room(self, room: str) -> None:
        """Drop the dedup cache for the source that owns ``room``."""
        source_key = self._room_source.get(room)
        if source_key is not None:
            self._invalidate_source(source_key)

    async def async_hold(self, room: str, kind: str) -> None:
        """Hold a room at a look (night/day/manual) and apply it now."""
        now = dt_util.utcnow()
        changed = await self._modes.set_held(
            room, kind=kind, armed_at=now, expires_at=self._next_solar_midnight(now)
        )
        if changed:
            self._invalidate_room(room)
        await self._run_push(force=True, only_source=self._room_source.get(room))
        self.async_set_updated_data(self._snapshot())

    async def async_release_hold(self, room: str) -> None:
        """Resume adaptive for a room and snap it back at once."""
        if not await self._modes.release(room):
            return
        self._invalidate_room(room)
        await self._run_push(force=True, only_source=self._room_source.get(room))
        self.async_set_updated_data(self._snapshot())

    async def async_clear_holds(self) -> None:
        """Resume adaptive everywhere."""
        for room in await self._modes.clear():
            self._invalidate_room(room)
        await self._run_push(force=True)
        self.async_set_updated_data(self._snapshot())

    async def async_force_push(self) -> None:
        """Re-publish every source now, bypassing dedup."""
        await self._run_push(force=True)

    # --- master on/off switch ----------------------------------------------

    async def async_set_push_enabled(self, *, enabled: bool) -> None:
        """Master on/off. ON runs the adaptive push; OFF makes Light Man inert."""
        self.push_enabled = enabled
        if enabled:
            await self._run_push(force=True)
        self.async_set_updated_data(self._snapshot())

    # --- MQTT message handling ---------------------------------------------

    async def _handle_message(self, msg: mqtt.ReceiveMessage) -> None:
        """Route an incoming switch message to action/state handling."""
        topic = msg.topic
        payload = str(msg.payload)
        if topic.endswith(ACTION_SUFFIX):
            await self._on_action(topic[: -len(ACTION_SUFFIX)], payload)
            return
        data = _parse_json(payload)
        if data is None:
            return
        action = data.get("action")
        if isinstance(action, str) and action:
            await self._on_action(topic, action)
        state = data.get("state")
        if isinstance(state, str):
            await self._on_state(topic, state)

    async def _on_action(self, base: str, action: str) -> None:
        """Apply an action's mode intent to the base switch's room."""
        if not self.push_enabled:
            return  # inert when Light Man is off — the blueprint owns taps
        mapping = self._switch_map.get(base)
        if mapping is None:
            return
        _, room = mapping
        mode = action_to_mode(action)
        if mode is None:
            return
        if mode == MODE_ADAPTIVE:
            await self.async_release_hold(room)
        else:
            await self.async_hold(room, mode)

    async def _on_state(self, base: str, state: str) -> None:
        """Track the paddle on/off; turning a held light off ends its hold."""
        mapping = self._switch_map.get(base)
        if mapping is None:
            return
        on = state == STATE_ON
        if self._paddle_on.get(base) == on:
            return
        self._paddle_on[base] = on
        if not self.push_enabled:
            return
        source_key, light_ref = mapping
        # Switching a held light off resumes adaptive (the night/day look should
        # not linger and reappear when the light is turned back on).
        if not on and await self._modes.release(light_ref):
            _LOGGER.info("hold released by switch-off for %s", light_ref)
        self._invalidate_source(source_key)
        await self._run_push(force=True, only_source=source_key)
        self.async_set_updated_data(self._snapshot())

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


def _hhmm(text: str) -> int:
    """Parse ``"HH:MM"`` to clock minutes since midnight."""
    hours, _, minutes = text.partition(":")
    return int(hours) * 60 + int(minutes)


def _curve_inflections(
    curve: SourceProfile,
    samples: list[tuple[int, float]],
    brightness: list[float],
    noon_minute: int | None,
) -> dict[str, dict[str, float]] | None:
    """Locate a curve's day-shape corners for the viz, as ``{name: {t, br}}``.

    The four points the eye looks for: when brightness **starts to ramp up** off
    the night floor, when it **hits peak**, when it **starts to ramp down**, and
    when it **reaches minimum** again. With a ``day_window`` these are exact (its
    start, solar noon, its end, and end + wind-down); without one they're read
    from the sampled curve (floor crossings + the peak). ``None`` for a flat curve.
    """
    times = [minute for minute, _e in samples]
    floor = min(brightness)
    peak = max(brightness)
    if peak - floor < 1.0:
        return None  # a flat curve has no inflections worth marking
    window = curve.get("day_window")
    if (
        isinstance(window, dict)
        and window.get("enabled")
        and window.get("start")
        and window.get("end")
    ):
        start = float(_hhmm(window["start"]))
        end = float(_hhmm(window["end"]))
        wind = float(window.get("wind_down_s", DEFAULT_WIND_DOWN_S)) / 60
        noon = (
            float(noon_minute)
            if noon_minute is not None
            else float(times[brightness.index(peak)])
        )
        points = {
            "ramp_up": start,
            "peak": min(max(noon, start), end),
            "ramp_down": end,
            "minimum": min(end + wind, 1439.0),
        }
    else:
        peak_idx = brightness.index(peak)
        threshold = floor + 0.02 * (peak - floor)
        up = next(
            (times[i] for i, v in enumerate(brightness) if v > threshold), times[0]
        )
        down = next(
            (
                times[i]
                for i in range(peak_idx, len(brightness))
                if brightness[i] <= threshold
            ),
            times[-1],
        )
        points = {
            "ramp_up": float(up),
            "peak": float(times[peak_idx]),
            "ramp_down": float(times[peak_idx]),
            "minimum": float(down),
        }
    return {
        name: {
            "t": round(minute),
            "br": round(_interp_at(times, brightness, minute), 1),
        }
        for name, minute in points.items()
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
    )


def _parse_json(payload: str) -> dict[str, Any] | None:
    """Parse a Z2M JSON state payload, returning None if it is not an object."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None
