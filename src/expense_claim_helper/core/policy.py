"""Explicit fictional policy rules and exact day/allowance arithmetic."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import yaml

from .common import ClaimError, cents

DEFAULT_POLICY = {
    "version": 1,
    "currency": "CNY",
    "day_rule": "half_day",
    "cutoff_hour": 12,
    "same_day_half_hours": 6,
    "meal_mode": "allowance",
    "local_transport_mode": "allowance",
    "require_receipts": ["flight", "train", "lodging", "meeting", "office", "other"],
    "tiers": {
        "A": {"lodging_per_night": "400.00", "meal_per_day": "80.00", "transport_per_day": "40.00"},
        "B": {"lodging_per_night": "300.00", "meal_per_day": "60.00", "transport_per_day": "30.00"},
        "C": {"lodging_per_night": "200.00", "meal_per_day": "50.00", "transport_per_day": "20.00"},
    },
    "allowed_classes": {"flight": ["economy"], "train": ["second", "standard"]},
}
CATEGORIES = {
    "flight",
    "train",
    "taxi",
    "transport",
    "lodging",
    "meals",
    "meeting",
    "office",
    "other",
}
ALIASES = {
    "transport": "transport",
    "lodging": "lodging",
    "meals": "meals",
    "office": "office",
    "交通": "transport",
    "住宿": "lodging",
    "餐饮": "meals",
    "办公": "office",
    "机票": "flight",
    "火车": "train",
    "打车": "taxi",
    "会议": "meeting",
    "其他": "other",
}


def category(value: str) -> str:
    key = ALIASES.get(value.casefold(), value.casefold())
    if key not in CATEGORIES:
        raise ClaimError(
            "INVALID_CATEGORY", f"Category must be one of {', '.join(sorted(CATEGORIES))}"
        )
    return key


def validate_policy(policy: dict) -> dict:
    if not isinstance(policy, dict) or set(policy) != set(DEFAULT_POLICY):
        raise ClaimError("INVALID_POLICY", "Policy must contain exactly the documented keys")
    if policy["version"] != 1 or policy["day_rule"] not in {"half_day", "calendar_day"}:
        raise ClaimError("INVALID_POLICY", "Unsupported policy version or day rule")
    if type(policy["cutoff_hour"]) is not int or not 1 <= policy["cutoff_hour"] <= 23:
        raise ClaimError("INVALID_POLICY", "cutoff_hour must be between 1 and 23")
    if (
        type(policy["same_day_half_hours"]) is not int
        or not 1 <= policy["same_day_half_hours"] <= 23
    ):
        raise ClaimError("INVALID_POLICY", "same_day_half_hours must be between 1 and 23")
    if (
        not isinstance(policy["currency"], str)
        or len(policy["currency"]) != 3
        or not policy["currency"].isascii()
        or not policy["currency"].isalpha()
    ):
        raise ClaimError("INVALID_POLICY", "currency must be a three-letter code")
    for key in ["meal_mode", "local_transport_mode"]:
        if policy[key] not in {"allowance", "actual"}:
            raise ClaimError("INVALID_POLICY", f"{key} must be allowance or actual")
    if not isinstance(policy["require_receipts"], list) or any(
        value not in CATEGORIES for value in policy["require_receipts"]
    ):
        raise ClaimError("INVALID_POLICY", "require_receipts must contain supported categories")
    if not isinstance(policy["tiers"], dict) or not policy["tiers"]:
        raise ClaimError("INVALID_POLICY", "At least one city tier is required")
    for name, rule in policy["tiers"].items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(rule, dict)
            or set(rule) != {"lodging_per_night", "meal_per_day", "transport_per_day"}
        ):
            raise ClaimError(
                "INVALID_POLICY", "Each tier requires lodging, meal and transport limits"
            )
        for value in rule.values():
            cents(value)
    if not isinstance(policy["allowed_classes"], dict):
        raise ClaimError("INVALID_POLICY", "allowed_classes must be an object")
    for key, values in policy["allowed_classes"].items():
        if (
            key not in {"flight", "train"}
            or not isinstance(values, list)
            or not values
            or not all(isinstance(v, str) and v for v in values)
        ):
            raise ClaimError("INVALID_POLICY", "Transport class rules are invalid")
    return policy


def load_policy(root, config: dict) -> dict:
    path = config.get("policy_file") or root / "policy.yaml"
    from pathlib import Path

    policy = (
        yaml.safe_load(Path(path).read_text("utf-8"))
        if Path(path).exists()
        else yaml.safe_load(yaml.safe_dump(DEFAULT_POLICY))
    )
    return validate_policy(policy)


def local_datetime(value: str, timezone: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ClaimError("INVALID_TIME", "Use ISO date/time values") from None
    zone = ZoneInfo(timezone)
    return result.replace(tzinfo=zone) if result.tzinfo is None else result.astimezone(zone)


def day_units(start_at: str, end_at: str, policy: dict, timezone: str) -> dict[str, int]:
    """Return half-day units per local calendar date, including explicit boundary rules."""
    start, end = local_datetime(start_at, timezone), local_datetime(end_at, timezone)
    if end <= start or (end - start).days > 366:
        raise ClaimError("INVALID_TRIP", "Trip must last more than zero and at most 367 days")
    result = {}
    current = start.date()
    while current <= end.date():
        if policy["day_rule"] == "calendar_day":
            units = 2
        elif start.date() == end.date():
            units = (
                1 if (end - start).total_seconds() <= policy["same_day_half_hours"] * 3600 else 2
            )
        elif current == start.date():
            units = 1 if start.time() >= time(policy["cutoff_hour"]) else 2
        elif current == end.date():
            units = 1 if end.time() <= time(policy["cutoff_hour"]) else 2
        else:
            units = 2
        result[current.isoformat()] = units
        current += timedelta(days=1)
    return result


def days_text(units: int) -> str:
    return str(Decimal(units) / 2)


def expense_allowance(expense: dict, trip: dict, policy: dict) -> tuple[int, list[str]]:
    """Return reimbursable cents and policy flags for one actual expense."""
    amount = expense["amount_cents"]
    flags = []
    if not expense["included"]:
        return 0, ["excluded pending review"]
    if expense["paid_by"] != "employee":
        return 0, ["company paid; no employee reimbursement"]
    kind = expense["category"]
    if (kind == "meals" and policy["meal_mode"] == "allowance") or (
        kind in {"taxi", "transport"} and policy["local_transport_mode"] == "allowance"
    ):
        return 0, ["covered by daily allowance"]
    eligible = amount
    if kind == "lodging":
        limit = (
            cents(policy["tiers"][trip["city_tier"]]["lodging_per_night"])
            * expense["nights"]
            * trip["participants"]
        )
        eligible = min(amount, limit)
        if eligible < amount:
            flags.append("above lodging cap")
    if (
        kind in policy["allowed_classes"]
        and expense["travel_class"] not in policy["allowed_classes"][kind]
    ):
        eligible = 0
        flags.append("transport class requires review")
    if kind in policy["require_receipts"] and not expense["receipt_path"]:
        eligible = 0
        flags.append("receipt required")
    if expense.get("source_duplicate_of"):
        flags.append("duplicate source explicitly included")
    return eligible, flags


def allowance_rows(trip: dict, dates: dict[str, int], policy: dict) -> list[dict]:
    rows = []
    tier = policy["tiers"][trip["city_tier"]]
    for day, units in dates.items():
        for mode, key, label in [
            ("meal_mode", "meal_per_day", "meal_allowance"),
            ("local_transport_mode", "transport_per_day", "transport_allowance"),
        ]:
            if policy[mode] != "allowance":
                continue
            # Half-cent allowances are rounded down per trip/day to avoid overpayment.
            amount = cents(tier[key]) * units * trip["participants"] // 2
            rows.append(
                {
                    "trip_id": trip["id"],
                    "date": day,
                    "destination": trip["destination"],
                    "category": label,
                    "description": (
                        f"{days_text(units)} day(s) × {trip['participants']} participant(s)"
                    ),
                    "amount_cents": 0,
                    "eligible_cents": amount,
                    "self_paid_cents": 0,
                    "receipt_path": "",
                    "flags": [],
                    "expense_id": None,
                }
            )
    return rows


def month_bounds(value: str) -> tuple[date, date]:
    try:
        start = date.fromisoformat(value + "-01")
    except ValueError:
        raise ClaimError("INVALID_MONTH", "Month must have YYYY-MM format") from None
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end
