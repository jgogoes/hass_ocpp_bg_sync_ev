"""Sensor platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
import homeassistant
from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CONF_MONITORED_VARIABLES
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.util import slugify

from .api import CentralSystem
from .const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_NUM_CONNECTORS,
    DATA_UPDATED,
    DEFAULT_CLASS_UNITS_HA,
    DEFAULT_NUM_CONNECTORS,
    DOMAIN,
    ICON,
    Measurand,
)
from .enums import HAChargerDetails, HAChargerSession, HAChargerStatuses

# BG Sync fork: friendlier display names for sensors whose default name is the
# raw OCPP metric string with dots swapped for spaces ("Current.Import" ->
# "Current Import"). Keyed on the exact metric value as passed to _mk_desc();
# anything not listed falls back to the upstream auto-generated name.
SENSOR_NAME_OVERRIDES = {
    "Status": "Charger Status",
    "Status.Firmware": "Firmware Update Status",
    "Heartbeat": "Last Heartbeat",
    "Id.Tag": "Active ID Tag",
    "Latency.Ping": "Ping Latency",
    "Latency.Pong": "Pong Latency",
    "Reconnects": "Reconnect Count",
    "ID": "OCPP Charge Point ID",
    "Vendor": "Manufacturer",
    "Serial": "Serial Number",
    "Version.Firmware": "Firmware Version",
    "Features": "Supported Features",
    "Connectors": "Connector Count",
    "Timestamp.Config.Response": "Last Config Response",
    "Timestamp.Data.Response": "Last Data Response",
    "Timestamp.Data.Transfer": "Last Data Transfer",
    "Current.Import": "Charging Current",
    "Current.Offered": "Max Current Offered to EV",
    "Energy.Active.Import.Register": "Lifetime Energy Delivered",
    "Power.Active.Import": "Charging Power",
    "Temperature": "Charger Temperature",
    "Voltage": "Mains Voltage",
    "Current.CtClamp": "House Current (CT Clamp)",
    "Voltage.CtClamp": "House Voltage (CT Clamp)",
    "Status.Connector": "Connector Status",
    "Error.Code.Connector": "Connector Error Code",
    "Stop.Reason": "Last Stop Reason",
    "Transaction.Id": "Transaction ID",
    "Time.Session": "Session Duration",
    "Energy.Meter.Start": "Meter Reading at Session Start",
}

# BG Sync fork: metrics this charger cannot report, so their entities would sit
# permanently 'unavailable'. Registered disabled-by-default to keep the
# dashboard tidy; re-enable per-entity in the HA UI if a different charger or
# firmware starts reporting them.
#
# This is not guesswork. MeterValuesSampledData on SL320S647 reads exactly:
#   Energy.Active.Import.Register, Power.Active.Import, Current.Offered,
#   Current.Import, Voltage, Temperature
# and the key, though writable, rejects any attempt to add measurands outside
# that set with NotSupported (verified 2026-08-10 with SoC, Frequency and
# Power.Factor). The charger will never sample these.
DISABLED_BY_DEFAULT_METRICS = {
    HAChargerStatuses.firmware_status.value.lower(),  # no FirmwareStatusNotification
    "current.export",  # export/V2G not supported
    "power.active.export",
    "power.reactive.export",
    "power.reactive.import",
    "soc",  # rejected with NotSupported
    "energy.active.export.interval",
    "energy.active.export.register",
    "energy.active.import.interval",
    "energy.reactive.export.interval",
    "energy.reactive.export.register",
    "energy.reactive.import.interval",
    "energy.reactive.import.register",
    "frequency",  # rejected with NotSupported
    "power.factor",  # rejected with NotSupported
    "power.offered",
    "rpm",
}


@dataclass
class OcppSensorDescription(SensorEntityDescription):
    """Class to describe a Sensor entity."""

    metric: str | None = None


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the sensor platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    entities: list[ChargePointMetric] = []
    ent_reg = er.async_get(hass)

    # setup all chargers added to config
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

        configured = [
            m.strip()
            for m in str(cp_id_settings.get(CONF_MONITORED_VARIABLES, "")).split(",")
            if m and m.strip()
        ]
        default_measurands: list[str] = []
        measurands = sorted(configured or default_measurands)

        # Ordered for presentation: nameplate, then live health, then
        # supply-side measurements, then diagnostic timestamps.
        CHARGER_ONLY = [
            # Nameplate / identity
            HAChargerDetails.identifier.value,
            HAChargerDetails.vendor.value,
            HAChargerDetails.model.value,
            HAChargerDetails.serial.value,
            HAChargerDetails.firmware_version.value,
            HAChargerDetails.connectors.value,
            HAChargerDetails.features.value,
            # Live connection health
            HAChargerStatuses.status.value,
            HAChargerStatuses.error_code.value,
            HAChargerStatuses.firmware_status.value,
            HAChargerStatuses.heartbeat.value,
            HAChargerStatuses.latency_ping.value,
            HAChargerStatuses.latency_pong.value,
            HAChargerStatuses.reconnects.value,
            HAChargerStatuses.id_tag.value,
            # Supply-side live measurements (CT clamp)
            HAChargerDetails.ct_clamp_current.value,
            HAChargerDetails.ct_clamp_voltage.value,
            # Diagnostic timestamps
            HAChargerDetails.config_response.value,
            HAChargerDetails.data_response.value,
            HAChargerDetails.data_transfer.value,
        ]

        CONNECTOR_ONLY = measurands + [
            # Live connector state
            HAChargerStatuses.status_connector.value,
            HAChargerStatuses.error_code_connector.value,
            # Session lifecycle in chronological order, ending with the outcome
            HAChargerSession.transaction_id.value,
            HAChargerSession.session_time.value,
            HAChargerSession.session_energy.value,
            HAChargerSession.meter_start.value,
            HAChargerStatuses.stop_reason.value,
        ]

        def _mk_desc(metric: str, *, cat_diag: bool = False) -> OcppSensorDescription:
            ms = str(metric).strip()
            return OcppSensorDescription(
                key=ms.lower().replace(".", "_"),
                name=SENSOR_NAME_OVERRIDES.get(ms, ms.replace(".", " ")),
                metric=ms,
                entity_category=EntityCategory.DIAGNOSTIC if cat_diag else None,
                entity_registry_enabled_default=ms.lower()
                not in DISABLED_BY_DEFAULT_METRICS,
            )

        def _uid(cpid: str, key: str, connector_id: int | None) -> str:
            """Mirror ChargePointMetric unique_id construction.

            BG Sync fork: upstream lowercases without replacing dots, so this
            lookup never matched any dotted metric (Status.Connector and
            friends) and the stale-entity cleanup below silently did nothing.
            Upstream fixes this properly in v0.10.19b0 by single-sourcing the
            format as const.sensor_unique_id(); when this fork rebases past
            v0.10.18, delete this helper and delegate to that instead.
            """
            key = key.lower().replace(".", "_")
            parts = [DOMAIN, cpid, key, SENSOR_DOMAIN]
            if connector_id is not None:
                parts.insert(2, f"conn{connector_id}")
            return ".".join(parts)

        if num_connectors > 1:
            for metric in CONNECTOR_ONLY:
                uid = _uid(cpid, metric, connector_id=None)
                stale_eid = ent_reg.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, uid)
                if stale_eid:
                    # Remove the old entity so it doesn't linger as 'unavailable'
                    ent_reg.async_remove(stale_eid)
        else:
            # Clear out entities registered enabled before these metrics were
            # known to be unreportable. Freshly created ones register disabled
            # by default via _mk_desc; this only removes the old registrations
            # so they stop appearing on dashboards as 'unavailable'.
            for metric_key in DISABLED_BY_DEFAULT_METRICS:
                if metric_key == HAChargerStatuses.firmware_status.value.lower():
                    # Reports 'unknown' rather than 'unavailable' and may still
                    # populate on a charger that sends FirmwareStatusNotification.
                    continue
                uid = _uid(cpid, metric_key, connector_id=None)
                stale_eid = ent_reg.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, uid)
                if stale_eid:
                    ent_reg.async_remove(stale_eid)

        # Root/charger-entities
        for metric in CHARGER_ONLY:
            entities.append(
                ChargePointMetric(
                    hass,
                    central_system,
                    cpid,
                    _mk_desc(metric, cat_diag=True),
                    connector_id=None,
                )
            )

        if num_connectors > 1:
            for conn_id in range(1, num_connectors + 1):
                for metric in CONNECTOR_ONLY:
                    entities.append(
                        ChargePointMetric(
                            hass,
                            central_system,
                            cpid,
                            _mk_desc(
                                metric,
                                cat_diag=metric
                                in [
                                    HAChargerStatuses.status_connector.value,
                                    HAChargerStatuses.error_code_connector.value,
                                ],
                            ),
                            connector_id=conn_id,
                        )
                    )
        else:
            for metric in CONNECTOR_ONLY:
                entities.append(
                    ChargePointMetric(
                        hass,
                        central_system,
                        cpid,
                        _mk_desc(
                            metric,
                            cat_diag=metric
                            in [
                                HAChargerStatuses.status_connector.value,
                                HAChargerStatuses.error_code_connector.value,
                            ],
                        ),
                        connector_id=None,
                    )
                )

    async_add_devices(entities, False)


class ChargePointMetric(RestoreSensor, SensorEntity):
    """Individual sensor for charge point metrics."""

    _attr_has_entity_name = False
    entity_description: OcppSensorDescription

    def __init__(
        self,
        hass: HomeAssistant,
        central_system: CentralSystem,
        cpid: str,
        description: OcppSensorDescription,
        connector_id: int | None = None,
    ):
        """Instantiate instance of a ChargePointMetrics."""
        self.central_system = central_system
        self.cpid = cpid
        self.entity_description = description
        self.metric = self.entity_description.metric
        self.connector_id = connector_id
        self._hass = hass
        self._extra_attr = {}
        self._last_reset = homeassistant.util.dt.utc_from_timestamp(0)
        parts = [DOMAIN, self.cpid, self.entity_description.key, SENSOR_DOMAIN]
        if self.connector_id is not None:
            parts.insert(2, f"conn{self.connector_id}")
        self._attr_unique_id = ".".join(parts)
        self._attr_name = self.entity_description.name
        if self.connector_id is not None:
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
        self.entity_id = f"{SENSOR_DOMAIN}.{slugify(object_id)}"
        self._attr_icon = ICON
        self._attr_native_unit_of_measurement = None

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        return self.central_system.get_available(self.cpid, self.connector_id)

    @property
    def should_poll(self) -> bool:
        """Return True if entity has to be polled for state.

        False if entity pushes its state to HA.
        """
        return False

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self.central_system.get_extra_attr(
            self.cpid, self.metric, self.connector_id
        )

    @property
    def state_class(self):
        """Return the state class of the sensor."""
        state_class = None
        if self.device_class is SensorDeviceClass.ENERGY:
            state_class = SensorStateClass.TOTAL_INCREASING
        elif self.device_class in [
            SensorDeviceClass.CURRENT,
            SensorDeviceClass.VOLTAGE,
            SensorDeviceClass.POWER,
            SensorDeviceClass.REACTIVE_POWER,
            SensorDeviceClass.TEMPERATURE,
            SensorDeviceClass.BATTERY,
            SensorDeviceClass.FREQUENCY,
        ] or self.metric in [
            HAChargerStatuses.latency_ping.value,
            HAChargerStatuses.latency_pong.value,
            HAChargerSession.session_time.value,
        ]:
            state_class = SensorStateClass.MEASUREMENT

        return state_class

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        device_class = None
        if self.metric.lower().startswith("current."):
            device_class = SensorDeviceClass.CURRENT
        elif self.metric.lower().startswith("voltage"):
            device_class = SensorDeviceClass.VOLTAGE
        elif self.metric.lower().startswith("energy.r"):
            device_class = None
        elif self.metric.lower().startswith("energy"):
            device_class = SensorDeviceClass.ENERGY
        elif self.metric in [
            Measurand.frequency,
            Measurand.rpm,
        ] or self.metric.lower().startswith("frequency"):
            device_class = SensorDeviceClass.FREQUENCY
        elif self.metric.lower().startswith(("power.a", "power.o")):
            device_class = SensorDeviceClass.POWER
        elif self.metric.lower().startswith("power.r"):
            device_class = SensorDeviceClass.REACTIVE_POWER
        elif self.metric.lower().startswith("temperature"):
            device_class = SensorDeviceClass.TEMPERATURE
        elif self.metric.lower().startswith("timestamp") or self.metric in [
            HAChargerDetails.config_response.value,
            HAChargerDetails.data_response.value,
            HAChargerStatuses.heartbeat.value,
        ]:
            device_class = SensorDeviceClass.TIMESTAMP
        elif self.metric.lower().startswith("soc"):
            device_class = SensorDeviceClass.BATTERY
        return device_class

    @property
    def native_value(self):
        """Return the state of the sensor, rounding if a number."""
        value = self.central_system.get_metric(
            self.cpid, self.metric, self.connector_id
        )

        # Special case for features - show profiles as labels from IntFlag
        if self.metric == HAChargerDetails.features.value and value is not None:
            if hasattr(value, "labels"):
                self._attr_native_value = value.labels()
            else:
                self._attr_native_value = str(value)

            return self._attr_native_value

        if value is not None:
            self._attr_native_value = value
        return self._attr_native_value

    @property
    def native_unit_of_measurement(self):
        """Return the native unit of measurement."""
        value = self.central_system.get_ha_unit(
            self.cpid, self.metric, self.connector_id
        )
        if value is not None:
            self._attr_native_unit_of_measurement = value
        else:
            self._attr_native_unit_of_measurement = DEFAULT_CLASS_UNITS_HA.get(
                self.device_class
            )
        return self._attr_native_unit_of_measurement

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()

        if restored := await self.async_get_last_sensor_data():
            self._attr_native_value = restored.native_value
            self._attr_native_unit_of_measurement = restored.native_unit_of_measurement

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

        self.async_schedule_update_ha_state(True)
