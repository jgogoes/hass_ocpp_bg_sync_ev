"""Switch platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Final

from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import slugify
from ocpp.v16.enums import ChargePointStatus

from .api import CentralSystem
from .const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_NUM_CONNECTORS,
    DEFAULT_NUM_CONNECTORS,
    DATA_UPDATED,
    DOMAIN,
    ICON,
    SYNCEV_VENDOR_KEY_MODELS,
)
from .enums import HAChargerDetails, HAChargerServices, HAChargerStatuses

_LOGGER: logging.Logger = logging.getLogger(__package__)


# Switch configuration definitions
# At a minimum define switch name and on service call,
# metric and condition combination can be used to drive switch state, use default to set initial state to True
@dataclass
class OcppSwitchDescription(SwitchEntityDescription):
    """Class to describe a Switch entity."""

    on_action: str | None = None
    off_action: str | None = None
    metric_state: str | None = None
    metric_condition: list[str] | None = None
    default_state: bool = False
    per_connector: bool = False
    # BG Sync fork: when ocpp_key is set the switch drives an OCPP
    # ChangeConfiguration key rather than a service call.
    ocpp_key: str | None = None
    ocpp_on_value: str = "true"
    ocpp_off_value: str = "false"
    # True when the charger reports a value for this key that maps to neither
    # on_value nor off_value even though the write took effect. See
    # _apply_readback: such a key cannot be reconciled from a read, so the
    # commanded value is authoritative and raw_value carries what was reported.
    ocpp_readback_ambiguous: bool = False
    # Only offered on chargers whose reported model is in
    # SYNCEV_VENDOR_KEY_MODELS -- for vendor keys not present on every charger.
    model_gated: bool = False


SWITCHES: Final[list[OcppSwitchDescription]] = [
    OcppSwitchDescription(
        key="charge_control",
        name="Charging",
        icon=ICON,
        on_action=HAChargerServices.service_charge_start.name,
        off_action=HAChargerServices.service_charge_stop.name,
        metric_state=HAChargerStatuses.status_connector.value,
        metric_condition=[
            ChargePointStatus.charging.value,
            ChargePointStatus.suspended_evse.value,
            ChargePointStatus.suspended_ev.value,
        ],
        per_connector=True,
    ),
    OcppSwitchDescription(
        key="availability",
        name="Charger Available",
        icon=ICON,
        on_action=HAChargerServices.service_availability.name,
        off_action=HAChargerServices.service_availability.name,
        metric_state=HAChargerStatuses.status.value,  # charger-level status
        metric_condition=[ChargePointStatus.available.value],
        default_state=True,
        per_connector=False,
    ),
    OcppSwitchDescription(
        key="connnector_availability",
        name="Connector Availability",
        icon=ICON,
        on_action=HAChargerServices.service_availability.name,
        off_action=HAChargerServices.service_availability.name,
        metric_state=HAChargerStatuses.status_connector.value,  # connector-level status
        metric_condition=[
            ChargePointStatus.available.value,
            ChargePointStatus.preparing.value,
            ChargePointStatus.charging.value,
            ChargePointStatus.suspended_evse.value,
            ChargePointStatus.suspended_ev.value,
            ChargePointStatus.finishing.value,
            ChargePointStatus.reserved.value,
        ],
        default_state=True,
        per_connector=True,
    ),
    # --- BG Sync fork: OCPP ChangeConfiguration switches ------------------
    OcppSwitchDescription(
        key="unlock_connector_on_ev_side_disconnect",
        name="Auto-Unlock on Unplug",
        icon="mdi:lock-open",
        default_state=True,
        ocpp_key="UnlockConnectorOnEVSideDisconnect",
    ),
    OcppSwitchDescription(
        key="get_ct_clamp_value",
        name="Enable CT Clamp",
        icon="mdi:gauge",
        # Verified on SL320S647 2026-08-10: writing 0 stops the vendor
        # DataTransfer stream outright, writing 1 resumes it within ~30s.
        # The readings arrive as a DataTransfer push, parsed in ocppv16.py.
        default_state=False,
        ocpp_key="GetCTClampValue",
        ocpp_on_value="1",
        ocpp_off_value="0",
    ),
    OcppSwitchDescription(
        key="charge_on_plug_in",
        name="Charge When Plugged In (No Server Required)",
        icon="mdi:ev-plug-type2",
        # Vendor key "ChargerMode". Confirmed by live test on SL320S647
        # 2026-08-10: with the value written to 3, physically unplugging and
        # replugging the car produced an unprompted StartTransaction from the
        # charger with idTag "freeIdTag" and no RemoteStartTransaction from
        # HA -- i.e. the charger self-authorises. Value 1 restores app/OCPP
        # control.
        #
        # The charger reports the key back as "2" once set to 3, so a
        # read-back cannot tell modes 2 and 3 apart. Hence
        # ocpp_readback_ambiguous: trust what was commanded, and surface the
        # reported value through the raw_value attribute.
        #
        # Note this fights smart charging: the charger will start a session on
        # every plug-in regardless of what evcc or a schedule wants.
        default_state=False,
        ocpp_key="ChargerMode",
        ocpp_on_value="3",
        ocpp_off_value="1",
        ocpp_readback_ambiguous=True,
        model_gated=True,
    ),
]

# Switch keys removed from SWITCHES above. Their entity registry entries are
# cleaned up on setup so they do not linger as permanently unavailable.
#
# All four were verified against a live GetConfiguration on SL320S647
# (2026-08-10):
#   StopTransactionOnEVSideDisconnect  readonly: true
#   StopTransactionOnInvalidId         readonly: true
#     configure() silently no-ops on readonly keys, so these toggles could
#     never have done anything.
#   LocalAuthListEnabled               readonly: false, but inert
#     It governs whether the charger checks idTags against its own cached
#     local list, populated via SendLocalList. Neither this integration nor
#     upstream implements SendLocalList anywhere, so that list is guaranteed
#     empty, and per the OCPP 1.6 spec an empty local list falls through to
#     the normal Authorize/RemoteStartTransaction flow already in use.
#   AuthorizeRemoteTxRequests          readonly: true, unused
REMOVED_SWITCH_KEYS: Final = [
    "authorize_remote_tx_requests",
    "stop_transaction_on_ev_side_disconnect",
    "stop_transaction_on_invalid_id",
    "local_auth_list_enabled",
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the switch platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    entities: list[ChargePointSwitch] = []
    ent_reg = er.async_get(hass)

    for charger in entry.data[CONF_CPIDS]:
        cp_settings = list(charger.values())[0]
        cpid = cp_settings[CONF_CPID]

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
        flatten_single = num_connectors == 1

        # BG Sync fork: drop registry entries for switches this fork no longer
        # defines, so they do not linger as permanently unavailable.
        for old_key in REMOVED_SWITCH_KEYS:
            uid = ".".join([SWITCH_DOMAIN, DOMAIN, cpid, old_key])
            stale_eid = ent_reg.async_get_entity_id(SWITCH_DOMAIN, DOMAIN, uid)
            if stale_eid:
                ent_reg.async_remove(stale_eid)

        if num_connectors > 1:
            for desc in SWITCHES:
                if not desc.per_connector:
                    continue
                # unique_id used when flattened: "<switch>.<domain>.<cpid>.<key>"
                uid_flat = ".".join([SWITCH_DOMAIN, DOMAIN, cpid, desc.key])
                stale_eid = ent_reg.async_get_entity_id(SWITCH_DOMAIN, DOMAIN, uid_flat)
                if stale_eid:
                    ent_reg.async_remove(stale_eid)

        for desc in SWITCHES:
            if desc.per_connector:
                # Only create Connector Availability switches for multi-connector chargers
                if desc.key == "connnector_availability" and num_connectors <= 1:
                    continue
                for conn_id in range(1, num_connectors + 1):
                    entities.append(
                        ChargePointSwitch(
                            central_system,
                            cpid,
                            desc,
                            connector_id=conn_id,
                            flatten_single=flatten_single,
                        )
                    )
            else:
                entities.append(
                    ChargePointSwitch(
                        central_system,
                        cpid,
                        desc,
                        connector_id=None,
                        flatten_single=False,
                    )
                )

    async_add_devices(entities, False)


class ChargePointSwitch(SwitchEntity):
    """Individual switch for charge point."""

    _attr_has_entity_name = False
    entity_description: OcppSwitchDescription

    def __init__(
        self,
        central_system: CentralSystem,
        cpid: str,
        description: OcppSwitchDescription,
        connector_id: int | None = None,
        flatten_single: bool = False,
    ):
        """Instantiate instance of a ChargePointSwitch."""
        self.cpid = cpid
        self.central_system = central_system
        self.entity_description = description
        self.connector_id = connector_id
        self._flatten_single = flatten_single
        self._state = self.entity_description.default_state
        # BG Sync fork: last raw value read back from the charger for ocpp_key
        # switches. Populated after every write and on entity add, so the switch
        # reflects the charger rather than only what was last commanded.
        self._raw_value: str | None = None
        parts = [SWITCH_DOMAIN, DOMAIN, cpid]
        if self.connector_id and not self._flatten_single:
            parts.append(f"conn{self.connector_id}")
        parts.append(description.key)
        self._attr_unique_id = ".".join(parts)
        self._attr_name = self.entity_description.name
        if self.connector_id and not self._flatten_single:
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
        if self.connector_id is not None and not flatten_single:
            object_id = f"{self.cpid}_connector_{self.connector_id}_{self.entity_description.key}"
        else:
            object_id = f"{self.cpid}_{self.entity_description.key}"
        self.entity_id = f"{SWITCH_DOMAIN}.{slugify(object_id)}"

    @property
    def available(self) -> bool:
        """Return if switch is available."""
        if self.entity_description.model_gated:
            model = self.central_system.get_metric(
                self.cpid, HAChargerDetails.model.value
            )
            if model not in SYNCEV_VENDOR_KEY_MODELS:
                return False
        if self.entity_description.ocpp_key:
            # Config keys are charger-wide, never per-connector.
            return bool(self.central_system.get_available(self.cpid, None))
        target_conn = (
            self.connector_id if self.entity_description.per_connector else None
        )
        return bool(self.central_system.get_available(self.cpid, target_conn))

    @property
    def extra_state_attributes(self):
        """Expose the charger's raw reported value for ocpp_key switches.

        HA switches are binary but some vendor keys have more than two values,
        and at least one (ChargerMode) reports a value that does not match what
        was written. raw_value surfaces exactly what the charger last reported.
        """
        if self.entity_description.ocpp_key and self._raw_value is not None:
            return {"raw_value": self._raw_value}
        return None

    @property
    def should_poll(self) -> bool:
        """Don't poll - updates will be pushed."""
        return False

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        """Test metric state against condition if present"""
        if self.entity_description.ocpp_key:
            return self._state
        if self.entity_description.metric_state is not None:
            metric_conn = (
                self.connector_id
                if (
                    self.entity_description.metric_state
                    == HAChargerStatuses.status_connector.value
                    or self.entity_description.per_connector
                )
                else None
            )
            resp = self.central_system.get_metric(
                self.cpid, self.entity_description.metric_state, metric_conn
            )
            if self.entity_description.metric_condition is not None:
                self._state = resp in self.entity_description.metric_condition
            else:
                self._state = bool(resp)
        return self._state

    def _apply_readback(self, actual: str | None) -> None:
        """Reconcile switch state with what the charger actually reports.

        Vendor keys do not always report back what was written. On SL320S647,
        ChargerMode set to 3 reads back as 2 even though the mode genuinely
        took effect (verified by an unprompted StartTransaction on plug-in).
        So a mismatch is not evidence the write failed, and for keys flagged
        ocpp_readback_ambiguous the commanded value stays authoritative.

        Comparison is case-insensitive because this charger is inconsistent
        about it: UnlockConnectorOnEVSideDisconnect reports lowercase "true"
        when set, but capital "False" when cleared, and AuthorizeRemoteTxRequests
        reports "False" too while LocalAuthorizeOffline reports lowercase
        "true". A case-sensitive match sent every boolean 'off' state down the
        unknown-value path, which logged a spurious warning and -- worse -- left
        the switch unable to determine its state from a read on startup, so it
        fell back to default_state. Measured 2026-08-11.
        """
        self._raw_value = actual
        if actual is None:
            return
        reported = actual.strip().casefold()
        if reported == self.entity_description.ocpp_on_value.strip().casefold():
            self._state = True
        elif reported == self.entity_description.ocpp_off_value.strip().casefold():
            self._state = False
        elif self.entity_description.ocpp_readback_ambiguous:
            # Known and expected for this key -- keep the commanded state.
            _LOGGER.debug(
                "%s (%s): charger reports '%s'; known ambiguous read-back, "
                "keeping commanded state %s",
                self.cpid,
                self.entity_description.ocpp_key,
                actual,
                self._state,
            )
        else:
            _LOGGER.warning(
                "%s (%s): charger reports '%s', which is neither the configured "
                "on-value ('%s') nor off-value ('%s'). Leaving switch as last "
                "commanded -- check the raw_value attribute.",
                self.cpid,
                self.entity_description.ocpp_key,
                actual,
                self.entity_description.ocpp_on_value,
                self.entity_description.ocpp_off_value,
            )

    def _resolve_cp(self):
        """Return the connected ChargePoint for this cpid, or None."""
        cp_id = self.central_system.cpids.get(self.cpid, self.cpid)
        return self.central_system.charge_points.get(cp_id)

    async def _ocpp_refresh(self) -> None:
        """Read the charger's current value for this key and reconcile."""
        try:
            cp = self._resolve_cp()
            if cp is None:
                return
            actual = await cp.get_configuration(self.entity_description.ocpp_key)
            self._apply_readback(actual)
            self.async_write_ha_state()
        except Exception as ex:
            _LOGGER.debug(
                "Failed to refresh %s: %s", self.entity_description.ocpp_key, ex
            )

    async def _ocpp_configure(self, value: str) -> None:
        """Send ChangeConfiguration, then confirm what actually stuck."""
        try:
            cp = self._resolve_cp()
            if cp is None:
                _LOGGER.warning(
                    "Cannot set %s: charger %s not connected.",
                    self.entity_description.ocpp_key,
                    self.cpid,
                )
                return
            result = await cp.configure(self.entity_description.ocpp_key, value)
            _LOGGER.debug(
                "Set %s = %s, result: %s",
                self.entity_description.ocpp_key,
                value,
                result,
            )
            # An Accepted response is not proof the value was applied -- read it
            # back. (And see _apply_readback: not proof it was rejected either.)
            actual = await cp.get_configuration(self.entity_description.ocpp_key)
            self._apply_readback(actual)
        except Exception as ex:
            _LOGGER.warning(
                "Failed to set %s: %s", self.entity_description.ocpp_key, ex
            )
        finally:
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        if self.entity_description.ocpp_key:
            self._state = True
            self.async_write_ha_state()
            await self._ocpp_configure(self.entity_description.ocpp_on_value)
            return
        target_conn = self.connector_id if self.entity_description.per_connector else 0
        self._state = await self.central_system.set_charger_state(
            self.cpid, self.entity_description.on_action, True, connector_id=target_conn
        )

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        if self.entity_description.ocpp_key:
            self._state = False
            self.async_write_ha_state()
            await self._ocpp_configure(self.entity_description.ocpp_off_value)
            return
        target_conn = self.connector_id if self.entity_description.per_connector else 0
        if self.entity_description.off_action is None:
            resp = True
        elif self.entity_description.off_action == self.entity_description.on_action:
            resp = await self.central_system.set_charger_state(
                self.cpid,
                self.entity_description.off_action,
                False,
                connector_id=target_conn,
            )
        else:
            resp = await self.central_system.set_charger_state(
                self.cpid, self.entity_description.off_action, connector_id=target_conn
            )
        self._state = not resp

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()

        @callback
        def update(*args):
            """Pass through real-time updates to state."""
            active_lookup = None
            if args:
                try:
                    active_lookup = set(args[0])
                except Exception:
                    active_lookup = None

            if active_lookup is None or self.entity_id in active_lookup:
                self.async_schedule_update_ha_state(True)

            # BG Sync fork: keep retrying the initial read-back until it lands.
            # A single attempt at add-time is not enough: after an HA restart
            # the entity is added before the charger's websocket reconnects, so
            # _resolve_cp() returns None, the refresh silently no-ops, and the
            # switch is stuck on its constructor default forever. Retrying on
            # each dispatcher tick until _raw_value is populated fixes that.
            if (
                self.entity_description.ocpp_key
                and self._raw_value is None
                and self.central_system.get_available(self.cpid, None)
            ):
                self.hass.async_create_task(self._ocpp_refresh())

        # subscribe to updates
        self.async_on_remove(async_dispatcher_connect(self.hass, DATA_UPDATED, update))

        # Ensure switch publishes its current state immediately after being added
        self.async_schedule_update_ha_state(True)

        if self.entity_description.ocpp_key:
            # Sync with the charger's actual value rather than trusting
            # default_state -- it may already be in a non-default state from
            # before HA started.
            self.hass.async_create_task(self._ocpp_refresh())
