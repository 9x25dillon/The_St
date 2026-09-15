#!/usr/bin/env python3
"""
The Saint -- X (Twitter) source adapter.

Meets an X "Download an archive of your data" export. Data files live under a `data/`
folder as `.js` files, each wrapped as a JS assignment --
`window.YTD.<stream>.part0 = [ ... ];` -- not raw JSON, with the real data nested one level
deeper under a stream-specific key (`"tweet"`, `"like"`, `"savedSearch"`, ...). This strips
the wrapper, keeps the stream name it announces, and walks whatever JSON comes out,
classifying by key name and by the stream/keys leading to it.

Checked against X's own archive README (2025-01 archive) and real published archives
(2026-09). Three kinds of data come out, and they are NOT equivalent:

  1. EXPRESSED text  -- your own posts (tweets.js `full_text`; "RT @..." retweets are kept
                        apart as `repost`, since the words aren't yours) and saved searches
                        (saved-search.js `query` -- the archive has no full search history).
  2. SERVED text     -- liked posts (like.js `fullText`): someone else's words you endorsed,
                        kept as `like`, never as your own post.
  3. ASSIGNED topics -- personalization.js `p13nData.interests.interests[]` objects
                        ({name, isDisabled}; disabled ones are skipped), partnerInterests,
                        and inferred `shows`. X's OUTPUT about you, not their weights.

Ad impressions, follows, DMs, and Grok chats are intentionally not parsed for v1. Tweet
timestamps use the classic "Wed Oct 10 20:19:24 +0000 2018" form and are parsed as such.

Run:
    python x.py /path/to/unzipped_export
"""
from __future__ import annotations

from datetime import datetime
import glob
import json
import os
import re

from mirror import Record, parse_timestamp

_WRAPPER = re.compile(r"^\s*window\.YTD\.(\w+)\.part\d+\s*=\s*")
_QUERY_FIELD = re.compile(r"^query$", re.I)
_TWEET_TEXT_FIELD = re.compile(r"^full_?text$", re.I)
_INTEREST_LIST = re.compile(r"^(interests|partnerInterests)$", re.I)
_DATE_FIELD = re.compile(r"date|time|created.?at", re.I)


def _iter_dicts(node, path=()):
    if isinstance(node, dict):
        yield node, path
        for k, v in node.items():
            yield from _iter_dicts(v, path + (k,))
    elif isinstance(node, list):
        for v in node:
            yield from _iter_dicts(v, path)


def _parse_date(value: str) -> float | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        try:
            parsed = datetime.strptime(value.strip(), "%a %b %d %H:%M:%S %z %Y").timestamp()
        except ValueError:
            pass
    return parsed


def _unwrap(text: str) -> tuple[str | None, object]:
    """(stream name from the window.YTD wrapper or None, parsed JSON)."""
    stripped = text.lstrip("\ufeff")
    stream = None
    wrapper = _WRAPPER.match(stripped)
    if wrapper:
        stream = wrapper.group(1)
        stripped = stripped[wrapper.end():].rstrip()
        if stripped.endswith(";"):
            stripped = stripped[:-1]
    try:
        return stream, json.loads(stripped)
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
    return _load_streams([_unwrap(file["text"]) for file in files])


def load_blobs(blobs: list) -> tuple[list[Record], list[str]]:
    """Already-parsed JSON with no wrapper, so no stream name to go on."""
    return _load_streams([(None, blob) for blob in blobs])


def _load_streams(streams: list[tuple[str | None, object]]) -> tuple[list[Record], list[str]]:
    expressed: list[Record] = []
    categories: list[str] = []
    seen: set[str] = set()

    def add(text: str, source: str, when: float | None):
        t = (text or "").strip()
        key = f"{source}:{t.lower()}"
        if t and key not in seen:
            seen.add(key)
            expressed.append(Record(t, source, "x", when))

    def add_label(item):
        if isinstance(item, dict):
            if item.get("isDisabled") is True:
                return
            item = item.get("name")
        if isinstance(item, str) and 0 < len(item.strip()) < 60:
            categories.append(item.strip())

    for stream, blob in streams:
        for node, path in _iter_dicts(blob, (stream,) if stream else ()):
            liked = any(isinstance(k, str) and k.lower() == "like" for k in path)
            in_interests = any(isinstance(k, str) and k.lower() == "interests" for k in path)
            node_date = None
            for k, v in node.items():
                if isinstance(v, str) and _DATE_FIELD.search(k):
                    parsed = _parse_date(v)
                    if parsed is not None:
                        node_date = parsed
            for k, v in node.items():
                if isinstance(v, str):
                    if _TWEET_TEXT_FIELD.match(k):
                        source = "like" if liked else "repost" if v.lstrip().startswith("RT @") else "post"
                        add(v, source, node_date)
                    elif _QUERY_FIELD.match(k):
                        add(v, "search", node_date)
                elif isinstance(v, list) and (_INTEREST_LIST.match(k) or (k == "shows" and in_interests)):
                    for item in v:
                        add_label(item)
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
