![OCPP](https://github.com/home-assistant/brands/raw/master/custom_integrations/ocpp/icon.png)

# OCPP - BG Sync EV

A Home Assistant integration for EV chargers speaking OCPP, **forked for
BG SyncEV / Sync Energy chargers**.

This is [lbbrhzn/ocpp](https://github.com/lbbrhzn/ocpp) with fixes for
behaviour specific to SyncEV hardware. Everything upstream does, this does too.

> Developed and measured against a **SyncEV EVSC7S** (firmware
> `RD0045-V1.02-S1.01`) driven by evcc. Base: upstream **v0.10.18**.

## Why this fork

On stock upstream, **charge-rate control does not work on this charger**. Three
separate causes, all confirmed on real hardware:

| Problem | Effect |
|---|---|
| The charger rejects the stack level upstream sends. It reports `ChargeProfileMaxStackLevel` 5 and refuses every profile at 5, accepting 0–4. | The ceiling and the in-session profile **never applied at all**. |
| OCPP requires charge limits be a multiple of 0.1 A, and upstream sends raw floats. | evcc's values (e.g. `11.653986956521738`) were rejected before leaving Home Assistant, surfacing as **HTTP 500 errors**. |
| The station ceiling was rewritten to the live target on every change. | A stale lower value silently **capped the session** — a 6 A ceiling held a 16 A limit down to 6 A. |

All three are fixed. The charger also floors requests to whole amps (10.9 A
behaves as 10 A), so requests that cannot change anything are no longer sent —
which matters when a solar controller recalculates every few seconds.

## What you get beyond upstream

**Extra controls**, for settings the charger supports but upstream never exposed:

- Max Current (hardware limit), Overvoltage Protection Limit, Connection Timeout,
  Meter Reading Interval
- Indicator LED Brightness
- Auto-Unlock on Unplug
- Enable CT Clamp
- Charge When Plugged In — the charger authorises itself on plug-in, no server
  needed. Note this competes with smart charging rather than complementing it.

**Extra sensors:** House Current and House Voltage from the charger's CT clamp,
which upstream receives and discards.

**Tidier dashboard:** sensors get readable names ("Charging Current" rather than
`Current.Import`), and metrics this charger physically cannot report are hidden
by default instead of sitting permanently `unavailable`.

**Fewer surprises:** no more persistent notification on every restart about a
read-only setting, and reconnecting mid-session no longer interrupts a charge —
that used to trip a `C1249 Too Many Wake-Up Requests` fault on a VW ID.3.

## Installing

Via HACS:

1. HACS → three-dot menu → **Custom repositories**
2. Add `https://github.com/jgogoes/hass_ocpp_bg_sync_ev`, type **Integration**
3. Search for **BG Sync**, download, restart Home Assistant

Or copy `custom_components/ocpp/` into your `config/custom_components/` folder
and restart.

⚠️ This uses the same `ocpp` domain as upstream, so **do not install both** —
they will overwrite each other.

## Will this work on my charger?

Vendor-specific features are gated on the model the charger reports, so on
non-SyncEV hardware they simply do not appear. The charging-profile fixes are
general and should be safe anywhere, though only EVSC7S has been tested.

Confirmed on **EVSC7S**. Expected to apply to other Sync Energy models
(`EVL7PS`, `EVL7MS`, `EVLR7MS`, `EVLS7MS` and their G variants) — untested.

## Documentation

- **[FORK.md](FORK.md)** — what changed and why, including things deliberately
  *not* changed
- **[HARDWARE-FINDINGS.md](HARDWARE-FINDINGS.md)** — the raw measurements every
  fix is based on, so you can check the reasoning rather than trust it

For general OCPP setup, upstream's docs still apply:
[home-assistant-ocpp.readthedocs.io](https://home-assistant-ocpp.readthedocs.io)

## Credit

All the real work is [lbbrhzn](https://github.com/lbbrhzn)'s and the
[contributors](https://github.com/lbbrhzn/ocpp/graphs/contributors) to upstream,
built on the [Python OCPP package](https://github.com/mobilityhouse/ocpp). This
fork is a thin layer of hardware-specific fixes on top.

If you find it useful, consider buying the upstream author a coffee:

<a href="https://www.buymeacoffee.com/lbbrhzn" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/default-black.png" alt="Buy Me A Coffee" width="150px">
</a>
