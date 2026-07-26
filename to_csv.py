"""Flatten data/olli-catalog-full.json lecture records into a Bernie-friendly CSV.

Usage: python3 to_csv.py [-o data/olli-catalog-full.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

DATA = Path(__file__).parent / "data"

COLUMNS = [
    "edition",
    "series_type",
    "series_name",
    "date",
    "time",
    "title",
    "speaker",
    "speaker_title",
    "institution",
    "description",
    "notes",
    "speaker_bio",  # added last (BLC-002) so pre-existing column order is preserved
]


def lecture_rows(catalog: dict) -> list[dict]:
    rows = []
    for rec in catalog["records"]:
        if rec["type"] != "lecture":
            continue
        rows.append({col: rec.get(col) or "" for col in COLUMNS})
    return rows


def write_csv(catalog: dict, out_path: Path) -> int:
    rows = lecture_rows(catalog)
    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-i", "--input", default=str(DATA / "olli-catalog-full.json"))
    ap.add_argument("-o", "--output", default=str(DATA / "olli-catalog-full.csv"))
    args = ap.parse_args()
    catalog = json.loads(Path(args.input).read_text())
    n = write_csv(catalog, Path(args.output))
    print(f"wrote {n} lecture rows to {args.output}")


if __name__ == "__main__":
    main()
