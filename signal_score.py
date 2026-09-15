"""
The Saint -- local "profile health" scoring.

Turns the UMAP/HDBSCAN cluster output that already exists in semantic_map() into a
per-passage score: how strongly does this event look like something you actually meant
(sits solidly inside a behavioral island you recognize) versus noise (an outlier, a
curiosity click, something that blurs two islands together)? Nothing here is sent
anywhere -- it is a local read of your own already-computed embedding space, and the
score is a lens on your own data, not a signal fed back to any platform.

Scoring runs on the SAME mid-dimensional (15D) vectors used for clustering, never on the
2D display projection -- clustering/scoring on a 2D picture manufactures structure that is
only a projection artifact (see mirror.py's cluster() for the same rule).

Like mirror.py, numpy is imported lazily inside each function rather than at module load,
so importing this module (e.g. from app.py's dispatch table) never requires the optional
semantic dependencies -- only actually calling a scoring function does.

What this deliberately does NOT implement yet, and why:
  - Passive-engagement weighting (dwell time) -- no source in this app currently carries
    reliable per-event dwell time, so the noise term below is outlier + homogenization
    only, not the three-term version.
  - Declared per-event weights with a learning/feedback loop -- needs a UI for labeling
    events and a place to persist the adjusted weights across sessions. Deferred; every
    event uses a uniform weight of 1.0 for now.
  - Drift across sessions -- needs cluster centroids persisted across app restarts, which
    this app deliberately does not do. profile_health() takes drift=0.0 until that exists.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

_SECONDS_PER_DAY = 86_400.0


def _cluster_stats(mid_vectors, labels):
    """Per-label (centroid, radius) for every real cluster; noise (-1) excluded. Radius is
    the RMS distance from centroid -- the cluster's characteristic size, not the spread OF
    that spread (std of distances is much smaller and over-penalizes ordinary members)."""
    import numpy as np
    stats = {}
    for label in set(labels.tolist()):
        if label == -1:
            continue
        members = mid_vectors[labels == label]
        centroid = members.mean(axis=0)
        d = np.linalg.norm(members - centroid, axis=1)
        radius = float(np.sqrt((d ** 2).mean()))
        stats[label] = (centroid, max(radius, 1e-6))
    return stats


def anchor_strength(mid_vectors, labels) -> np.ndarray:
    """A(v): how tightly a point sits inside its nearest cluster. (0, 1]; 1 = dead-center,
    ->0 far outside every cluster's characteristic radius."""
    import numpy as np
    stats = _cluster_stats(mid_vectors, labels)
    if not stats:
        return np.zeros(len(mid_vectors))
    centroids = np.array([c for c, _ in stats.values()])
    out = np.zeros(len(mid_vectors))
    for i, v in enumerate(mid_vectors):
        dists = np.linalg.norm(centroids - v, axis=1)
        _, radius = list(stats.values())[int(np.argmin(dists))]
        out[i] = np.exp(-(dists.min() ** 2) / (2 * radius ** 2))
    return out


def divergence_penalty(mid_vectors, labels) -> np.ndarray:
    """D(v): 1 when clearly closest to one cluster, ->0 when straddling two equally. [0, 1]."""
    import numpy as np
    stats = _cluster_stats(mid_vectors, labels)
    if len(stats) < 2:
        return np.ones(len(mid_vectors))
    centroids = np.array([c for c, _ in stats.values()])
    out = np.ones(len(mid_vectors))
    for i, v in enumerate(mid_vectors):
        dists = np.sort(np.linalg.norm(centroids - v, axis=1))
        d1, d2 = dists[0], dists[1]
        out[i] = 1 - (d1 / d2) if d2 > 0 else 1.0
    return out


RECENCY_HALF_LIFE_DAYS = 730.0


