# Light Man — Adaptive Target Algorithm (Phase 2 design)

Snapshot: 2026-06-08. This is the design for the Light Man adaptive-value engine that **replaces the
HACS Adaptive Lighting "dummy" switches** (Phase 2). It computes per-source `{brightness_pct,
color_temp_kelvin | rgb_color}` from **real solar elevation** at the house's lat/long, feeding the
push (`docs/PLAN.md` §1.4–1.5). Until Phase 2 lands, Phase 1 keeps reading the AL dummy switches; this
engine drops them.

## Location (from HA core config)

| | value |
|---|---|
| latitude / longitude | **41.90845, −87.66669** (Chicago) |
| elevation | 181 m |
| time zone | America/Chicago |

Solar-noon elevation `= 90 − |lat − declination|`:

| | noon elevation | sunrise→sunset (local) | day length |
|---|---|---|---|
| Summer solstice | **71.5°** | ~5:16 → ~8:29 | ~15 h |
| Equinox | 48.1° | ~6:30 → ~6:40 | ~12 h |
| Winter solstice | **24.6°** | ~7:15 → ~4:23 | ~9 h |

Elevation is computed each push from `astral` (bundled with HA, <1 ms). Real elevation gives the true
arc *shape*, *timing*, and *seasonal swing* for free — what AL's time-parabola fakes.

## Why not AL's model

- AL drives everything off a **time-parabola** between sun *events*, not real elevation; forcing
  sunrise/sunset **remaps the event times and distorts the whole curve including noon**.
- Color temp goes **flat at `min_ct` all night** unless `transition_until_sleep` (disabled in the
  current config) → 16 h of identical 2700 K.
