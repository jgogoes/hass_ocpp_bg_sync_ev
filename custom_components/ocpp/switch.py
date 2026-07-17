"""Switch platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
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

import logging
_LOGGER = logging.getLogger(__package__)


@dataclass
class OcppSwitchDescription(SwitchEntityDescription):
    """Class to describe a Switch entity."""

    on_action: str | None = None
    off_action: str | None = None
    metric_state: str | None = None
    metric_condition: list[str] | None = None
    default_state: bool = False
    per_connector: bool = False
    ocpp_key: str | None = None          # if set, use ChangeConfiguration
    ocpp_on_value: str = "true"          # value to send when turning on
    ocpp_off_value: str = "false"        # value to send when turning off
    # If True, entity is only available on chargers whose reported model is in
    # SYNCEV_VENDOR_KEY_MODELS (see const.py) — used for vendor-proprietary
    # keys not confirmed to exist on every OCPP charger.
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
        metric_state=HAChargerStatuses.status.value,
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
        metric_state=HAChargerStatuses.status_connector.value,
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
    # --- OCPP ChangeConfiguration switches (safety/security-relevant first,
    # diagnostic-refresh toggle last) ---
    OcppSwitchDescription(
        key="unlock_connector_on_ev_side_disconnect",
        name="Auto-Unlock on Unplug",
        icon="mdi:lock-open",
        default_state=True,
        ocpp_key="UnlockConnectorOnEVSideDisconnect",
    ),
    # --- CT clamp reporting (paired with number.<cpid>_grid_current_interval
    # in number.py — that entity sets the report interval, this one toggles
    # reporting on/off; both act on the same underlying vendor feature) ---
    OcppSwitchDescription(
        key="get_ct_clamp_value",
        name="Enable CT Clamp",
        icon="mdi:gauge",
        # Confirmed working 2026-07-07 (see git history for a false-negative
        # first pass): the charger DOES stream fresh CT clamp readings once
        # this is set and GridCurrentInterval (number.py) is non-zero. The
        # readings were arriving the whole time even during the earlier
        # "no-op" test — the real bug was that on_data_transfer() never told
        # HA to refresh the sensor entities (now fixed, see on_data_transfer
        # in ocppv16.py). Benefits from the same real-state polling as every
        # other ocpp_key switch (see _apply_readback/_ocpp_refresh).
        default_state=False,
        ocpp_key="GetCTClampValue",
        ocpp_on_value="1",
        ocpp_off_value="0",
    ),
    OcppSwitchDescription(
        key="charge_on_plug_in",
        name="Charge When Plugged In (No Server Required)",
        icon="mdi:ev-plug-type2",
        # Vendor key "ChargerMode" — per BG SyncEV's own installer app docs,
        # this field has 3 named modes: APP (smart charging via app/OCPP,
        # value likely "1"), RFID (value likely "2" — not applicable, this
        # unit has no RFID reader), and "Plug And Charge" (non-smart,
        # autonomous — charger self-authorizes and starts as soon as a
        # vehicle is plugged in, no server needed; value confirmed "3" via
        # live test on 2026-07-07: setting it to 3 while a car was already
        # plugged in caused an immediate self-authorized StartTransaction
        # with idTag "freeIdTag"). The 1/3 mapping to APP/Plug-And-Charge is
        # a strong behavioral inference, not vendor-documented — verify
        # after use if the charger ever ships firmware that changes this.
        default_state=False,
        ocpp_key="ChargerMode",
        ocpp_on_value="3",
        ocpp_off_value="1",
        model_gated=True,
    ),
]

# Switch keys removed from SWITCHES above — clean up their stale entity
# registry entries on setup so they don't linger as permanently unavailable.
REMOVED_SWITCH_KEYS: Final = [
    "authorize_remote_tx_requests",  # removed, not used
    # Confirmed via live GetConfiguration on SL320S647 (2026-07-07): both
    # keys report readonly:true on this charger. configure() already
    # silently no-ops on readonly keys, so these toggles never did anything
    # — removed rather than leaving a switch that can't actually switch.
    "stop_transaction_on_ev_side_disconnect",  # StopTransactionOnEVSideDisconnect readonly:true
    "stop_transaction_on_invalid_id",  # StopTransactionOnInvalidId readonly:true
    # Removed 2026-07-07: LocalAuthListEnabled governs whether the charger
    # checks idTags against its own cached local list (populated via the
    # OCPP SendLocalList action). Confirmed by code inspection — neither
    # this integration nor pristine upstream lbbrhzn/ocpp implements
    # SendLocalList anywhere — so that list is guaranteed permanently empty.
    # Per the OCPP 1.6 spec, an empty local list just falls through to the
    # normal Authorize.req/RemoteStartTransaction flow already in use, so
    # this key has zero practical effect regardless of its value.
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

        for old_key in REMOVED_SWITCH_KEYS:
            uid = ".".join([SWITCH_DOMAIN, DOMAIN, cpid, old_key])
            stale_eid = ent_reg.async_get_entity_id(SWITCH_DOMAIN, DOMAIN, uid)
            if stale_eid:
                ent_reg.async_remove(stale_eid)

        if num_connectors > 1:
            for desc in SWITCHES:
                if not desc.per_connector:
                    continue
                uid_flat = ".".join([SWITCH_DOMAIN, DOMAIN, cpid, desc.key])
                stale_eid = ent_reg.async_get_entity_id(SWITCH_DOMAIN, DOMAIN, uid_flat)
                if stale_eid:
                    ent_reg.async_remove(stale_eid)

        for desc in SWITCHES:
            if desc.per_connector:
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
        # Last raw value actually read back from the charger for ocpp_key
        # switches — populated after every write and on entity add, so the
        # switch reflects reality instead of just what we last commanded.
        # (Needed because e.g. ChargerMode=3 reads back as "2" on this
        # hardware — the write doesn't stick as sent.)
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
            return bool(self.central_system.get_available(self.cpid, None))
        target_conn = (
            self.connector_id if self.entity_description.per_connector else None
        )
        return bool(self.central_system.get_available(self.cpid, target_conn))

    @property
    def extra_state_attributes(self):
        """Expose the charger's raw reported value for ocpp_key switches.

        HA switches are binary, but some vendor keys (e.g. ChargerMode) have
        more than two possible values. raw_value surfaces exactly what the
        charger last reported, even when it doesn't cleanly map to on/off.
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

        Vendor keys don't always stick as written — e.g. this hardware's
        ChargerMode reads back as "2" immediately after being set to "3".
        Trusting the commanded value instead of the charger's own readback
        would silently drift from reality, so this is the single source of
        truth for is_on after any write or refresh.
        """
        self._raw_value = actual
        if actual is None:
            return
        if actual == self.entity_description.ocpp_on_value:
            self._state = True
        elif actual == self.entity_description.ocpp_off_value:
            self._state = False
        else:
            _LOGGER.warning(
                "%s (%s): charger reports '%s', which is neither the "
                "configured on-value ('%s') nor off-value ('%s'). Leaving "
                "switch as last commanded — check raw_value attribute.",
                self.cpid,
                self.entity_description.ocpp_key,
                actual,
                self.entity_description.ocpp_on_value,
                self.entity_description.ocpp_off_value,
            )

    async def _ocpp_refresh(self) -> None:
        """Read the charger's current value for this key and reconcile state."""
        try:
            cp_id = self.central_system.cpids.get(self.cpid, self.cpid)
            cp = self.central_system.charge_points.get(cp_id)
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
        """Send ChangeConfiguration to the charger, then confirm what stuck."""
        try:
            cp_id = self.central_system.cpids.get(self.cpid, self.cpid)
            cp = self.central_system.charge_points.get(cp_id)
            if cp is None:
                _LOGGER.warning(
                    "Cannot set %s: charger %s not connected.",
                    self.entity_description.ocpp_key, self.cpid,
                )
                return
            result = await cp.configure(self.entity_description.ocpp_key, value)
            _LOGGER.debug(
                "Set %s = %s, result: %s",
                self.entity_description.ocpp_key, value, result,
            )
            # Confirm what actually stuck rather than trusting the write.
            actual = await cp.get_configuration(self.entity_description.ocpp_key)
            self._apply_readback(actual)
        except Exception as ex:
            _LOGGER.warning("Failed to set %s: %s", self.entity_description.ocpp_key, ex)
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

            # ocpp_key switches: opportunistically (re)confirm the real
            # charger value on every dispatcher tick until it succeeds once.
            # A single attempt at add-time is not enough — right after an HA
            # restart the entity is added before the charger's websocket has
            # reconnected, so cp is None and the refresh silently no-ops,
            # leaving the switch stuck on its constructor default forever.
            # This was confirmed live 2026-07-07: GetCTClampValue was "1" on
            # the charger the whole time, but the switch showed "off" with
            # no raw_value attribute at all, because the one-shot refresh
            # never got a second chance to run.
            if (
                self.entity_description.ocpp_key
                and self._raw_value is None
                and self.central_system.get_available(self.cpid, None)
            ):
                self.hass.async_create_task(self._ocpp_refresh())

        self.async_on_remove(async_dispatcher_connect(self.hass, DATA_UPDATED, update))
        self.async_schedule_update_ha_state(True)

        if self.entity_description.ocpp_key:
            # Sync with the charger's actual value on startup rather than
            # trusting default_state — the charger may already be in a
            # non-default state from before HA started (or restarted).
            self.hass.async_create_task(self._ocpp_refresh())