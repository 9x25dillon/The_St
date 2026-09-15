#!/usr/bin/env python3
"""
The Saint -- Reddit source adapter.

Meets Reddit's official GDPR data export (reddit.com/settings/data-request), specifically
posts.csv and comments.csv. Column headers are matched by regex rather than hard-coded
names, since Reddit has adjusted export headers before -- same "the schema drifts, so scan
rather than hard-code" reasoning tiktok.py gives. A file is classified as posts or comments
by its own header shape (posts carry a title/url column, comments carry a parent/link
column), not by filename, so renamed files still parse correctly.

Two kinds of data come out, and they are NOT equivalent:

  1. POSTS    -- title (+ body, when present). Long-form intent you wrote.
  2. COMMENTS -- body text. Shorter, more reactive intent you wrote.

Headers were checked against the UChicago DSAR export schemas (2026-09): posts.csv is
id,permalink,date,ip,subreddit,gildings,title,url,body and comments.csv is
id,permalink,date,ip,subreddit,gildings,link,parent,body,media. "[deleted]"/"[removed]"
placeholders are dropped. Google Takeout also ships a comments.csv (YouTube comments,
"Comment Text" column); that one is recognized and refused with a clear message rather
than silently yielding nothing.

Both are EXPRESSED text -- there is no equivalent of TikTok's assigned ad-interest
categories in Reddit's standard export, and no served/watch stream (Reddit doesn't export
your feed impressions). Saved posts and vote history exist in the export but aren't parsed
for v1: they're pointers to other people's content, not something you wrote.

Run:
    python reddit.py /path/to/export
"""
from __future__ import annotations

import csv
import glob
import io
import os
import re

from mirror import Record, parse_timestamp

_TITLE_COL = re.compile(r"title", re.I)
_BODY_COL = re.compile(r"^body$", re.I)
_SUBREDDIT_COL = re.compile(r"subreddit", re.I)
_DATE_COL = re.compile(r"^date$", re.I)
_POST_HINT = re.compile(r"^(title|url)$", re.I)
_COMMENT_HINT = re.compile(r"^(parent|link)$", re.I)
_REMOVED_TEXT = {"[deleted]", "[removed]"}


def _parse_reddit_date(value: str | None):
    if not value:
        return None
    # Reddit's export uses "YYYY-MM-DD HH:MM:SS UTC"; strip the trailing zone name so
    # mirror.parse_timestamp's fromisoformat-based parsing can read it.
    return parse_timestamp(re.sub(r"\s*UTC$", "+00:00", value.strip(), flags=re.I))


def _classify(fieldnames: list[str]) -> str | None:
    names = {name.strip().lower() for name in fieldnames if name}
    if names & {"title", "url"}:
        return "post"
    if names & {"parent", "link"}:
        return "comment"
    return None


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
            kind = _classify(fieldnames)
            if kind is None:
                if any(name and name.strip().lower() == "comment text" for name in fieldnames):
                    raise ValueError("This comments.csv looks like a YouTube Takeout export, which isn't "
                                     "supported yet. Choose Reddit's posts.csv or comments.csv.")
                continue  # not a posts/comments export -- skip rather than guess wrong
            title_col = _find_column(fieldnames, _TITLE_COL)
            body_col = _find_column(fieldnames, _BODY_COL)
            subreddit_col = _find_column(fieldnames, _SUBREDDIT_COL)
            date_col = _find_column(fieldnames, _DATE_COL)
            for row in reader:
                title = (row.get(title_col) or "").strip() if title_col else ""
                body = (row.get(body_col) or "").strip() if body_col else ""
                title, body = ("" if title in _REMOVED_TEXT else title), ("" if body in _REMOVED_TEXT else body)
                text_value = f"{title}. {body}".strip(". ").strip() if kind == "post" else body
                if not text_value:
                    continue
                key = f"{kind}:{text_value.lower()}"
                if key in seen:
                    continue
                seen.add(key)
                detail = (row.get(subreddit_col) or "reddit").strip() if subreddit_col else "reddit"
                when = _parse_reddit_date(row.get(date_col)) if date_col else None
                records.append(Record(text_value, kind, detail, when))
        except csv.Error as exc:
            raise ValueError(f"Cannot read CSV export: {exc}") from exc
    return records


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="The Saint -- Reddit adapter")
    ap.add_argument("export", help="folder containing posts.csv / comments.csv, or a single csv file")
    ap.add_argument("--out", default="reddit_mirror.html")
    ap.add_argument("--inspect", action="store_true",
                    help="report parsed counts without downloading a model or rendering")
    args = ap.parse_args()
    try:
        records = load(args.export)
    except ValueError as exc:
        ap.error(str(exc))
    posts = sum(1 for r in records if r.source == "post")
    comments = sum(1 for r in records if r.source == "comment")
    print(f"posts {posts}, comments {comments}")
    if not args.inspect:
        if len(records) < 30:
            raise SystemExit(f"only {len(records)} usable records -- too few to cluster meaningfully")
        from mirror import embed, cluster, render
        render(records, *cluster(embed([r.text for r in records])), out=args.out)
