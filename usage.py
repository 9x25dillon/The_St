#!/usr/bin/env python3
"""
The Saint -- device screen-time / app-usage source adapter.

There is no single standard screen-time export format across platforms (Android's
Digital Wellbeing has no direct export; iOS Screen Time reports are read-only in-app).
This adapter defines a simple, explicit contract instead: a JSON array of
{"app": "Instagram", "minutes": 47, "date": "2024-01-01"} rows, one row per app per day.
Build that file yourself from whatever usage view your device offers, or from a
third-party usage-tracking app that can export JSON.

Usage rows are volume/presence data, not text you wrote -- there is nothing to embed for
semantic meaning. Each row is turned into a short synthesized passage ("Instagram: 47
minutes") so it flows through the exact same Record -> import -> merge -> search ->
word-count -> (optionally) embed pipeline as every other source, rather than inventing a
second, chart-shaped data model. That is a real simplification: it represents a quantity
as a sentence. A dedicated time-series view (a bar chart per app per day) is a reasonable
future improvement, not implemented here.

Run:
    python usage.py /path/to/usage.json
"""
from __future__ import annotations

import glob
import json
import os

from mirror import Record, parse_timestamp


def _load_json(path: str) -> list:
    if not os.path.exists(path):
        raise ValueError(f"Export path does not exist: {path}")
    files = [path] if os.path.isfile(path) else sorted(glob.glob(
        os.path.join(path, "**", "*.json"), recursive=True))
    if not files:
        raise ValueError(f"No JSON files found in: {path}")
    blobs = []
    for f in files:
        try:
            with open(f, encoding="utf-8-sig") as fh:
                blobs.append(json.load(fh))
        except (ValueError, OSError) as exc:
            raise ValueError(f"Cannot read JSON export {f}: {exc}") from exc
    return blobs


def load(path: str) -> list[Record]:
    return load_blobs(_load_json(path))


def load_blobs(blobs: list) -> list[Record]:
    records: list[Record] = []
    seen: set[str] = set()
    for blob in blobs:
        rows = blob if isinstance(blob, list) else [blob]
        for row in rows:
            if not isinstance(row, dict):
                continue
            app = row.get("app")
            minutes = row.get("minutes")
            if not isinstance(app, str) or not app.strip():
                continue
            if not isinstance(minutes, (int, float)) or minutes < 0:
                continue
            app = app.strip()
            date = row.get("date")
            when = parse_timestamp(date) if isinstance(date, str) else None
            key = f"{app.lower()}:{date}"
            if key in seen:
                continue
            seen.add(key)
            value = int(minutes) if float(minutes).is_integer() else round(minutes, 1)
            records.append(Record(f"{app}: {value} minutes", "usage", app, when))
    return records


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="The Saint -- device usage adapter")
    ap.add_argument("export", help="a usage.json file, or a folder containing one")
    ap.add_argument("--out", default="usage_mirror.html")
    ap.add_argument("--inspect", action="store_true",
                    help="report parsed counts without downloading a model or rendering")
    args = ap.parse_args()
    try:
        records = load(args.export)
    except ValueError as exc:
        ap.error(str(exc))
    print(f"usage rows {len(records)}")
    if not args.inspect:
        if len(records) < 30:
            raise SystemExit(f"only {len(records)} usable records -- too few to cluster meaningfully")
        from mirror import embed, cluster, render
        render(records, *cluster(embed([r.text for r in records])), out=args.out)
