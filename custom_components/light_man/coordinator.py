"""Light Man coordinator: the single adaptive brain for the bulb push.

Timer-driven on its own cadence. Each cycle: sweep expired held modes, then (if
Light Man owns the push) publish every source. Per room the target is its live
adaptive value, its held look (night/day), or nothing (off / manually frozen) —
so an off room is never re-on'd and a held look is never clobbered.

Modes are driven only by explicit Inovelli action intents (``config_*`` hold,
single taps release, held-dim freezes) — never inferred from switch on/off
bounce. The single ``push_enable`` switch swaps the whole legacy stack: ON
disables the master tick automation + the a16-a19 booleans and runs Light Man;
OFF restores them and goes inert.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict

from homeassistant.components import mqtt
from homeassistant.const import STATE_ON as HA_STATE_ON
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.sun import get_astral_event_next
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_SUFFIX,
    ATTR_BRIGHTNESS_PCT,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    CONF_AL_SWITCH,
    CONF_LEGACY_ENABLE,
    CONF_PUSH_INTERVAL,
    CONF_ROOMS,
    CONF_SLEEP_SWITCH,
    CONF_SOURCES,
    CONF_TRANSITION,
    DEFAULT_TRANSITION_S,
    MODE_ADAPTIVE,
    SOLAR_MIDNIGHT_EVENT,
    STATE_ON,
    TICK_AUTOMATION,
)
from .models import AdaptiveValues
from .modes import action_to_mode
from .push import (
    build_night_payload,
    build_payload,
    plan_publishes,
    resolve_color_mode,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import StateType

    from .config_loader import ValidatedConfig
    from .models import LightManConfig, PushPayload, SourceConfig
    from .modes import ModeManager

_LOGGER = logging.getLogger(__name__)


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
        self._config = config
        self._modes = modes
        self._switch_map = validated.switch_map
        self._room_source: dict[str, str] = {
            room: source_key
            for source_key, source in config[CONF_SOURCES].items()
            for room in source.get(CONF_ROOMS, {})
        }
        self._last_published: dict[tuple[str, str], PushPayload] = {}
        self._paddle_on: dict[str, bool] = {}
        self._unsubs: list[Any] = []
        self.push_enabled = False
        self.mqtt_available = True
        self.diagnostics: dict[str, Any] = {
            "issues": validated.issues,
            "dedup_skips": 0,
            "addressing": {},
            "last_publish": {},
        }

    # --- lifecycle ----------------------------------------------------------

    async def _async_setup(self) -> None:
        """Subscribe to switch topics; reconcile the legacy stack once HA is up."""
        for base in self._switch_map:
            self._unsubs.append(
                await mqtt.async_subscribe(self.hass, base, self._handle_message)
            )
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass, base + ACTION_SUFFIX, self._handle_message
                )
            )
        # Defer the legacy reconcile until HA has started — the automation /
        # input_boolean services aren't registered yet during entry setup.
        self._unsubs.append(
            async_at_started(self.hass, self._reconcile_legacy_on_start)
        )

    async def _reconcile_legacy_on_start(self, _hass: HomeAssistant) -> None:
        """Put the legacy stack in legacy mode once HA (and its services) are up."""
        await self._reconcile_legacy(enabled=self.push_enabled)

    def shutdown_subscriptions(self) -> None:
        """Unsubscribe all MQTT subscriptions (called on unload)."""
        while self._unsubs:
            self._unsubs.pop()()

    async def async_restore_legacy(self) -> None:
        """Re-enable the legacy stack (tick + booleans) — called on unload."""
        await self._reconcile_legacy(enabled=False)

    def is_known_room(self, room: str) -> bool:
        """Return True if ``room`` exists in the seed config (service validation)."""
        return room in self._room_source

    def config_summary(self) -> dict[str, Any]:
        """Non-secret config shape for diagnostics."""
        return {
            "push_interval_s": self._config[CONF_PUSH_INTERVAL],
            "sources": {
                key: {
                    "al_switch": source.get(CONF_AL_SWITCH),
                    "rooms": sorted(source.get(CONF_ROOMS, {})),
                }
                for key, source in self._config[CONF_SOURCES].items()
            },
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

    # --- push ---------------------------------------------------------------

    async def _run_push(self, *, force: bool, only_source: str | None = None) -> None:
        """Push every source (or just one). No-op unless Light Man owns the push."""
        if not self.push_enabled:
            return
        for key, source in self._config[CONF_SOURCES].items():
            if only_source is not None and key != only_source:
                continue
            await self._push_source(key, source, force=force)

    async def _push_source(
        self, key: str, source: SourceConfig, *, force: bool
    ) -> None:
        """Plan and publish one source's per-room targets."""
        adaptive = self._read_adaptive(source)
        if adaptive is None:
            return
        transition = float(source.get(CONF_TRANSITION, DEFAULT_TRANSITION_S))
        sleeping = self._is_sleeping(source)
        adaptive_payload = build_payload(
            brightness_pct=adaptive["brightness_pct"],
            color_temp_kelvin=adaptive["color_temp_kelvin"],
            rgb_color=adaptive["rgb_color"],
            mode=resolve_color_mode(source, sleeping=sleeping),
            transition=transition,
        )
        rooms = source.get(CONF_ROOMS, {})
        plan = plan_publishes(
            source,
            adaptive_payload=adaptive_payload,
            night_payload=build_night_payload(source, transition),
            modes={r: self._modes.mode_of(r) for r in rooms if self._modes.is_held(r)},
        )
        self.diagnostics["addressing"][key] = [topic for topic, _ in plan]
        for topic, payload in plan:
            await self._publish(key, topic, payload, force=force)

    async def _publish(
        self, source_key: str, topic: str, payload: PushPayload, *, force: bool
    ) -> None:
        """Publish to a topic, applying write-on-change dedup unless forced."""
        dedup_key = (source_key, topic)
        if not force and self._last_published.get(dedup_key) == payload:
            self.diagnostics["dedup_skips"] += 1
            return
        if await self._mqtt_publish(topic, payload):
            self._last_published[dedup_key] = payload
            self.diagnostics["last_publish"][topic] = payload

    async def _mqtt_publish(self, topic: str, payload: PushPayload) -> bool:
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

    # --- adaptive value reads (AL dummy switches are HA-internal, not Zigbee) -

    def _read_adaptive(self, source: SourceConfig) -> AdaptiveValues | None:
        """Read brightness/ct/rgb from the source's AL dummy switch state."""
        state = self.hass.states.get(source[CONF_AL_SWITCH])
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        bri: StateType = state.attributes.get(ATTR_BRIGHTNESS_PCT)
        ct: StateType = state.attributes.get(ATTR_COLOR_TEMP_KELVIN)
        if bri is None or ct is None:
            return None
        return AdaptiveValues(
            brightness_pct=float(bri),
            color_temp_kelvin=float(ct),
            rgb_color=state.attributes.get(ATTR_RGB_COLOR),
        )

    def _is_sleeping(self, source: SourceConfig) -> bool:
        """Return True when the source's sleep switch is on."""
        sleep_switch = source.get(CONF_SLEEP_SWITCH)
        if not sleep_switch:
            return False
        state = self.hass.states.get(sleep_switch)
        return state is not None and state.state == HA_STATE_ON

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

    # --- single-toggle stack switch ----------------------------------------

    async def async_set_push_enabled(self, *, enabled: bool) -> None:
        """Flip the stack: ON runs LM + legacy off; OFF restores legacy."""
        self.push_enabled = enabled
        await self._reconcile_legacy(enabled=enabled)
        if enabled:
            await self._run_push(force=True)
        self.async_set_updated_data(self._snapshot())

    async def _reconcile_legacy(self, *, enabled: bool) -> None:
        """Drive the legacy stack to the inverse of Light Man ownership."""
        await self.hass.services.async_call(
            "automation",
            "turn_off" if enabled else "turn_on",
            {"entity_id": TICK_AUTOMATION},
            blocking=False,
        )
        entity_ids = [
            legacy
            for source in self._config[CONF_SOURCES].values()
            if (legacy := source.get(CONF_LEGACY_ENABLE))
        ]
        if entity_ids:
            await self.hass.services.async_call(
                "input_boolean",
                "turn_off" if enabled else "turn_on",
                {"entity_id": entity_ids},
                blocking=False,
            )

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
            return  # inert in legacy mode — the blueprint owns taps
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
        """Track the paddle on/off and re-push the source on a change."""
        mapping = self._switch_map.get(base)
        if mapping is None:
            return
        on = state == STATE_ON
        if self._paddle_on.get(base) == on:
            return
        self._paddle_on[base] = on
        if not self.push_enabled:
            return
        source_key, _ = mapping
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


def _parse_json(payload: str) -> dict[str, Any] | None:
    """Parse a Z2M JSON state payload, returning None if it is not an object."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None
