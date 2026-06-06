# Light Man

Home Assistant custom integration — a house-wide **adaptive-lighting orchestrator** for a
Zigbee2MQTT + Philips Hue + Inovelli Blue (VZM31-SN / VZM32-SN) system.

Light Man is the Python successor to the "Ataraxia Adaptive Lighting" blueprint stack. It owns the
stateful, error-prone parts of adaptive lighting that have outgrown YAML/Jinja:

- **Per-room manual holds** that survive the adaptive tick (the immediate problem this repo solves).
- **Dynamic Zigbee group membership** to exclude held rooms from the consolidated native-`multiColor`
  group floods, with response-checked retries and a reconciler.
- (Later phases) the adaptive tick fan-out, write-on-change dedup, drift-check, and per-source pushes.

Home Assistant owns nothing about the radios — Light Man talks to **Zigbee2MQTT over MQTT only**
(`bridge/request|response` + group `/set`). Z2M keeps owning groups, bindings, `hue_native_control`,
and the Inovelli Smart Bulb Mode bindings.

## Status

Pre-bronze scaffold. See `docs/reference/ARCHITECTURE.md` for the full design, the confirmed regression
this solves, and the current AL YAML it replaces. Implementation plan is being refined.

## Layout

| Path | Role |
|------|------|
| `custom_components/light_man/` | The integration (coordinator, config flow, platforms) |
| `docs/reference/` | Snapshot of the current AL blueprint/automation/Z2M config it supersedes |
| `tests/` | pytest (PHCC) suite |

## Branch / release workflow

- `dev` — default working branch; pushed before each deploy to live HA.
- `main` — stable releases only; merged from `dev` and tagged per numbered release.

## License

MIT — see [LICENSE](LICENSE).
