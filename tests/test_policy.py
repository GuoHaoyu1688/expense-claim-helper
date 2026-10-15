from copy import deepcopy

import pytest

from expense_claim_helper.core.common import ClaimError, cents
from expense_claim_helper.core.policy import (
    DEFAULT_POLICY,
    allowance_rows,
    day_units,
    expense_allowance,
    validate_policy,
)


@pytest.mark.parametrize(
    "start,end,tier,participants,units,allowance",
    [
        ("2026-01-03T09:00", "2026-01-03T12:00", "A", 1, 1, 6000),
        ("2026-01-03T08:00", "2026-01-03T18:00", "B", 1, 2, 9000),
        ("2026-01-30T15:00", "2026-02-02T10:00", "B", 1, 6, 27000),
        ("2026-01-01T08:00", "2026-01-02T18:00", "A", 1, 4, 24000),
        ("2026-01-01T15:00", "2026-01-02T10:00", "C", 2, 2, 14000),
        ("2025-12-31T15:00", "2026-01-01T10:00", "C", 1, 2, 7000),
    ],
)
def test_six_hand_calculated_allowances(start, end, tier, participants, units, allowance):
    dates = day_units(start, end, DEFAULT_POLICY, "Asia/Shanghai")
    assert sum(dates.values()) == units
    rows = allowance_rows(
        {"id": 1, "city_tier": tier, "participants": participants, "destination": "CITY-X"},
        dates,
        DEFAULT_POLICY,
    )
    assert sum(row["eligible_cents"] for row in rows) == allowance


def test_caps_and_classes_hand_calculation():
    trip = {"city_tier": "B", "participants": 1}
    expense = {
        "amount_cents": 76000,
        "category": "lodging",
        "nights": 2,
        "included": True,
        "paid_by": "employee",
        "receipt_path": "receipt.png",
        "travel_class": "",
    }
    eligible, flags = expense_allowance(expense, trip, DEFAULT_POLICY)
    assert eligible == 60000 and 76000 - eligible == 16000 and "above lodging cap" in flags
    expense.update(category="train", amount_cents=18000, travel_class="first")
    assert expense_allowance(expense, trip, DEFAULT_POLICY)[0] == 0
    expense["travel_class"] = "second"
    assert expense_allowance(expense, trip, DEFAULT_POLICY)[0] == 18000
    expense["paid_by"] = "company"
    assert expense_allowance(expense, trip, DEFAULT_POLICY)[0] == 0


def test_policy_modes_and_invalid_values():
    policy = deepcopy(DEFAULT_POLICY)
    policy["day_rule"] = "calendar_day"
    assert day_units("2026-01-01T15:00", "2026-01-02T10:00", policy, "Asia/Shanghai") == {
        "2026-01-01": 2,
        "2026-01-02": 2,
    }
    policy["meal_mode"] = "actual"
    policy["local_transport_mode"] = "actual"
    assert (
        allowance_rows(
            {"id": 1, "city_tier": "B", "participants": 1, "destination": "CITY-X"},
            {"2026-01-01": 2},
            policy,
        )
        == []
    )
    policy["tiers"]["B"]["lodging_per_night"] = "NaN"
    with pytest.raises(ClaimError):
        validate_policy(policy)
    for value in ["1.001", "NaN", "Infinity", "-1.00", True]:
        with pytest.raises(ClaimError):
            cents(value)
    with pytest.raises(ClaimError):
        day_units("2026-01-01T10:00", "2026-01-01T09:00", DEFAULT_POLICY, "Asia/Shanghai")
