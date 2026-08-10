# BG Sync EV fork of lbbrhzn/ocpp

A fork of [lbbrhzn/ocpp](https://github.com/lbbrhzn/ocpp) carrying fixes for
**SyncEV / Sync Energy** chargers, developed against a **SyncEV EVSC7S**
(serial `SL320S647`, firmware `RD0045-V1.02-S1.01`) driven by evcc.

**Base: upstream `v0.10.18`.** Every change is additive or a narrowly scoped
fix; no upstream behaviour is removed.

> Every claim below was measured on real hardware on 2026-08-10, not inferred.
> Full method and raw responses are in [`HARDWARE-FINDINGS.md`](HARDWARE-FINDINGS.md).

## Why this fork exists

On stock upstream, **charge-rate control does not work on this charger**. Three
independent causes:

| # | Cause | Effect |
|---|---|---|
| 1 | `ChargeProfileMaxStackLevel` is an **exclusive** bound. The charger reports 5 and rejects every profile purpose at 5 with `NotSupported`, accepting 0–4. Upstream sends the reported value verbatim. | `ChargePointMaxProfile` and `TxProfile` **never applied**. Only `TxDefaultProfile` worked, and only because upstream sends it at `max(1, level - 1)` by coincidence. |
| 2 | OCPP enforces `multipleOf 0.1` on the schedule limit, validated locally by the `ocpp` library. | A raw float from evcc (`11.653986956521738`) raised `FormatViolationError` and never reached the charger. Under upstream #2052 this surfaces as an **HTTP 500**. |
| 3 | The composite schedule takes the **minimum** across purposes, and upstream rewrites the ceiling to the live target on every call. | Any moment the ceiling held an older, lower value silently capped the session. Measured: a 6 A ceiling capped a 16 A `TxProfile` to 6 A. |

## What is fixed

- **Stack level clamped** to `reported_max - 1` rather than sent verbatim.
  Clamping instead of hardcoding 0 keeps purpose priority usable on chargers
  that honour it.
- **Limits rounded** to one decimal place before transmission.
- **Ceiling pinned** to the configured hardware maximum; the Tx-purpose
  profiles do the modulating.
- **No-op suppression** — the charger floors to whole amps (10.0/10.1/10.5/10.9 A
  all yield 10 A), so a request flooring to the same amp as the last accepted
  one is not sent. evcc recomputes a target every cycle; most are no-ops.
- **Readonly config keys** return early instead of sending a
  `ChangeConfiguration` that cannot succeed, and no longer raise a persistent
  notification on every restart for a key written during setup.
- **`post_connect` hardened** — a slow `set_standard_configuration` is no longer
  fatal, and `set_availability` is skipped during an active transaction (it
  toggles the CP pilot signal, which raises `C1249 Too Many Wake-Up Requests`
  on a VW ID.3 and drops the charge).

### What was investigated and deliberately *not* changed

- **`Relative` → `Absolute` profile kind.** Not needed. Every accepted profile
  used `Relative`; the `NotSupported` responses were caused entirely by the
  stack level. An earlier version of this fork changed it on a misdiagnosis.
- **`GridCurrentInterval` as an entity.** Writable and reads back correctly, but
  has no observable effect — the CT clamp push stays at 30 s whether the key is
  10, 30 or 60, and does not latch when the feature is toggled. A control that
  silently does nothing is worse than none.
- **Extra measurands.** `MeterValuesSampledData` is writable, but adding `SoC`,
  `Frequency` or `Power.Factor` is rejected with `NotSupported`. The charger
  samples exactly six measurands and no more, which is why the others are
  registered disabled by default.

## Added entities

| Entity | OCPP key | Notes |
|---|---|---|
| `number.*_max_current_config` | `MaxCurrent` | hardware limit |
| `number.*_upper_limit_protection_voltage` | `UpperLimitProtectionVoltage` | |
| `number.*_connection_timeout` | `ConnectionTimeOut` | |
| `number.*_meter_value_sample_interval` | `MeterValueSampleInterval` | |
| `number.*_light_intensity` | `LightIntensity` | model-gated; hidden key |
| `switch.*_unlock_connector_on_ev_side_disconnect` | `UnlockConnectorOnEVSideDisconnect` | |
| `switch.*_get_ct_clamp_value` | `GetCTClampValue` | genuinely starts/stops the stream |
| `switch.*_charge_on_plug_in` | `ChargerMode` | model-gated; see below |
| `sensor.*_current_ctclamp` / `_voltage_ctclamp` | — | parsed from a vendor `DataTransfer` |

Sensors are given readable names, and metrics the charger cannot report are
registered disabled by default.

## Two hardware quirks worth knowing

**The charger lies about `ChargerMode`.** Written to `3` it replies `Accepted`
and then reports the key as `2`. The mode nonetheless takes effect — verified by
unplugging and replugging the car and observing an unprompted `StartTransaction`
with idTag `freeIdTag` and no `RemoteStartTransaction` from HA. So a read-back
mismatch proves neither that a write was applied nor that it was rejected. Modes
2 and 3 are indistinguishable by reading, so the commanded value is treated as
authoritative and the reported value is exposed as a `raw_value` attribute.

Note `ChargerMode 3` self-starts a session on **every** plug-in, so it competes
with smart charging rather than complementing it.

**A hidden key space.** `GetConfigurationMaxKeys` is 25 and the dump returns
exactly 25 keys, so it is capped. `LightIntensity` and `ClockAlignedDataInterval`
are both readable by name but absent from it. Assume other keys exist.

## Installing

Copy `custom_components/ocpp/` into your Home Assistant `config/custom_components/`
and restart. If your config share is SMB, note that deletes are often blocked —
`cp -f` works where `rm` and `tar -x` do not.

## Upstream divergences to retire on the next rebase

- `sensor.py` keeps a local `_uid()` that replaces dots with underscores.
  Upstream fixes the same bug properly in `v0.10.19b0` by single-sourcing
  `const.sensor_unique_id()`. Delete the local copy when rebasing past v0.10.18.
- The CT clamp handler calls the full `update()`. `v0.10.19b0` adds
  `_async_refresh_metric_entities()`, which should be used instead.

## Still unverified

- Whether the charger resets `LightIntensity` on power-cycle — the entire
  justification for reasserting it on connect. Needs a reboot to confirm.
- Whether `GridCurrentInterval` latches at boot.
- Vehicle state of charge is **not available from this charger** — the `SoC`
  measurand is rejected. It has to come from the vehicle API (e.g. an evcc
  vehicle definition), not from OCPP.
