"""Number platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Final

from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import slugify

from .api import CentralSystem
from .const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_MAX_CURRENT,
    CONF_NUM_CONNECTORS,
    DATA_UPDATED,
    DEFAULT_LIGHT_INTENSITY,
    DEFAULT_MAX_CURRENT,
    DEFAULT_NUM_CONNECTORS,
    DOMAIN,
    ICON,
    SYNCEV_VENDOR_KEY_MODELS,
)
from .enums import HAChargerDetails, Profiles

_LOGGER: logging.Logger = logging.getLogger(__package__)


@dataclass
class OcppNumberDescription(NumberEntityDescription):
    """Class to describe a Number entity."""

    initial_value: float | None = None
    ocpp_key: str | None = None  # if set, use ChangeConfiguration instead of set_charge_rate
    mode: NumberMode | None = None  # NumberMode.BOX for a plain input instead of a slider
    # If True, entity is only available on chargers whose reported model is in
    # SYNCEV_VENDOR_KEY_MODELS (see const.py) — used for vendor-proprietary
    # keys not confirmed to exist on every OCPP charger.
    model_gated: bool = False


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
    # --- CT clamp reporting (paired with switch.<cpid>_get_ct_clamp_value
    # in switch.py — this entity sets the report interval, that one toggles
    # reporting on/off; both act on the same underlying vendor feature) ---
    # Not a precise seconds value in practice — ~30 for fast reporting,
    # 0/high for the ~60s default (confirmed via live testing 2026-07-07).
    OcppNumberDescription(
        key="grid_current_interval",
        name="CT Clamp Report Interval",
        icon="mdi:transmission-tower",
        initial_value=0,
        native_min_value=0,
        native_max_value=300,
        native_step=10,
        native_unit_of_measurement=TIME_SECONDS,
        ocpp_key="GridCurrentInterval",
        mode=NumberMode.BOX,
    ),
    OcppNumberDescription(
        key="light_intensity",
        name="Indicator LED Brightness",
        icon="mdi:brightness-6",
        # Standard OCPP key, 0-100 (% of max brightness per spec). This
        # charger omits it from its default GetConfiguration dump but it's
        # confirmed supported and writable when queried/set by name.
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

# Keys previously exposed as number entities that have since moved to
# another platform (or been removed) — their stale entity registry entries
# get cleaned up on setup so they don't linger as permanently unavailable.
REMOVED_NUMBER_KEYS: Final = [
    "get_ct_clamp_value",  # moved to switch.py as a toggle (0/1)
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the number platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    entities: list[ChargePointNumber] = []
    ent_reg = er.async_get(hass)

    for charger in entry.data[CONF_CPIDS]:
        cp_id_settings = list(charger.values())[0]
        cpid = cp_id_settings[CONF_CPID]

        num_connectors = 1
        for item in entry.data.get(CONF_CPIDS, []):
            for _, cfg in item.items():
                if cfg.get(CONF_CPID) == cpid:
                    num_connectors = int(
                        cfg.get(CONF_NUM_CONNECTORS, DEFAULT_NUM_CONNECTORS)
                    )
                    break
            else:
                continue
            break

        for old_key in REMOVED_NUMBER_KEYS:
            uid = ".".join([NUMBER_DOMAIN, DOMAIN, cpid, old_key])
            stale_eid = ent_reg.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, uid)
            if stale_eid:
                ent_reg.async_remove(stale_eid)

        if num_connectors > 1:
            for desc in NUMBERS:
                uid_flat = ".".join([NUMBER_DOMAIN, DOMAIN, cpid, desc.key])
                stale_eid = ent_reg.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, uid_flat)
                if stale_eid:
                    ent_reg.async_remove(stale_eid)

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

            if num_connectors > 1:
                for conn_id in range(1, num_connectors + 1):
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
                            connector_id=conn_id,
                            op_connector_id=conn_id,
                        )
                    )
            else:
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
                    )
                )

    async_add_devices(entities, False)


class ChargePointNumber(RestoreNumber, NumberEntity):
    """Individual slider for setting charge rate or OCPP config values."""

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
    ):
        """Initialize a Number instance."""
        self.cpid = cpid
        self._hass = hass
        self.central_system = central_system
        self.entity_description = description
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
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{cpid}-conn{self.connector_id}")},
                name=f"{cpid} Connector {self.connector_id}",
                via_device=(DOMAIN, cpid),
            )
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
        self._attr_should_poll = False
        self._attr_mode = self.entity_description.mode or NumberMode.AUTO

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if restored := await self.async_get_last_number_data():
            self._attr_native_value = restored.native_value

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
        features = self.central_system.get_supported_features(self.cpid)
        has_smart = bool(features & Profiles.SMART)
        return bool(
            self.central_system.get_available(self.cpid, self._op_connector_id)
            and has_smart
        )

    async def async_set_native_value(self, value: float) -> None:
        """Set new value — either charge rate or OCPP ChangeConfiguration."""
        # Round to 1 decimal place for display. OCPP's SmartCharging schema
        # already enforces multipleOf 0.1 on the wire (see set_charge_rate
        # in ocppv16.py) — callers like evcc pass raw computed floats
        # (e.g. watts/volts) with far more precision than that, so without
        # this the UI shows noise like 11.6672652173913 instead of 11.7.
        self._attr_native_value = round(float(value), 1)
        self.async_write_ha_state()

        ocpp_key = self.entity_description.ocpp_key

        if ocpp_key:
            try:
                cp_id = self.central_system.cpids.get(self.cpid, self.cpid)
                cp = self.central_system.charge_points.get(cp_id)
                if cp is None:
                    _LOGGER.warning("Cannot set %s: charger %s not connected.", ocpp_key, self.cpid)
                    return
                result = await cp.configure(ocpp_key, str(int(value)))
                _LOGGER.debug("Set %s = %s, result: %s", ocpp_key, value, result)
            except Exception as ex:
                _LOGGER.warning("Failed to set %s: %s", ocpp_key, ex)
        else:
            try:
                ok = await self.central_system.set_max_charge_rate_amps(
                    self.cpid, self._attr_native_value, connector_id=self._op_connector_id
                )
                if not ok:
                    _LOGGER.warning(
                        "Set current limit rejected by CP (kept optimistic UI at %.1f A).",
                        value,
                    )
            except Exception as ex:
                _LOGGER.warning(
                    "Set current limit failed: %s (kept optimistic UI at %.1f A).",
                    ex,
                    value,
                )