import csv
import json
import zipfile
from pathlib import Path

import pytest
import yaml

from expense_claim_helper.core.common import ClaimError, settings
from expense_claim_helper.core.store import Store


def test_trip_expense_review_and_check(store, trip, receipt):
    assert trip["travel_days"] == "3"
    assert store.add_trip("2026-01-30T15:00", "2026-02-02T10:00", "CITY-A", "CITY-B", "B")["reused"]
    with pytest.raises(ClaimError):
        store.add_trip("2026-02-01T12:00", "2026-02-03T10:00", "A", "B")
    expense = store.add_expense(
        trip["id"],
        "lodging",
        "760.00",
        "2026-01-31",
        receipt=str(receipt),
        nights=2,
        receipt_amount="760.00",
    )
    assert expense["eligible_cents"] == 60000
    assert store.add_expense(
        trip["id"],
        "lodging",
        "760.00",
        "2026-01-31",
        receipt=str(receipt),
        nights=2,
        receipt_amount="760.00",
    )["reused"]
    store.review_expense(expense["id"], amount="759.00")
    assert store.check()["issues"][0]["issues"] == ["receipt amount mismatch"]
    orphan = receipt.parent / "orphan.png"
    orphan.write_bytes(receipt.read_bytes() + b"extra")
    store.add_receipt(str(orphan))
    assert len(store.check()["orphan_receipts"]) == 1
    with pytest.raises(ClaimError):
        store.add_expense(trip["id"], "other", "1.00", "2026-03-01")
    with pytest.raises(ClaimError):
        store.add_expense(trip["id"], "other", "1.00", currency="USD")


def test_claim_snapshot_months_payment_due_and_stats(store, trip, receipt, tmp_path):
    expense = store.add_expense(
        trip["id"], "lodging", "760.00", "2026-01-31", receipt=str(receipt), nights=2
    )
    preview = store.preview([trip["id"]])
    assert preview["totals"] == {
        "amount_cents": 76000,
        "eligible_cents": 87000,
        "self_paid_cents": 16000,
        "allowance_cents": 27000,
        "advance_cents": 76000,
    }
    jan = store.build_claim([trip["id"]], ["xlsx", "pdf", "docx", "zip"], "2026-01")
    assert jan["snapshot"]["totals"]["eligible_cents"] == 73500
    assert store.build_claim([trip["id"]], ["xlsx"], "2026-01")["reused"]
    feb = store.build_claim([trip["id"]], ["pdf"], "2026-02")
    assert feb["snapshot"]["totals"]["eligible_cents"] == 13500
    with pytest.raises(ClaimError):
        store.review_expense(expense["id"], amount="1.00")
    original = jan["snapshot"]["policy_sha"]
    changed = store.policy.copy()
    changed["meal_mode"] = "actual"
    (store.root / "policy.yaml").write_text(yaml.safe_dump(changed))
    assert store.claim(jan["code"])["snapshot"]["policy_sha"] == original
    with pytest.raises(ClaimError):
        store.claim_status(jan["code"], "paid", "100.00", "2026-02-03")
    store.claim_status(jan["code"], "submitted", effective_date="2026-02-03")
    store.claim_status(jan["code"], "approved", effective_date="2026-02-04")
    item = store.claim_status(jan["code"], "paid", "500.00", "2026-02-05")
    assert item["outstanding_cents"] == 23500
    assert store.claim_status(jan["code"], "paid", "500.00", "2026-02-05")["paid_cents"] == 50000
    assert store.db.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    assert any(row["kind"] == "unpaid_claim" for row in store.due("2026-03-10")["items"])
    assert store.due("2026-03-10")["advance_balance_cents"] == 26000
    store.claim_status(jan["code"], "paid", "735.00", "2026-03-11")
    assert store.db.execute("SELECT SUM(amount_cents) FROM payments").fetchone()[0] == 73500
    assert store.stats(2026)["monthly_travel_days"] == {"2026-01": "1.5", "2026-02": "1.5"}
    ledger = tmp_path / "ledger.csv"
    store.export("csv", str(ledger), "ledger")
    with ledger.open(encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 3 and rows[0]["direction"] == "outflow"


def test_receipt_csv_matching_duplicate_and_atomic_validation(store, trip, receipt, tmp_path):
    source = tmp_path / "receipts.csv"
    rows = [
        {
            "id": "R1",
            "date": "2026-01-31",
            "amount": "120.50",
            "tax": "1.50",
            "currency": "CNY",
            "vendor": "Example Hotel",
            "category": "Lodging",
            "invoice_no": "EXAMPLE1",
            "file": receipt.name,
            "duplicate_of": "",
        },
        {
            "id": "R2",
            "date": "2026-01-31",
            "amount": "120.50",
            "tax": "1.50",
            "currency": "CNY",
            "vendor": "Example Hotel",
            "category": "Lodging",
            "invoice_no": "EXAMPLE1",
            "file": receipt.name,
            "duplicate_of": "R1",
        },
        {
            "id": "R3",
            "date": "2026-04-01",
            "amount": "10.00",
            "tax": "0.00",
            "currency": "CNY",
            "vendor": "Example Cafe",
            "category": "Meals",
            "invoice_no": "EXAMPLE3",
            "file": "",
            "duplicate_of": "",
        },
    ]
    with source.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    result = store.import_receipts(str(source), True)
    assert (result["imported"], result["unmatched"], result["excluded_duplicates"]) == (3, 1, 1)
    assert store.import_receipts(str(source), True)["reused"] == 3
    expenses = store.expenses()
    assert expenses[1]["included"] is False
    store.review_expense(expenses[1]["id"], included=True, note="Distinct sample verified")
    assert store.expense(expenses[1]["id"])["included"]
    data = tmp_path / "bad.json"
    data.write_text(json.dumps([rows[0], {**rows[2], "id": "bad", "amount": "NaN"}]))
    with pytest.raises(ClaimError):
        store.import_receipts(str(data), True)
    assert len(store.expenses()) == 3


def test_portable_archives_and_backup(store, trip, receipt, tmp_path):
    store.add_expense(trip["id"], "lodging", "80.00", "2026-01-31", receipt=str(receipt))
    store.build_claim([trip["id"]], ["xlsx", "pdf", "zip"])
    for format in ["csv", "json"]:
        out = tmp_path / f"archive.{format}"
        store.export(format, str(out))
        root, config = settings(tmp_path / f"restored-{format}")
        target = Store(root, config)
        result = target.import_archive(str(out))
        assert result["expenses"] == 1 and len(target.claims()) == 1
        assert Path(target.expenses()[0]["receipt_path"]).is_file()
        assert target.import_archive(str(out))["reused"]
        assert str(root) in target.claims()[0]["snapshot"]["artifacts"]["pdf"]
        target.close()
    backup = store.backup()
    with zipfile.ZipFile(backup["path"]) as archive:
        assert {"expenses.db", "portable.json", "policy.yaml"} <= set(archive.namelist())


def test_cross_year_travel_and_security(store, receipt, tmp_path):
    store.add_trip("2025-12-31T15:00", "2026-01-01T10:00", "CITY-X", "CITY-Y", "C")
    assert store.travel_days(2025)["travel_days"] == "0.5"
    assert store.travel_days(2026)["travel_days"] == "0.5"
    link = tmp_path / "link.png"
    link.symlink_to(receipt)
    with pytest.raises(ClaimError):
        store.add_receipt(str(link))
    with pytest.raises(ClaimError):
        store.trip(999)
    with pytest.raises(ClaimError):
        store.travel_days(3000)


def test_daily_allowances_not_double_claimed_and_formats_expand(store):
    first = store.add_trip("2026-01-01T01:00", "2026-01-01T09:00", "CITY-A", "CITY-B")
    second = store.add_trip("2026-01-01T10:00", "2026-01-01T19:00", "CITY-B", "CITY-C")
    assert store.preview([first["id"], second["id"]])["totals"]["allowance_cents"] == 9000
    claim = store.build_claim([first["id"]], ["pdf"])
    assert "xlsx" in store.build_claim([first["id"]], ["xlsx"])["snapshot"]["artifacts"]
    with pytest.raises(ClaimError):
        store.build_claim([second["id"]], ["pdf"])
    store.claim_status(claim["code"], "submitted", effective_date="2026-02-01")
    assert (
        store.claim_status(claim["code"], "submitted", effective_date="2026-03-01")["submitted_at"]
        == "2026-02-01"
    )


def test_archive_rejects_external_snapshot_and_symlink_targets(store, trip, receipt, tmp_path):
    store.add_expense(trip["id"], "lodging", "80.00", "2026-01-31", receipt=str(receipt))
    store.build_claim([trip["id"]], ["pdf"])
    archive = tmp_path / "archive.json"
    store.export("json", str(archive))
    payload = json.loads(archive.read_text())
    frozen = json.loads(payload["tables"]["claims"][0]["snapshot_json"])
    frozen["details"][0]["receipt_path"] = str(receipt)
    payload["tables"]["claims"][0]["snapshot_json"] = json.dumps(frozen)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(payload))
    root, config = settings(tmp_path / "bad-restore")
    target = Store(root, config)
    with pytest.raises(ClaimError, match="escapes"):
        target.import_archive(str(bad))
    assert target.trips() == []
    target.close()
    root, config = settings(tmp_path / "link-restore")
    target = Store(root, config)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "receipts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        target.import_archive(str(archive))
    assert not list(outside.iterdir()) and target.trips() == []
    target.close()


