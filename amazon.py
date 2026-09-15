#!/usr/bin/env python3
"""
The Saint -- Amazon source adapter.

Meets Amazon's "Request My Data" export (amazon.com/gp/privacycentral/dsar), specifically
the retail order history CSV -- Retail.OrderHistory.1.csv in older exports (sometimes split
across numbered files), "Your Amazon Orders/Order History.csv" in newer ones (checked against
a published 2024-25 sample, 2026-09: same Product Name / ISO-8601 Order Date columns). Column headers are matched by regex rather than hard-coded
names, since Amazon's export headers have varied across versions -- same "the schema
drifts, so scan rather than hard-code" reasoning reddit.py gives. A file is classified as
order history by its own header shape (a product-name-like column present), not by
filename, so renamed files still parse correctly.

One kind of data comes out:

  ORDERS -- product names from your order history. EXPRESSED intent (what you bought),
            not served or algorithmic data. Repeat purchases of the same product collapse
            into one entry (deduped case-insensitively) -- a known simplification; how many
            times you bought something is not preserved, only that you did.

A search-history CSV isn't always included in Amazon's export and isn't parsed for v1
(best-effort, order history only). No assigned-category stream exists in the export --
this adapter never invents one.

Run:
    python amazon.py /path/to/export
"""
from __future__ import annotations

import csv
import glob
import io
import os
import re

from mirror import Record, parse_timestamp

_PRODUCT_COL = re.compile(r"product.*name|^title$", re.I)
_DATE_COL = re.compile(r"order.*date|^date$", re.I)


def _parse_amazon_date(value: str | None):
    if not value:
        return None
    # Amazon's export uses "YYYY-MM-DD HH:MM:SS UTC"; strip the trailing zone name so
    # mirror.parse_timestamp's fromisoformat-based parsing can read it.
    return parse_timestamp(re.sub(r"\s*UTC$", "+00:00", value.strip(), flags=re.I))


def _classify(fieldnames: list[str]) -> str | None:
    return "order" if _find_column(fieldnames, _PRODUCT_COL) else None


def _find_column(fieldnames: list[str], pattern: re.Pattern) -> str | None:
    for name in fieldnames:
        if name and pattern.search(name):
            return name
    return None


def _load_files(path: str) -> list[dict]:
    if not os.path.exists(path):
        raise ValueError(f"Export path does not exist: {path}")
    files = [path] if os.path.isfile(path) else sorted(glob.glob(
        os.path.join(path, "**", "*.csv"), recursive=True))
    if not files:
        raise ValueError(f"No CSV files found in: {path}")
    out = []
    for f in files:
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                out.append({"name": os.path.basename(f), "text": fh.read()})
        except OSError as exc:
            raise ValueError(f"Cannot read CSV export {f}: {exc}") from exc
    return out


def load(path: str) -> list[Record]:
    return load_rows(_load_files(path))


def load_rows(files: list[dict]) -> list[Record]:
    records: list[Record] = []
    seen: set[str] = set()
    for file in files:
        text = file["text"]
        try:
            reader = csv.DictReader(io.StringIO(text))
            fieldnames = reader.fieldnames or []
            if _classify(fieldnames) is None:
                continue  # not an order-history export -- skip rather than guess wrong
            product_col = _find_column(fieldnames, _PRODUCT_COL)
            date_col = _find_column(fieldnames, _DATE_COL)
            for row in reader:
                text_value = (row.get(product_col) or "").strip()
                if not text_value:
                    continue
                key = text_value.lower()
                if key in seen:
                    continue
                seen.add(key)
                when = _parse_amazon_date(row.get(date_col)) if date_col else None
                records.append(Record(text_value, "order", "amazon", when))
        except csv.Error as exc:
            raise ValueError(f"Cannot read CSV export: {exc}") from exc
    return records


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="The Saint -- Amazon adapter")
    ap.add_argument("export", help="folder containing an order-history CSV, or a single csv file")
    ap.add_argument("--out", default="amazon_mirror.html")
    ap.add_argument("--inspect", action="store_true",
                    help="report parsed counts without downloading a model or rendering")
    args = ap.parse_args()
    try:
        records = load(args.export)
    except ValueError as exc:
        ap.error(str(exc))
    print(f"orders {len(records)}")
    if not args.inspect:
        if len(records) < 30:
            raise SystemExit(f"only {len(records)} usable records -- too few to cluster meaningfully")
        from mirror import embed, cluster, render
        render(records, *cluster(embed([r.text for r in records])), out=args.out)
