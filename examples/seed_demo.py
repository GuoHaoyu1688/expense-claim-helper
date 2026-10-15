"""Create a fictional three-trip library and import the actual OCR export fixture."""

from pathlib import Path

from expense_claim_helper.core.common import settings
from expense_claim_helper.core.store import Store


def main():
    root, config = settings()
    store = Store(root, config)
    for start, end, destination, tier in [
        ("2026-01-01T08:00", "2026-01-04T18:00", "CITY-B", "B"),
        ("2026-01-05T08:00", "2026-01-07T18:00", "CITY-C", "A"),
        ("2026-01-08T08:00", "2026-01-10T18:00", "CITY-D", "C"),
    ]:
        store.add_trip(start, end, "CITY-A", destination, tier, purpose_code="EXAMPLE-VISIT")
    result = store.import_receipts(str(Path(__file__).parent / "receipts.csv"), True)
    store.add_expense(
        1,
        "lodging",
        "480.00",
        "2026-01-03",
        receipt=str(Path(__file__).parent / "receipts/receipt-03.png"),
        nights=1,
        note="Fictional cap and receipt amount mismatch review example",
    )
    print(result)
    store.close()


if __name__ == "__main__":
    main()
