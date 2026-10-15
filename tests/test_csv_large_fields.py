"""Native CSV archives retain real binary attachments beyond CSV's default field cap."""

import csv
import hashlib
import random
from pathlib import Path

import pytest
from PIL import Image

from expense_claim_helper.core.common import ClaimError, settings
from expense_claim_helper.core.interchange import ARCHIVE_IMPORT_BYTES
from expense_claim_helper.core.store import Store


def test_large_receipt_native_csv_roundtrip(store, trip, tmp_path):
    receipt = tmp_path / "original-generated-pattern.png"
    Image.frombytes("RGB", (256, 256), random.Random(123).randbytes(256 * 256 * 3)).save(receipt)
    expected = receipt.read_bytes()
    assert len(expected) > 131072
    store.add_expense(trip["id"], "lodging", "80.00", "2026-01-31", receipt=str(receipt))
    archive = tmp_path / "archive.csv"
    store.export("csv", str(archive))
    assert archive.stat().st_size > 131072
    previous_limit = csv.field_size_limit()
    root, config = settings(tmp_path / "restored")
    target = Store(root, config)
    try:
        assert target.import_archive(str(archive))["expenses"] == 1
        assert csv.field_size_limit() == previous_limit
        restored = Path(target.expenses()[0]["receipt_path"]).read_bytes()
        assert hashlib.sha256(restored).digest() == hashlib.sha256(expected).digest()
        assert target.trip(trip["id"]) == store.trip(trip["id"])
        assert target.expenses()[0]["amount_cents"] == 8000
        assert target.import_archive(str(archive))["reused"]
    finally:
        target.close()


def test_large_csv_parse_error_restores_limit_and_keeps_store_empty(store, tmp_path):
    previous_limit = csv.field_size_limit()
    path = tmp_path / "malformed.csv"
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["entity", "payload"])
        writer.writerow(["file", "x" * 140000])
    with pytest.raises(ValueError):
        store.import_archive(str(path))
    assert csv.field_size_limit() == previous_limit
    assert store.trips() == []


def test_csv_archive_keeps_existing_file_size_cap(store, tmp_path):
    path = tmp_path / "oversized.csv"
    with path.open("wb") as stream:
        stream.truncate(ARCHIVE_IMPORT_BYTES + 1)
    with pytest.raises(ClaimError) as error:
        store.import_archive(str(path))
    assert error.value.code == "IMPORT_TOO_LARGE"