def recency_decay(timestamps, now: float | None = None, half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> np.ndarray:
    """R(t): exponential recency weight. A missing timestamp gets a neutral 1.0, not a
    penalty -- sources without dates (notes, browser history) shouldn't always look stale
    next to sources that happen to carry one.

    Half-life is two years (was 14 days): the past year keeps ~70% of its weight, a 2-year-old
    entry counts half, a 4-year-old one a quarter. With 14 days, and signal needing IWS >= 0.35,
    anything older than a few weeks could never be signal, so a real 2014-2022 tweet archive
    scored 0% everywhere. Measured on that archive (2026-09): even with recency removed, only
    7% of passages reached the threshold -- the divergence term is the usual limiting factor
    there -- so a longer half-life stops recency from erasing history but doesn't by itself make
    old exports score as signal. semantic_map uses usage_peak_recency instead, which measures
    age from each source's busiest month rather than from today."""
    import numpy as np
    now = time.time() if now is None else now
    lam = np.log(2) / (half_life_days * _SECONDS_PER_DAY)
    out = np.ones(len(timestamps))
    for i, t in enumerate(timestamps):
        if t is not None:
            out[i] = np.exp(-lam * max(now - t, 0.0))
    return out


def usage_peak_recency(timestamps, sources, half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> np.ndarray:
    """R(t) measured from each source's busiest month instead of from today: entries from the
    start of that month or later count fully (1.0); earlier ones fade with the same half-life.
    A source you stopped using years ago isn't marked stale against one you use now, and an old
    export isn't penalized for the time since it was downloaded. Ties between equally busy months
    go to the later one. Undated entries stay neutral (1.0).

    Chosen by the user (2026-09). On a real 2014-2022 tweet archive plus a 2021-2022 YouTube
    history, median recency went from 0.02 (measured from today) to 1.00."""
    import numpy as np
    from collections import Counter
    months = {}
    for t, source in zip(timestamps, sources):
        if t is not None:
            moment = datetime.fromtimestamp(t, tz=timezone.utc)
            months.setdefault(source, Counter())[(moment.year, moment.month)] += 1
    anchors = {}
    for source, counts in months.items():
        year, month = max(counts, key=lambda ym: (counts[ym], ym))
        anchors[source] = datetime(year, month, 1, tzinfo=timezone.utc).timestamp()
    lam = np.log(2) / (half_life_days * _SECONDS_PER_DAY)
    out = np.ones(len(timestamps))
    for i, (t, source) in enumerate(zip(timestamps, sources)):
        if t is not None:
            out[i] = np.exp(-lam * max(anchors[source] - t, 0.0))
    return out


def homogenization_index(labels) -> float:
    """HI: the share of non-noise passages sitting in the single largest behavioral
    island. ->0 several islands share the data fairly evenly (healthy variety); ->1
    almost everything falls into one dominant island (the same vector region, repeatedly).
    Needs only cluster labels, not the vectors themselves."""
    import numpy as np
    labels = np.asarray(labels)
    real = labels[labels != -1]
    if len(real) == 0:
        return 0.0
    _, counts = np.unique(real, return_counts=True)
    return float(counts.max() / len(real))


def noise_exposure(outlier_scores, labels, *, alpha: float = 0.6, beta: float = 0.4) -> np.ndarray:
    """N(v): pollution score in [0, 1] per passage. alpha * GLOSH outlier score (already
    computed by HDBSCAN, so no extra IsolationForest dependency) + beta * how dominant this
    passage's own island is (its cluster's share of all non-noise passages -- the more a
    single island swallows the dataset, the more each of its members is "more of the
    same"). Noise passages (label -1) get 0 for the homogenization term; the outlier term
    already accounts for them, and double-penalizing would be redundant."""
    import numpy as np
    outlier = np.nan_to_num(np.asarray(outlier_scores, dtype=float), nan=1.0)
    labels = np.asarray(labels)
    real = labels[labels != -1]
    share = {}
    if len(real):
        values, counts = np.unique(real, return_counts=True)
        share = dict(zip(values.tolist(), (counts / len(real)).tolist()))
    homogenization = np.array([share.get(l, 0.0) if l != -1 else 0.0 for l in labels.tolist()])
    return np.clip(alpha * outlier + beta * homogenization, 0, 1)


# Every factor counts as at least this much before the four are multiplied, and a passage is
# signal at SIGNAL_THRESHOLD or above. Both chosen together (2026-09, the user asked to soften all
# four factors equally and lower the cutoff from 0.35). Softening every factor with the same
# power (e.g. square roots) would only rescale the product -- the same as moving the cutoff --
# so softening is a floor instead: factor' = floor + (1 - floor) * factor, which stops any one
# factor from zeroing a score. Measured on a real tweet archive plus a YouTube history (563
# passages, 188 outside any island): floor 0 / cutoff 0.35 marked 54% of island passages and
# 2% of unclustered ones as signal; 0.25 / 0.20 marks 88% and 29% (a 0.5 floor reached 79% of
# unclustered passages at 0.25 -- too loose to mean anything). floor=0 reproduces the original
# IWS = w * A * D * R * (1 - N).
FACTOR_FLOOR = 0.25
SIGNAL_THRESHOLD = 0.20


def soften(values, floor: float = FACTOR_FLOOR):
    import numpy as np
    return floor + (1 - floor) * np.asarray(values, dtype=float)


def injection_weight(anchor, divergence, recency, noise, weights=None, floor: float = FACTOR_FLOOR) -> np.ndarray:
    """IWS = w * A' * D' * R' * (1 - N)', each factor softened by `soften`. w defaults to a
    uniform 1.0 (no declared per-event intent yet -- see module docstring)."""
    import numpy as np
    anchor, divergence, recency, noise = (np.asarray(a, dtype=float)
                                           for a in (anchor, divergence, recency, noise))
    w = np.ones_like(anchor) if weights is None else np.asarray(weights, dtype=float)
    return w * soften(anchor, floor) * soften(divergence, floor) * soften(recency, floor) * soften(1 - noise, floor)


def signal_to_noise(iws, threshold: float = SIGNAL_THRESHOLD) -> float:
    """Fraction of passages at/above the signal threshold. 1.0 for an empty set (nothing
    to call noise yet)."""
    import numpy as np
    iws = np.asarray(iws, dtype=float)
    if len(iws) == 0:
        return 1.0
    return float((iws >= threshold).sum() / len(iws))


def profile_health(snr: float, homogenization: float, drift: float = 0.0, kappa: float = 0.3) -> float:
    """PHS = SNR * (1 - HI) * exp(-|drift| / kappa). drift fixed at 0.0 until cross-session
    history is persisted (see module docstring)."""
    import numpy as np
    return float(snr * (1 - homogenization) * np.exp(-abs(drift) / kappa))
