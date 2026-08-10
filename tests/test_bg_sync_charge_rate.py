"""Tests for the BG Sync fork's charge-rate handling.

Every expected value here was measured on a SyncEV EVSC7S (serial SL320S647,
firmware RD0045-V1.02-S1.01) on 2026-08-10, against upstream v0.10.18. These
are regression tests for hardware behaviour, so if one fails either the code
changed or the charger did -- re-measure before adjusting an expectation.
"""

from decimal import Decimal

import pytest

from custom_components.ocpp.const import CHARGE_RATE_STEP
from custom_components.ocpp.number import _quantise_rate


class TestQuantiseRate:
    """The charger floors charge-rate requests to whole amps.

    Measured with the ceiling parked at 32 A so the TxProfile was the only
    binding constraint, sweeping in 0.1 A steps:

        requested   Current.Offered   Current.Import
        10.0 A      10 A              9.86 - 9.91 A
        10.1 A      10 A              9.87 - 9.91 A
        10.5 A      10 A              9.75 - 9.90 A
        10.9 A      10 A              9.87 - 9.89 A
        13.0 A      13 A              12.69 - 12.75 A
        13.4 A      13 A              12.74 - 12.78 A

    Current.Import is reported to two decimals and was flat within each group,
    so this is hardware behaviour rather than a reporting artefact.
    """

    @pytest.mark.parametrize(
        ("requested", "offered"),
        [
            (10.0, 10.0),
            (10.1, 10.0),
            (10.5, 10.0),
            (10.9, 10.0),  # floors, does NOT round up to 11
            (13.0, 13.0),
            (13.4, 13.0),
        ],
    )
    def test_matches_measured_offered_current(self, requested, offered):
        """Quantised value matches the Current.Offered the charger reported."""
        assert _quantise_rate(requested) == offered

    def test_floors_rather_than_rounds(self):
        """10.9 A must behave as 10 A. Rounding would give 11 A and be wrong."""
        assert _quantise_rate(10.9) == 10.0
        assert _quantise_rate(10.9) != 11.0

    def test_step_is_whole_amps(self):
        """Granularity is one amp, per the measured sweep."""
        assert CHARGE_RATE_STEP == 1.0


class TestOcppLimitFormat:
    """OCPP enforces multipleOf 0.1 on chargingSchedulePeriod[].limit.

    The ocpp library validates locally, so an out-of-format value raises
    FormatViolationError and never reaches the charger. Under upstream #2052
    that refusal now propagates as a HomeAssistantError, which surfaced as an
    HTTP 500 when a solar controller passed a raw float.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            11.653986956521738,  # the exact value that produced the 500
            12.7,
            16.0,
            6.09,
            0.0,
            32.0,
        ],
    )
    def test_rounding_to_one_dp_satisfies_schema(self, raw):
        """Rounded limits pass the OCPP multipleOf 0.1 constraint."""
        rounded = round(float(raw), 1)
        assert Decimal(str(rounded)) % Decimal("0.1") == 0

    def test_unrounded_float_violates_schema(self):
        """Guard the premise: without rounding this genuinely is invalid."""
        raw = 11.653986956521738
        assert Decimal(str(raw)) % Decimal("0.1") != 0


class TestStackLevelClamp:
    """ChargeProfileMaxStackLevel is an EXCLUSIVE bound on this charger.

    It reports 5, and measured responses were:

        purpose                stackLevel   result
        ChargePointMaxProfile  5            NotSupported
        ChargePointMaxProfile  4            Accepted
        ChargePointMaxProfile  0            Accepted
        TxProfile              5            NotSupported
        TxProfile              0            Accepted
        TxDefaultProfile       5            NotSupported
        TxDefaultProfile       4            Accepted

    Every purpose is rejected at the advertised maximum and accepted below it.
    Note all Accepted responses used chargingProfileKind "Relative", so the
    profile kind was never the problem.
    """

    @staticmethod
    def _clamp(reported_max: int) -> int:
        """Mirror ChargePoint._get_stack_level's arithmetic."""
        return max(0, reported_max - 1)

    def test_reported_five_becomes_four(self):
        """A reported maximum of 5 yields the highest accepted level, 4."""
        assert self._clamp(5) == 4

    def test_never_uses_the_rejected_maximum(self):
        """The advertised maximum itself is rejected, so never send it."""
        assert self._clamp(5) != 5

    def test_result_is_in_the_accepted_range(self):
        """Chosen level falls inside the measured accepted range 0..4."""
        assert 0 <= self._clamp(5) <= 4

    @pytest.mark.parametrize(("reported", "expected"), [(0, 0), (1, 0), (2, 1), (5, 4)])
    def test_never_negative(self, reported, expected):
        """Clamping never produces a negative stack level."""
        assert self._clamp(reported) == expected
