# SyncEV EVSC7S (SL320S647) — live OCPP 1.6 characterisation

Tested 2026-08-10, 15:05–15:22 BST, against **vanilla upstream v0.10.18** with an
active transaction (`1786370988`) and a VW ID.3 attached. evcc disabled for the
duration (`select.evcc_garage_mode` = `off`).

Firmware `RD0045-V1.02-S1.01`. Vendor `Sync Energy`. Model `EVSC7S`.

> Everything below is observed on the wire, not inferred. Where it contradicts
> the July 2026 comments in the old fork, the contradiction is called out.

---

## 1. Charging profiles — the stack level rule

`ChargeProfileMaxStackLevel` = **5** (`readonly: true`).

| Purpose | connectorId | stackLevel | Kind | Result |
|---|---|---|---|---|
| ChargePointMaxProfile | 0 | 5 | Relative | ❌ `NotSupported` |
| ChargePointMaxProfile | 0 | 4 | Relative | ✅ `Accepted` |
| ChargePointMaxProfile | 0 | 0 | Relative | ✅ `Accepted` |
| TxProfile | 1 | 5 | Relative | ❌ `NotSupported` |
| TxProfile | 1 | 0 | Relative | ✅ `Accepted` |
| TxDefaultProfile | 1 | 5 | Relative | ❌ `NotSupported` |
| TxDefaultProfile | 1 | 4 | Relative | ✅ `Accepted` |

**Rule: stackLevel must be 0–4. Level 5 — exactly the advertised maximum — is
rejected for every purpose.** The charger treats `ChargeProfileMaxStackLevel` as
an exclusive bound (a count, effectively), not an inclusive one.

Upstream's bug is using the reported value directly:

```python
stack_level_resp = await self.get_configuration(
    ckey.charge_profile_max_stack_level.value
)
stack_level = int(stack_level_resp)          # -> 5, always rejected
```

Vanilla partially escapes this only because `TxDefaultProfile` is sent at
`max(1, stack_level - 1)` = 4, which lands inside the valid range by accident.
`ChargePointMaxProfile` and `TxProfile` are both sent at 5 and **always fail**.

### ⚠️ Correction to the July notes

