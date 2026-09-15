#!/usr/bin/env python3
"""
The Saint -- YouTube source adapter.

Meets a Google Takeout YouTube export (takeout.google.com -> select YouTube and
YouTube Music -> History). Expects watch-history.json and/or search-history.json,
each a JSON array of {title, titleUrl, time, ...} entries. That is Google's own
documented Takeout schema and has stayed stable, so this parses fields directly
rather than the heuristic key-scanning tiktok.py needs for a drift-prone export.

Two kinds of data come out, and they are NOT equivalent:

  1. EXPRESSED search -- what you typed into YouTube search. Intent you typed.
  2. WATCHED titles   -- what YouTube served and you watched. Takeout gives real video
                         titles (unlike TikTok's bare watch links), so these ARE
                         embeddable, but they are served content, not something you typed.

Every entry is classified by its own "Searched for " / "Watched " title prefix rather
than by filename, so watch-history.json and search-history.json can be passed together
or separately, in either order. A standard Takeout export carries no assigned
ad-interest-category stream (that lives in a separate, non-standard Google Ads Settings
export) -- this adapter never invents one.

Run:
    python youtube.py "/path/to/Takeout/YouTube and YouTube Music/history"
"""
from __future__ import annotations

import glob
import json
import os
import re

from mirror import Record, parse_timestamp

_SEARCHED = re.compile(r"^searched for\s+", re.I)
_WATCHED = re.compile(r"^watched\s+", re.I)
_REMOVED = re.compile(r"a video that has been removed|a video that isn.t available", re.I)


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
        entries = blob if isinstance(blob, list) else [blob]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            when = parse_timestamp(entry.get("time")) if isinstance(entry.get("time"), str) else None
            detail = entry.get("titleUrl") if isinstance(entry.get("titleUrl"), str) else "youtube"
            if _SEARCHED.match(title):
                text, source = _SEARCHED.sub("", title).strip(), "search"
            elif _REMOVED.search(title):
                continue  # no usable content
            else:
                text, source = _WATCHED.sub("", title).strip(), "watch"
            key = f"{source}:{text.lower()}"
            if text and key not in seen:
                seen.add(key)
                records.append(Record(text=text, source=source, detail=detail, when=when))
    return records


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="The Saint -- YouTube adapter")
    ap.add_argument("export", help="Takeout YouTube history folder, or a single json file")
    ap.add_argument("--out", default="youtube_mirror.html")
    ap.add_argument("--inspect", action="store_true",
                    help="report parsed counts without downloading a model or rendering")
    args = ap.parse_args()
    try:
        records = load(args.export)
    except ValueError as exc:
        ap.error(str(exc))
    searched = sum(1 for r in records if r.source == "search")
    watched = sum(1 for r in records if r.source == "watch")
    print(f"searched {searched}, watched {watched}")
    if not args.inspect:
        if len(records) < 30:
            raise SystemExit(f"only {len(records)} usable records -- too few to cluster meaningfully")
        from mirror import embed, cluster, render
        render(records, *cluster(embed([r.text for r in records])), out=args.out)