- Sleep is a **hard switch to fixed values** — no ramp, no schedule (the perceived "ramp" is only the
  bulb's one-shot `sleep_transition` fade).
- Brightness (tanh, keyed to sunrise/sunset) and color (parabola, keyed to noon/midnight) run on
  **different clocks**.
- Color interpolates in **Kelvin** (reciprocal to white point) and brightness in **raw %** (ignores
  eye gamma) → both perceptually uneven.

## The model — three regimes + a sleep overlay

Compute `e = solar_elevation(now)` (degrees). Locked parameters: **color reference elevation
`REF = 71.5°`** (fixed → seasonal honesty: winter daylight is genuinely warmer); interpolate color in
**mired**, brightness in **perceptual** space.

### 1. Daytime (`e ≥ 0`)
```
ct_pct     = clamp(e / REF, 0, 1)                       # REF = 71.5° (fixed)
color_temp = mired_lerp(min_ct, max_ct, ct_pct)         # interpolate in mired, not Kelvin

br_pct     = clamp(e / (sat · today_noon_elevation), 0, 1)   # sat ≈ 0.5 → max by mid-morning
brightness = perceptual_lerp(min_br, max_br, br_pct)
```
- Color uses the **fixed** REF: summer noon → full `max_ct`; equinox noon (48°) → ~5250 K; **winter
  noon (24.6°) → ~4000 K**. Lights track how daylight actually looks across seasons.
- Brightness normalizes by **today's** noon and saturates early (`sat`) → effectively full brightness
  whenever the sun is meaningfully up, every season (no dim winter middays).

### 2. Dusk / dawn (`0 > e ≥ −18°`, real twilight)
```
tw         = clamp(-e / 18, 0, 1)                       # 0 at horizon → 1 at astronomical dark
color_temp = mired_lerp(min_ct, dusk_floor_ct, tw)      # e.g. 2700 → 2200
brightness = perceptual_lerp(min_br, night_floor_br, tw)
```
Replaces AL's flat night with a wind-down tied to actual twilight.

### 3. Night (`e < −18°`)
Hold the dusk floor (`night_floor_br`, `dusk_floor_ct`/rgb). The **sleep overlay** (below) does the
bedtime work.

### Sleep overlay — a toggle switch (not a schedule)
Light Man exposes `switch.light_man_sleep` (global; optional per-source overrides). On a transition it
ramps; the target is the per-source sleep values:
```
s      = sleep_ramp(now)        # toggle ON → ease 0→1 over ramp_in; held 1; toggle OFF → 1→0 over ramp_out
target = blend(base_target, sleep_target, s)   # sleep_target = sleep_br + (sleep_ct | sleep_rgb per §1.5)
```
- **You** own the schedule (automate the toggle at bedtime, or flip it manually). Light Man owns the
  ramp. Overlays on any regime (afternoon nap works).
- `ramp_in` / `ramp_out` are the "ramp to/from sleep" tunables; per-source sleep targets carry over
  from the current AL config.

## Optional: forced day-window (off by default)

Lets you set your own sunrise/sunset **without distorting midday**, because real `e(t)` is never
remapped — the option only gates *which regime applies*:

- **Off (default):** regimes 1–3 by real `e` end-to-end.
- **On (`day_start`, `day_end` clock times):**
  - before `day_start` → hold pre-dawn/night floor (even if `e ≥ 0`);
  - at `day_start` → ramp (over `edge_transition`) into the **live real-elevation** curve at its
    current value;
  - `day_start … day_end` → **untouched real-elevation daytime** (regime 1 — midday circadian intact);
  - at `day_end` → timed wind-down to night floor over `wind_down_duration` (regardless of real `e`);
  - after → night floor. Sleep overlay applies throughout.

The option clips the **edges**; the midday core is identical to the default. This is the explicit fix
for "let me set sunrise/sunset but don't screw up mid-day circadian."

## Per-source config schema (Phase 2 OptionsFlow)

```jsonc
"overhead": {
  "min_br": 30, "max_br": 90,
  "min_ct": 2700, "max_ct": 6500,
  "sat": 0.5,                       // brightness saturation fraction of today's noon
  "base_color_mode": "color_temp",  // awake/day color: color_temp | rgb
  "base_rgb": null,                 // fixed daytime color when base_color_mode == rgb
  "dusk_floor_ct": 2200, "night_floor_br": 30,
  "sleep": { "br": 30, "color_mode": "color_temp", "ct": 2200, "rgb": null,
             "ramp_in": "90m", "ramp_out": "30m" },
  "day_window": { "enabled": false, "start": "08:00", "end": "16:00",
                  "edge_transition": "30m", "wind_down_duration": "90m" }
}
```
Globals: `REF = 71.5` (fixed), twilight band `−18°`, perceptual brightness + mired color (engine
constants, not per-source).

**Per-regime color mode — explicit configured RGB (generalizes Phase-1 §1.5).** Each source carries a
`base_color_mode` (`color_temp` | `rgb`) for the awake/day target; the sleep target already has its own
`color_mode`/`rgb`. When `base_color_mode == rgb`, the base/day target emits a **fixed configured
`base_rgb`** (brightness still elevation-driven), *not* an elevation-derived `color_temp` — today there
is **no** daytime single-color option, so this is new. Night color comes from `sleep.rgb`. So e.g.
`hallway_up = { base_color_mode: rgb, base_rgb: [r,g,b] }` runs a set day RGB and blends to a set night
RGB (`sleep.rgb`). Both colors are owned by Light Man, replacing the AL switch's `rgb_color` / sleep-rgb
(removed in Phase 2). A source with no rgb in either regime is unaffected (pure `color_temp`).

**Concrete requirement (2026-06-09, from live cutover):** `hallway_up` day target = **sky blue**
(`base_color_mode: rgb`, `base_rgb` ≈ a sky blue — dial in the exact value on the bulbs during Phase 2),
blending to the existing night RGB. Phase 1 cannot do this — in rgb mode it just mirrors the AL switch's
`rgb_color` (currently warm white ~`[255,240,227]`), so the daytime hallway_up is warm-white-rgb until
`base_rgb` lands.

## Carrying the current AL config forward

`min/max_brightness`, `min/max_color_temp`, `sleep_brightness`, `sleep_color_temp` / `sleep_rgb` map
straight to the endpoints above. Dropped: forced 08:00/16:00 (now real, or the optional gate),
`brightness_mode: tanh` (elevation is the curve), flat-night color (now real twilight + sleep ramp).
Fixed in the rewrite: overhead sleep no-op (`sleep_br == min_br` and `sleep_ct == min_ct`), asymmetric
hallway `sleep_transition`.

## Engine (per push)
```
e = solar_elevation(now + transition_lookahead)
base = regime_target(e)                      # regimes 1–3; color = base_rgb if base_color_mode==rgb
if day_window.enabled: base = gate(base, now, day_window)
target = blend(base, sleep_target, sleep_ramp(now))   # sleep color = sleep.rgb when sleep rgb
emit(brightness=perceptual→254, color_temp=mired | rgb)   # → push §1.5
```
Pure math, no I/O, <1 ms — fully unit-testable (elevation is the only input; freeze the clock + lat/long
in tests).

## Open micro-decisions
1. Sleep toggle scope: single global vs per-source switches (default global, per-source sleep targets).
2. `sat` brightness-saturation default (0.5 ≈ full brightness by mid-morning).
3. Twilight band end: astronomical (−18°) vs civil (−6°) for the dusk floor reach.
