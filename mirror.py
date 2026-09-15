#!/usr/bin/env python3
"""
The Saint -- module one: the self-mirror.

Hypothesis under test:
    My own browser exhaust, embedded and clustered, resolves into
    behavioral islands I actually recognize.

If it does, the rest of the system (DNS gating, egress inspection, drift
tracking, a node graph) is worth building. If it does NOT, stop here -- you
learned that in a weekend instead of after building a D3 graph.

Run:
    python mirror.py --demo               # synthetic data, no browser, proves the machinery
    python mirror.py --browser firefox
    python mirror.py --browser chrome

Output:
    mirror.html -- interactive 2D scatter, points colored by cluster, hover to
                   read the title or query behind each point, outliers marked.
                   The plotting library is embedded, so nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse, parse_qs


if TYPE_CHECKING:
    import numpy as np


@dataclass
class Record:
    text: str        # what gets embedded -- a page title or a search query
    source: str      # "title" or "query"
    detail: str      # url, kept for hover context
    when: float | None = None  # unix timestamp; None when the source has no reliable date


def parse_timestamp(v) -> float | None:
    """Parse an ISO-8601-ish date string to a unix timestamp, naive dates treated as UTC."""
    if not isinstance(v, str):
        return None
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ------------------------------- ingestion --------------------------------

FIREFOX_GLOBS = [
    "~/.mozilla/firefox/*/places.sqlite",
    "~/snap/firefox/common/.mozilla/firefox/*/places.sqlite",
    "~/.var/app/org.mozilla.firefox/.mozilla/firefox/*/places.sqlite",
    "~/Library/Application Support/Firefox/Profiles/*/places.sqlite",
    "~/AppData/Roaming/Mozilla/Firefox/Profiles/*/places.sqlite",
]

CHROME_GLOBS = [
    "~/.config/google-chrome/Default/History",
    "~/.config/chromium/Default/History",
    "~/.config/google-chrome/Profile */History",
    "~/.config/chromium/Profile */History",
    "~/Library/Application Support/Google/Chrome/Default/History",
    "~/AppData/Local/Google/Chrome/User Data/Default/History",
]


def _first_existing(globs: list[str]) -> str | None:
    for g in globs:
        hits = sorted(glob.glob(os.path.expanduser(g)))
        if hits:
            return hits[0]
    return None


def _read_history(db_path: str, table: str) -> list[Record]:
    # SQLite backup includes committed WAL data and never modifies the source.
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="saint-history-") as folder:
        source = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True,
                                 timeout=2)
        target = sqlite3.connect(os.path.join(folder, "history.sqlite"))
        try:
            import time
            deadline = time.monotonic() + 5
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise ValueError("Browser history is busy. Close the browser and retry.")
            source.backup(target, pages=256, progress=progress)
            rows = target.execute(f"SELECT url, title FROM {table}").fetchall()
        finally:
            target.close()
            source.close()
    return _rows_to_records(rows)


def _extract_query(url: str) -> str | None:
    try:
        q = parse_qs(urlparse(url).query).get("q")
        return q[0].strip() if q and q[0].strip() else None
    except Exception:
        return None


def _rows_to_records(rows) -> list[Record]:
    recs: list[Record] = []
    for url, title in rows:
        query = _extract_query(url)
        if query:
            recs.append(Record(text=query, source="query", detail=url))
        elif title:
            recs.append(Record(text=title, source="title", detail=url))
    # collapse identical texts, preserve order
    seen: set[str] = set()
    out: list[Record] = []
    for r in recs:
        key = r.text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def read_firefox() -> list[Record]:
    db = _first_existing(FIREFOX_GLOBS)
    if not db:
        raise ValueError("No Firefox places.sqlite found. Try --browser chrome, or edit FIREFOX_GLOBS.")
    return _read_history(db, "moz_places")


def read_chrome() -> list[Record]:
    db = _first_existing(CHROME_GLOBS)
    if not db:
        raise ValueError("No Chrome History DB found. Try --browser firefox, or edit CHROME_GLOBS.")
    return _read_history(db, "urls")


def read_demo(n: int = 600):
    """Synthetic exhaust with KNOWN structure -- proves the pipeline with no browser.

    Returns records AND ready-made embeddings so the model step is skipped.
    Five planted themes plus uniform-noise curiosity-clicks. If these five
    separate cleanly in mirror.html, the machinery is sound.
    """
    import numpy as np
    rng = np.random.default_rng(7)
    themes = ["rust async runtimes", "terahertz biophysics", "modular synth patching",
              "osage headright law", "late-night doomscroll"]
    dim = 384
    centers = {t: rng.normal(0, 1.0, dim) for t in themes}
    recs: list[Record] = []
    vecs: list[np.ndarray] = []
    for _ in range(n):
        t = themes[rng.integers(len(themes))]
        vecs.append(centers[t] + rng.normal(0, 0.45, dim))
        recs.append(Record(text=f"{t} #{rng.integers(9999)}", source="title", detail="demo://"))
    for _ in range(n // 12):  # the curiosity-click outliers
        vecs.append(rng.normal(0, 1.4, dim))
        recs.append(Record(text="random curiosity click", source="title", detail="demo://"))
    return recs, np.asarray(vecs)


# ------------------------------- embedding --------------------------------

def embed(texts: list[str], *, offline: bool = False) -> np.ndarray:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=offline)
    return model.encode(texts, show_progress_bar=True, normalize_embeddings=True)


# ---------------------------- cluster + render ----------------------------

def cluster(vectors: np.ndarray):
    import umap
    import hdbscan
    # reduce-for-CLUSTERING: mid-dimensional, cosine. Never cluster on the 2D picture --
    # a 2D projection manufactures groups that are only projection artifacts.
    mid = umap.UMAP(n_components=15, metric="cosine", random_state=7).fit_transform(vectors)
    clusterer = hdbscan.HDBSCAN(min_cluster_size=15, min_samples=5)
    labels = clusterer.fit_predict(mid)
    outlier = clusterer.outlier_scores_          # GLOSH -- a principled outlier score, free
    # reduce-for-DISPLAY: a SEPARATE 2D projection, for the eye only.
    xy = umap.UMAP(n_components=2, metric="cosine", random_state=7).fit_transform(vectors)
    return labels, outlier, xy


def render(recs, labels, outlier, xy, out="mirror.html"):
    import numpy as np
    import plotly.graph_objects as go
    labels = np.asarray(labels)
    fig = go.Figure()
    for lab in sorted(set(labels.tolist())):
        m = labels == lab
        name = "outliers / noise" if lab == -1 else f"island {lab}"
        idx = np.where(m)[0]
        fig.add_trace(go.Scattergl(
            x=xy[m, 0], y=xy[m, 1], mode="markers", name=name,
            text=[recs[i].text for i in idx],
            hovertemplate="%{text}<extra></extra>",
            marker=dict(size=6, opacity=0.30 if lab == -1 else 0.85),
        ))
    fig.update_layout(
        title="The Saint -- your exhaust, clustered",
        template="plotly_dark", showlegend=True,
        xaxis=dict(visible=False), yaxis=dict(visible=False),
    )
    fig.write_html(out, include_plotlyjs=True)   # library embedded -> no CDN call, fully local

    islands = sorted(l for l in set(labels.tolist()) if l != -1)
    noise = int((labels == -1).sum())
    print(f"\n{len(recs)} interactions -> {len(islands)} islands, {noise} flagged as noise")
    print(f"signal-to-noise (kept / total): {1 - noise / len(recs):.0%}")
    print(f"wrote {out}")
    print("open it and ask the only question that matters:")
    print("   do these islands look like someone I recognize?")


def main():
    ap = argparse.ArgumentParser(description="The Saint -- module one")
    ap.add_argument("--browser", choices=["firefox", "chrome"], help="which history to read")
    ap.add_argument("--demo", action="store_true", help="synthetic data, proves the machinery")
    ap.add_argument("--out", default="mirror.html")
    args = ap.parse_args()

    if args.demo:
        recs, vectors = read_demo()
    elif args.browser == "firefox":
        recs = read_firefox()
    elif args.browser == "chrome":
        recs = read_chrome()
    else:
        ap.error("pass --demo, or --browser with firefox or chrome")

    if len(recs) < 30:
        sys.exit(f"only {len(recs)} usable records -- too few to cluster meaningfully")

    if not args.demo:
        vectors = embed([r.text for r in recs])

    labels, outlier, xy = cluster(vectors)
    render(recs, labels, outlier, xy, args.out)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, ImportError) as exc:
        sys.exit(f"{exc}\nFor semantic analysis, run python setup_local.py first.")
