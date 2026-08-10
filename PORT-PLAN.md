# BG Sync EV — port plan onto clean upstream

Generated 2026-08-10. **Target base: `v0.10.18` (latest stable).**

## What was established

| Fact | Value | How it was determined |
|---|---|---|
| Deployed integration | `/Volumes/config/custom_components/ocpp` | live HA config share |
| Its actual base | **`v0.10.15`** (confirmed) | minimum diff across tags v0.10.12–v0.10.19b0: 995 changed lines vs v0.10.15, rising monotonically either side |
| Custom work | 995 changed lines across 8 files | `diff -u` vs `git archive v0.10.15` |
| Clean repo HEAD | `b0f11c9` = **`v0.10.18`** (stable, 2026-08-10) | `git describe --tags HEAD` |
| Upstream drift, base → target | 1246 insertions / 118 deletions | `git diff --stat v0.10.15 v0.10.18` |

`main`, `bg-sync-custom`, `origin/main` and `origin/bg-sync-custom` are all at
`b0f11c9`. The repo was previously on `v0.10.19b0` (beta) and has been moved back
to stable — see [Consequences of choosing stable](#consequences-of-choosing-stable).

### On the `0.8.0` in `manifest.json`

It is a stale placeholder, **not** the version this code is based on. Every tag
from `v0.10.14` to `v0.10.19b0` carries the same `"version": "0.8.0"`; the line
last changed on 2025-07-25 (`2c800b1`). `.github/workflows/update-version.yml`
stamps the real tag in *after* a release publishes, and that commit is not
landing back on `main` — so the git tree keeps the old string while the HACS zip
gets the correct one. Your deployed copy reads `v0.10.15` because it came from a
release zip.

Files touched by the custom work, and by how much:

```
ocppv16.py     355   number.py   232   switch.py   225   sensor.py   109
chargepoint.py  34   const.py     31   enums.py      5   button.py     4
```

`__init__.py`, `api.py`, `config_flow.py`, `ocppv201.py`, `exception.py`,
`services.yaml` and `translations/` are **byte-identical to v0.10.15** — no
custom work there, nothing to port.

Artifacts produced:

- `bgsync-custom-vs-v0.10.15.patch` — the complete extracted diff
- `deployed-snapshot/` — clean copy of the deployed `.py` files (backups/zips stripped)

## Conflict map

Against `v0.10.18`, five of your eight files land on **untouched** upstream code.

| File | Upstream v0.10.15 → v0.10.18 | Verdict |
|---|---|---|
| `button.py` | unchanged | applies clean |
| `enums.py` | unchanged | applies clean |
| `sensor.py` | unchanged | applies clean — all 5 changes |
| `switch.py` | unchanged | applies clean — all 6 changes |
| `ocppv16.py` | unchanged | applies clean — all 10 changes |
| `const.py` | +12, different region | applies clean |
| `chargepoint.py` | +10/−5, in `stop()` only | applies clean — `post_connect` untouched |
| `number.py` | **+95/−12, `async_set_native_value` rewritten (#2052)** | **the sole real conflict** |

---

## Change-by-change

### 1. `button.py` — cosmetic renames · **apply as-is**

`Reset` → `Restart Charger`, `Unlock` → `Unlock Connector`.

### 2. `const.py` — two new constants · **apply as-is**

- `DEFAULT_LIGHT_INTENSITY = 100`
- `SYNCEV_VENDOR_KEY_MODELS` — 9 model strings, only `EVSC7S` live-tested

Upstream added `CONF_OCPP_VERSION` / `OCPP_VERSIONS` to this file. Different
region; no textual conflict.

### 3. `enums.py` — two enum additions · **apply as-is**

- `HAChargerDetails`: `ct_clamp_current`, `ct_clamp_voltage`
- `OcppMisc`: `vendor_id`, `vendor_error_code`, `info`

`ConfigurationKey.light_intensity = "LightIntensity"` already exists upstream —
nothing to add for that.

### 4. `chargepoint.py` — two `post_connect` guards · **apply as-is**

- Wrap `set_standard_configuration()` in try/except so a slow charger's timeout
  no longer aborts `post_connect` and leaves `post_connect_success=False` in a retry loop
- Skip `set_availability()` when `active_transaction_id != 0`, to stop the
  mid-session CP-signal toggle that triggered VW ID.3 `C1249` wake-up faults

Verified: upstream's only change to this file in the range is `stop()` gaining a
`try/finally` around task cancellation. `post_connect` is untouched.

### 5. `sensor.py` — all five changes apply as-is

Upstream has not modified `sensor.py` between v0.10.15 and v0.10.18.

- `SENSOR_NAME_OVERRIDES` (36 friendly names)
- `DISABLED_BY_DEFAULT_METRICS` (18 metrics) + `entity_registry_enabled_default=`
- `CHARGER_ONLY` / `CONNECTOR_ONLY` reordering
- **The local `_uid()` fix** (`key.lower().replace(".", "_")`) — **keep it.**
  Without it the stale-entity cleanup silently never matches any dotted metric
  (`Status.Connector` and friends). Upstream fixes this properly in v0.10.19b0
  by single-sourcing `sensor_unique_id()` in `const.py`, but that does not exist
  on v0.10.18, so your fix is still doing real work here.
- The stale-entity cleanup loop for `DISABLED_BY_DEFAULT_METRICS` in the
  single-connector branch

> When you eventually move to v0.10.19b0 or later, drop the `_uid()` fix and
> delegate to `sensor_unique_id()` — see [Deferred](#deferred-until-you-move-past-v01018).

### 6. `switch.py` — all six changes apply as-is

Upstream has not modified `switch.py` since v0.10.15.

- Renames: `Charge Control` → `Charging`, `Availability` → `Charger Available`
- `OcppSwitchDescription` gains `ocpp_key` / `ocpp_on_value` / `ocpp_off_value` / `model_gated`
- Three new `ChangeConfiguration`-backed switches:
  - `UnlockConnectorOnEVSideDisconnect` → "Auto-Unlock on Unplug"
  - `GetCTClampValue` (1/0) → "Enable CT Clamp"
  - `ChargerMode` (3/1, model-gated) → "Charge When Plugged In"
- `REMOVED_SWITCH_KEYS` cleanup for 4 dead switches (2 confirmed `readonly:true`
  on your hardware, `local_auth_list_enabled` a no-op without `SendLocalList`)
- `_raw_value` / `_apply_readback()` / `_ocpp_refresh()` / `_ocpp_configure()` —
  read-back reconciliation, because `ChargerMode=3` reads back as `2`
- Retry-until-resolved refresh on the dispatcher tick (fixes the switch being
  stuck on its constructor default after an HA restart)

⚠️ The deployed `switch.py` has **no trailing newline**. Add one when porting.

### 7. `ocppv16.py` — all ten changes apply as-is

Upstream has not modified `ocppv16.py` between v0.10.15 and v0.10.18.

| # | Change | Why it exists |
|---|---|---|
| O1 | Reassert `LightIntensity` on connect (model-gated) | charger resets its LED to full bright on every power-cycle |
| O2 | `configure(..., notify_on_readonly=False)` + early-return on readonly | stops a persistent HA notification for `ClockAlignedDataInterval` on every restart |
| O3 | `_rate_unit_cache` | `ChargingScheduleAllowedChargingRateUnit` is `readonly:true`; re-querying 4×/min saturated the request queue until `GetConfiguration` timed out |
| O4 | `_applied_limits` no-op cache + invalidation on boot/start-tx/stop-tx | evcc issues a new limit every ~15s; each cost 3 round-trips |
| O5 | `stack_level = 0` hardcoded | charger advertises a higher `ChargeProfileMaxStackLevel` than it accepts; rejects with `NotSupported` above 0 |
| O6 | `Relative` → `Absolute` profile kind | charger rejects Relative (`PropertyConstraintViolation`) — see lbbrhzn/ocpp#1565 |
| O7 | `ChargePointMaxProfile` pinned to hardware max, not live target | composite schedule takes the **minimum** across purposes, so a stale ceiling silently capped the session at ~10–12 A |
| O8 | `round(limit, 1)` | OCPP schema enforces `multipleOf 0.1`; evcc's raw floats got rejected locally |
| O9 | `vendorId`/`vendorErrorCode`/`info` → `extra_attr` on the error-code sensor | surfaces e.g. `errorCode=OtherError, vendorErrorCode="CP abnormal"` without debug logging |
| O10 | `on_data_transfer` parses vendor `energy.sync` / `GetCTClampValue` JSON into CT clamp sensors, then calls `update()` | readings were arriving all along; nothing told HA to refresh |

⚠️ **O2 is a real behaviour change, not just logging.** The custom `configure()`
adds a `return` after the readonly branch. Upstream logs the warning and then
*still sends* `ChangeConfiguration`. Intentional, but note it applies to *all*
readonly keys, not just the one it was written for.

### 8. `number.py` — the one that genuinely conflicts

**Apply as-is:**

- Rename `Maximum Current` → `Charging Current (Live)`
- `OcppNumberDescription` gains `ocpp_key` / `mode` / `model_gated`
- Six new `ChangeConfiguration`-backed numbers: `MaxCurrent`,
  `UpperLimitProtectionVoltage`, `ConnectionTimeOut`, `MeterValueSampleInterval`,
  `GridCurrentInterval`, `LightIntensity` (model-gated)
- `REMOVED_NUMBER_KEYS` cleanup (`get_ct_clamp_value` moved to a switch)
- `available()` model gating
- Passing `ocpp_key` / `mode` / `model_gated` through both description-rebuild sites

**Must be re-derived — do not paste:**

Upstream PR **#2052 ("Revert the current limit when the charger refuses it")** is
in v0.10.18 and rewrote `async_set_native_value` end to end. The two versions
disagree on the core contract:

| | your v0.10.15 fork | v0.10.18 |
|---|---|---|
| On rejection | keep optimistic value, log a warning | revert to `_confirmed_value` and **raise `HomeAssistantError`** |
| State tracking | none | `_confirmed_value`, `_request_seq`, `_accepted_seq` |
| Restore on restart | value only | value **and** `_confirmed_value` |

Confirmed by inspection: `set_max_charge_rate_amps` → `ChargePoint.set_charge_rate`
in `ocppv16.py` returns a **bool** and never raises `HomeAssistantError`. So on
OCPP 1.6 the `if not ok:` branch is the live path — **every charger rejection now
raises into HA**. With evcc driving the slider every ~15s, a charger that starts
refusing will generate repeated HA errors where the old fork was silent.

Recommended shape — keep upstream's logic, insert yours before the send:

1. `ocpp_key` branch first. Config-key numbers have nothing to do with charge
   rate; `return` before touching `_request_seq` / `_confirmed_value`.
2. `round(float(value), 1)` — keep. Upstream does `float(value)` with no rounding
   and relies on `native_step=1`; evcc passes raw floats, so drop this and the UI
   shows `11.6672652173913` again.
3. `CHARGE_RATE_STEP` / `_quantise_rate` no-op suppression — keep, placed *after*
   the optimistic `async_write_ha_state()` and *before* `set_max_charge_rate_amps`.
   Suppressed requests must `return` **without** touching `_accepted_seq` or
   `_confirmed_value` (nothing was sent, so nothing was confirmed).
4. Everything after the send is upstream's — revert-on-refusal, sequence guard,
   `_confirmed_value` update. Delete `_last_sent_rate` and read `_confirmed_value`
   instead; it is the same quantity, now restored across restarts too.

**Open question:** do you want upstream's raise-on-refusal at all? Keeping it is
the safer default (a slider showing 6 A while the charger runs unrestricted is
genuinely dangerous), but it changes how your setup behaves under evcc. If you'd
rather keep the old silent behaviour, that is a one-line deviation to document,
not a merge conflict.

⚠️ The deployed `number.py` also has **no trailing newline**.

---

## Consequences of choosing stable

Moving from `v0.10.19b0` to `v0.10.18` drops exactly two commits:

- `b075a59` — "Refresh only the sensors a write actually touched" (#2059)
- `83ddd05` — "Scope the test-fixture hazards out, and fix the migration bug they hid" (#2060)

| | v0.10.18 | v0.10.19b0 |
|---|---|---|
| `sensor_unique_id()` in `const.py` | absent | present |
| `_async_refresh_metric_entities()` | absent | present |
| `number.py` raise-on-refusal (#2052) | present | present |

Net effect: **the port gets simpler.** On the beta, upstream had started
modifying `sensor.py` and `ocppv16.py` — the two files carrying your largest
patches. On v0.10.18 both are untouched, so they apply verbatim. The cost is that
you keep maintaining two small fixes upstream has since solved.

### Deferred until you move past v0.10.18

Revisit these when you next rebase onto v0.10.19+ :

- **`sensor.py`** — delete the local `_uid()` and delegate to
  `sensor_unique_id()` from `const.py`. Upstream's docstring explicitly warns
  against hand-mirrored copies of that format; yours is one.
- **`ocppv16.py` O10** — replace `self.hass.async_create_task(self.update(...))`
  in the CT clamp handler with
  `self._async_refresh_metric_entities([cdet.ct_clamp_current.value, cdet.ct_clamp_voltage.value])`,
  avoiding a full device-registry walk at CT-clamp rate.
- Upstream also changed `ChargePointMetric._attr_unique_id` to build from
  `entity_description.metric` rather than `.key`. Same output for your entities,
  but worth knowing before debugging any "entity went unavailable after upgrade".

---

## Suggested sequence

Six commits on `bg-sync-custom`, smallest-risk first, so a bisect is meaningful:

1. `const.py` + `enums.py` — constants and enum members only, no behaviour
2. `button.py` + `sensor.py` — naming, disabled-by-default, stale cleanup
3. `chargepoint.py` — the two `post_connect` guards
4. `switch.py` — the `ocpp_key` switch framework + 3 switches + cleanup
5. `ocppv16.py` — charging-profile fixes (O1–O9) and CT clamp (O10)
6. `number.py` — the config-key numbers, then the `async_set_native_value` merge

Commits 1–5 are verbatim applications. Commit 6 is the only one requiring
judgement. Commit 5 is where the actual charging behaviour lives (stack level,
Absolute kind, ceiling pinning) — if charging regresses after the port, that is
the commit.

## Decisions still needed

- ~~**Base**~~ — resolved: `v0.10.18` stable, `b0f11c9`.
- **`manifest.json`**: leaving `0.8.0` means HACS will think your fork is
  permanently on 0.8.0 and never offer updates. Pick a version scheme for
  `bg-sync-custom` before publishing.
- **Raise-on-refusal**: adopt #2052's behaviour, or preserve the old silent
  optimistic UI?
