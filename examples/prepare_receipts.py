"""Copy only the known fictional receipt-ocr-csv export into standalone examples."""

import csv
import shutil
import sys
from pathlib import Path


def main(source: str):
    directory = Path(__file__).parent
    (directory / "receipts").mkdir(exist_ok=True)
    with Path(source).open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    if len(rows) != 10 or any(not row["invoice_no"].startswith("EXAMPLE") for row in rows):
        raise ValueError("Use the ten generated fictional receipt-ocr-csv samples only")
    for index, row in enumerate(rows, 1):
        original = Path(row["file"])
        name = f"receipt-{index:02d}" + original.suffix
        shutil.copyfile(original, directory / "receipts" / name)
        row["file"] = "receipts/" + name
        row["id"] = f"FICTIONAL-{index:03d}"
    duplicate = {**rows[0], "id": "FICTIONAL-DUPLICATE", "duplicate_of": rows[0]["id"]}
    rows.append(duplicate)
    with (directory / "receipts.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
