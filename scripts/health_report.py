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

  .venv/bin/python scripts/health_report.py --since 2026-11-03T20:00 \
      --capture FILE [--from 2026-11-03T20:01] [--to ...] [--operator-actions N]

``--from`` / ``--to`` limit the capture rows to one build's hours. Light Man's own
counters (forced floods, latency) reset at every HA start, so run the report on
the build that is live and set ``--since`` to its start.
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
import capture_checks  # noqa: E402  # sibling helper: checks on a live_probe capture
import ha_ws  # noqa: E402  # sibling helper: authenticated HA websocket
import z2m_failures  # noqa: E402  # sibling helper: Z2M file-log parser

CAP = 8
LIVE_STORE = Path("/mnt/ha-config/.storage/light_man_config")


def _stamp(text: str | None) -> float | None:
    return (
        None if text is None else datetime.fromisoformat(text).astimezone().timestamp()
    )


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


def main() -> None:
    """Print the budget table; exit non-zero if any row fails."""
    parser = argparse.ArgumentParser(description="Light Man network-health report")
    parser.add_argument(
        "--since", required=True, help="local time, e.g. 2026-11-03T08:00"
    )
    parser.add_argument("--capture", type=Path, default=None)
    parser.add_argument("--from", dest="start", default=None, help="capture rows from")
    parser.add_argument("--to", dest="end", default=None, help="capture rows until")
    parser.add_argument(
        "--operator-actions",
        type=int,
        default=0,
        help="force_push / master-on uses since HA start (each is one forced flood)",
    )
    parser.add_argument("--store", type=Path, default=LIVE_STORE)
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
            f"{1 + args.operator_actions} (boot + operator actions)",
            state.get("forced_floods") == 1 + args.operator_actions,
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
    p95 = latency.get("p95_ms")
    rows = rows_missing + rows
    taps = 0
    if args.capture:
        config = json.loads(args.store.read_text())["data"]
        capture_rows, taps = capture_checks.all_rows(
            args.capture,
            config,
            _stamp(args.start),
            _stamp(args.end),
            float(config["push_interval_s"]),
            int(state.get("ignored_config_taps", 0)),
        )
        rows += capture_rows
    rows.append(
        (
            f"interactive latency p95 = {p95} ms (Light Man, since HA start)",
            "<= 300 ms; no samples only if no taps",
            p95 <= 300 if p95 is not None else taps == 0,
        )
    )
    width = max(len(r[0]) for r in rows)
    ok = True
    for label, target, passed in rows:
        ok = ok and passed
        print(f"{'PASS' if passed else 'FAIL'}  {label:<{width}}  target {target}")
    print("\nOVERALL", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
