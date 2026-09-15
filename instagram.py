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

Checked against real published exports and the UChicago DSAR export schemas (2026-09):
search history is `searches_keyword[].string_map_data.Search` (in
logged_information/recent_searches/word_or_phrase_searches.json) and topics are
`topics_your_topics[].string_map_data.Name` (in preferences/your_topics/
recommended_topics.json, formerly your_topics.json). The field names inside
string_map_data are LOCALIZED to the account's language ("Nome" in a non-English export),
so entries are classified by the stable container key (topics_* / searches_*) first and
by English field names only as a fallback. Profile searches (searches_user) are other
people's usernames, not topics you searched, and are skipped. Meta writes UTF-8 text as
if each byte were a Latin-1 character ("â€™" for ’); that is undone per value when it
cleanly round-trips (not observable in the ASCII-only samples, so treat as best-effort).

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
_TOPIC_CONTAINER = re.compile(r"^topics_", re.I)
_SEARCH_CONTAINER = re.compile(r"^searches_", re.I)
_PEOPLE_SEARCH_CONTAINER = re.compile(r"user|profile|account", re.I)


def _iter_dicts(node, path=()):
    if isinstance(node, dict):
        yield node, path
        for k, v in node.items():
            yield from _iter_dicts(v, path + (k,))
    elif isinstance(node, list):
        for v in node:
            yield from _iter_dicts(v, path)


def fix_meta_text(value: str) -> str:
    """Undo Meta's export encoding, where each UTF-8 byte was stored as a Latin-1 character.
    Text that doesn't round-trip (real Latin-1 accents, anything beyond U+00FF) is untouched."""
    if not any(0x80 <= ord(c) <= 0xFF for c in value):
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


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
    def add_search(value, when):
        key = f"search:{value.lower()}"
        if key not in seen:
            seen.add(key)
            expressed.append(Record(value, "search", "instagram", when))

    for blob in blobs:
        for node, path in _iter_dicts(blob):
            smd = node.get("string_map_data")
            if not isinstance(smd, dict):
                continue
            when = None
            for v in smd.values():
                if isinstance(v, dict) and isinstance(v.get("timestamp"), (int, float)) and v["timestamp"]:
                    when = float(v["timestamp"])
            values = []
            for field, v in smd.items():
                value = v.get("value") if isinstance(v, dict) else None
                if isinstance(value, str) and value.strip() and value.strip().lower() != "not_stored":
                    values.append((field, fix_meta_text(value.strip())))
            container = next((k for k in reversed(path) if isinstance(k, str)
                              and (_TOPIC_CONTAINER.match(k) or _SEARCH_CONTAINER.match(k))), None)
            if container and _TOPIC_CONTAINER.match(container):
                categories += [value for _, value in values if len(value) < 60]
            elif container:
                if not _PEOPLE_SEARCH_CONTAINER.search(container) and values:
                    add_search(values[0][1], when)
            else:
                for field, value in values:
                    if _TOPIC_FIELD.match(field):
                        if len(value) < 60:
                            categories.append(value)
                    elif _SEARCH_FIELD.search(field):
                        add_search(value, when)
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
