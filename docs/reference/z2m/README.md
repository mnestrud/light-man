# Zigbee2MQTT external converter (retired 2026-11-10)

**Retired.** Z2M 2.x never loaded it: `bridge/converters` was `[]` and no bulb exposed `on_level`
(2.x reads only the `external_converters/` folder, not the old `external_converters:` key). The key was
removed from `configuration.yaml` on 2026-11-10 (snapshot `backups/2026-11-10-z2m-tune/`); every bulb runs
the upstream definition. The text below is the historical note.

`philips_datura.mjs` is a verbatim copy of the external converter Zigbee2MQTT loads on the live system
(`/mnt/ha-config/zigbee2mqtt/philips_datura.mjs`, listed under `external_converters` in Z2M's
`configuration.yaml`; file dated 2026-04-15). It is tracked here so it is versioned and recoverable.

What it does: re-declares nine Philips Hue models — every bulb Light Man drives — with
`philips.m.light(..., levelConfig: {features: ['on_level']})`, and adds the Datura ceiling light
(`929003736701`).

- `on_level` (ZCL `genLevelCtrl` 0x0011) was added for the legacy AL script's turn-on prestage race.
  Nothing writes it today.
- The Datura definition is now upstream in zigbee-herdsman-converters.
- It imports an internal converters path, so a Z2M upgrade can break it, and a Z2M reinstall drops it.

All A0 bulb measurements in the 2026-11 plan were taken **with this converter loaded**. Whether to keep
or retire it is decided last (plan Workstream E), using those measurements as the regression check.

Editing rule: the live file is the source Z2M runs. Change it only as a deliberate Z2M config change
(snapshot first, confirm, restart Z2M), then copy it back here.
