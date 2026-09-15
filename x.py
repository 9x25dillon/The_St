#!/usr/bin/env python3
"""
The Saint -- X (Twitter) source adapter.

Meets an X "Download an archive of your data" export. Data files live under a `data/`
folder as `.js` files, each wrapped as a JS assignment --
`window.YTD.<stream>.part0 = [ ... ];` -- not raw JSON, and the real data is usually
nested one level deeper under a stream-specific key (`"tweet"`, `"searchHistory"`, ...).
Which streams exist and how they're nested has drifted before and will again, so this
strips the `window.YTD...=` wrapper and then walks whatever JSON comes out, classifying by
key name wherever it appears -- the same "the schema drifts, so scan rather than hard-code"
approach tiktok.py and instagram.py already take. If a stream comes up empty, widen the
matchers below against your real export -- that tuning is expected.

Two kinds of data come out, and they are NOT equivalent:

  1. EXPRESSED text  -- search queries and your own tweet/post text. Intent you typed.
  2. ASSIGNED topics -- X's own inferred interest categories for you (personalization
                        data). Not their weights; their OUTPUT -- who they decided you are.

Engagement/impression history (likes received, ad impressions, who you follow) is
intentionally not parsed for v1 -- it's a much less reliable "expressed intent" signal than
a search query or a tweet you wrote, the same reasoning instagram.py gives for skipping ad
views. Classic tweet timestamps ("Mon Jan 01 00:00:00 +0000 2024") are not ISO-8601 and are
not parsed -- `when` is None for those rather than guessing at a second date format.

Run:
    python x.py /path/to/unzipped_export
"""
from __future__ import annotations

import glob
import json
import os
import re

from mirror import Record, parse_timestamp

_WRAPPER = re.compile(r"^\s*window\.YTD\.\w+\.part\d+\s*=\s*")
_QUERY_FIELD = re.compile(r"query", re.I)
_TWEET_TEXT_FIELD = re.compile(r"^full_?text$", re.I)
_INTEREST_FIELD = re.compile(r"interest", re.I)
_DATE_FIELD = re.compile(r"date|time", re.I)


def _iter_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_dicts(v)


def _unwrap(text: str):
    stripped = text.lstrip("﻿")
    if stripped.startswith("window.YTD"):
        stripped = _WRAPPER.sub("", stripped, count=1).rstrip()
        if stripped.endswith(";"):
            stripped = stripped[:-1]
    try:
        return json.loads(stripped)
    except ValueError as exc:
        raise ValueError(f"Cannot read JSON export: {exc}") from exc


def _load_files(path: str) -> list[dict]:
    if not os.path.exists(path):
        raise ValueError(f"Export path does not exist: {path}")
    files = [path] if os.path.isfile(path) else sorted(glob.glob(
        os.path.join(path, "**", "*.js"), recursive=True))
    if not files:
        raise ValueError(f"No .js files found in: {path}")
    out = []
    for f in files:
        try:
            with open(f, encoding="utf-8-sig") as fh:
                out.append({"name": os.path.basename(f), "text": fh.read()})
        except OSError as exc:
            raise ValueError(f"Cannot read export file {f}: {exc}") from exc
    return out


def load(path: str) -> tuple[list[Record], list[str]]:
    return load_files(_load_files(path))


def load_files(files: list[dict]) -> tuple[list[Record], list[str]]:
    return load_blobs([_unwrap(file["text"]) for file in files])


def load_blobs(blobs: list) -> tuple[list[Record], list[str]]:
    expressed: list[Record] = []
    categories: list[str] = []
    seen: set[str] = set()

    def add(text: str, source: str, when: float | None):
        t = (text or "").strip()
        key = f"{source}:{t.lower()}"
        if t and key not in seen:
            seen.add(key)
            expressed.append(Record(t, source, "x", when))

    for blob in blobs:
        for node in _iter_dicts(blob):
            node_date = None
            for k, v in node.items():
                if isinstance(v, str) and _DATE_FIELD.search(k):
                    parsed = parse_timestamp(v)
                    if parsed is not None:
                        node_date = parsed
            for k, v in node.items():
                if isinstance(v, str):
                    if _TWEET_TEXT_FIELD.match(k):
                        add(v, "post", node_date)
                    elif _QUERY_FIELD.search(k):
                        add(v, "search", node_date)
                    elif _INTEREST_FIELD.search(k):
                        label = v.strip()
                        if 0 < len(label) < 60:
                            categories.append(label)
                elif isinstance(v, list) and _INTEREST_FIELD.search(k):
                    categories += [i.strip() for i in v if isinstance(i, str) and 0 < len(i.strip()) < 60]
    return expressed, sorted(set(categories))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="The Saint -- X (Twitter) adapter")
    ap.add_argument("export", help="unzipped X archive folder, or a single .js file")
    ap.add_argument("--out", default="x_mirror.html")
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
                             "against your real export (key names may differ)")
        from mirror import embed, cluster, render
        render(expressed, *cluster(embed([r.text for r in expressed])), out=args.out)
