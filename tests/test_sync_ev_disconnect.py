"""Tests for the BG Sync EV / Sync Energy unload workaround."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.protocol import State

from custom_components.ocpp import _abort_sync_charge_points
from custom_components.ocpp.chargepoint import ChargePoint
from custom_components.ocpp.enums import HAChargerDetails as cdet


def _charge_point_with_vendor(vendor: str | None) -> ChargePoint:
    charge_point = object.__new__(ChargePoint)
    metric = MagicMock()
    metric.value = vendor
    charge_point._metrics = {(0, cdet.vendor.value): metric}
    return charge_point


@pytest.mark.parametrize(
    ("vendor", "expected"),
    [
        ("Sync Energy", True),
        ("SYNC ENERGY", True),
        ("BG Sync EV", True),
        ("BG SyncEV", True),
        ("sync ev", True),
        ("syncev", True),
        ("Wallbox", False),
        ("Easee", False),
        ("ABB", False),
        ("", False),
        (None, False),
    ],
)
def test_requires_abrupt_disconnect_is_sync_specific(vendor, expected):
    """Detection is narrow and case-insensitive."""
    charge_point = _charge_point_with_vendor(vendor)
    assert charge_point.requires_abrupt_disconnect is expected


def test_abort_sync_charge_points_only_aborts_sync():
    """Only Sync hardware is hard-aborted before normal server shutdown."""
    sync_cp = MagicMock()
    sync_cp.id = "sync"
    sync_cp.requires_abrupt_disconnect = True
    sync_cp._aborting_connection = False

    generic_cp = MagicMock()
    generic_cp.id = "generic"
    generic_cp.requires_abrupt_disconnect = False
    generic_cp._aborting_connection = False

    central = MagicMock()
    central.charge_points = {"sync": sync_cp, "generic": generic_cp}

    _abort_sync_charge_points(central)

    assert sync_cp._aborting_connection is True
    sync_cp._connection.transport.abort.assert_called_once_with()

    assert generic_cp._aborting_connection is False
    generic_cp._connection.transport.abort.assert_not_called()


def _session_for_close(aborting: bool):
    """Build a charge point whose session close can be observed."""
    charge_point = object.__new__(ChargePoint)
    charge_point.id = "TEST"
    charge_point._aborting_connection = aborting
    connection = MagicMock()
    connection.state = State.OPEN
    connection.close = AsyncMock()
    session = {"connection": connection, "tasks": (), "cleanup": None}
    charge_point._retirement_tasks = set()
    return charge_point, session, connection


@pytest.mark.asyncio
async def test_close_skips_graceful_close_after_sync_abort():
    """A Sync unload must not send a second graceful WebSocket CLOSE."""
    charge_point, session, connection = _session_for_close(aborting=True)

    await ChargePoint._close_session(charge_point, session, None)

    connection.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_keeps_graceful_close_for_normal_connection():
    """Non-Sync and normal disconnects retain the existing close behavior."""
    charge_point, session, connection = _session_for_close(aborting=False)

    await ChargePoint._close_session(charge_point, session, None)

    connection.close.assert_awaited_once_with()


def test_model_alone_identifies_a_sync_charger():
    """A known Sync model counts even when the vendor string is unexpected."""
    charge_point = object.__new__(ChargePoint)
    vendor, model = MagicMock(), MagicMock()
    vendor.value, model.value = "Unknown", "EVSC7S"
    charge_point._metrics = {
        (0, cdet.vendor.value): vendor,
        (0, cdet.model.value): model,
    }
    assert charge_point.is_sync_charger is True
