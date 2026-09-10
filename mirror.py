#!/usr/bin/env python3
"""
The Algorithmic Mirror -- module one: the self-mirror.

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
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import numpy as np


@dataclass
class Record:
    text: str        # what gets embedded -- a page title or a search query
    source: str      # "title" or "query"
    detail: str      # url, kept for hover context


# ------------------------------- ingestion --------------------------------

FIREFOX_GLOBS = [
    "~/.mozilla/firefox/*/places.sqlite",
    "~/Library/Application Support/Firefox/Profiles/*/places.sqlite",
    "~/AppData/Roaming/Mozilla/Firefox/Profiles/*/places.sqlite",
]

CHROME_GLOBS = [
    "~/.config/google-chrome/Default/History",
    "~/.config/chromium/Default/History",
    "~/Library/Application Support/Google/Chrome/Default/History",
    "~/AppData/Local/Google/Chrome/User Data/Default/History",
]


def _first_existing(globs: list[str]) -> str | None:
    for g in globs:
        hits = glob.glob(os.path.expanduser(g))
        if hits:
            return hits[0]
    return None


def _copy_unlocked(db_path: str) -> str:
    # history DBs are locked while the browser runs; work on a copy.
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    shutil.copy2(db_path, tmp.name)
    return tmp.name


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
        sys.exit("No Firefox places.sqlite found. Try --browser chrome, or edit FIREFOX_GLOBS.")
    copy = _copy_unlocked(db)
    con = sqlite3.connect(copy)
    rows = con.execute(
        "SELECT url, title FROM moz_places WHERE title IS NOT NULL AND title != ''"
    ).fetchall()
    con.close()
    os.unlink(copy)
    return _rows_to_records(rows)


def read_chrome() -> list[Record]:
    db = _first_existing(CHROME_GLOBS)
    if not db:
        sys.exit("No Chrome History DB found. Try --browser firefox, or edit CHROME_GLOBS.")
    copy = _copy_unlocked(db)
    con = sqlite3.connect(copy)
    rows = con.execute(
        "SELECT url, title FROM urls WHERE title IS NOT NULL AND title != ''"
    ).fetchall()
    con.close()
    os.unlink(copy)
    return _rows_to_records(rows)


def read_demo(n: int = 600):
    """Synthetic exhaust with KNOWN structure -- proves the pipeline with no browser.

    Returns records AND ready-made embeddings so the model step is skipped.
    Five planted themes plus uniform-noise curiosity-clicks. If these five
    separate cleanly in mirror.html, the machinery is sound.
    """
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

def embed(texts: list[str]) -> np.ndarray:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")
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
        title="The Algorithmic Mirror -- your exhaust, clustered",
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
    ap = argparse.ArgumentParser(description="The Algorithmic Mirror -- module one")
    ap.add_argument("--browser", choices=["firefox", "chrome"], help="which history to read")
    ap.add_argument("--demo", action="store_true", help="synthetic data, proves the machinery")
    ap.add_argument("--out", default="mirror.html")
    args = ap.parse_args()

    if args.demo:
        recs, vectors = read_demo()
    elif args.browser == "firefox":
        recs = read_firefox()
        vectors = embed([r.text for r in recs])
    elif args.browser == "chrome":
        recs = read_chrome()
        vectors = embed([r.text for r in recs])
    else:
        ap.error("pass --demo, or --browser with firefox or chrome")

    if len(recs) < 30:
        sys.exit(f"only {len(recs)} usable records -- too few to cluster meaningfully")

    labels, outlier, xy = cluster(vectors)
    render(recs, labels, outlier, xy, args.out)


if __name__ == "__main__":
    main()
