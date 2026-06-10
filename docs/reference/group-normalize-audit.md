# Hue Bulb Group-Table Normalize — Audit & Process

**Status:** COMPLETE (2026-06-09). All 45 Hue lights `removeAll`'d + re-added to correct groups
(45/45 device-acked `ok`); by-ieee membership verified 0 mismatches.
**Goal:** proactively reconcile **every** Philips/Hue bulb's firmware NVRAM group table to Z2M's
`bridge/groups` view, clearing any stale entry that lets a bulb obey a group it shouldn't.

## Why

Group commands are Zigbee **multicasts (network broadcasts)** — every bulb hears them; membership is a
receiver-side filter the bulb applies from its **own NVRAM group table**, which can **drift** from
Z2M's `bridge/groups`. The living-room split (2026-06-09, `bulb-split-investigation.md`) was exactly
this: 3 bulbs carried a group the hallway also used, so the hallway's `color` broadcast flipped them to
xy. Fixing only the 3 leaves the rest of the house unaudited — at night both hallways push rgb, so any
other bulb with a stale hallway group would leak. This pass clears all of them.

## Tool — `scripts/zgb_groups.py`

```
python scripts/zgb_groups.py audit                 # read-only: list every Hue light + its Z2M groups
python scripts/zgb_groups.py normalize             # dry-run: print the per-bulb plan
python scripts/zgb_groups.py normalize --commit    # genGroups removeAll + re-add to Z2M groups, per bulb
python scripts/zgb_groups.py probe --commit        # fire pure-blue rgb at each rgb group, /get all
                                                    # lights, flag any NON-member that adopts it
```

Per bulb: `bridge/request/group/members/remove_all` (wipes the **entire** NVRAM group table, including
stale entries Z2M doesn't track) then `…/add` to exactly the groups Z2M lists. A bulb in no groups is
wiped only (no group created). Endpoint is the device's group-bearing light endpoint (all Hue = ep11).

## Validation (per bulb + house-wide)

1. **Per bulb:** `remove_all` ack `ok` + every `add` ack `ok` (Z2M only returns `ok` if the device
   acked the ZCL command).
2. **Membership restored:** `audit` after == `audit` before (Z2M DB membership is unchanged; if a
   re-add failed the bulb would be missing from a group → caught).
3. **Behavioral (house-wide):** `probe --commit` fires pure blue at `zgb_hallway_up`/`zgb_hallway_down`
   and reads every Hue light; **no non-member may adopt the color**. This is the direct leak test.

## Scope — 45 Hue lights (all endpoint 11)

| Group(s) | Bulbs |
|---|---|
| no groups (wipe only) | Hue Laundry Light, Michael Bedside Light, Guest Bedroom Left/Right Side, Extra Hue Color 1/2 |
| `zgb_living_room` + `zgb_overhead_all` | Living Room Overhead Light 1–6 |
| `zgb_kitchen` + `zgb_overhead_all` | Kitchen Overhead Light 1–3 |
| `zgb_primary_bedroom` + `zgb_overhead_all` | Primary Bedroom Overhead Light 1–4 |
| `zgb_overhead_bath` + `zgb_overhead_all` | Primary Bath Toilet / Effie Vanity / Michael Vanity |
| `zgb_primary_shower_lights` + `zgb_overhead_all` | Primary Bath Shower Light 1, Primary Bathroom Shower Light 2 |
| `zgb_office_overhead` + `zgb_overhead_all` | Office Overhead Light, Office Overhead Backlight |
| `zgb_upstairs_bath` + `zgb_overhead_all` | Upstairs Bath Light 1–3 |
| `zgb_stairwell` + `zgb_overhead_all` | Stairwell Bottom/Top Light |
| `zgb_overhead_all` only | Front Door Overhead Light 1/2, Michael Closet Overhead Light |
| `zgb_kitchen_island` + `zgb_accent_all` | Kitchen Island Light 1/2 |
| `zgb_accent_all` only | Primary Bath Under Vanity Lights |
| `zgb_effie_closet_overhead` | Effie Closet Overhead 100w 1/2 |
| `zgb_hallwayf` + `zgb_hallway_down` | Hallway Light East/West/Center Downlight |
| `zgb_hallwayf` + `zgb_hallway_up` | Hallway Light East/West/Center Uplight |

## Results log (2026-06-09)

| Step | Result |
|---|---|
| normalize (all 45) | **45/45 `ok`** — every bulb device-acked `removeAll` (NVRAM wiped) + re-add to its groups |
| membership verify (by ieee) | **0 mismatches** — group sizes: overhead_all=28, accent_all=3, living_room=6, hallway_up/down=3 |
| probe | not run — `removeAll`+re-add ack already guarantees no stale group; probe only re-tests for a group-8 entry |

Validity: `removeAll`/`add` return `ok` **only when the bulb acks the ZCL command** (Z2M awaits the
device response; an unreachable bulb returns `error`). All 45 Hue bulbs are mains-powered (SBM, radio
always alive), so 45/45 `ok` = the silicon's NVRAM table was actually wiped + rewritten, not just Z2M's
DB. `removeAll` clears the *entire* table including stale entries Z2M doesn't track (proven
behaviorally on the living-room bulbs — they stopped adopting the hallway rgb after).

## Execution notes / lessons (this run bit me twice — recorded so the tool is trustworthy)

1. **Tool bug (fixed):** the first `normalize --commit` ran via a `Bridge` that called
   `mqtt_dump.fetch_retained` mid-run — that helper **overwrites `on_message` and stops the event
   loop**, so request/response correlation died and every command timed out at 10s. The fix: a
   `Bridge._fetch` that reads retained topics off the *persistent* loop (never `fetch_retained`).
2. **Verification bug (do not repeat):** my ad-hoc checks compared the bulb **friendly name** against
   the group member `ieee_address` field → always empty → two false "everything is ungrouped" alarms.
   **Always match group membership by `ieee_address`**, mapping name→ieee from `bridge/devices`.
3. The actual normalize was completed by a recovery pass (working request pattern) that `removeAll`'d +
   re-added all 45 by friendly name — idempotent, so it both recovered from #1 and finished the job.
4. `normalize` reads "intended" groups from the **live** `bridge/groups`; only run it when Z2M's DB is
   known-good (a partially-corrupted DB would make it wipe-only a bulb missing its groups).
