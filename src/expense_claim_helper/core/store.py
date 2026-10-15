"""Local trip/expense persistence and immutable claim snapshots."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import yaml

from .common import ClaimError, canonical, cents, money, timestamp
from .interchange import InterchangeMixin
from .policy import (
    allowance_rows,
    category,
    day_units,
    days_text,
    expense_allowance,
    load_policy,
    local_datetime,
    month_bounds,
)


class Store(InterchangeMixin):
    def __init__(self, root: Path, config: dict):
        self.root, self.config = root, config
        root.mkdir(parents=True, exist_ok=True)
        self.policy = load_policy(root, config)
        self.db = sqlite3.connect(root / "expenses.db", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS trips(id INTEGER PRIMARY KEY,start_at TEXT,end_at TEXT,
          origin TEXT,
          destination TEXT,city_tier TEXT,purpose_code TEXT,participants INTEGER,transport TEXT,
          identity_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,path TEXT,original_name TEXT,
          amount_cents INTEGER,created_at TEXT);
        CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY,
          trip_id INTEGER REFERENCES trips(id),
          date TEXT,category TEXT,amount_cents INTEGER,currency TEXT,paid_by TEXT,
          payment_method TEXT,
          has_receipt INTEGER,receipt_id TEXT REFERENCES receipts(id),invoice_type TEXT,
          invoice_no TEXT,
          nights INTEGER,travel_class TEXT,note TEXT,included INTEGER,source_duplicate_of TEXT,
          identity_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS policy_snapshots(id TEXT PRIMARY KEY,yaml TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY,code TEXT UNIQUE,scope_key TEXT,
          trip_ids_json TEXT,status TEXT,created_at TEXT,submitted_at TEXT,approved_at TEXT,
          paid_at TEXT,paid_cents INTEGER,policy_id TEXT REFERENCES policy_snapshots(id),
          snapshot_json TEXT);

        CREATE TABLE IF NOT EXISTS claim_expenses(
          expense_id INTEGER PRIMARY KEY REFERENCES expenses(id),
          claim_code TEXT REFERENCES claims(code));
        CREATE TABLE IF NOT EXISTS claim_days(trip_id INTEGER REFERENCES trips(id),day TEXT,
          units INTEGER,claim_code TEXT REFERENCES claims(code),PRIMARY KEY(trip_id,day));
        CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY,
          claim_code TEXT REFERENCES claims(code),
          date TEXT,amount_cents INTEGER);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def trip(self, identifier: int) -> dict:
        row = self.db.execute("SELECT * FROM trips WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ClaimError("NOT_FOUND", f"Trip not found: {identifier}", 3)
        item = dict(row)
        item.pop("identity_key")
        units = day_units(item["start_at"], item["end_at"], self.policy, self.config["timezone"])
        item["travel_days"] = days_text(sum(units.values()))
        return item

    def trips(self) -> list[dict]:
        return [
            self.trip(row[0])
            for row in self.db.execute("SELECT id FROM trips ORDER BY start_at,id")
        ]

    def add_trip(
        self,
        start_at: str,
        end_at: str,
        origin: str,
        destination: str,
        city_tier: str = "B",
        purpose_code: str = "SAMPLE",
        participants: int = 1,
        transport: str = "train",
    ) -> dict:
        if city_tier not in self.policy["tiers"]:
            raise ClaimError("INVALID_TIER", "City tier is not defined in policy")
        if type(participants) is not int or not 1 <= participants <= 100:
            raise ClaimError(
                "INVALID_PARTICIPANTS", "participants must be an integer from 1 to 100"
            )
        for value in [origin, destination, purpose_code, transport]:
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
                raise ClaimError("INVALID_TRIP", "Trip labels must contain 1 to 200 characters")
        start = local_datetime(start_at, self.config["timezone"]).isoformat()
        end = local_datetime(end_at, self.config["timezone"]).isoformat()
        day_units(start, end, self.policy, self.config["timezone"])
        values = [
            start,
            end,
            origin.strip(),
            destination.strip(),
            city_tier,
            purpose_code.strip(),
            participants,
            transport,
        ]
        key = hashlib.sha256(canonical(values).encode()).hexdigest()
        old = self.db.execute("SELECT id FROM trips WHERE identity_key=?", (key,)).fetchone()
        if old:
            return {**self.trip(old[0]), "reused": True}
        for row in self.db.execute("SELECT start_at,end_at FROM trips"):
            if local_datetime(row[0], self.config["timezone"]) < local_datetime(
                end, self.config["timezone"]
            ) and local_datetime(row[1], self.config["timezone"]) > local_datetime(
                start, self.config["timezone"]
            ):
                raise ClaimError("TRIP_OVERLAP", "Trip times overlap an existing itinerary")
        with self.transaction():
            cursor = self.db.execute(
                "INSERT INTO trips(start_at,end_at,origin,destination,city_tier,purpose_code"
                ",participants,transport,identity_key) VALUES(?,?,?,?,?,?,?,?,?)",
                [*values, key],
            )
        return {**self.trip(cursor.lastrowid), "reused": False}

    def add_receipt(self, path: str, amount: str | None = None) -> dict:
        source = Path(path).expanduser().absolute()
        if any(p.is_symlink() for p in [source, *source.parents]):
            raise ClaimError("UNSAFE_RECEIPT", "Symlink receipts are not supported")
        if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".pdf", ".heic", ".webp"}:
            raise ClaimError("INVALID_RECEIPT", "Receipt must be an image or PDF")
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            limit = self.config["max_receipt_mb"] * 1024 * 1024
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ClaimError("INVALID_RECEIPT", "Receipt is not a bounded regular file")
            content = file.read(limit + 1)
        if len(content) > limit:
            raise ClaimError("INVALID_RECEIPT", "Receipt exceeds size limit")
        digest = hashlib.sha256(content).hexdigest()
        old = self.db.execute("SELECT * FROM receipts WHERE id=?", (digest,)).fetchone()
        expected = cents(amount) if amount not in {None, ""} else None
        if old:
            if (
                expected is not None
                and old["amount_cents"] is not None
                and expected != old["amount_cents"]
            ):
                raise ClaimError(
                    "RECEIPT_AMOUNT_CONFLICT", "Same receipt has conflicting source amounts"
                )
            return dict(old)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", source.name)[:100]
        destination = self.root / "receipts" / f"{digest[:16]}-{safe}"
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(content)
        with self.transaction():
            self.db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?)",
                (digest, str(destination), source.name, expected, timestamp(self.config)),
            )
        return dict(self.db.execute("SELECT * FROM receipts WHERE id=?", (digest,)).fetchone())

    def expense(self, identifier: int) -> dict:
        row = self.db.execute(
            "SELECT e.*,r.path AS receipt_path,r.amount_cents AS receipt_amount_cents "
            "FROM expenses e LEFT JOIN receipts r ON e.receipt_id=r.id WHERE e.id=?",
            (identifier,),
        ).fetchone()
        if not row:
            raise ClaimError("NOT_FOUND", f"Expense not found: {identifier}", 3)
        item = dict(row)
        item.pop("identity_key")
        item["receipt_path"] = item["receipt_path"] or ""
        item["amount"] = money(item["amount_cents"])
        item["has_receipt"] = bool(item["has_receipt"])
        item["included"] = bool(item["included"])
        claimed = self.db.execute(
            "SELECT claim_code FROM claim_expenses WHERE expense_id=?", (identifier,)
        ).fetchone()
        item["claim_code"] = claimed[0] if claimed else None
        if item["trip_id"]:
            item["eligible_cents"], item["flags"] = expense_allowance(
                item, self.trip(item["trip_id"]), self.policy
            )
        else:
            item["eligible_cents"], item["flags"] = 0, ["unmatched trip; assign before claiming"]
        return item

    def expenses(self, trip_id: int | None = None) -> list[dict]:
        rows = self.db.execute(
            "SELECT id FROM expenses WHERE (? IS NULL OR trip_id=?) ORDER BY date,id",
            (trip_id, trip_id),
        )
        return [self.expense(row[0]) for row in rows]

    def add_expense(
        self,
        trip_id: int | None,
        category_name: str,
        amount: str,
        expense_date: str | None = None,
        paid_by: str = "employee",
        payment_method: str = "card",
        receipt: str | None = None,
        has_receipt: bool = False,
        invoice_type: str = "receipt",
        invoice_no: str = "",
        nights: int = 1,
        travel_class: str = "",
        note: str = "",
        currency: str | None = None,
        included: bool = True,
        source_duplicate_of: str = "",
        source_key: str | None = None,
        receipt_amount: str | None = None,
    ) -> dict:
        trip = self.trip(trip_id) if trip_id is not None else None
        day = expense_date or (trip["start_at"][:10] if trip else None)
        if not day or date.fromisoformat(day).isoformat() != day:
            raise ClaimError("INVALID_DATE", "Expense date must be ISO YYYY-MM-DD")
        if trip and not trip["start_at"][:10] <= day <= trip["end_at"][:10]:
            raise ClaimError("DATE_OUTSIDE_TRIP", "Expense date is outside trip dates")
        kind = category(category_name)
        unit = currency or self.policy["currency"]
        if unit != self.policy["currency"]:
            raise ClaimError("CURRENCY_MISMATCH", "Convert currency explicitly before importing")
        if paid_by not in {"employee", "company"}:
            raise ClaimError("INVALID_PAYER", "paid_by must be employee or company")
        if type(nights) is not int or not 1 <= nights <= 366:
            raise ClaimError("INVALID_NIGHTS", "nights must be between 1 and 366")
        if any(
            not isinstance(v, str) or len(v) > 2000
            for v in [
                payment_method,
                invoice_type,
                invoice_no,
                travel_class,
                note,
                source_duplicate_of,
            ]
        ):
            raise ClaimError("INVALID_EXPENSE", "Text fields must be at most 2000 characters")
        value = cents(amount)
        signature = [
            trip_id,
            day,
            kind,
            value,
            unit,
            paid_by,
            payment_method,
            str(receipt or ""),
            invoice_type,
            invoice_no,
            nights,
            travel_class,
            note,
        ]
        identity = source_key or hashlib.sha256(canonical(signature).encode()).hexdigest()
        old = self.db.execute(
            "SELECT id FROM expenses WHERE identity_key=?", (identity,)
        ).fetchone()
        if old:
            return {**self.expense(old[0]), "reused": True}
        attachment = self.add_receipt(receipt, receipt_amount) if receipt else None
        with self.transaction():
            cursor = self.db.execute(
                "INSERT INTO expenses(trip_id,date,category,amount_cents,currency,paid_by,pa"
                "yment_method,has_receipt,receipt_id,invoice_type,invoice_no,nights,travel_c"
                "lass,note,included,source_duplicate_of,identity_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trip_id,
                    day,
                    kind,
                    value,
                    unit,
                    paid_by,
                    payment_method,
                    int(has_receipt or bool(attachment)),
                    attachment["id"] if attachment else None,
                    invoice_type,
                    invoice_no,
                    nights,
                    travel_class,
                    note,
                    int(included),
                    source_duplicate_of,
                    identity,
                ),
            )
        return {**self.expense(cursor.lastrowid), "reused": False}

    def review_expense(
        self,
        identifier: int,
        trip_id: int | None = None,
        included: bool | None = None,
        amount: str | None = None,
        note: str | None = None,
    ) -> dict:
        item = self.expense(identifier)
        if item["claim_code"]:
            raise ClaimError(
                "CLAIM_LOCKED", "Claimed expenses are immutable; record a separate adjustment"
            )
        if trip_id is not None:
            trip = self.trip(trip_id)
            if not trip["start_at"][:10] <= item["date"] <= trip["end_at"][:10]:
                raise ClaimError("DATE_OUTSIDE_TRIP", "Expense date is outside selected trip")
            item["trip_id"] = trip_id
        if included is not None:
            item["included"] = included
        if amount is not None:
            item["amount_cents"] = cents(amount)
        if note is not None:
            item["note"] = note
        with self.transaction():
            self.db.execute(
                "UPDATE expenses SET trip_id=?,included=?,amount_cents=?,note=? WHERE id=?",
                (
                    item["trip_id"],
                    int(item["included"]),
                    item["amount_cents"],
                    item["note"],
                    identifier,
                ),
            )
        return self.expense(identifier)

    def check(self) -> dict:
        issues = []
        for expense in self.expenses():
            flags = []
            if not expense["receipt_path"]:
                flags.append("missing receipt file")
            elif not Path(expense["receipt_path"]).is_file():
                flags.append("receipt file is missing from disk")
            if (
                expense["receipt_amount_cents"] is not None
                and expense["amount_cents"] != expense["receipt_amount_cents"]
            ):
                flags.append("receipt amount mismatch")
            if not expense["trip_id"]:
                flags.append("unmatched trip")
            if expense["source_duplicate_of"]:
                flags.append("duplicate source")
            if not expense["included"]:
                flags.append("excluded pending review")
            if flags:
                issues.append({"expense_id": expense["id"], "issues": flags})
        orphans = [
            dict(row)
            for row in self.db.execute(
                "SELECT r.* FROM receipts r LEFT JOIN expenses e ON e.receipt_id=r.id WHERE "
                "e.id IS NULL"
            )
        ]
        return {
            "issues": issues,
            "orphan_receipts": orphans,
            "issue_count": len(issues) + len(orphans),
        }

    def _calculation(
        self, trip_ids: list[int], month: str | None = None, unclaimed: bool = False
    ) -> dict:
        bounds = month_bounds(month) if month else None
        details = []
        receipt_issues = {item["expense_id"]: item["issues"] for item in self.check()["issues"]}
        units_by_trip = {}
        day_budget = {}
        for identifier in sorted(set(trip_ids), key=lambda key: self.trip(key)["start_at"]):
            trip = self.trip(identifier)
            dates = day_units(
                trip["start_at"], trip["end_at"], self.policy, self.config["timezone"]
            )
            dates = {
                day: units
                for day, units in dates.items()
                if not bounds or bounds[0].isoformat() <= day <= bounds[1].isoformat()
            }
            if unclaimed:
                dates = {
                    day: units
                    for day, units in dates.items()
                    if not self.db.execute(
                        "SELECT 1 FROM claim_days WHERE trip_id=? AND day=?", (identifier, day)
                    ).fetchone()
                }
            capped = {}
            for day, units in dates.items():
                previous = (
                    self.db.execute(
                        "SELECT COALESCE(SUM(units),0) FROM claim_days WHERE day=?", (day,)
                    ).fetchone()[0]
                    if unclaimed
                    else 0
                )
                available = max(0, 2 - previous - day_budget.get(day, 0))
                if available:
                    capped[day] = min(units, available)
                    day_budget[day] = day_budget.get(day, 0) + capped[day]
            dates = capped
            units_by_trip[identifier] = dates
            for expense in self.expenses(identifier):
                if not expense["included"] or (unclaimed and expense["claim_code"]):
                    continue
                if bounds and not bounds[0].isoformat() <= expense["date"] <= bounds[1].isoformat():
                    continue
                eligible, flags = expense_allowance(expense, trip, self.policy)
                flags = [
                    *flags,
                    *(
                        "receipt validation: " + issue
                        for issue in receipt_issues.get(expense["id"], [])
                    ),
                ]
                details.append(
                    {
                        "expense_id": expense["id"],
                        "trip_id": identifier,
                        "date": expense["date"],
                        "destination": trip["destination"],
                        "category": expense["category"],
                        "description": expense["note"]
                        or expense["invoice_no"]
                        or expense["category"],
                        "amount_cents": expense["amount_cents"],
                        "eligible_cents": eligible,
                        "self_paid_cents": max(0, expense["amount_cents"] - eligible)
                        if expense["paid_by"] == "employee"
                        else 0,
                        "receipt_path": expense["receipt_path"],
                        "flags": flags,
                        "paid_by": expense["paid_by"],
                    }
                )
            details.extend(allowance_rows(trip, dates, self.policy))
        details.sort(
            key=lambda row: (
                row["date"],
                row["trip_id"],
                row["expense_id"] or 10**12,
                row["category"],
            )
        )
        for index, row in enumerate(details, 1):
            row["sequence"] = index
        totals = {
            key: sum(row[key] for row in details)
            for key in ["amount_cents", "eligible_cents", "self_paid_cents"]
        }
        totals["allowance_cents"] = sum(
            row["eligible_cents"] for row in details if row["expense_id"] is None
        )
        totals["advance_cents"] = sum(
            row["amount_cents"] for row in details if row.get("paid_by") == "employee"
        )
        return {
            "trip_ids": sorted(set(trip_ids)),
            "month": month,
            "currency": self.policy["currency"],
            "details": details,
            "totals": totals,
            "day_units": units_by_trip,
            "warnings": sorted(
                {
                    f"Expense {row['expense_id']}: {flag}"
                    for row in details
                    for flag in row["flags"]
                    if flag.startswith("receipt validation:")
                }
            ),
        }

    def preview(
        self, trip_ids: list[int] | None = None, month: str | None = None, unclaimed: bool = False
    ) -> dict:
        selected = trip_ids or [trip["id"] for trip in self.trips()]
        if not selected:
            raise ClaimError("NOT_FOUND", "No trips available", 3)
        return self._calculation(selected, month, unclaimed)

    def build_claim(
        self,
        trip_ids: list[int] | None = None,
        formats: list[str] | None = None,
        month: str | None = None,
    ) -> dict:
        from .reporting import build_reports

        formats = formats or ["xlsx", "pdf", "zip"]
        if not formats or set(formats) - {"xlsx", "pdf", "docx", "zip"}:
            raise ClaimError("INVALID_FORMAT", "Choose xlsx, pdf, docx or zip")
        selected = trip_ids or [trip["id"] for trip in self.trips()]
        if not selected:
            raise ClaimError("NOT_FOUND", "No trips to claim", 3)
        scope = canonical({"trips": sorted(set(selected)), "month": month})
        self.db.execute("BEGIN IMMEDIATE")
        directory = None
        try:
            snapshot = self._calculation(selected, month, True)
            if not snapshot["details"]:
                old = self.db.execute(
                    "SELECT code FROM claims WHERE scope_key=? ORDER BY id DESC LIMIT 1", (scope,)
                ).fetchone()
                self.db.rollback()
                if old:
                    existing = self.claim(old[0])
                    frozen = existing["snapshot"]
                    missing = set(formats) - set(frozen["artifacts"])
                    if missing:
                        additions = build_reports(
                            frozen, self.root / "claims" / old[0], sorted(missing)
                        )
                        frozen["artifacts"].update(additions)
                        with self.transaction():
                            self.db.execute(
                                "UPDATE claims SET snapshot_json=? WHERE code=?",
                                (canonical(frozen), old[0]),
                            )
                    return {**self.claim(old[0]), "reused": True}
                raise ClaimError(
                    "NO_NEW_ITEMS", "No unclaimed expenses or allowance days in this scope"
                )
            created = timestamp(self.config)
            code = "CLM-" + created[:10].replace("-", "") + "-" + uuid.uuid4().hex[:6].upper()
            policy_yaml = yaml.safe_dump(self.policy, sort_keys=True)
            policy_sha = hashlib.sha256(policy_yaml.encode()).hexdigest()
            snapshot.update(
                code=code,
                created_at=created,
                status="unsubmitted",
                policy_sha=policy_sha,
                policy_yaml=policy_yaml,
            )
            directory = self.root / "claims" / code
            directory.mkdir(parents=True)
            # Freeze attachment bytes inside the claim before publishing any artifact.
            for row in snapshot["details"]:
                if row["receipt_path"]:
                    source = Path(row["receipt_path"])
                    if source.is_symlink() or not source.is_file():
                        raise ClaimError(
                            "MISSING_RECEIPT", "A receipt disappeared before claim generation"
                        )
                    frozen = directory / "sources" / source.name
                    frozen.parent.mkdir(exist_ok=True)
                    if not frozen.exists():
                        shutil.copyfile(source, frozen)
                    row["receipt_path"] = str(frozen)
            artifacts = build_reports(snapshot, directory, formats)
            snapshot["artifacts"] = artifacts
            self.db.execute(
                "INSERT OR IGNORE INTO policy_snapshots VALUES(?,?,?)",
                (policy_sha, policy_yaml, created),
            )
            self.db.execute(
                "INSERT INTO claims(code,scope_key,trip_ids_json,status,created_at,submitted"
                "_at,approved_at,paid_at,paid_cents,policy_id,snapshot_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    code,
                    scope,
                    canonical(selected),
                    "unsubmitted",
                    created,
                    None,
                    None,
                    None,
                    0,
                    policy_sha,
                    canonical(snapshot),
                ),
            )
            for row in snapshot["details"]:
                if row["expense_id"]:
                    self.db.execute(
                        "INSERT INTO claim_expenses VALUES(?,?)", (row["expense_id"], code)
                    )
            for identifier, dates in snapshot["day_units"].items():
                for day, units in dates.items():
                    self.db.execute(
                        "INSERT INTO claim_days VALUES(?,?,?,?)", (identifier, day, units, code)
                    )
            self.db.commit()
        except Exception:
            self.db.rollback()
            if directory:
                shutil.rmtree(directory)
            raise
        return {**self.claim(code), "reused": False}

    def claim(self, code: str) -> dict:
        row = self.db.execute("SELECT * FROM claims WHERE code=?", (code,)).fetchone()
        if not row:
            raise ClaimError("NOT_FOUND", f"Claim not found: {code}", 3)
        item = dict(row)
        snapshot = json.loads(item.pop("snapshot_json"))
        item.pop("scope_key")
        item["trip_ids"] = json.loads(item.pop("trip_ids_json"))
        item["snapshot"] = snapshot
        item["warnings"] = snapshot.get("warnings", [])
        item["outstanding_cents"] = snapshot["totals"]["eligible_cents"] - item["paid_cents"]
        return item

    def claims(self) -> list[dict]:
        return [
            self.claim(row[0]) for row in self.db.execute("SELECT code FROM claims ORDER BY id")
        ]

    def claim_status(
        self, code: str, status: str, amount: str | None = None, effective_date: str | None = None
    ) -> dict:
        item = self.claim(code)
        order = {"unsubmitted": 0, "submitted": 1, "approved": 2, "paid": 3}
        if status not in order:
            raise ClaimError(
                "INVALID_STATUS", "Status must be unsubmitted, submitted, approved or paid"
            )
        if order[status] < order[item["status"]] or order[status] > order[item["status"]] + 1:
            raise ClaimError(
                "INVALID_TRANSITION", "Move through submitted, approved and paid in order"
            )
        if status == item["status"] and (
            status != "paid" or (amount is not None and cents(amount) == item["paid_cents"])
        ):
            return item
        day = effective_date or timestamp(self.config)[:10]
        date.fromisoformat(day)
        prior = item["paid_at"] or item["approved_at"] or item["submitted_at"]
        if prior and day < prior[:10]:
            raise ClaimError("INVALID_DATE", "Status date precedes the last transition")
        paid = item["paid_cents"]
        if status == "paid":
            if amount is None:
                raise ClaimError(
                    "AMOUNT_REQUIRED", "Paid status requires the cumulative amount received"
                )
            paid = cents(amount)
            if not item["paid_cents"] <= paid <= item["snapshot"]["totals"]["eligible_cents"]:
                raise ClaimError(
                    "INVALID_PAYMENT", "Cumulative payment cannot decrease or exceed the claim"
                )
        elif amount is not None:
            raise ClaimError("INVALID_PAYMENT", "amount is only valid for paid status")
        with self.transaction():
            if paid > item["paid_cents"]:
                self.db.execute(
                    "INSERT INTO payments(claim_code,date,amount_cents) VALUES(?,?,?)",
                    (code, day, paid - item["paid_cents"]),
                )
            field = {"submitted": "submitted_at", "approved": "approved_at", "paid": "paid_at"}.get(
                status
            )
            if field:
                self.db.execute(
                    f"UPDATE claims SET status=?,{field}=?,paid_cents=? WHERE code=?",
                    (status, day, paid, code),
                )
        return self.claim(code)

    def travel_days(self, year: int) -> dict:
        if not 1900 <= year <= 2200:
            raise ClaimError("INVALID_YEAR", "Year must be between 1900 and 2200")
        days = {}
        for trip in self.trips():
            for day, units in day_units(
                trip["start_at"], trip["end_at"], self.policy, self.config["timezone"]
            ).items():
                if day.startswith(str(year)):
                    days[day] = min(2, days.get(day, 0) + units)
        return {
            "year": year,
            "travel_days": days_text(sum(days.values())),
            "half_day_units": sum(days.values()),
            "by_date": dict(sorted(days.items())),
        }

    def due(self, as_of: str | None = None) -> dict:
        today = date.fromisoformat(as_of or timestamp(self.config)[:10])
        items = []
        for trip in self.trips():
            unclaimed = [
                item
                for item in self.expenses(trip["id"])
                if item["included"] and not item["claim_code"]
            ]
            elapsed = (today - date.fromisoformat(trip["end_at"][:10])).days
            if unclaimed and elapsed >= self.config["unsubmitted_days"]:
                items.append(
                    {
                        "kind": "unclaimed_expenses",
                        "trip_id": trip["id"],
                        "days": elapsed,
                        "expense_ids": [item["id"] for item in unclaimed],
                    }
                )
        for item in self.claims():
            if item["status"] == "unsubmitted":
                elapsed = (today - date.fromisoformat(item["created_at"][:10])).days
                if elapsed >= self.config["unsubmitted_days"]:
                    items.append(
                        {"kind": "unsubmitted_claim", "code": item["code"], "days": elapsed}
                    )
            elif item["outstanding_cents"] > 0:
                elapsed = (today - date.fromisoformat(item["submitted_at"][:10])).days
                if elapsed >= self.config["unpaid_days"]:
                    items.append(
                        {
                            "kind": "unpaid_claim",
                            "code": item["code"],
                            "days": elapsed,
                            "outstanding_cents": item["outstanding_cents"],
                        }
                    )
        actual_advance = sum(
            item["amount_cents"]
            for item in self.expenses()
            if item["paid_by"] == "employee" and item["included"]
        )
        received = self.db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payments").fetchone()[
            0
        ]
        return {
            "as_of": today.isoformat(),
            "items": items,
            "cash_advanced_cents": actual_advance,
            "received_cents": received,
            "advance_balance_cents": max(0, actual_advance - received),
            "outstanding_claim_cents": sum(item["outstanding_cents"] for item in self.claims()),
        }

    def stats(self, year: int) -> dict:
        travel = self.travel_days(year)
        buckets = {"month": {}, "category": {}, "destination": {}}
        total = 0
        for expense in self.expenses():
            if not expense["date"].startswith(str(year)):
                continue
            if not expense["included"]:
                continue
            destination = (
                self.trip(expense["trip_id"])["destination"] if expense["trip_id"] else "Unmatched"
            )
            total += expense["amount_cents"]
            for key, value in [
                ("month", expense["date"][:7]),
                ("category", expense["category"]),
                ("destination", destination),
            ]:
                buckets[key][value] = buckets[key].get(value, 0) + expense["amount_cents"]
        days_month = {}
        for day, units in travel["by_date"].items():
            days_month[day[:7]] = days_month.get(day[:7], 0) + units
        return {
            "year": year,
            "currency": self.policy["currency"],
            "expense_cents": total,
            "travel_days": travel["travel_days"],
            "monthly_travel_days": {key: days_text(value) for key, value in days_month.items()},
            "by_month_cents": buckets["month"],
            "by_category_cents": buckets["category"],
            "by_destination_cents": buckets["destination"],
            "balance": self.due(),
        }
