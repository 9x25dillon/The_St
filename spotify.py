#!/usr/bin/env python3
"""
The Saint -- Spotify source adapter.

Meets a Spotify "Extended Streaming History" export (privacy settings -> request extended
streaming history), arriving as Streaming_History_Audio_*.json files, each a JSON array of
play events. Spotify has also shipped a simpler, older export (endTime/trackName/artistName
fields instead of ts/master_metadata_track_name/master_metadata_album_artist_name) -- both
field sets are checked per entry, preferring the modern ones, so either export works. That
is a stable, documented shape, so this parses fields directly rather than the heuristic
key-scanning tiktok.py needs for a drift-prone export.

A real limitation, stated plainly: streaming history records what you played, not why it
was queued. A track can arrive from a search, a deliberate replay, or a Discover Weekly /
Radio recommendation you happened to click -- this export cannot tell those apart. Treat
every entry as EXPRESSED taste with that caveat, not as proof of self-directed listening.
There is no assigned-category stream in the export -- this adapter never invents one.

Run:
    python spotify.py /path/to/export
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


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def load_blobs(blobs: list) -> list[Record]:
    records: list[Record] = []
    seen: set[str] = set()
    for blob in blobs:
        entries = blob if isinstance(blob, list) else [blob]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            track = _text(entry.get("master_metadata_track_name")) or _text(entry.get("trackName"))
            artist = _text(entry.get("master_metadata_album_artist_name")) or _text(entry.get("artistName"))
            when_raw = entry.get("ts") if "ts" in entry else entry.get("endTime")
            when = parse_timestamp(when_raw) if isinstance(when_raw, str) else None
            if track and artist:
                text, source = f"{track} — {artist}", "track"
            else:
                episode = _text(entry.get("episode_name"))
                show = _text(entry.get("episode_show_name"))
                if not episode:
                    continue  # neither a track nor a podcast episode -- nothing usable
                text, source = (f"{episode} — {show}" if show else episode), "podcast"
            key = f"{source}:{text.lower()}"
            if key not in seen:
                seen.add(key)
                records.append(Record(text=text, source=source, detail="spotify", when=when))
    return records


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="The Saint -- Spotify adapter")
    ap.add_argument("export", help="Spotify extended streaming history folder, or a single json file")
    ap.add_argument("--out", default="spotify_mirror.html")
    ap.add_argument("--inspect", action="store_true",
                    help="report parsed counts without downloading a model or rendering")
    args = ap.parse_args()
    try:
        records = load(args.export)
    except ValueError as exc:
        ap.error(str(exc))
    tracks = sum(1 for r in records if r.source == "track")
    podcasts = sum(1 for r in records if r.source == "podcast")
    print(f"tracks {tracks}, podcast episodes {podcasts}")
    if not args.inspect:
        if len(records) < 30:
            raise SystemExit(f"only {len(records)} usable records -- too few to cluster meaningfully")
        from mirror import embed, cluster, render
        render(records, *cluster(embed([r.text for r in records])), out=args.out)
