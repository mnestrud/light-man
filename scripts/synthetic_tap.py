#!/usr/bin/env python3
"""Synthetic switch taps for acceptance tests (no human at the switch).

Publishes the exact message Z2M emits for a real tap — the switch's cached
attributes (its retained state) plus ``action`` — on the switch's own topic, so
Light Man's path from receipt to bulb runs end to end. Only switches named in
Light Man's bundled config and only known tap actions are allowed; nothing is
published without ``--commit``. The taps blueprint reacts to these too (LED
effects).

  synthetic_tap.py tap 'Office Smart Switch' config_single --commit
  synthetic_tap.py cycles 'Primary Bath Shower Smart Switch' --n 20 --commit
  synthetic_tap.py at 15:00 'Office Smart Switch' config_single --commit

(run with .venv/bin/python)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import live_probe  # noqa: E402  # sibling helper: broker connection
import mqtt_dump  # noqa: E402  # sibling helper: broker credentials

BASE = "zigbee2mqtt"
ACTIONS = {
    "config_single",  # hold night
    "config_double",  # hold day
    "up_single",  # paddle on / release
    "down_single",  # paddle off
}
ARM_TO_RELEASE_S = 15.0
RELEASE_TO_NEXT_S = 15.0
PADDLE_ON_LEAD_S = 2.0


def allowed_switches() -> set[str]:
    """Switch names Light Man maps (from the bundled config)."""
    seed = json.loads(
        (REPO / "custom_components/light_man/light_man_config.json").read_text()
    )
    return {
        sw["topic"].removeprefix(f"{BASE}/")
        for room in seed["rooms"].values()
        for sw in room.get("switches", [])
    }


class Tapper:
    """Holds one broker connection and each switch's latest retained state."""

    def __init__(self) -> None:
        """Connect with the broker credentials HA uses."""
        args = argparse.Namespace(
            ha_config=mqtt_dump.DEFAULT_HA_CONFIG,
            external_host=mqtt_dump.DEFAULT_EXTERNAL_HOST,
            host=None,
            port=None,
            user=None,
            password=None,
        )
        self.client = live_probe.connect(args)
        self.state: dict[str, dict[str, Any]] = {}
        self.ready = threading.Event()
        self.client.on_message = self._on_message
        self.client.loop_start()

    def _on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        try:
            data = json.loads(msg.payload)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict) and "action" not in data:
            self.state[msg.topic] = data
            self.ready.set()

    def tap(self, switch: str, action: str, *, commit: bool) -> None:
        """Publish one tap as Z2M would (cached attributes + action)."""
        topic = f"{BASE}/{switch}"
        if topic not in self.state:
            self.ready.clear()
            self.client.subscribe(topic)
            self.ready.wait(5)
        payload = {**self.state.get(topic, {}), "action": action}
        stamp = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
        if not commit:
            print(f"{stamp} DRY-RUN {topic} action={action}")
            return
        self.client.publish(topic, json.dumps(payload))
        print(f"{stamp} TAP {switch} {action}", flush=True)


def _check(switch: str, action: str | None = None) -> None:
    if switch not in allowed_switches():
        raise SystemExit(f"not a Light Man switch: {switch!r}")
    if action is not None and action not in ACTIONS:
        raise SystemExit(f"not an allowed action: {action!r} (one of {ACTIONS})")


def run_cycles(tapper: Tapper, switch: str, n: int, *, commit: bool) -> None:
    """Arm/release ``n`` times; every second cycle 2 s after a paddle-on."""
    for i in range(n):
        if i % 2:
            tapper.tap(switch, "up_single", commit=commit)
            time.sleep(PADDLE_ON_LEAD_S)
        tapper.tap(switch, "config_single", commit=commit)
        time.sleep(ARM_TO_RELEASE_S)
        tapper.tap(switch, "up_single", commit=commit)
        time.sleep(RELEASE_TO_NEXT_S)