def test_excluded_duplicate_does_not_inflate_advance_ledger(store, trip, tmp_path):
    store.add_expense(trip["id"], "meals", "10.00", "2026-01-31")
    store.add_expense(
        trip["id"],
        "meals",
        "10.00",
        "2026-01-31",
        source_key="duplicate",
        included=False,
        source_duplicate_of="1",
    )
    assert store.due()["cash_advanced_cents"] == 1000
    path = tmp_path / "ledger.json"
    store.export("json", str(path), "ledger")
    assert len(json.loads(path.read_text())["cash_flows"]) == 1


def test_draft_claim_preserves_receipt_validation_findings(store, trip, receipt):
    from expense_claim_helper.core.common import success

    expense = store.add_expense(
        trip["id"], "lodging", "480.00", "2026-01-31", receipt=str(receipt), receipt_amount="75.50"
    )
    preview = store.preview([trip["id"]])
    detail = next(row for row in preview["details"] if row["expense_id"] == expense["id"])
    assert detail["eligible_cents"] == 30000
    assert "above lodging cap" in detail["flags"]
    assert "receipt validation: receipt amount mismatch" in detail["flags"]
    claim = store.build_claim([trip["id"]], ["pdf", "xlsx", "docx", "zip"])
    assert claim["snapshot"]["warnings"] == preview["warnings"]
    assert "receipt amount mismatch" in success(claim)["warnings"][0]
    from pypdf import PdfReader

    document = PdfReader(claim["snapshot"]["artifacts"]["pdf"])
    text = " ".join(page.extract_text() for page in document.pages)
    assert "receipt amount mismatch" in " ".join(text.split())
