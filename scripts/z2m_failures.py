#!/usr/bin/env python3
"""Zigbee2MQTT publish-failure report from Z2M's own file logs (long-term baseline).

Z2M writes one log directory per start under ``<ha-config>/zigbee2mqtt/log/``; at
``log_level: warning`` they hold every failed publish since the first start
(2026-07-01 on this system), far longer than the journal keeps.

Failure kinds (zigbee-herdsman ember adapter, v11.2.0):
  busy             ``status=BUSY`` on a group send — coordinator broadcast table full
  unconfirmed      ``timed out after 10000ms`` — command APS-acked, ZCL response lost
  delivery_failed  ``Delivery failed`` / ``Delivery of MULTICAST failed`` /
                   ``Failed to send request with status=``
  other            anything else after ``failed:``

Usage:
  .venv/bin/python scripts/z2m_failures.py [--since YYYY-MM-DD] [--until YYYY-MM-DD]
      [--target NAME] [--days] [--hours] [--bursts N] [--json]

Read-only. ``--holds`` adds, for each day, the hours a Light Man hold was active
(from the HA recorder database, opened read-only).
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_HA_CONFIG = Path("/mnt/ha-config")
HOLDS_ENTITY = "sensor.light_man_active_holds"
BURST_GAP_S = 300

_LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]\s*\w+:\s*(.*)$")
_FAIL = re.compile(r"Publish '\w+' '\w*' to '([^']+)' failed: (.*)$")


@dataclass(frozen=True)
class Failure:
    """One failed publish line."""

    when: datetime
    target: str
    kind: str


def classify(error: str) -> str:
    """Map herdsman's error text to a failure kind."""
    if "status=BUSY" in error:
        return "busy"
    if "timed out after" in error:
        return "unconfirmed"
    if (
        "Delivery failed" in error
        or "Delivery of MULTICAST failed" in error
        or "Failed to send request with status=" in error
    ):
        return "delivery_failed"
    return "other"


def read_failures(ha_config: Path) -> list[Failure]:
    """Parse every Z2M file log under the config share, oldest first."""
    out: list[Failure] = []
    for log in sorted((ha_config / "zigbee2mqtt" / "log").glob("*/log.log")):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _LINE.match(line)
            if not m:
                continue
            f = _FAIL.search(m.group(2))
            if not f:
                continue
            when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007  # Z2M logs local time
            out.append(Failure(when, f.group(1), classify(f.group(2))))
    out.sort(key=lambda item: item.when)
    return out


def hold_hours_by_day(ha_config: Path) -> dict[str, float]:
    """Hours per local day with at least one Light Man hold active (recorder)."""
    db = ha_config / "home-assistant_v2.db"
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    cur = con.cursor()
    row = cur.execute(
        "select metadata_id from states_meta where entity_id = ?", (HOLDS_ENTITY,)
    ).fetchone()
    if row is None:
        return {}
    rows = cur.execute(
        "select last_updated_ts, state from states where metadata_id = ? "
        "order by last_updated_ts",
        (row[0],),
    ).fetchall()
    hours: dict[str, float] = collections.defaultdict(float)
    for (t0, state), (t1, _next) in zip(rows, [*rows[1:], (None, None)], strict=True):
        try:
            active = float(state) > 0
        except TypeError, ValueError:
            active = False
        if not active:
            continue
        start = datetime.fromtimestamp(t0)  # noqa: DTZ006  # local, to match Z2M logs
        end = datetime.fromtimestamp(t1) if t1 else datetime.now()  # noqa: DTZ005, DTZ006
        while start < end:
            midnight = (start + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            stop = min(end, midnight)
            hours[start.strftime("%Y-%m-%d")] += (stop - start).total_seconds() / 3600
            start = stop
    return dict(hours)


def bursts(times: list[datetime]) -> list[tuple[datetime, datetime, int]]:
    """Group failures separated by more than BURST_GAP_S into bursts."""
    out: list[tuple[datetime, datetime, int]] = []
    for t in times:
        if out and (t - out[-1][1]).total_seconds() <= BURST_GAP_S:
            first, _last, n = out[-1]
            out[-1] = (first, t, n + 1)
        else:
            out.append((t, t, 1))
    return out


def _report_days(failures: list[Failure], args: argparse.Namespace) -> None:
    """Per-day failure counts, optionally with hold hours from the recorder."""
    holds = hold_hours_by_day(args.ha_config) if args.holds else {}
    if args.since:
        holds = {d: h for d, h in holds.items() if d >= args.since}
    if args.until:
        holds = {d: h for d, h in holds.items() if d < args.until}
    suffix = " / hold h" if holds else ""
    print(f"\nper day (busy / unconfirmed / other{suffix}):")
    per_day: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    for f in failures:
        per_day[f.when.strftime("%Y-%m-%d")][f.kind] += 1
    for day in sorted(set(per_day) | set(holds)):
        c = per_day.get(day, collections.Counter())
        other = c["delivery_failed"] + c["other"]
        extra = f"  {holds.get(day, 0.0):5.1f}" if holds else ""
        print(f"  {day}  {c['busy']:4d} {c['unconfirmed']:4d} {other:4d}{extra}")


def report(failures: list[Failure], args: argparse.Namespace) -> None:
    """Print the requested views."""
    if not failures:
        print("no failures in range")
        return
    print(f"{len(failures)} failures {failures[0].when} to {failures[-1].when}")
    kinds = collections.Counter(f.kind for f in failures)
    print("by kind:", dict(kinds))
    by_target = collections.Counter((f.target, f.kind) for f in failures)
    print("\nby target:")
    for (target, kind), n in by_target.most_common():
        print(f"  {n:5d}  {kind:15s} {target}")
    if args.days or args.holds:
        _report_days(failures, args)
    if args.hours:
        per_hour = collections.Counter(f.when.hour for f in failures)
        print(
            "\nper hour:", " ".join(f"{h:02d}:{n}" for h, n in sorted(per_hour.items()))
        )
    if args.bursts:
        print(f"\nlast {args.bursts} bursts (> {BURST_GAP_S} s apart):")
        for first, last, n in bursts([f.when for f in failures])[-args.bursts :]:
            print(f"  {first:%m-%d %H:%M}-{last:%H:%M}  x{n}")


def main() -> None:
    """Parse arguments, filter, and report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ha-config", type=Path, default=DEFAULT_HA_CONFIG)
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--target", default=None)
    parser.add_argument("--kind", default=None)
    parser.add_argument("--days", action="store_true")
    parser.add_argument("--hours", action="store_true")
    parser.add_argument("--holds", action="store_true")
    parser.add_argument("--bursts", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    failures = read_failures(args.ha_config)
    if args.since:
        failures = [f for f in failures if f.when >= datetime.fromisoformat(args.since)]
    if args.until:
        failures = [f for f in failures if f.when < datetime.fromisoformat(args.until)]
    if args.target:
        failures = [f for f in failures if f.target == args.target]
    if args.kind:
        failures = [f for f in failures if f.kind == args.kind]
    if args.json:
        counts = collections.Counter(f.kind for f in failures)
        print(json.dumps({"total": len(failures), "by_kind": dict(counts)}))
        return
    report(failures, args)


if __name__ == "__main__":
    main()