HA_URL = "https://meddle.home.botworth.net"
PADDLE_SETTLE_S = 12.0


def _paddle(switch: str, state: str) -> None:
    """Press the paddle for real: a validated switch state write (binding acts)."""
    subprocess.run(  # noqa: S603  # fixed argv, validated helper
        [
            sys.executable,
            str(REPO / "scripts/mqtt_dump.py"),
            "set",
            switch,
            json.dumps({"state": state}),
            "--commit",
        ],
        check=True,
        capture_output=True,
    )
    stamp = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
    print(f"{stamp} PADDLE {switch} {state}", flush=True)


def _release(room: str) -> None:
    """Release a hold through Light Man's own service (no fake paddle state)."""
    token = Path("~/.ha-token").expanduser().read_text().strip()
    req = urllib.request.Request(  # noqa: S310  # fixed https URL
        f"{HA_URL}/api/services/light_man/release_hold",
        data=json.dumps({"room": room}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):  # noqa: S310
        pass
    stamp = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
    print(f"{stamp} RELEASE {room}", flush=True)


def run_true_cycles(tapper: Tapper, switch: str, room: str, n: int) -> None:
    """Arm/release ``n`` times with the switch's physical state kept true.

    Every second cycle the paddle is really pressed on (switch state write, the
    binding turns the room on) 2 s before the config tap, and off at the end.
    Releases use the ``release_hold`` service, so no synthetic ``up_single``
    leaves Light Man believing the paddle is on while the switch is off.
    """
    for i in range(n):
        if i % 2:
            _paddle(switch, "ON")
            time.sleep(PADDLE_ON_LEAD_S)
        tapper.tap(switch, "config_single", commit=True)
        time.sleep(ARM_TO_RELEASE_S)
        _release(room)
        time.sleep(3.0)
        if i % 2:
            _paddle(switch, "OFF")
        time.sleep(PADDLE_SETTLE_S)


def wait_until(hhmm: str) -> None:
    """Sleep until the next local ``HH:MM``."""
    now = datetime.now().astimezone()
    hour, minute = (int(x) for x in hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    print(f"waiting until {target:%Y-%m-%d %H:%M}", flush=True)
    time.sleep((target - now).total_seconds())


def main() -> None:
    """Parse arguments and run one mode."""
    parser = argparse.ArgumentParser(description="Synthetic switch taps")
    sub = parser.add_subparsers(dest="mode", required=True)
    tap = sub.add_parser("tap")
    tap.add_argument("switch")
    tap.add_argument("action")
    tap.add_argument("--commit", action="store_true")
    cyc = sub.add_parser("cycles")
    cyc.add_argument("switch")
    cyc.add_argument("--n", type=int, default=20)
    cyc.add_argument("--commit", action="store_true")
    true = sub.add_parser("true-cycles")
    true.add_argument("switch")
    true.add_argument("room", help="Light Man room key, e.g. primary_bath.shower")
    true.add_argument("--n", type=int, default=20)
    true.add_argument("--commit", action="store_true")
    at = sub.add_parser("at")
    at.add_argument("time", help="local HH:MM (next occurrence)")
    at.add_argument("switch")
    at.add_argument("action")
    at.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    _check(args.switch, getattr(args, "action", None))
    tapper = Tapper()
    if args.mode == "tap":
        tapper.tap(args.switch, args.action, commit=args.commit)
    elif args.mode == "cycles":
        run_cycles(tapper, args.switch, args.n, commit=args.commit)
    elif args.mode == "true-cycles":
        if not args.commit:
            raise SystemExit("true-cycles presses real paddles; pass --commit")
        run_true_cycles(tapper, args.switch, args.room, args.n)
    else:
        wait_until(args.time)
        tapper.tap(args.switch, args.action, commit=args.commit)
    time.sleep(0.5)
    tapper.client.loop_stop()


if __name__ == "__main__":
    main()
