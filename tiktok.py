#!/usr/bin/env python3
"""
The Algorithmic Mirror -- TikTok source adapter.

Meets a TikTok data export (Settings and privacy > Account > Download your data > JSON).
The export's schema drifts across app versions and splits across files, so this does NOT
hard-code paths -- it walks whatever JSON you point it at and finds the streams by their
shape. Point it at the unzipped export folder (or a single user_data.json). If a stream
comes up empty, widen the matchers below against your real file -- that tuning is expected.

Three kinds of data come out, and they are NOT equivalent:

  1. EXPRESSED text  -- search terms, hashtags, comments. Intent you typed. Embeddable.
  2. SERVED links    -- watch history is bare video URLs + timestamps, no captions.
                        A volume/timeline signal; not embeddable without resolving each
                        URL (slow, rate-limited, ToS-gray -- skip for v1).
  3. ASSIGNED labels -- TikTok's own inferred ad-interest categories for you. Not their
                        weights; their OUTPUT -- who they decided you are.

The examination: cluster (1), then drop (3) into the same space. Categories that land far
from anything you actually searched are the algorithm's reach beyond your stated intent --
the divergence browser history could never surface.

Run:
    python tiktok.py /path/to/unzipped_export
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mirror import Record, embed          # reuse module-one primitives

_URL = re.compile(r"https?://\S*tiktok\.com/\S+", re.I)
_DATE_KEY = re.compile(r"date|time", re.I)
_SEARCH = re.compile(r"search.?term", re.I)
_HASHTAG = re.compile(r"hashtag.?name|^hashtag$", re.I)
_SOUND = re.compile(r"sound.?name|song.?name", re.I)
_INTEREST_KEY = re.compile(r"interest|categor", re.I)


@dataclass
class TikTokExport:
    expressed: list[Record] = field(default_factory=list)   # embeddable intent
    watch_times: list[float] = field(default_factory=list)  # served-video timestamps
    ad_categories: list[str] = field(default_factory=list)  # TikTok's labels for you


def _parse_date(v: str):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(v.strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, AttributeError):
            continue
    return None


def _iter_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_dicts(v)


def _load_json(path: str) -> list:
    files = [path] if os.path.isfile(path) else glob.glob(
        os.path.join(path, "**", "*.json"), recursive=True)
    blobs = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                blobs.append(json.load(fh))
        except (json.JSONDecodeError, OSError):
            continue
    return blobs


def load(path: str) -> TikTokExport:
    out = TikTokExport()
    seen: set[str] = set()

    def add(text: str, source: str):
        t = (text or "").strip().lstrip("#")
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.expressed.append(Record(text=t, source=source, detail="tiktok"))

    for blob in _load_json(path):
        for node in _iter_dicts(blob):
            node_date = None
            for k, v in node.items():
                if isinstance(v, str) and _DATE_KEY.search(k):
                    node_date = _parse_date(v) or node_date
            for k, v in node.items():
                if isinstance(v, str):
                    if _SEARCH.search(k):
                        add(v, "search")
                    elif _HASHTAG.search(k):
                        add(v, "hashtag")
                    elif _SOUND.search(k):
                        add(v, "sound")
                    elif k.strip().lower() == "comment":
                        add(v, "comment")
                    elif _URL.search(v) and node_date is not None:
                        out.watch_times.append(node_date)     # served video, timestamp only
                elif isinstance(v, list) and _INTEREST_KEY.search(k):
                    out.ad_categories += [i.strip() for i in v
                                          if isinstance(i, str) and 0 < len(i) < 60]

    out.ad_categories = sorted(set(out.ad_categories))
    return out


def divergence_html(exp: TikTokExport, out: str = "tiktok_mirror.html"):
    """Expressed signals (dots) and TikTok's assigned categories (stars) in one space."""
    import hdbscan
    import numpy as np
    import plotly.graph_objects as go
    import umap

    if len(exp.expressed) < 30:
        raise SystemExit(
            f"only {len(exp.expressed)} expressed items -- too few; widen the matchers "
            "against your real export (search/hashtag/comment key names may differ)")

    texts = [r.text for r in exp.expressed]
    vecs = embed(texts)
    reducer = umap.UMAP(n_components=2, metric="cosine", random_state=7).fit(vecs)
    xy = reducer.embedding_
    # cluster in mid-dim, never on the 2D picture
    mid = umap.UMAP(n_components=15, metric="cosine", random_state=7).fit_transform(vecs)
    labels = np.asarray(hdbscan.HDBSCAN(min_cluster_size=10, min_samples=3).fit_predict(mid))

    fig = go.Figure()
    for lab in sorted(set(labels.tolist())):
        m = labels == lab
        fig.add_trace(go.Scattergl(
            x=xy[m, 0], y=xy[m, 1], mode="markers",
            name="noise" if lab == -1 else f"island {lab}",
            text=[texts[i] for i in np.where(m)[0]],
            hovertemplate="%{text}<extra></extra>",
            marker=dict(size=6, opacity=0.3 if lab == -1 else 0.8)))

    if exp.ad_categories:                      # project TikTok's labels into the SAME space
        cxy = reducer.transform(embed(exp.ad_categories))
        fig.add_trace(go.Scatter(
            x=cxy[:, 0], y=cxy[:, 1], mode="markers+text",
            name="TikTok says you're into", text=exp.ad_categories, textposition="top center",
            marker=dict(size=15, symbol="star", color="gold", line=dict(width=1, color="black"))))

    fig.update_layout(
        title="What you typed (dots) vs what TikTok decided you are (stars)",
        template="plotly_dark", showlegend=True,
        xaxis=dict(visible=False), yaxis=dict(visible=False))
    fig.write_html(out, include_plotlyjs=True)
    print(f"wrote {out}: {len(texts)} expressed signals, {len(exp.ad_categories)} assigned categories")
    print("stars far from any dot cluster are the algorithm's reach past what you actually sought")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="The Algorithmic Mirror -- TikTok adapter")
    ap.add_argument("export", help="unzipped TikTok export folder, or a single user_data.json")
    ap.add_argument("--out", default="tiktok_mirror.html")
    args = ap.parse_args()
    exp = load(args.export)
    print(f"expressed {len(exp.expressed)}, watched {len(exp.watch_times)}, "
          f"categories {len(exp.ad_categories)}")
    divergence_html(exp, args.out)
