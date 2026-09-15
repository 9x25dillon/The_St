#!/usr/bin/env python3
"""
The Saint -- Instagram source adapter.

Meets a Meta "Download your information" JSON export (Instagram > Settings > Accounts
Center > Your information and permissions > Download your information > JSON). Instagram's
export wraps almost every value in a {"string_map_data": {"<Field>": {"value": ...,
"timestamp": ...}}} shape, and which files exist (and what they're named) drifts across
export versions -- the same problem TikTok's export has -- so this walks whatever JSON you
point it at and classifies by field name inside that wrapper, not by filename.

Two kinds of data come out, and they are NOT equivalent:

  1. EXPRESSED search -- "Search" fields inside your search history. Intent you typed.
  2. ASSIGNED topics  -- "Name" fields inside your_topics.json. Meta's own inferred
                         interest categories for you. Not their weights; their OUTPUT.

Ad/post view history is intentionally not parsed for v1 -- it typically carries little
more than an advertiser name and a timestamp, a volume signal rather than expressible
text (the same reasoning tiktok.py gives for skipping bare watch links). If a stream comes
up empty, widen the matchers below against your real export -- that tuning is expected.

Run:
    python instagram.py /path/to/unzipped_export
"""
from __future__ import annotations

import glob
import json
import os
import re

from mirror import Record

_SEARCH_FIELD = re.compile(r"search", re.I)
_TOPIC_FIELD = re.compile(r"^name$", re.I)


def _iter_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_dicts(v)


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


def load(path: str) -> tuple[list[Record], list[str]]:
    return load_blobs(_load_json(path))


def load_blobs(blobs: list) -> tuple[list[Record], list[str]]:
    expressed: list[Record] = []
    categories: list[str] = []
    seen: set[str] = set()
    for blob in blobs:
        for node in _iter_dicts(blob):
            smd = node.get("string_map_data")
            if not isinstance(smd, dict):
                continue
            when = None
            for v in smd.values():
                if isinstance(v, dict) and isinstance(v.get("timestamp"), (int, float)):
                    when = float(v["timestamp"])
            for field, v in smd.items():
                if not isinstance(v, dict):
                    continue
                value = v.get("value")
                if not isinstance(value, str):
                    continue
                value = value.strip()
                if not value or value.lower() == "not_stored":
                    continue
                if _TOPIC_FIELD.match(field):
                    if 0 < len(value) < 60:
                        categories.append(value)
                elif _SEARCH_FIELD.search(field):
                    key = f"search:{value.lower()}"
                    if key not in seen:
                        seen.add(key)
                        expressed.append(Record(value, "search", "instagram", when))
    return expressed, sorted(set(categories))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="The Saint -- Instagram adapter")
    ap.add_argument("export", help="unzipped Instagram export folder, or a single json file")
    ap.add_argument("--out", default="instagram_mirror.html")
    ap.add_argument("--inspect", action="store_true",
                    help="report parsed counts without downloading a model or rendering")
    args = ap.parse_args()
    try:
        expressed, categories = load(args.export)
    except ValueError as exc:
        ap.error(str(exc))
    print(f"expressed {len(expressed)}, categories {len(categories)}")
    if not args.inspect:
        if len(expressed) < 30:
            raise SystemExit(f"only {len(expressed)} expressed items -- too few; widen the matchers "
                             "against your real export (string_map_data field names may differ)")
        from mirror import embed, cluster, render
        render(expressed, *cluster(embed([r.text for r in expressed])), out=args.out)
