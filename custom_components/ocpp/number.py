"""Number platform for ocpp."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
import logging
import math
import re
from typing import Final

from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    NumberExtraStoredData,
    RestoreNumber,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import slugify

from .api import CentralSystem
from .chargepoint import session_default_profile_id, session_profile_id
from .const import (
    CHARGE_RATE_STEP,
    CONF_NUM_CONNECTORS,
    CONF_CPID,
    CONF_CPIDS,
    CONF_MAX_CURRENT,
    CONF_SESSION_LIMIT_DEFAULT_PROFILE,
    DATA_UPDATED,
    DEFAULT_LIGHT_INTENSITY,
    DEFAULT_MAX_CURRENT,
    DEFAULT_SESSION_LIMIT_DEFAULT_PROFILE,
    DOMAIN,
    ICON,
    SYNCEV_VENDOR_KEY_MODELS,
)
from .enums import HAChargerDetails, Profiles

MAX_SESSION_CONNECTORS = 10
_TX_DEFAULT_PROFILE = "TxDefaultProfile"
_LOGGER: logging.Logger = logging.getLogger(__package__)


@dataclass
class OcppNumberDescription(NumberEntityDescription):
    """Class to describe a Number entity."""

    initial_value: float | None = None
    # BG Sync fork: when set, the entity writes an OCPP configuration key via
    # ChangeConfiguration rather than setting a charge rate.
    ocpp_key: str | None = None
    # NumberMode.BOX gives a plain input instead of a slider.
    mode: NumberMode | None = None
    # Only offered on chargers whose reported model is in
    # SYNCEV_VENDOR_KEY_MODELS.
    model_gated: bool = False


def _quantise_rate(value: float) -> float:
    """Floor a requested rate to the charger's control granularity.

    See CHARGE_RATE_STEP in const.py: this charger floors rather than rounds,
    so 10.9 A behaves as 10 A. Comparing floored values rather than applying a
    deadband means no control resolution is lost -- every step the hardware can
    actually make still gets through.
    """
    if not CHARGE_RATE_STEP:
        return value
    return math.floor(value / CHARGE_RATE_STEP) * CHARGE_RATE_STEP


ELECTRIC_CURRENT_AMPERE = UnitOfElectricCurrent.AMPERE
ELECTRIC_POTENTIAL_VOLT = UnitOfElectricPotential.VOLT
TIME_SECONDS = UnitOfTime.SECONDS

NUMBERS: Final = [
    OcppNumberDescription(
        key="maximum_current",
        name="Charging Current (Live)",
        icon=ICON,
        initial_value=DEFAULT_MAX_CURRENT,
        native_min_value=0,
        native_max_value=DEFAULT_MAX_CURRENT,
        native_step=1,
        native_unit_of_measurement=ELECTRIC_CURRENT_AMPERE,
    ),
    # --- BG Sync fork: OCPP ChangeConfiguration numbers -------------------
    OcppNumberDescription(
        key="max_current_config",
        name="Max Current (Hardware Limit)",
        icon="mdi:current-ac",
        initial_value=32,
        native_min_value=6,
        native_max_value=32,
        native_step=1,
        native_unit_of_measurement=ELECTRIC_CURRENT_AMPERE,
        ocpp_key="MaxCurrent",
        mode=NumberMode.BOX,
    ),
    OcppNumberDescription(
        key="upper_limit_protection_voltage",
        name="Overvoltage Protection Limit",
        icon="mdi:lightning-bolt",
        initial_value=252,
        native_min_value=220,
        native_max_value=260,
        native_step=1,
        native_unit_of_measurement=ELECTRIC_POTENTIAL_VOLT,
        ocpp_key="UpperLimitProtectionVoltage",
        mode=NumberMode.BOX,
    ),
    OcppNumberDescription(
        key="connection_timeout",
        name="Connection Timeout",
        icon="mdi:timer-outline",
        initial_value=180,
        native_min_value=0,
        native_max_value=600,
        native_step=10,
        native_unit_of_measurement=TIME_SECONDS,
        ocpp_key="ConnectionTimeOut",
        mode=NumberMode.BOX,
    ),
    OcppNumberDescription(
        key="meter_value_sample_interval",
        name="Meter Reading Interval",
        icon="mdi:chart-line",
        initial_value=60,
        native_min_value=1,
        native_max_value=300,
        native_step=1,
        native_unit_of_measurement=TIME_SECONDS,
        ocpp_key="MeterValueSampleInterval",
    ),
    OcppNumberDescription(
        key="light_intensity",
        name="Indicator LED Brightness",
        icon="mdi:brightness-6",
        # Standard OCPP key, 0-100 (% of max brightness). Absent from this
        # charger's GetConfiguration dump but confirmed readable and writable
        # by name -- 30 and 100 both accepted and read back on SL320S647.
        initial_value=DEFAULT_LIGHT_INTENSITY,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        ocpp_key="LightIntensity",
        mode=NumberMode.BOX,
        model_gated=True,
    ),
]

# Keys previously exposed as number entities that have since moved platform or
# been removed. Their stale registry entries are cleaned up on setup.
#
# GridCurrentInterval was deliberately NOT included as an entity. It is
# writable and reads back correctly, but has no observable effect: the vendor
# CT clamp DataTransfer arrives every 30s regardless of whether the key is set
# to 10, 30 or 60, and the interval does not latch when the feature is toggled
# off and on either (measured on SL320S647 2026-08-10). Exposing a control that
# silently does nothing is worse than not exposing it. Worth retesting after a
# charger power-cycle, in case the value only applies at boot.
REMOVED_NUMBER_KEYS: Final = [
    "get_ct_clamp_value",  # moved to switch.py as a toggle (0/1)
    "grid_current_interval",  # writable but inert, see above
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the number platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    entities: list[ChargePointNumber | SessionCurrentLimitNumber] = []
    ent_reg = er.async_get(hass)

    for charger in entry.data[CONF_CPIDS]:
        cp_id_settings = list(charger.values())[0]
        cpid = cp_id_settings[CONF_CPID]

        charger_device_id = dr.async_get_device_id_by_identifier(
            hass, (DOMAIN, cpid), config_entry_id=entry.entry_id
        )

        # BG Sync fork: drop registry entries for numbers no longer defined.
        for old_key in REMOVED_NUMBER_KEYS:
            uid = ".".join([NUMBER_DOMAIN, DOMAIN, cpid, old_key])
            stale_eid = ent_reg.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, uid)
            if stale_eid:
                ent_reg.async_remove(stale_eid)

        try:
            connector_count = max(1, int(cp_id_settings.get(CONF_NUM_CONNECTORS, 1)))
        except (TypeError, ValueError):
            connector_count = 1

        legacy_uid = re.compile(
            rf"{NUMBER_DOMAIN}\.{DOMAIN}\.{re.escape(cpid)}\.conn\d+\.maximum_current"
        )
        session_connector_uid = re.compile(
            rf"{NUMBER_DOMAIN}\.{DOMAIN}\.{re.escape(cpid)}\.conn(\d+)\.session_current_limit"
        )
        session_flat_uid = f"{NUMBER_DOMAIN}.{DOMAIN}.{cpid}.session_current_limit"
        for registry_entry in er.async_entries_for_config_entry(
            ent_reg, entry.entry_id
        ):
            if (
                registry_entry.platform == DOMAIN
                and registry_entry.domain == NUMBER_DOMAIN
                and legacy_uid.fullmatch(registry_entry.unique_id)
            ):
                _LOGGER.info(
                    "Removing stale connector-level entity %s; "
                    "Maximum Current is station-wide on this charger",
                    registry_entry.entity_id,
                )
                ent_reg.async_remove(registry_entry.entity_id)
                continue
            if (
                registry_entry.platform != DOMAIN
                or registry_entry.domain != NUMBER_DOMAIN
            ):
                continue
            # A session entity whose shape no longer matches the connector
            # count is stale: the flat one on a multi-connector charger, the
            # connector ones on a single-connector charger, or a connector
            # beyond the configured count.
            session_match = session_connector_uid.fullmatch(registry_entry.unique_id)
            stale_session = (connector_count == 1 and session_match is not None) or (
                connector_count > 1 and registry_entry.unique_id == session_flat_uid
            )
            if session_match is not None and int(session_match.group(1)) > min(
                connector_count, MAX_SESSION_CONNECTORS
            ):
                stale_session = True
            if stale_session:
                _LOGGER.info(
                    "Removing stale session-current entity %s after connector "
                    "topology changed",
                    registry_entry.entity_id,
                )
                ent_reg.async_remove(registry_entry.entity_id)

        for desc in NUMBERS:
            if desc.key == "maximum_current":
                max_cur = float(
                    cp_id_settings.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT)
                )
                ent_initial = max_cur
                ent_max = max_cur
            else:
                ent_initial = desc.initial_value
                ent_max = desc.native_max_value

            uid_flat = ".".join([NUMBER_DOMAIN, DOMAIN, cpid, desc.key])
            fresh = ent_reg.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, uid_flat) is None
            entities.append(
                ChargePointNumber(
                    hass=hass,
                    central_system=central_system,
                    cpid=cpid,
                    description=OcppNumberDescription(
                        key=desc.key,
                        name=desc.name,
                        icon=desc.icon,
                        initial_value=ent_initial,
                        native_min_value=desc.native_min_value,
                        native_max_value=ent_max,
                        native_step=desc.native_step,
                        native_unit_of_measurement=desc.native_unit_of_measurement,
                        ocpp_key=desc.ocpp_key,
                        mode=desc.mode,
                        model_gated=desc.model_gated,
                    ),
                    connector_id=None,
                    op_connector_id=0,
                    fresh=fresh,
                    charger_device_id=charger_device_id,
                )
            )

        max_cur = float(cp_id_settings.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT))
        if connector_count > MAX_SESSION_CONNECTORS:
            _LOGGER.warning(
                "%s: session current limit entities are limited to connectors 1..%s",
                cpid,
                MAX_SESSION_CONNECTORS,
            )
        session_cls = (
            SessionDefaultLimitNumber
            if cp_id_settings.get(
                CONF_SESSION_LIMIT_DEFAULT_PROFILE,
                DEFAULT_SESSION_LIMIT_DEFAULT_PROFILE,
            )
            else SessionCurrentLimitNumber
        )
        for connector_id in range(1, min(connector_count, MAX_SESSION_CONNECTORS) + 1):
            entities.append(
                session_cls(
                    hass,
                    central_system,
                    cpid,
                    connector_id,
                    connector_count,
                    max_cur,
                    charger_device_id=charger_device_id,
                )
            )

    async_add_devices(entities, False)


class ChargePointNumber(RestoreNumber, NumberEntity):
    """Individual slider for setting charge rate."""

    _attr_has_entity_name = False
    entity_description: OcppNumberDescription

    def __init__(
        self,
        hass: HomeAssistant,
        central_system: CentralSystem,
        cpid: str,
        description: OcppNumberDescription,
        connector_id: int | None = None,
        op_connector_id: int | None = None,
        fresh: bool = False,
        charger_device_id: str | None = None,
    ):
        """Initialize a Number instance."""
        self.cpid = cpid
        self._hass = hass
        self.central_system = central_system
        self.entity_description = description
        self._fresh = fresh
        self.connector_id = connector_id
        self._op_connector_id = (
            op_connector_id if op_connector_id is not None else (connector_id or 1)
        )

        parts = [NUMBER_DOMAIN, DOMAIN, cpid, description.key]
        if self.connector_id:
            parts.insert(3, f"conn{self.connector_id}")
        self._attr_unique_id = ".".join(parts)
        self._attr_name = self.entity_description.name
        if self.connector_id:
            device_info: DeviceInfo = {
                "identifiers": {(DOMAIN, f"{cpid}-conn{self.connector_id}")},
                "name": f"{cpid} Connector {self.connector_id}",
            }
            if charger_device_id is not None:
                device_info["via_device_id"] = charger_device_id
            else:
                device_info["via_device"] = (DOMAIN, cpid)
            self._attr_device_info = DeviceInfo(**device_info)
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, cpid)},
                name=cpid,
            )
        if self.connector_id is not None:
            object_id = f"{self.cpid}_connector_{self.connector_id}_{self.entity_description.key}"
        else:
            object_id = f"{self.cpid}_{self.entity_description.key}"
        self.entity_id = f"{NUMBER_DOMAIN}.{slugify(object_id)}"
        self._attr_native_value = self.entity_description.initial_value
        self._attr_mode = self.entity_description.mode or NumberMode.AUTO
        # The last limit this integration believes the charger is holding:
        # confirmed by an accepted request this session, or restored from
        # the previous one (the charger keeps its profile across our
        # restarts). None on a fresh install, so a rollback cannot invent
        # a limit. Requests the charger performs without this entity - the
        # ocpp.clear_profile / ocpp.set_charge_rate services - are not
        # reflected here, the same blind spot the pre-#2049 code had.
        self._confirmed_value: float | None = None
        # Monotonic ticket per request, and the ticket of the newest
        # accepted one. The transport serialises calls today (the ocpp
        # library holds its call lock across send and response), so
        # completions cannot cross - but that is a property of a library
        # two layers down, not of this entity. The guard keeps the
        # display-owns-latest-accepted invariant provable right here.
        self._request_seq: int = 0
        self._accepted_seq: int = 0
        # BG Sync fork: True once this session has had a request accepted by
        # the charger. Deliberately NOT restored, unlike _confirmed_value --
        # it gates the no-op suppression so a restored value can never cause
        # the first request after a restart to be skipped.
        self._sent_this_session: bool = False
        self._attr_should_poll = False

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        # Restore data is keyed by entity id, not unique id. A new station
        # entity must not inherit an old connector slider's renamed state.
        if not self._fresh and (restored := await self.async_get_last_number_data()):
            self._attr_native_value = restored.native_value
            # What the previous session last settled on. The charger keeps
            # its charging profile across our restarts, so this is the best
            # available proxy for what it is holding - stale only if the
            # charger was reset or cleared in between, and corrected by the
            # next accepted request either way.
            self._confirmed_value = restored.native_value

        @callback
        def _maybe_update(*args):
            active_lookup = None
            if args:
                try:
                    active_lookup = set(args[0])
                except Exception:
                    active_lookup = None

            if active_lookup is None or self.entity_id in active_lookup:
                self.async_schedule_update_ha_state(True)

        self.async_on_remove(
            async_dispatcher_connect(self.hass, DATA_UPDATED, _maybe_update)
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if self.entity_description.model_gated:
            model = self.central_system.get_metric(
                self.cpid, HAChargerDetails.model.value
            )
            if model not in SYNCEV_VENDOR_KEY_MODELS:
                return False
        if self.entity_description.ocpp_key:
            # Config keys need only a live connection, not smart charging.
            return bool(self.central_system.get_available(self.cpid, None))
        features = self.central_system.get_supported_features(self.cpid)
        has_smart = bool(features & Profiles.SMART)
        return bool(
            self.central_system.get_available(self.cpid, self._op_connector_id)
            and has_smart
        )

    def _is_sync_charger(self) -> bool:
        """Return True when the charger floors rates to whole amps (Sync EV).

        The no-op suppression below is a measured property of Sync hardware,
        so it must not apply to chargers that honour fractional or watt
        limits.
        """
        cpids = getattr(self.central_system, "cpids", None) or {}
        charge_points = getattr(self.central_system, "charge_points", None) or {}
        cp = charge_points.get(cpids.get(self.cpid, self.cpid))
        return getattr(cp, "is_sync_charger", False) is True

    async def _async_set_ocpp_key(self, value: float) -> None:
        """Write an OCPP configuration key for ocpp_key-backed numbers."""
        ocpp_key = self.entity_description.ocpp_key
        try:
            cp_id = self.central_system.cpids.get(self.cpid, self.cpid)
            cp = self.central_system.charge_points.get(cp_id)
            if cp is None:
                raise HomeAssistantError(
                    f"Cannot set {ocpp_key}: charger {self.cpid} is not connected."
                )
            result = await cp.configure(ocpp_key, str(int(value)))
            _LOGGER.debug("Set %s = %s, result: %s", ocpp_key, value, result)
        except HomeAssistantError:
            raise
        except Exception as ex:
            raise HomeAssistantError(f"Failed to set {ocpp_key}: {ex}") from ex

    async def async_set_native_value(self, value):
        """Set the station-wide maximum current.

        - Optimistic UI: move the slider immediately so it tracks the drag.
        - On refusal, put it back and raise: a current limit that reads as
          applied while the charger runs unrestricted is worse than an
          error, because the number is the only thing telling the user what
          the circuit is doing. Keeping the value only logged the problem.
        """
        # BG Sync fork: configuration-key entities have nothing to do with
        # charge rate, so they return before any of the sequencing or
        # confirmed-value bookkeeping below.
        if self.entity_description.ocpp_key:
            self._attr_native_value = float(value)
            self.async_write_ha_state()
            await self._async_set_ocpp_key(value)
            return

        # Round to one decimal place for display. The wire format is already
        # constrained to multipleOf 0.1 (see set_charge_rate in ocppv16.py),
        # and callers such as evcc pass raw computed floats with far more
        # precision, so without this the UI shows noise like 11.6672652173913.
        target = round(float(value), 1)

        # Suppress requests the charger cannot act on. This charger floors to
        # whole amps (see CHARGE_RATE_STEP), so a request flooring to the same
        # amp as the last one sent cannot change the delivered current and is
        # not worth an OCPP round-trip -- a solar controller will ask for
        # 12.7 -> 13.1 -> 12.9 A within a minute, all of which are 13 A here.
        #
        # Boundary values are always sent, so 'stop' (min) and 'full rate'
        # (max) are never swallowed. Nothing was transmitted in the suppressed
        # case, so _confirmed_value and _accepted_seq are deliberately left
        # untouched: the charger still holds whatever it last accepted.
        #
        # Never suppress before this session has successfully sent something.
        # _confirmed_value is restored from the previous session, and the
        # charger may have been reset or had its profiles cleared in between --
        # suppressing on the strength of a restored value could then leave the
        # limit never applied at all. As everywhere else in this flow, the
        # failure mode is unsafe-by-default: the limit would fail upward.
        at_boundary = target in (self.native_min_value, self.native_max_value)
        if (
            CHARGE_RATE_STEP
            and self._is_sync_charger()
            and not at_boundary
            and self._sent_this_session
            and self._confirmed_value is not None
            and _quantise_rate(target) == _quantise_rate(self._confirmed_value)
        ):
            _LOGGER.debug(
                "Charge rate %.1f A floors to %.0f A, same as last confirmed "
                "%.1f A; not sending.",
                target,
                _quantise_rate(target),
                self._confirmed_value,
            )
            self._attr_native_value = target
            self.async_write_ha_state()
            return

        self._request_seq += 1
        seq = self._request_seq
        self._attr_native_value = target
        self.async_write_ha_state()

        try:
            ok = await self.central_system.set_max_charge_rate_amps(self.cpid, target)
        except HomeAssistantError:
            # set_charge_rate raises this for a rejected profile, and its
            # message carries the charger's own status_info - the only
            # explanation of why. Surface it rather than restating it.
            self._revert_to_confirmed()
            raise
        except Exception as ex:
            self._revert_to_confirmed()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_charge_rate_error",
                translation_placeholders={"message": str(ex)},
            ) from ex

        if not ok:
            self._revert_to_confirmed()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_charge_rate_error",
                translation_placeholders={
                    "message": f"charger did not accept {target:.1f} A"
                },
            )

        if seq <= self._accepted_seq:
            # A newer request was already accepted while this one was in
            # flight; its limit superseded this one on the charger, so it
            # owns the display and the confirmed value.
            _LOGGER.debug(
                "Accepted limit %.1f A superseded in flight; display stays at %s",
                target,
                self._attr_native_value,
            )
            return
        self._accepted_seq = seq
        self._confirmed_value = target
        self._sent_this_session = True
        if self._attr_native_value != target:
            # A request that started later failed while this one was in
            # flight and rolled the slider back. This limit is the one the
            # charger is holding, so it owns what is displayed.
            self._attr_native_value = target
            self.async_write_ha_state()

    def _revert_to_confirmed(self) -> None:
        """Put the slider back to the last limit the charger accepted.

        Reverting to whatever was displayed when this request started would
        clobber a concurrent request that has since been accepted - two
        quick drags, or an automation racing the UI - and leave the slider
        disagreeing with the charger, which is the thing this is meant to
        prevent rather than cause.
        """
        # BG Sync fork: upstream noted this comparison was exact because
        # native_step is 1 and values were whole amps. This fork rounds to one
        # decimal place instead, so values can be fractional. Equality is still
        # safe because every value stored here has been through
        # round(x, 1), so identical inputs produce identical floats -- but a
        # finer step, or any arithmetic on these values, would need a tolerance
        # here and in the superseded check above.
        if self._attr_native_value == self._confirmed_value:
            return
        _LOGGER.debug(
            "Reverting current limit display from %s to last accepted %s",
            self._attr_native_value,
            self._confirmed_value,
        )
        self._attr_native_value = self._confirmed_value
        self.async_write_ha_state()


class SessionCurrentLimitNumber(NumberEntity):
    """Per-connector current limit bound to the transaction the connector displays.

    The thin shape agreed on #2142: availability comes from the connector's
    status and a displayed transaction id, the value is the last limit the
    charger accepted for that transaction, and a request is a direct
    SetChargingProfile. The charger is the safety net: it rejects a TxProfile
    whose transaction id no longer matches, and it discards the profile when
    the transaction ends. Nothing is restored across restarts, because the
    entity cannot know what the previous run left on the charger.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = ICON
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_native_unit_of_measurement = ELECTRIC_CURRENT_AMPERE

    def __init__(
        self,
        hass: HomeAssistant,
        central_system: CentralSystem,
        cpid: str,
        connector_id: int,
        connector_count: int,
        max_current: float,
        charger_device_id: str | None = None,
    ) -> None:
        """Initialize a connector-scoped session number."""
        self._hass = hass
        self.central_system = central_system
        self.cpid = cpid
        self.connector_id = connector_id
        self._attr_native_max_value = max_current
        self._attr_name = "Session Current Limit"
        if connector_count == 1:
            self._attr_unique_id = ".".join(
                [NUMBER_DOMAIN, DOMAIN, cpid, "session_current_limit"]
            )
            self.entity_id = (
                f"{NUMBER_DOMAIN}.{slugify(f'{cpid}_session_current_limit')}"
            )
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, cpid)},
                name=cpid,
            )
        else:
            self._attr_unique_id = ".".join(
                [
                    NUMBER_DOMAIN,
                    DOMAIN,
                    cpid,
                    f"conn{connector_id}",
                    "session_current_limit",
                ]
            )
            object_id = f"{cpid}_connector_{connector_id}_session_current_limit"
            self.entity_id = f"{NUMBER_DOMAIN}.{slugify(object_id)}"
            device_info: DeviceInfo = {
                "identifiers": {(DOMAIN, f"{cpid}-conn{connector_id}")},
                "name": f"{cpid} Connector {connector_id}",
            }
            if charger_device_id is not None:
                device_info["via_device_id"] = charger_device_id
            else:
                device_info["via_device"] = (DOMAIN, cpid)
            self._attr_device_info = DeviceInfo(**device_info)
        # The value on display belongs to one transaction of one charger
        # generation: the id the connector shows plus the charger's boot
        # count, because a rebooted charger may reuse an id. When the key no
        # longer matches, the charger has discarded that profile and the
        # value goes back to unknown. _confirmed_value is what the charger
        # accepted; _attr_native_value may run ahead of it while a request is
        # in flight and falls back to it on failure.
        self._display_key: tuple[int, str] | None = None
        self._confirmed_value: float | None = None
        self._attr_native_value = None
        # One operation at a time per connector, in the order asked. A set
        # spends its first round trips in capability discovery before it
        # reaches the transport's call lock, so nothing else guarantees that
        # two requests reach the charger in order, and their replies would
        # otherwise interleave their updates of the confirmed value.
        self._operation_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """Follow the charger's metric refreshes like the other entities."""
        await super().async_added_to_hass()

        @callback
        def _maybe_update(*args):
            active_lookup = None
            if args:
                try:
                    active_lookup = set(args[0])
                except Exception:
                    active_lookup = None
            if active_lookup is None or self.entity_id in active_lookup:
                self.async_schedule_update_ha_state(True)

        self.async_on_remove(
            async_dispatcher_connect(self.hass, DATA_UPDATED, _maybe_update)
        )

    def _current_transaction(self) -> str | None:
        return self.central_system.session_transaction_id(self.cpid, self.connector_id)

    def _current_key(self) -> tuple[int, str] | None:
        transaction = self._current_transaction()
        if transaction is None:
            return None
        return (self.central_system.charger_generation(self.cpid), transaction)

    @property
    def available(self) -> bool:
        """Charger up with SmartCharging, connector charging, transaction displayed."""
        return bool(
            self.central_system.session_limit_available(self.cpid, self.connector_id)
        )

    @property
    def native_value(self) -> float | None:
        """The value for the displayed transaction of this charger generation."""
        key = self._current_key()
        if key is not None and key == self._display_key:
            return self._attr_native_value
        if self._display_key is not None:
            # A different transaction, or the same id after a reboot: the
            # profile that value described is gone with it.
            self._display_key = None
            self._confirmed_value = None
            self._attr_native_value = None
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Expose what the request is bound to."""
        return {
            "transaction_id": self._current_transaction(),
            "charger_generation": self.central_system.charger_generation(self.cpid),
            "profile_id": session_profile_id(self.connector_id),
            "confirmed_current": self._confirmed_value,
        }

    async def async_set_native_value(self, value: float) -> None:
        """Send the limit and keep only what the charger accepted.

        Optimistic like Maximum Current: the slider moves while the request
        is in flight, and goes back to the last accepted value on anything
        but an Accepted reply. The maximum is a limit like any other: the
        profile ends with its transaction, so it is set rather than cleared.
        Operations on one connector run one at a time, in the order they
        were asked for.
        """
        async with self._operation_lock:
            self._begin_set()
            target = float(value)
            self._attr_native_value = target
            self.async_write_ha_state()
            try:
                ok = await self._send(target)
            except asyncio.CancelledError:
                # Not an Exception: without this the in-flight value would
                # stay on display as if the charger had accepted it.
                self._revert_to_confirmed()
                raise
            except HomeAssistantError:
                self._revert_to_confirmed()
                raise
            except Exception as ex:
                self._revert_to_confirmed()
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="set_charge_rate_error",
                    translation_placeholders={"message": str(ex)},
                ) from ex
            if not ok:
                self._revert_to_confirmed()
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="set_charge_rate_error",
                    translation_placeholders={
                        "message": f"charger did not accept {target:.1f} A"
                    },
                )
            self._confirmed_value = target
            self._attr_native_value = target
            self.async_write_ha_state()

    def _begin_set(self) -> None:
        """Bind the display to the transaction the request is for."""
        key = self._current_key()
        if key is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_charge_rate_error",
                translation_placeholders={
                    "message": (
                        f"connector {self.connector_id} has no active "
                        "transaction to limit"
                    )
                },
            )
        if key != self._display_key:
            self._display_key = key
            self._confirmed_value = None

    async def _send(self, target: float) -> bool:
        return await self.central_system.set_session_charge_rate_amps(
            self.cpid, self.connector_id, target
        )

    def _revert_to_confirmed(self) -> None:
        if self._attr_native_value == self._confirmed_value:
            return
        self._attr_native_value = self._confirmed_value
        self.async_write_ha_state()


class SessionDefaultLimitNumber(SessionCurrentLimitNumber, RestoreNumber):
    """Session Current Limit sent as a connector TxDefaultProfile.

    Selected per charger by session_limit_default_profile, for chargers
    that refuse both the station ceiling and a TxProfile but accept a
    connector default and apply it to the running transaction (#2158). The
    profile is not bound to a transaction, so the slider is available
    whenever the charger is up with SmartCharging, and its value is the last
    limit the charger accepted for the connector. That value is restored
    across restarts, because the charger keeps the profile; a value left by
    the transaction-bound slider is not, because that profile has ended.
    """

    async def async_added_to_hass(self) -> None:
        """Restore the last accepted default, if this mode left one."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if (
            last_state is None
            or last_state.attributes.get("profile_purpose") != _TX_DEFAULT_PROFILE
        ):
            return
        restored = await self.async_get_last_number_data()
        if restored is not None and restored.native_value is not None:
            self._attr_native_value = restored.native_value
            self._confirmed_value = restored.native_value

    @property
    def extra_restore_state_data(self) -> NumberExtraStoredData:
        """Save what the charger accepted, never a request still in flight."""
        return NumberExtraStoredData(
            self.native_max_value,
            self.native_min_value,
            self.native_step,
            self.native_unit_of_measurement,
            self._confirmed_value,
        )

    @property
    def available(self) -> bool:
        """Charger up with SmartCharging; no transaction needed."""
        return bool(
            self.central_system.session_default_limit_available(
                self.cpid, self.connector_id
            )
        )

    @property
    def native_value(self) -> float | None:
        """The last limit the charger accepted for this connector."""
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict:
        """Expose what the request writes."""
        return {
            "profile_purpose": _TX_DEFAULT_PROFILE,
            "profile_id": session_default_profile_id(self.connector_id),
            "confirmed_current": self._confirmed_value,
        }

    def _begin_set(self) -> None:
        """Send without a transaction: a default profile needs none."""

    async def _send(self, target: float) -> bool:
        return await self.central_system.set_session_default_charge_rate_amps(
            self.cpid, self.connector_id, target
        )
