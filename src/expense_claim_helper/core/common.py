"""Shared validation, exact money and configuration."""

import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


class ClaimError(Exception):
    def __init__(self, code: str, message: str, status: int = 2):
        super().__init__(message)
        self.code, self.status = code, status


def cents(value: object) -> int:
    if isinstance(value, bool) or value is None:
        raise ClaimError("INVALID_AMOUNT", "Amount must be a decimal string")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("9999999999.99"):
            raise InvalidOperation
        result = amount * 100
        if result != result.to_integral_value():
            raise InvalidOperation
        return int(result)
    except InvalidOperation:
        raise ClaimError(
            "INVALID_AMOUNT", f"Invalid amount with at most two decimals: {value}"
        ) from None


def money(value: int) -> str:
    return f"{Decimal(value) / 100:.2f}"


def success(data: object) -> dict:
    return {
        "ok": True,
        "data": data,
        "warnings": data.get("warnings", []) if isinstance(data, dict) else [],
    }


def failure(exc: Exception) -> tuple[dict, int]:
    if isinstance(exc, ClaimError):
        code, status = exc.code, exc.status
    elif isinstance(exc, FileNotFoundError):
        code, status = "NOT_FOUND", 3
    elif isinstance(exc, (ValueError, TypeError, KeyError, yaml.YAMLError)):
        code, status = "INVALID_DATA", 2
    else:
        code, status = "PROCESSING_ERROR", 1
    return {"ok": False, "error": {"code": code, "message": str(exc)}}, status


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def settings(
    data_dir: str | Path | None = None, config_path: str | None = None
) -> tuple[Path, dict]:
    root = Path(data_dir or os.environ.get("ECH_DATA_DIR", "data")).expanduser().absolute()
    path = Path(config_path) if config_path else root / "config.yaml"
    config = {
        "timezone": "Asia/Shanghai",
        "policy_file": None,
        "unsubmitted_days": 14,
        "unpaid_days": 30,
        "max_receipt_mb": 30,
    }
    if path.is_file():
        loaded = yaml.safe_load(path.read_text("utf-8")) or {}
        if not isinstance(loaded, dict) or set(loaded) - set(config):
            raise ClaimError("INVALID_CONFIG", "Unknown configuration keys")
        config.update(loaded)
        if config["policy_file"]:
            config["policy_file"] = str((path.parent / config["policy_file"]).resolve())
    ZoneInfo(config["timezone"])
    for key in ["unsubmitted_days", "unpaid_days", "max_receipt_mb"]:
        if type(config[key]) is not int or config[key] < 1:
            raise ClaimError("INVALID_CONFIG", f"{key} must be a positive integer")
    return root, config


def timestamp(config: dict) -> str:
    return datetime.now(ZoneInfo(config["timezone"])).isoformat(timespec="seconds")
