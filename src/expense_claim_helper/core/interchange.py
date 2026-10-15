"""Receipt ingestion, portable archive interchange and backup helpers."""

import base64
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path, PurePosixPath
from threading import RLock

from .common import ClaimError, canonical, cents, money, timestamp
from .policy import category

TABLES = [
    "trips",
    "receipts",
    "expenses",
    "policy_snapshots",
    "claims",
    "claim_expenses",
    "claim_days",
    "payments",
]
RECEIPT_IMPORT_BYTES = 20 * 1024 * 1024
ARCHIVE_IMPORT_BYTES = 150 * 1024 * 1024
_CSV_LOCK = RLock()


@contextmanager
def _csv_field_limit(maximum: int):
    """Scope the process-wide CSV parser limit to the already bounded input."""
    with _CSV_LOCK:
        previous = csv.field_size_limit(maximum)
        try:
            yield
        finally:
            csv.field_size_limit(previous)


class InterchangeMixin:
    def import_receipts(
        self, path: str, auto_match: bool = False, trip_id: int | None = None
    ) -> dict:
        source = Path(path).expanduser().absolute()
        if source.stat().st_size > RECEIPT_IMPORT_BYTES:
            raise ClaimError("IMPORT_TOO_LARGE", "Receipt import exceeds 20 MiB")
        if source.suffix.lower() == ".json":
            payload = json.loads(source.read_text("utf-8-sig"))
            rows = payload.get("records") if isinstance(payload, dict) else payload
        else:
            with (
                _csv_field_limit(RECEIPT_IMPORT_BYTES),
                source.open(encoding="utf-8-sig", newline="") as file,
            ):
                rows = list(csv.DictReader(file))
        if not isinstance(rows, list) or len(rows) > 100000:
            raise ClaimError("INVALID_IMPORT", "Expected a bounded record list")
        planned = []
        for row in rows:
            if not isinstance(row, dict):
                raise ClaimError("INVALID_IMPORT", "Each receipt must be an object")
            day = row.get("date", "")
            date.fromisoformat(day)
            amount = money(cents(row.get("amount")))
            kind = category(row.get("category", "other"))
            unit = row.get("currency") or self.policy["currency"]
            if unit != self.policy["currency"]:
                raise ClaimError("CURRENCY_MISMATCH", "Receipt currency differs from claim policy")
            match = trip_id
            if match is None and auto_match:
                matches = [
                    trip["id"]
                    for trip in self.trips()
                    if trip["start_at"][:10] <= day <= trip["end_at"][:10]
                ]
                match = matches[0] if len(matches) == 1 else None
            if match is not None:
                trip = self.trip(match)
                if not trip["start_at"][:10] <= day <= trip["end_at"][:10]:
                    raise ClaimError("DATE_OUTSIDE_TRIP", "Receipt date is outside selected trip")
            receipt = row.get("file") or row.get("receipt_path") or ""
            if receipt:
                receipt_path = Path(receipt).expanduser()
                if not receipt_path.is_absolute():
                    receipt_path = source.parent / receipt_path
                if not receipt_path.is_file() or receipt_path.is_symlink():
                    raise ClaimError(
                        "MISSING_RECEIPT", f"Receipt file missing or unsafe: {receipt_path}"
                    )
                receipt = str(receipt_path.absolute())
            identity = hashlib.sha256(
                canonical(
                    {key: row.get(key) for key in ["id", "date", "amount", "vendor", "invoice_no"]}
                ).encode()
            ).hexdigest()
            planned.append(
                {
                    "trip_id": match,
                    "category_name": kind,
                    "amount": amount,
                    "expense_date": day,
                    "currency": unit,
                    "receipt": receipt or None,
                    "receipt_amount": amount,
                    "invoice_no": row.get("invoice_no") or "",
                    "payment_method": row.get("payment_method") or "card",
                    "travel_class": "",
                    "note": "Imported receipt: " + str(row.get("vendor") or "Example vendor"),
                    "included": not bool(row.get("duplicate_of")),
                    "source_duplicate_of": row.get("duplicate_of") or "",
                    "source_key": "receipt:" + identity,
                }
            )
        result = []
        before = set((self.root / "receipts").glob("*"))
        try:
            with self.transaction():
                for values in planned:
                    result.append(self.add_expense(**values))
        except Exception:
            for path in set((self.root / "receipts").glob("*")) - before:
                path.unlink(missing_ok=True)
            raise
        return {
            "imported": sum(not item["reused"] for item in result),
            "reused": sum(item["reused"] for item in result),
            "unmatched": sum(item["trip_id"] is None for item in result),
            "excluded_duplicates": sum(not item["included"] for item in result),
            "expense_ids": [item["id"] for item in result],
        }

    def _portable(self, value, restore: bool = False):
        if isinstance(value, dict):
            return {key: self._portable(item, restore) for key, item in value.items()}
        if isinstance(value, list):
            return [self._portable(item, restore) for item in value]
        if isinstance(value, str):
            if restore and value.startswith("$DATA/"):
                relative = PurePosixPath(value[6:])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ClaimError("UNSAFE_PATH", "Archive contains unsafe file pointer")
                return str(self.root.joinpath(*relative.parts))
            if not restore and value.startswith(str(self.root) + "/"):
                return "$DATA/" + str(Path(value).relative_to(self.root))
            if value.startswith("{") or value.startswith("["):
                try:
                    return canonical(self._portable(json.loads(value), restore))
                except json.JSONDecodeError:
                    pass
        return value

    def export(
        self,
        format: str,
        out: str,
        kind: str = "archive",
        overwrite: bool = False,
        bom: bool = True,
    ) -> dict:
        if format not in {"json", "csv"} or kind not in {"archive", "ledger", "expenses"}:
            raise ClaimError("INVALID_FORMAT", "Use json/csv and archive/ledger/expenses")
        target = Path(out).expanduser().absolute()
        if target.exists() and not overwrite:
            raise ClaimError("EXISTS", "Output exists; use --yes to overwrite")
        if kind == "archive":
            tables = {
                name: [
                    self._portable(dict(row)) for row in self.db.execute(f"SELECT * FROM {name}")
                ]
                for name in TABLES
            }
            files = {}
            for folder in ["receipts", "claims"]:
                for path in (self.root / folder).rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        files[str(path.relative_to(self.root))] = base64.b64encode(
                            path.read_bytes()
                        ).decode()
            payload = {"schema_version": 1, "tables": tables, "files": files, "policy": self.policy}
            rows = [
                {
                    "entity": "meta",
                    "payload": canonical({"schema_version": 1, "policy": self.policy}),
                }
            ]
            for name, items in tables.items():
                rows.extend({"entity": name, "payload": canonical(item)} for item in items)
            rows.extend(
                {"entity": "file", "payload": canonical({"path": name, "data": data})}
                for name, data in files.items()
            )
        elif kind == "expenses":
            rows = [
                {
                    key: item[key]
                    for key in [
                        "id",
                        "trip_id",
                        "date",
                        "category",
                        "amount",
                        "currency",
                        "paid_by",
                        "payment_method",
                        "receipt_path",
                        "invoice_no",
                        "included",
                        "source_duplicate_of",
                        "claim_code",
                        "note",
                    ]
                }
                for item in self.expenses()
            ]
            payload = {"schema_version": 1, "expenses": rows}
        else:
            rows = []
            for expense in self.expenses():
                if expense["paid_by"] == "employee" and expense["included"]:
                    rows.append(
                        {
                            "date": expense["date"],
                            "kind": "advance",
                            "reference": f"expense:{expense['id']}",
                            "amount": money(expense["amount_cents"]),
                            "currency": expense["currency"],
                            "direction": "outflow",
                        }
                    )
            for payment in self.db.execute("SELECT * FROM payments ORDER BY date,id"):
                rows.append(
                    {
                        "date": payment["date"],
                        "kind": "reimbursement",
                        "reference": payment["claim_code"],
                        "amount": money(payment["amount_cents"]),
                        "currency": self.policy["currency"],
                        "direction": "inflow",
                    }
                )
            rows.sort(key=lambda row: (row["date"], row["reference"]))
            payload = {"schema_version": 1, "cash_flows": rows}
        if format == "json":
            content = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        else:
            stream = io.StringIO(newline="")
            headers = (
                list(rows[0])
                if rows
                else {
                    "archive": ["entity", "payload"],
                    "ledger": ["date", "kind", "reference", "amount", "currency", "direction"],
                    "expenses": ["id", "trip_id", "date", "category", "amount"],
                }[kind]
            )
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
            content = stream.getvalue().encode("utf-8-sig" if bom else "utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb" if overwrite else "xb") as file:
            file.write(content)
        return {"path": str(target), "format": format, "kind": kind, "count": len(rows)}

    def import_archive(self, path: str) -> dict:
        source = Path(path)
        if source.stat().st_size > ARCHIVE_IMPORT_BYTES:
            raise ClaimError("IMPORT_TOO_LARGE", "Archive exceeds 150 MiB")
        if source.suffix.lower() == "json" or source.suffix.lower() == ".json":
            payload = json.loads(source.read_text("utf-8-sig"))
        else:
            payload = {"tables": {name: [] for name in TABLES}, "files": {}}
            with (
                _csv_field_limit(ARCHIVE_IMPORT_BYTES),
                source.open(encoding="utf-8-sig", newline="") as file,
            ):
                for row in csv.DictReader(file):
                    value = json.loads(row["payload"])
                    entity = row["entity"]
                    if entity == "meta":
                        payload.update(value)
                    elif entity == "file":
                        payload["files"][value["path"]] = value["data"]
                    elif entity in TABLES:
                        payload["tables"][entity].append(value)
                    else:
                        raise ClaimError("INVALID_ARCHIVE", "Unknown archive entity")
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or set(payload.get("tables", {})) != set(TABLES)
        ):
            raise ClaimError(
                "INVALID_ARCHIVE", "Expected an ech archive, not a flat expense/ledger export"
            )
        from .policy import validate_policy

        validate_policy(payload["policy"])
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        marker = self.root / ".last-import"
        if marker.exists() and marker.read_text() == digest:
            return {"reused": True, "trips": len(self.trips()), "expenses": len(self.expenses())}
        if any(self.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in TABLES):
            raise ClaimError("NONEMPTY_LIBRARY", "Restore an archive into an empty data directory")
        staged = {}
        for name, data in payload.get("files", {}).items():
            relative = PurePosixPath(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
                or relative.parts[0] not in {"receipts", "claims"}
            ):
                raise ClaimError("UNSAFE_PATH", "Archive file path escapes managed folders")
            staged[relative] = base64.b64decode(data, validate=True)
        restored = {
            name: [self._portable(row, True) for row in rows]
            for name, rows in payload["tables"].items()
        }
        for name, rows in restored.items():
            columns = [row[1] for row in self.db.execute(f"PRAGMA table_info({name})")]
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(columns):
                    raise ClaimError("INVALID_ARCHIVE", f"Unexpected columns in {name}")
                for key, value in row.items():
                    if (
                        key.endswith("_cents")
                        and value is not None
                        and (type(value) is not int or value < 0)
                    ):
                        raise ClaimError(
                            "INVALID_ARCHIVE", "Money must be nonnegative integer cents"
                        )
        self._validate_archive(restored, staged, payload["policy"])
        written = []
        try:
            with self.transaction():
                for name, rows in restored.items():
                    for row in rows:
                        keys = list(row)
                        self.db.execute(
                            f"INSERT INTO {name} ({','.join(keys)}) "
                            f"VALUES({','.join('?' for _ in keys)})",
                            [row[key] for key in keys],
                        )
                if self.db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ClaimError("INVALID_ARCHIVE", "Archive references are inconsistent")
                for relative, content in staged.items():
                    target = self.root.joinpath(*relative.parts)
                    descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        for component in relative.parts[:-1]:
                            try:
                                os.mkdir(component, dir_fd=descriptor)
                            except FileExistsError:
                                pass
                            child = os.open(
                                component,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor,
                            )
                            os.close(descriptor)
                            descriptor = child
                        file_fd = os.open(
                            relative.name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=descriptor,
                        )
                        written.append(target)
                        with os.fdopen(file_fd, "wb") as file:
                            file.write(content)
                    finally:
                        os.close(descriptor)
                for receipt in self.db.execute("SELECT path FROM receipts"):
                    target = Path(receipt[0])
                    if not target.is_relative_to(self.root) or not target.is_file():
                        raise ClaimError("INVALID_ARCHIVE", "Receipt is missing from archive")
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        import yaml

        (self.root / "policy.yaml").write_text(
            yaml.safe_dump(payload["policy"], sort_keys=True), "utf-8"
        )
        self.policy = payload["policy"]
        marker.write_text(digest)
        return {"reused": False, "trips": len(self.trips()), "expenses": len(self.expenses())}

    def _validate_archive(self, tables: dict, files: dict, policy: dict):
        """Reject inconsistent money, policy snapshots and external file references."""
        import yaml

        from .policy import day_units, local_datetime, validate_policy

        def require(condition, message):
            if not condition:
                raise ClaimError("INVALID_ARCHIVE", message)

        def managed(value, prefix):
            require(isinstance(value, str), "File reference must be a string")
            target = Path(value)
            require(
                target.is_absolute() and target.is_relative_to(self.root),
                "File reference escapes the data directory",
            )
            relative = PurePosixPath(target.relative_to(self.root).as_posix())
            require(
                ".." not in relative.parts and relative.is_relative_to(prefix),
                "File reference escapes its managed folder",
            )
            require(relative in files, "A referenced file is missing from the archive")
            return files[relative]

        trips = {}
        intervals = []
        for trip in tables["trips"]:
            require(trip["city_tier"] in policy["tiers"], "Unknown city tier")
            require(
                type(trip["participants"]) is int and 1 <= trip["participants"] <= 100,
                "Invalid participant count",
            )
            day_units(trip["start_at"], trip["end_at"], policy, self.config["timezone"])
            start = local_datetime(trip["start_at"], self.config["timezone"])
            end = local_datetime(trip["end_at"], self.config["timezone"])
            require(
                all(end <= old_start or start >= old_end for old_start, old_end in intervals),
                "Archive trips overlap",
            )
            intervals.append((start, end))
            trips[trip["id"]] = trip
        for receipt in tables["receipts"]:
            content = managed(receipt["path"], PurePosixPath("receipts"))
            require(
                hashlib.sha256(content).hexdigest() == receipt["id"],
                "Receipt content does not match its identity",
            )
        for expense in tables["expenses"]:
            date.fromisoformat(expense["date"])
            require(category(expense["category"]) == expense["category"], "Invalid category")
            require(expense["currency"] == policy["currency"], "Mixed archive currencies")
            require(expense["paid_by"] in {"employee", "company"}, "Invalid payer")
            require(
                expense["included"] in {0, 1} and expense["has_receipt"] in {0, 1},
                "Invalid expense flags",
            )
            require(
                type(expense["nights"]) is int and 1 <= expense["nights"] <= 366,
                "Invalid night count",
            )
            if expense["trip_id"] is not None:
                trip = trips.get(expense["trip_id"])
                require(
                    trip is not None
                    and trip["start_at"][:10] <= expense["date"] <= trip["end_at"][:10],
                    "Expense date is outside its trip",
                )
        policies = {}
        for snapshot in tables["policy_snapshots"]:
            validate_policy(yaml.safe_load(snapshot["yaml"]))
            require(
                hashlib.sha256(snapshot["yaml"].encode()).hexdigest() == snapshot["id"],
                "Policy snapshot hash does not match",
            )
            policies[snapshot["id"]] = snapshot["yaml"]
        claims = {}
        for claim in tables["claims"]:
            require(
                isinstance(claim["code"], str)
                and bool(re.fullmatch(r"CLM-[0-9]{8}-[A-Z0-9]{6}", claim["code"])),
                "Invalid claim code",
            )
            require(
                claim["status"] in {"unsubmitted", "submitted", "approved", "paid"},
                "Invalid claim status",
            )
            snapshot = json.loads(claim["snapshot_json"])
            require(
                snapshot["code"] == claim["code"]
                and snapshot["policy_sha"] == claim["policy_id"]
                and snapshot["policy_yaml"] == policies.get(claim["policy_id"]),
                "Inconsistent frozen policy",
            )
            prefix = PurePosixPath("claims", claim["code"])
            for row in snapshot["details"]:
                for key in ["amount_cents", "eligible_cents", "self_paid_cents"]:
                    require(type(row[key]) is int and row[key] >= 0, "Invalid frozen amount")
                if row.get("receipt_path"):
                    managed(row["receipt_path"], prefix / "sources")
            for value in snapshot["artifacts"].values():
                managed(value, prefix)
            for key in ["amount_cents", "eligible_cents", "self_paid_cents"]:
                require(
                    snapshot["totals"][key] == sum(row[key] for row in snapshot["details"]),
                    "Frozen totals do not match details",
                )
            require(
                claim["paid_cents"] <= snapshot["totals"]["eligible_cents"],
                "Payments exceed the claim",
            )
            previous = ""
            for field in ["submitted_at", "approved_at", "paid_at"]:
                if claim[field]:
                    date.fromisoformat(claim[field])
                    require(claim[field] >= previous, "Claim dates are out of order")
                    previous = claim[field]
            if claim["status"] != "unsubmitted":
                require(bool(claim["submitted_at"]), "Submitted date is missing")
            claims[claim["code"]] = claim
        for claim in claims.values():
            paid = sum(
                row["amount_cents"]
                for row in tables["payments"]
                if row["claim_code"] == claim["code"]
            )
            require(paid == claim["paid_cents"], "Payment history does not match claim")
        days = {}
        for row in tables["claim_days"]:
            date.fromisoformat(row["day"])
            require(
                type(row["units"]) is int and row["units"] in {1, 2}, "Invalid claimed day units"
            )
            days[row["day"]] = days.get(row["day"], 0) + row["units"]
            require(days[row["day"]] <= 2, "A calendar day was claimed more than once")

    def backup(self, out: str | None = None) -> dict:
        stamp = timestamp(self.config).replace(":", "").replace("+", "_")
        target = Path(out) if out else self.root / "backups" / f"expenses-{stamp}.zip"
        if target.exists():
            raise ClaimError("EXISTS", "Backup already exists; choose a new path")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "expenses.db"
            with sqlite3.connect(snapshot) as database:
                self.db.backup(database)
            with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, "expenses.db")
                archive.writestr("effective-config.json", canonical(self.config))
                import yaml

                archive.writestr("policy.yaml", yaml.safe_dump(self.policy, sort_keys=True))
                for folder in ["receipts", "claims"]:
                    for path in (self.root / folder).rglob("*"):
                        if path.is_file() and not path.is_symlink():
                            archive.write(path, str(path.relative_to(self.root)))
                # A portable archive preserves path relocation when restoring on another machine.
                portable = Path(temporary) / "portable.json"
                self.export("json", str(portable))
                archive.write(portable, "portable.json")
        return {"path": str(target.absolute()), "claims": len(self.claims())}
