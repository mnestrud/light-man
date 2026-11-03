#!/usr/bin/env bash
# Fetch live Home Assistant OS logs (journald via the Supervisor HTTP proxy).
# There is NO log file on the config share; this is the only correct source.
#
#   scripts/ha_logs.sh core   [N]        HA core log, current boot, last N entries (default 20000)
#   scripts/ha_logs.sh z2m    [N]        Zigbee2MQTT add-on log (add-on log_level is 'warning')
#   scripts/ha_logs.sh mqtt   [N]        Mosquitto add-on log
#   scripts/ha_logs.sh host   [N]        host (OS) journal
#   scripts/ha_logs.sh lm     [N]        HA core log filtered to light_man, loader notices dropped
#   scripts/ha_logs.sh z2m-errors [N]    Z2M errors, deduped (ids/numbers normalised), with counts
#
# ANSI colour codes are stripped. Output goes to stdout; redirect to a file for analysis.
set -euo pipefail
BASE="${HA_BASE:-https://meddle.home.botworth.net}"
TOKEN="$(cat "${HA_TOKEN_FILE:-$HOME/.ha-token}")"
Z2M_SLUG="${Z2M_SLUG:-45df7312_zigbee2mqtt}"
what="${1:-core}"; n="${2:-20000}"
fetch() { # $1 = supervisor path
  curl -sf -H "Authorization: Bearer $TOKEN" -H "Range: entries=:-${n}:" "$BASE/api/hassio/$1" | sed 's/\x1b\[[0-9;]*m//g'
}
case "$what" in
  core) fetch core/logs/boots/0 ;;
  z2m)  fetch "addons/$Z2M_SLUG/logs" ;;
  mqtt) fetch addons/core_mosquitto/logs ;;
  host) fetch host/logs ;;
  lm)   fetch core/logs/boots/0 | grep 'light_man' | grep -v 'We found a custom integration' || true ;;
  z2m-errors)
    fetch "addons/$Z2M_SLUG/logs" | grep -E '\] error:' \
      | sed -E 's/^\[[^]]+\] //; s/0x[0-9a-f]+/0xID/g; s/[0-9]{3,}/N/g' | cut -c1-200 | sort | uniq -c | sort -rn ;;
  *) echo "unknown: $what" >&2; exit 2 ;;
esac
