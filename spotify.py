#!/usr/bin/env python3
"""
The Saint -- Spotify source adapter.

Meets both Spotify exports (Privacy settings -> Download your data):

  * Extended streaming history -- Streaming_History_Audio_*.json, each a JSON array of play
    events (ts, ms_played, master_metadata_track_name, master_metadata_album_artist_name,
    episode_name, episode_show_name, audiobook_title, reason_start, skipped, ...). Real
    files run ~12.8 MB apiece (~16k plays).
  * Account data -- StreamingHistory_music_*.json (endTime, artistName, trackName,
    msPlayed), StreamingHistory_podcast_*.json (endTime, podcastName, episodeName,
    msPlayed), SearchQueries.json (searchTime, searchQuery), and Inferences.json
    ({"inferences": [...]}).

Field names were checked against a real 2025 extended-history file and the UChicago DSAR
export schemas (2026-09); each entry is classified by the fields it carries, so any mix of
these files works.

Three kinds of data come out, and they are NOT equivalent:

  1. EXPRESSED searches -- SearchQueries.json, what you typed into Spotify search.
  2. PLAYED audio       -- tracks, podcast episodes, audiobook chapters. Streaming history
                           records what played, not why: a recommendation you let run and a
                           deliberate replay look alike. Plays under 30 seconds are dropped
                           as skips (54% of plays in one real sample), not counted as taste.
  3. ASSIGNED labels    -- Inferences.json, Spotify's own segments for you (e.g.
                           "1P_Custom_..."), shown with underscores as spaces.

Run:
    python spotify.py /path/to/export
"""
from __future__ import annotations

import glob
import json
import os
import re

from mirror import Record, parse_timestamp

MIN_PLAY_MS = 30_000
_ZONE_SUFFIX = re.compile(r"\[[^\]]*\]$")  # e.g. "...Z[UTC]"


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


def _text(value) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)  # the account-data schema allows numeric track names
    return value.strip() if isinstance(value, str) and value.strip() else None


def _when(value) -> float | None:
    return parse_timestamp(_ZONE_SUFFIX.sub("", value.strip())) if isinstance(value, str) else None


def load_blobs(blobs: list) -> tuple[list[Record], list[str]]:
    records: list[Record] = []
    categories: list[str] = []
    seen: set[str] = set()

    def add(text: str, source: str, when: float | None):
        key = f"{source}:{text.lower()}"
        if key not in seen:
            seen.add(key)
            records.append(Record(text=text, source=source, detail="spotify", when=when))

    for blob in blobs:
        if isinstance(blob, dict) and isinstance(blob.get("inferences"), list):
            categories += [label.replace("_", " ").strip() for label in blob["inferences"]
                           if isinstance(label, str) and 0 < len(label.strip()) < 100]
            continue
        entries = blob if isinstance(blob, list) else [blob]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if "searchQuery" in entry:
                query = _text(entry.get("searchQuery"))
                if query:
                    add(query, "search", _when(entry.get("searchTime")))
                continue
            played = entry.get("ms_played", entry.get("msPlayed"))
            if isinstance(played, (int, float)) and not isinstance(played, bool) and played < MIN_PLAY_MS:
                continue
            when = _when(entry.get("ts") if "ts" in entry else entry.get("endTime"))
            track = _text(entry.get("master_metadata_track_name")) or _text(entry.get("trackName"))
            artist = _text(entry.get("master_metadata_album_artist_name")) or _text(entry.get("artistName"))
            episode = _text(entry.get("episode_name")) or _text(entry.get("episodeName"))
            show = _text(entry.get("episode_show_name")) or _text(entry.get("podcastName"))
            audiobook = _text(entry.get("audiobook_title"))
            chapter = _text(entry.get("audiobook_chapter_title"))
            if track and artist:
                add(f"{track} — {artist}", "track", when)
            elif episode:
                add(f"{episode} — {show}" if show else episode, "podcast", when)
            elif audiobook:
                add(f"{chapter} — {audiobook}" if chapter else audiobook, "audiobook", when)
    return records, sorted(set(categories))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="The Saint -- Spotify adapter")
    ap.add_argument("export", help="Spotify extended streaming history folder, or a single json file")
    ap.add_argument("--out", default="spotify_mirror.html")
    ap.add_argument("--inspect", action="store_true",
                    help="report parsed counts without downloading a model or rendering")
    args = ap.parse_args()
    try:
        records, categories = load(args.export)
    except ValueError as exc:
        ap.error(str(exc))
    tracks = sum(1 for r in records if r.source == "track")
    podcasts = sum(1 for r in records if r.source == "podcast")
    searches = sum(1 for r in records if r.source == "search")
    print(f"tracks {tracks}, podcast episodes {podcasts}, searches {searches}, inferences {len(categories)}")
    if not args.inspect:
        if len(records) < 30:
            raise SystemExit(f"only {len(records)} usable records -- too few to cluster meaningfully")
        from mirror import embed, cluster, render
        render(records, *cluster(embed([r.text for r in records])), out=args.out)
