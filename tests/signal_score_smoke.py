"""Signal-score unit checks; needs the optional numpy dependency, like semantic_smoke.py.
Not named test_*.py on purpose -- kept out of the dependency-free base test discovery.
Run with: .venv/bin/python -m unittest tests.signal_score_smoke -v
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import time
import unittest

import numpy as np

from datetime import datetime, timezone

from signal_score import (anchor_strength, divergence_penalty, recency_decay, usage_peak_recency,
                           homogenization_index, noise_exposure, injection_weight,
                           signal_to_noise, profile_health, SIGNAL_THRESHOLD, FACTOR_FLOOR)


def _two_clusters():
    rng = np.random.default_rng(3)
    c0 = np.array([0.0, 0.0]) + rng.normal(0, 0.5, (20, 2))
    c1 = np.array([10.0, 0.0]) + rng.normal(0, 0.5, (20, 2))
    between = np.array([[5.0, 0.0]])
    far_outlier = np.array([[50.0, 50.0]])
    mv = np.vstack([c0, c1, between, far_outlier])
    labels = np.array([0] * 20 + [1] * 20 + [-1, -1])
    return mv, labels


class AnchorDivergenceTests(unittest.TestCase):
    def test_anchor_strength_scores_cluster_membership(self):
        mv, labels = _two_clusters()
        a = anchor_strength(mv, labels)
        self.assertGreater(a[:20].mean(), 0.5)   # ordinary in-cluster points score highly
        self.assertLess(a[-1], 0.01)             # the far outlier scores near zero

    def test_anchor_strength_is_one_at_exact_centroid(self):
        mv, labels = _two_clusters()
        centroid = mv[:20].mean(axis=0)
        mv2 = np.vstack([mv[:40], centroid[None, :]])
        labels2 = np.append(labels[:40], 0)
        a = anchor_strength(mv2, labels2)
        self.assertAlmostEqual(a[-1], 1.0, places=6)

    def test_divergence_penalty_flags_straddling_points(self):
        mv, labels = _two_clusters()
        d = divergence_penalty(mv, labels)
        self.assertGreater(d[0], 0.5)   # deep inside its own cluster
        self.assertLess(d[-2], 0.1)     # sitting almost exactly between both clusters


class RecencyTests(unittest.TestCase):
    def test_missing_timestamp_is_neutral(self):
        r = recency_decay([None, None], now=time.time())
        self.assertTrue((r == 1.0).all())

    def test_default_half_life_is_two_years(self):
        now = 1_000_000_000.0
        two_years = recency_decay([now - 730 * 86_400], now=now)
        self.assertAlmostEqual(float(two_years[0]), 0.5, places=6)

    def test_one_half_life_decays_to_half(self):
        now = 1_000_000.0
        half_life_days = 14.0  # any explicit half-life is honored
        r = recency_decay([now - half_life_days * 86_400], now=now, half_life_days=half_life_days)
        self.assertAlmostEqual(r[0], 0.5, places=6)


class UsagePeakRecencyTests(unittest.TestCase):
    def test_each_source_measured_from_its_own_busiest_month(self):
        def at(year, month, day=10):
            return datetime(year, month, day, tzinfo=timezone.utc).timestamp()
        timestamps = [at(2015, 6), at(2015, 6, 20), at(2015, 1), at(2020, 3),   # x: busiest Jun 2015
                      at(2022, 5), at(2021, 5), None]                            # youtube: tie -> later month
        sources = ['x', 'x', 'x', 'x', 'youtube', 'youtube', 'notes']
        r = usage_peak_recency(timestamps, sources)
        self.assertEqual(float(r[0]), 1.0)                     # in the busiest month
        self.assertEqual(float(r[3]), 1.0)                     # after it
        june_first = datetime(2015, 6, 1, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(float(r[2]), 0.5 ** ((june_first - at(2015, 1)) / 86_400 / 730), places=6)
        self.assertEqual(float(r[4]), 1.0)                     # youtube's later equally busy month
        self.assertLess(float(r[5]), 1.0)
        self.assertEqual(float(r[6]), 1.0)                     # undated stays neutral


class HomogenizationTests(unittest.TestCase):
    def test_balanced_clusters_score_low(self):
        labels = np.array([0] * 10 + [1] * 10 + [2] * 10)
        self.assertAlmostEqual(homogenization_index(labels), 1 / 3, places=6)

    def test_one_dominant_cluster_scores_high(self):
        labels = np.array([0] * 28 + [1] * 1 + [2] * 1)
        self.assertGreater(homogenization_index(labels), 0.9)

    def test_all_noise_is_zero(self):
        self.assertEqual(homogenization_index(np.array([-1, -1, -1])), 0.0)


class NoiseExposureTests(unittest.TestCase):
    def test_noise_labeled_points_skip_homogenization_term(self):
        labels = np.array([0, 0, 0, -1])
        outliers = np.array([0.1, 0.1, 0.1, 0.9])
        n = noise_exposure(outliers, labels, alpha=0.6, beta=0.4)
        # the noise point's score comes from the outlier term alone: 0.6 * 0.9
        self.assertAlmostEqual(n[-1], 0.6 * 0.9, places=6)

    def test_dominant_cluster_members_score_higher_than_minority(self):
        labels = np.array([0] * 9 + [1])
        outliers = np.zeros(10)
        n = noise_exposure(outliers, labels)
        self.assertGreater(n[0], n[-1])


class CompositeScoreTests(unittest.TestCase):
    def test_injection_weight_regression(self):
        iws = injection_weight([0.5], [0.8], [0.9], [0.2], floor=0.0)  # floor 0: the original product
        self.assertAlmostEqual(iws[0], 0.5 * 0.8 * 0.9 * 0.8, places=6)

    def test_softened_factors_cannot_zero_a_score(self):
        self.assertEqual((FACTOR_FLOOR, SIGNAL_THRESHOLD), (0.25, 0.20))
        soft = lambda f: 0.25 + 0.75 * f
        iws = injection_weight([0.5, 0.0], [0.8, 1.0], [0.9, 1.0], [0.2, 0.0])
        self.assertAlmostEqual(iws[0], soft(0.5) * soft(0.8) * soft(0.9) * soft(0.8), places=6)
        self.assertAlmostEqual(iws[1], 0.25, places=6)          # one factor at zero still leaves a quarter

    def test_signal_to_noise_ratio(self):
        self.assertAlmostEqual(signal_to_noise([0.5, 0.2, 0.9], threshold=0.35), 2 / 3, places=6)
        self.assertEqual(signal_to_noise([], threshold=0.35), 1.0)

    def test_profile_health_range_and_drift_penalty(self):
        clean = profile_health(snr=1.0, homogenization=0.0, drift=0.0)
        self.assertAlmostEqual(clean, 1.0, places=6)
        drifted = profile_health(snr=1.0, homogenization=0.0, drift=0.3, kappa=0.3)
        self.assertLess(drifted, clean)


if __name__ == "__main__":
    unittest.main()
