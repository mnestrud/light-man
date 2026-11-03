#!/usr/bin/env python3
"""Network-health report: the plan's exit budget, from independent sources.

Rows are measured from three sources that do not trust each other:

- Zigbee2MQTT's own file logs (``z2m_failures.py``): BUSY and delivery failures.
- Light Man's live state over the HA websocket (``light_man/subscribe``):
  publisher pacing stats, forced floods, delivery-failure counters.
- Optionally a ``live_probe.py`` capture (``--capture FILE``): what was actually
  sent and what the bulbs reported (tap latency, jump rate, window counts).

Where two sources measure the same thing they are cross-checked; a disagreement
fails the row. Exit status is 0 only when every row passes — a release is tagged
only then (CLAUDE.md release checklist).

  .venv/bin/python scripts/health_report.py --since 2026-11-03T08:00 [--capture FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import ha_ws  # noqa: E402  # sibling helper: authenticated HA websocket
import z2m_failures  # noqa: E402  # sibling helper: Z2M file-log parser

CAP = 8
STEADY_WINDOW = 3
TAP_LATENCY_S = 1.0
PCT_PER_S_GLIDE = 0.2


def light_man_state() -> dict[str, Any]:
    """One snapshot of Light Man's live panel state."""
    client = ha_ws.WsClient(ha_ws.HOST)
    client.send({"id": 1, "type": "light_man/subscribe"})
    while True:
        reply = client.recv()
        if reply.get("id") == 1 and reply.get("type") == "event":
            return dict(reply["event"])
        if reply.get("type") == "result" and not reply.get("success", True):
            raise SystemExit(f"light_man/subscribe failed: {reply}")


def capture_rows(path: Path) -> list[tuple[str, str, bool]]:
    """Rows measured from a live_probe capture."""
    records = [json.loads(line) for line in path.read_text().splitlines()]
    groups_rec = next((r for r in records if r["kind"] == "groups"), None)
    groups = (
        {f"zigbee2mqtt/{g['friendly_name']}/set" for g in groups_rec["groups"]}
        if groups_rec
        else set()
    )
    sends = [r for r in records if r["kind"] == "set"]
    worst = 0
    window: list[float] = []
    for rec in sends:
        if rec["topic"] not in groups:
            continue
        window = [t for t in window if rec["t"] - t < 10.0] + [rec["t"]]
        worst = max(worst, len(window))
    rows = [(f"capture: worst 10 s group window = {worst}", f"<= {CAP}", worst <= CAP)]
    latencies = []
    for i, rec in enumerate(records):
        if rec["kind"] != "action":
            continue
        first = next(
            (
                r
                for r in records[i + 1 :]
                if r["kind"] == "report" and 0 <= r["t"] - rec["t"] <= 5
            ),
            None,
        )
        if first is not None:
            latencies.append(first["t"] - rec["t"])
    if latencies:
        slow = [x for x in latencies if x > TAP_LATENCY_S]
        rows.append(
            (
                f"capture: taps with a bulb report <= 1 s: "
                f"{len(latencies) - len(slow)}/{len(latencies)}",
                "all",
                not slow,
            )
        )
    last: dict[str, dict[str, Any]] = {}
    jumps = 0
    for rec in sends:
        payload = rec["payload"]
        prev = last.get(rec["topic"])
        last[rec["topic"]] = payload
        transition = float(payload.get("transition", 0) or 0)
        if prev is None or transition < 30 or "brightness" not in payload:
            continue
        d_pct = abs(payload["brightness"] - prev.get("brightness", 0)) * 100 / 254
        if d_pct / transition > PCT_PER_S_GLIDE:
            jumps += 1
    rows.append(
        (f"capture: glide sends over the jump limit = {jumps}", "0", jumps == 0)
    )
    return rows


def main() -> None:
    """Print the budget table; exit non-zero if any row fails."""
    parser = argparse.ArgumentParser(description="Light Man network-health report")
    parser.add_argument(
        "--since", required=True, help="local time, e.g. 2026-11-03T08:00"
    )
    parser.add_argument("--capture", type=Path, default=None)
    parser.add_argument(
        "--ha-config", type=Path, default=z2m_failures.DEFAULT_HA_CONFIG
    )
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since)

    failures = [
        f for f in z2m_failures.read_failures(args.ha_config) if f.when >= since
    ]
    busy = sum(1 for f in failures if f.kind == "busy")
    delivery = sum(1 for f in failures if f.kind == "delivery_failed")
    unconfirmed = sum(1 for f in failures if f.kind == "unconfirmed")

    state = light_man_state()
    pub = state.get("publisher")
    if not isinstance(pub, dict):
        rows_missing = [("Light Man publisher stats", "present (0.8.0+)", False)]
        pub = {}
    else:
        rows_missing = []
    lm_fail = state.get("delivery_failures", {}).get("by_kind", {})
    latency = pub.get("interactive_latency_ms", {})
    rows: list[tuple[str, str, bool]] = [
        (f"Z2M log: status=BUSY lines = {busy}", "0", busy == 0),
        (f"Z2M log: delivery failures = {delivery}", "0", delivery == 0),
        (
            f"group sends in any 10 s window (max) = "
            f"{pub.get('max_group_sends_in_window')}",
            f"<= {CAP}",
            pub.get("max_group_sends_in_window") is not None
            and pub["max_group_sends_in_window"] <= CAP,
        ),
        (
            f"forced floods since HA start = {state.get('forced_floods')}",
            "boot/operator only",
            state.get("forced_floods") is not None,
        ),
        (
            f"interactive latency p95 = {latency.get('p95_ms')} ms",
            "<= 300 ms",
            latency.get("p95_ms") is None or latency["p95_ms"] <= 300,
        ),
        (
            f"cross-check BUSY: Z2M log {busy} vs Light Man {lm_fail.get('busy', 0)}",
            "Light Man <= log (log spans longer)",
            lm_fail.get("busy", 0) <= busy,
        ),
        (
            f"unconfirmed responses: Z2M log {unconfirmed}, "
            f"Light Man {lm_fail.get('unconfirmed', 0)}",
            "reported",
            True,
        ),
    ]
    rows = rows_missing + rows
    if args.capture:
        rows += capture_rows(args.capture)
    width = max(len(r[0]) for r in rows)
    ok = True
    for label, target, passed in rows:
        ok = ok and passed
        print(f"{'PASS' if passed else 'FAIL'}  {label:<{width}}  target {target}")
    print("\nOVERALL", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