The old fork's `O6` change — `Relative` → `Absolute` — is **not needed**. Every
`Accepted` row above used `chargingProfileKind: "Relative"`. The charger has no
objection to Relative profiles. The `NotSupported` responses were caused
*entirely* by stackLevel 5, and the July comment misattributed them to profile
kind (citing lbbrhzn/ocpp#1565, which does not apply here).

`O5` was also stated too strongly: "only stackLevel 0 is accepted" is wrong;
0 through 4 all work.

**Recommended fix** — clamp rather than hardcode:

```python
# ChargeProfileMaxStackLevel is an exclusive bound on this charger: the
# advertised value itself is rejected with NotSupported for every profile
# purpose. Verified on SL320S647 2026-08-10 (max=5: levels 0-4 accepted,
# 5 rejected). Clamping instead of hardcoding 0 keeps the priority ordering
# between purposes available on chargers that honour it.
stack_level = max(0, reported_max - 1)
```

Retaining a spread (e.g. TxProfile above TxDefaultProfile) is still possible
within 0–4 if you want purpose priority to mean something.

## 2. Charge rate value format

### 2a. Schema rejection on raw floats — **confirmed, and now worse**

Setting `11.653986956521738`:

```
FormatViolationError: Decimal('11.653986956521738') is not a multiple of 0.1
Failed validating 'multipleOf' in
  schema[...]['chargingSchedulePeriod']['items']['properties']['limit']:
  {'type': 'number', 'multipleOf': Decimal('0.1')}
```

All three profile calls failed. The request never reaches the charger — the
`ocpp` library rejects it locally.

**This is now a user-visible failure.** Under v0.10.18's #2052, the rejection
raises `HomeAssistantError`, which surfaced as an **HTTP 500** on the service
call. The old fork logged a warning and moved on. evcc pushes exactly this kind
of value every ~15 s.

`round(float(value), 1)` before building the schedule fixes it. Keep it.

### 2b. Sub-amp granularity — the charger FLOORS to whole amps

Swept `TxProfile` limit in 0.1 A steps at `MeterValueSampleInterval=10`,
ceiling and TxDefaultProfile both parked at 32 A so the TxProfile was the only
binding constraint:

| Requested | `Current.Offered` | `Current.Import` |
|---|---|---|
| 10.0 | 10 | 9.86 – 9.91 |
| 10.1 | 10 | 9.87 – 9.91 |
| 10.5 | 10 | 9.75 – 9.90 |
| 10.9 | 10 | 9.87 – 9.89 |
| 13.0 | 13 | 12.69 – 12.75 |
| 13.4 | 13 | 12.74 – 12.78 |

**10.9 A behaves as 10 A, not 11 A** — floor, not round. `Current.Import` is
reported to 2 dp and is flat across each group, so this is genuine hardware
behaviour, not a reporting artefact. Draw settles ~0.1–0.3 A under the offered
value, which is normal EV pilot behaviour.

This reproduces the July measurement exactly and validates `CHARGE_RATE_STEP =
1.0` with floor semantics: a request that floors to the same whole amp as the
last one sent cannot change anything and is not worth an OCPP round-trip.

## 3. Full configuration key set

`GetConfigurationMaxKeys` = **25**, and the unfiltered dump returns exactly 25
keys — **so the dump is capped and hides keys**. `LightIntensity` is readable by
name (`value: "100"`, `readonly: false`) but absent from the dump. July finding
confirmed; assume other hidden keys exist.

`ClockAlignedDataInterval` is also absent from the dump but returns `"0"` when
queried by name.

### Writable (`readonly: false`) — candidates for entities

| Key | Value | In old fork? |
|---|---|---|
| `MaxCurrent` | 32 | ✅ number |
| `UpperLimitProtectionVoltage` | 254 | ✅ number |
| `ConnectionTimeOut` | 180 | ✅ number |
| `MeterValueSampleInterval` | 60 | ✅ number |
| `GridCurrentInterval` | 30 | ✅ number |
| `LightIntensity` | 100 | ✅ number (hidden key) |
| `GetCTClampValue` | 1 | ✅ switch |
| `ChargerMode` | 1 | ✅ switch |
| `UnlockConnectorOnEVSideDisconnect` | true | ✅ switch |
| `LocalAuthListEnabled` | true | ❌ removed — still writable, but see note |
| `HeartbeatInterval` | 3600 | ❌ **new candidate** |
| `LocalAuthorizeOffline` | true | ❌ **new candidate** |
| `MeterValuesSampledData` | see below | ❌ **new candidate — highest value** |
| `CommunicationNetwork` | 4 | ❌ (you have an `input_number` helper) |
| `OCPPAdminMode` | 0 | ❌ (you have an `input_number` helper) |
| `ClockAlignedDataInterval` | 0 | ❌ (hidden key) |

### Read-only (`readonly: true`) — never expose as writable

`AuthorizeRemoteTxRequests` (False), `GetConfigurationMaxKeys` (25),
`StopTransactionOnEVSideDisconnect` (true), `StopTransactionOnInvalidId` (true),
`SupportedFeatureProfiles`, `LocalAuthListMaxLength` (100),
`SendLocalListMaxLength` (10), `ChargeProfileMaxStackLevel` (5),
`ChargingScheduleMaxPeriods` (8), `MaxChargingProfilesInstalled` (5),
`OEM_RSSI`, `ChargingScheduleAllowedChargingRateUnit` (`Current,Power`).

✅ The old fork's `REMOVED_SWITCH_KEYS` decision is **vindicated**:
`StopTransactionOnEVSideDisconnect` and `StopTransactionOnInvalidId` are both
genuinely `readonly: true`. `LocalAuthListEnabled` is writable, but the fork's
separate argument still holds — nothing implements `SendLocalList`, so the list
is permanently empty and the key has no practical effect.

### `MeterValuesSampledData` is the interesting one

```
Energy.Active.Import.Register, Power.Active.Import, Current.Offered,
Current.Import, Voltage, Temperature
```

Six measurands, and it is **writable**. This is the authoritative reason the
other metrics read `unavailable` — the charger simply isn't sampling them. That
justifies `DISABLED_BY_DEFAULT_METRICS` empirically rather than by observation
of dead entities, and it opens the option of *adding* measurands rather than
just hiding the missing ones.

## 4. Caching opportunities confirmed

Every `set_charge_rate` call issues two `GetConfiguration` round-trips first:

- `ChargingScheduleAllowedChargingRateUnit` → `Current,Power`, `readonly: true`
- `ChargeProfileMaxStackLevel` → `5`, `readonly: true`

Both are `readonly: true`, so neither can change while the connection is up.
With evcc setting a limit every ~15 s that is 8 wasted round-trips per minute,
on top of 3 `SetChargingProfile` calls of which 2 are guaranteed to fail. The
old fork's `_rate_unit_cache` (`O3`) is justified; extend it to cover the stack
level too.

## 5. CT clamp — vendor DataTransfer is live

The charger pushes this unprompted (`GetCTClampValue` = 1):

```json
{"vendorId":"energy.sync","messageId":"GetCTClampValue",
 "data":"{\"current\":-5510,\"voltage\":245900}"}
```

Milliamps and millivolts. Negative current = **export** (you were exporting
5.51 A at the time). Vanilla ACKs it and discards the payload — confirming `O10`'s
premise. Values observed: `3680`/`245100`, `2770`/`244700`, `-5510`/`245900`.

### 5a. `GetCTClampValue` is a real on/off switch ✅

| Value | Behaviour |
|---|---|
| `0` | Stream **stops completely**. Last message 15:32:18, silence for >2 min. |
| `1` | Stream resumes within ~30 s (first message 15:34:53). |

The fork's `switch.<cpid>_get_ct_clamp_value` is doing genuine work.

### 5b. `GridCurrentInterval` does **nothing** ❌ — correction to July notes

Cadence is hard-wired at **exactly 30 s regardless of the value written**:

| `GridCurrentInterval` | Observed cadence | Sample timestamps |
|---|---|---|
| 30 (baseline) | 30 s | 15:29:08, 15:29:38, 15:30:08 |
| **10** | **30 s** | 15:31:18, 15:31:48, 15:32:18 |
| 10 (after off→on toggle) | **30 s** | 15:34:53, 15:35:23, 15:35:53 |
| **60** | **30 s** | 15:37:13, 15:37:43, 15:38:13, 15:38:43, 15:39:13 |

The write succeeds and reads back correctly (`10`, then `60`), so the key is
accepted and stored — it simply has no observable effect on the push rate. A
one-off 80 s gap after writing `60` was the `ChangeConfiguration` disturbing the
timer, not a new cadence; it reverted to 30 s immediately after.

Also tested whether the interval latches when the feature is enabled — set
`GridCurrentInterval` = 10 while disabled, then re-enabled. Still 30 s.

**Consequence:** the fork's `number.<cpid>_grid_current_interval` ("CT Clamp
Report Interval") is **cosmetic**. It writes a value the charger stores and
ignores. The July comment ("~30 for fast reporting, 0/high for the ~60s
default — confirmed via live testing 2026-07-07") is contradicted: 10, 30 and 60
all produce identical 30 s cadence.

> Not fully excluded: the key might only take effect after a charger reboot.
> Worth one retest after the next power-cycle before deciding to drop the entity.

## 6. `MeterValuesSampledData` — writable, but the measurand set is fixed

Attempted to add `SoC`, `Frequency`, `Power.Factor`:

```
ChangeConfiguration -> NotSupported
WARNING: NotSupported while setting MeterValuesSampledData to
  Energy.Active.Import.Register,...,Temperature,SoC,Frequency,Power.Factor
```

Read-back confirmed the value was unchanged. But a **valid subset was accepted** —
writing the six minus `Temperature` stuck and read back correctly, then was
restored. So:

- The key is genuinely writable (not readonly-in-disguise)
- Measurands can be **removed**
- Unsupported measurands cannot be **added** — rejected with `NotSupported`
- The supported set is exactly those six

**This settles `DISABLED_BY_DEFAULT_METRICS` definitively.** SoC, frequency,
power factor and the export/reactive measurands aren't merely unreported — the
charger refuses to sample them at all. Hiding those entities is correct, and
there is no capability to unlock by extending the list.

> Note the shape of this failure: `configure()` returned without raising, the
> service call reported success, and only a read-back revealed nothing changed.
> That is exactly the silent-write failure the fork's `_apply_readback()` in
> `switch.py` was built to catch — good evidence for keeping that pattern.

## 7. `O7` ceiling pinning — **confirmed**, both directions

Now testable with valid stack levels. `TxProfile` held at 16 A throughout;
only `ChargePointMaxProfile` (stackLevel 4, connectorId 0) varied:

| Ceiling | `Current.Offered` | `Current.Import` |
|---|---|---|
| 6 A | **6.0** | 6.09 |
| 32 A | **16.0** | 15.63 |

The composite schedule takes the **minimum across purposes**: a 6 A ceiling caps
the session to 6 A even with a 16 A TxProfile active, and raising the ceiling
immediately releases it back to the TxProfile value. Fully reversible — no
stickiness.

So `O7`'s reasoning is sound: if `ChargePointMaxProfile` is rewritten to the live
target on every call, any moment where it holds an older, lower value silently
caps the session. Pinning the ceiling to the configured hardware maximum and
letting the Tx-purpose profiles modulate is the right design.

## 8. `ChargerMode` — works, but misreports its own value

Written and read back with a transaction active:

| Written | `ChangeConfiguration` reply | Reads back as |
|---|---|---|
| `2` | `Accepted` | **2** |
| `3` | `Accepted` | **2** ← misreport, not coercion (see §8a) |
| `1` | `Accepted` | **1** |

The July observation ("ChargerMode=3 reads back as 2") is reproduced exactly.
Mode changes did not disturb the running session (tx `1786370988` continued
uninterrupted at ~15.7 A throughout).

My first reading of this — that the write was being silently coerced — was
**wrong**, and §8a shows why.

### 8a. ✅ `ChargerMode = 3` **DOES work** — it just lies about its own value

Read-back is `2` whether or not a transaction is active, so read-back alone
proves nothing. The decisive test is **behavioural**: charge stopped, `ChargerMode`
written to `3` (reporting `2`), then the cable physically unplugged and replugged
with **no HA involvement whatsoever**:

```
16:01:30.024  receive  StatusNotification  {status:"Preparing"}        <- plug-in
16:01:31.061  receive  StartTransaction    {idTag:"freeIdTag", ...}    <- CHARGER self-authorised
16:01:31.066  send     [3, {transactionId:1786374091, status:"Accepted"}]
16:01:32.971  receive  StatusNotification  {status:"Charging"}
```

**No `RemoteStartTransaction` was sent by HA.** `StartTransaction` arrives as a
`receive` — the charger initiated and authorised it itself, with the synthetic
idTag `freeIdTag`. This is Plug-and-Charge, working exactly as July described.

**So the July finding is vindicated and the earlier entry in this document was
wrong.** The correct statement is:

> Writing `ChargerMode = 3` is applied and takes effect, but the charger
> subsequently **reports the key as `2`**. An `Accepted` response plus a
> read-back of `2` is *not* evidence the write was coerced.

| Written | Reply | Reads back | Actual behaviour |
|---|---|---|---|
| `1` | `Accepted` | `1` | App/OCPP control (default) |
| `2` | `Accepted` | `2` | No self-start observed |
| `3` | `Accepted` | **`2`** | **Plug-and-Charge — self-authorises on plug-in** |

### 8b. Design consequence — read-back is ambiguous for this key

Mode 2 and mode 3 are **indistinguishable by read-back**: both report `2`. So
`_apply_readback()` must not be trusted to determine on/off state for
`ChargerMode`.

The fork's existing code already does the right thing by accident: with
`ocpp_on_value="3"` and `ocpp_off_value="1"`, a reported `2` matches neither, so
it falls into the warning branch, **keeps `_state` as last commanded**, and
exposes `2` via the `raw_value` attribute. That behaviour is correct and should
be kept.

What should change is the framing: this is a **known, expected quirk of this
key**, not an anomaly worth a `_LOGGER.warning` on every refresh. Suggest either
downgrading to debug for `ChargerMode` specifically, or adding an explicit
"ambiguous read-back" declaration to `OcppSwitchDescription` so the description
itself records that `2` means "either 2 or 3 — trust the commanded value".

> Also worth noting: because mode 3 self-starts on every plug-in, it will fight
> evcc for control. It is a genuine either/or with smart charging, not an
> additive feature.

### 8b. TxProfile dies with its transaction — confirmed

After stopping and restarting the charge, `Current.Offered` jumped to **32 A**:
the `TxProfile` (id 9003, bound to tx `1786370988`) was discarded when that
transaction ended, leaving only the 32 A ceiling and TxDefaultProfile in force.

This directly validates the fork's `_invalidate_charging_profile_cache()` call in
`on_stop_transaction` — a cached "already applied" limit for a TxProfile is void
once the transaction ends, and trusting it would leave the next session
uncapped. **Note the failure mode is unsafe-by-default: the limit goes *up*, not
down.**

This is the single strongest argument for the fork's `_apply_readback()` design:
an `Accepted` response is not evidence the write took effect.

## 9. `LightIntensity` — writable, confirmed

| Written | Reads back |
|---|---|
| `30` | `30` ✅ |
| `100` | `100` ✅ |

`readonly: false`, accepts and retains arbitrary values, despite being absent
from the capped `GetConfiguration` dump. Restored to `100`.

## 10. Vehicle state of charge — not available, and why

| Source | Status |
|---|---|
| OCPP `SoC` measurand | ❌ charger returns `NotSupported` (§6) |
| `sensor.evcc_garage_vehicle_soc` | Exists, reads **0** |
| `select.evcc_garage_vehicle_name` | **`null`** — no vehicle assigned to the loadpoint |
| `sensor.evcc_garage_vehicle_range` / `_odometer` | 0 / 0 |
| Volkswagen HA integration | ❌ none installed |
| `sensor.evcc_battery_soc` (=100) | Home battery (Sunsynk), **not the car** |

The charger cannot supply SoC — proven, not assumed. evcc already has the
plumbing but no vehicle is configured, so every vehicle field reads zero.

**Routes to real SoC, best first:**

1. **Configure the ID.3 in `evcc.yaml`** as a `vw`/`id` vehicle with We Connect
   credentials. `sensor.evcc_garage_vehicle_soc` then populates from VW's API,
   and evcc gains proper SoC-target charging.
2. **Configure an "offline" vehicle in evcc** with just the battery capacity —
   evcc estimates SoC from energy delivered. No credentials, no API, but drifts
   and needs a known starting point.
3. **A HA VW integration** (e.g. `myvolkswagen` / `volkswagen_we_connect_id` via
   HACS) — gives SoC as a native HA sensor independent of evcc.

None of these involve the OCPP integration, so none of them affect the port.

## 11. Untested / still open

- **Plug-and-charge on a fresh plug-in** — needs a physical unplug/replug (§8a).
- **`O1` (LightIntensity reassert on boot)** — the *write* is confirmed working
  (§9); what remains untested is whether the charger resets it to a bright
  default on power-cycle, which is the entire justification for reasserting it
  on every `post_connect`. Needs a charger reboot.

> **`O2` resolved — rationale confirmed.** `ClockAlignedDataInterval` returns
> `{"value": "0", "readonly": true}`. It is a *hidden* key (absent from the
> capped 25-key dump) but genuinely read-only, and `set_standard_configuration`
> writes it on every connect — so upstream raises a persistent HA notification
> on every restart. Suppressing the notification for known-readonly keys, and
> returning early rather than sending a `ChangeConfiguration` that cannot
> succeed, are both justified.
- **`GridCurrentInterval` after a reboot** — see §5b; it may only latch on boot.
- **Hidden config keys** — the 25-key cap means the key space is not fully
  enumerated. `LightIntensity` and `ClockAlignedDataInterval` were found only by
  guessing names.
- **`O2` (`notify_on_readonly`)** — the original rationale was that
  `ClockAlignedDataInterval` is readonly. It is not in the dump at all, and
  returns `"0"` when queried. Re-derive the justification before porting; the
  early-`return` on readonly is a behaviour change affecting all keys.
- **`ChargerMode` values** — still reads `1`. The `3` = Plug-and-Charge mapping
  was inferred behaviourally in July, not vendor-documented, and was not
  re-tested today.
- Hidden keys beyond `LightIntensity` and `ClockAlignedDataInterval` — the
  25-key cap means the key space is not fully enumerated.

## 7. Net effect on the port plan

| Change | Status after live test |
|---|---|
| O5 stack level | ✅ needed — but **clamp to `max-1`**, not hardcode 0 |
| O6 Relative→Absolute | ❌ **drop entirely** — refuted |
| O8 round to 1 dp | ✅ needed — now causes HTTP 500s, not just warnings |
| `CHARGE_RATE_STEP` floor quantisation | ✅ validated at 0.1 A resolution |
| O3 rate-unit cache | ✅ needed — extend to stack level |
| O10 CT clamp | ✅ needed — data confirmed arriving |
| `REMOVED_SWITCH_KEYS` | ✅ vindicated by readonly flags |
| `DISABLED_BY_DEFAULT_METRICS` | ✅ proven — charger refuses to sample them |
| O7 ceiling pinning | ✅ **confirmed** — 6 A ceiling caps a 16 A TxProfile |
| `switch.get_ct_clamp_value` | ✅ real on/off control |
| `number.grid_current_interval` | ❌ **cosmetic** — writes are stored and ignored |
| `_apply_readback()` pattern | ✅ **strongly** vindicated — `ChargerMode=3` returns `Accepted` then coerces to `2` |
| `number.light_intensity` | ✅ writable, confirmed 30 ↔ 100 |
| `switch.charge_on_plug_in` (ChargerMode 3) | ✅ **works** — self-authorises on plug-in with `freeIdTag`; read-back is ambiguous, trust commanded value |
| `_invalidate_charging_profile_cache()` on stop_transaction | ✅ confirmed — TxProfile dies with its transaction, limit fails *upward* |
| O1 (reassert on boot), O2 | ⏸ rationale needs re-deriving |

**Two fixes — clamped stack level and 1 dp rounding — should restore full
charge-rate control including the ceiling profile.** That is a much smaller
change than the original plan assumed, and one of the ten `ocppv16.py` changes
turns out to be unnecessary.

---

## Test environment notes

- `MeterValueSampleInterval` was temporarily set to `10` for the granularity
  sweep and **restored to `60`** afterwards.
- Test profiles `9001` (ChargePointMaxProfile) and `9003` (TxProfile) were left
  installed at 32 A / 16 A respectively; `2001` (TxDefaultProfile, upstream's
  own id) at 32 A. `MaxChargingProfilesInstalled` = 5, so 3 slots are in use.
  Run `ocpp.clear_profile` to reset.
- evcc remains **disabled**. Do not re-enable until the 1 dp rounding is in —
  every fractional set it makes will throw an HTTP 500.
- Debug logging remains enabled for `custom_components.ocpp` and `ocpp`.
