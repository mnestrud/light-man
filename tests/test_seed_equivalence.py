"""Behaviour-preserving equivalence: the reconciled seed plans identically.

The v8 data-model migration reorganizes the *stored* shape but must not change a
single ``/set`` publish. The runtime addressing is derived (config_loader), so
this test pins the derived push plan: steady state = exactly the four consolidated
floods, and a hold adds one re-assert of the held light group to its own source's
flood only (0.8.0 addressing; verified live 2026-11-03).
"""

from __future__ import annotations

from typing import Any

from custom_components.light_man import _read_bundled_seed
from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.push import plan_source

# Synthetic looks — addressing is payload-agnostic, so distinct sentinels are
# enough to tell the flood / per-light / skip paths apart.
ADAPTIVE: dict[str, Any] = {"brightness": 200, "transition": 1.0, "color_temp": 250}
DAY: dict[str, Any] = {"brightness": 254, "transition": 1.0, "color_temp": 153}
NIGHT: dict[str, Any] = {"brightness": 76, "transition": 1.0, "color_temp": 370}

CONSOLIDATED = {
    "overhead": "zigbee2mqtt/zgb_overhead_all/set",
    "accent": "zigbee2mqtt/zgb_accent_all/set",
    "hallway_up": "zigbee2mqtt/zgb_hallway_up/set",
    "hallway_down": "zigbee2mqtt/zgb_hallway_down/set",
}


LOOKS = {"night": NIGHT, "day": DAY}


def _plan(source: Any, modes: dict[str, str]) -> list[tuple[str, Any]]:
    _addressing, sends = plan_source(
        source,
        adaptive_payload=ADAPTIVE,
        looks={room: LOOKS[mode] for room, mode in modes.items()},
    )
    return [(s.topic, s.payload) for s in sends]


def test_steady_state_is_four_consolidated_floods() -> None:
    """No holds -> exactly one consolidated flood per source group (4 total)."""
    config = validate_config(_read_bundled_seed()).config
    sources = config["sources"]
    assert set(sources) == set(CONSOLIDATED)
    floods = []
    for key, source in sources.items():
        plan = _plan(source, modes={})
        assert plan == [(CONSOLIDATED[key], ADAPTIVE)]
        floods.extend(plan)
    assert len(floods) == 4


def test_hold_adds_one_reassert_to_its_source_flood() -> None:
    """A night hold on one light group: the source floods, then re-asserts it."""
    config = validate_config(_read_bundled_seed()).config
    overhead = config["sources"]["overhead"]
    plan = _plan(overhead, modes={"kitchen.overhead": "night"})
    assert plan == [
        (CONSOLIDATED["overhead"], ADAPTIVE),
        ("zigbee2mqtt/zgb_kitchen/set", NIGHT),
    ]


def test_other_source_groups_unaffected_by_a_hold() -> None:
    """A hold in one source group never perturbs another group's flood."""
    config = validate_config(_read_bundled_seed()).config
    accent = config["sources"]["accent"]
    assert _plan(accent, modes={"kitchen.overhead": "night"}) == [
        (CONSOLIDATED["accent"], ADAPTIVE)
    ]
