import json
from pathlib import Path
import tempfile
import unittest

from mirror import parse_timestamp
from x import load, load_blobs, load_files


class XTests(unittest.TestCase):
    def test_unwraps_ytd_prefix_and_parses_search(self):
        raw = ('window.YTD.search_history.part0 = ' +
               json.dumps([{"searchHistory": {"query": "gardening tips",
                                                "searchTime": "2024-01-01T00:00:00.000Z"}}]) + ';')
        expressed, categories = load_files([{"name": "search-history.js", "text": raw}])
        self.assertEqual(len(expressed), 1)
        self.assertEqual(expressed[0].text, "gardening tips")
        self.assertEqual(expressed[0].source, "search")
        self.assertEqual(expressed[0].when, parse_timestamp("2024-01-01T00:00:00.000Z"))

    def test_tweet_text_captured_with_non_iso_date_left_none(self):
        blob = [{"tweet": {"full_text": "Just planted new tomatoes",
                            "created_at": "Mon Jan 01 00:00:00 +0000 2024"}}]
        expressed, _ = load_blobs([blob])
        self.assertEqual(expressed[0].text, "Just planted new tomatoes")
        self.assertEqual(expressed[0].source, "post")
        self.assertIsNone(expressed[0].when)

    def test_interests_collected_and_sorted(self):
        blob = {"p13nData": {"interests": {"interestedIn": ["Gardening", "Cooking"]}}}
        _, categories = load_blobs([blob])
        self.assertEqual(categories, ["Cooking", "Gardening"])

    def test_dedupes_case_insensitive_within_source(self):
        blob = [{"searchHistory": {"query": "Gardens"}}, {"searchHistory": {"query": "gardens"}}]
        expressed, _ = load_blobs([blob])
        self.assertEqual(len(expressed), 1)

    def test_raw_json_without_wrapper_still_parses(self):
        raw = json.dumps([{"searchHistory": {"query": "no wrapper here"}}])
        expressed, _ = load_files([{"name": "raw.js", "text": raw}])
        self.assertEqual(expressed[0].text, "no wrapper here")

    def test_invalid_inputs_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                load(str(Path(folder) / "missing"))
            with self.assertRaisesRegex(ValueError, "No \\.js files"):
                load(folder)
            broken = Path(folder) / "broken.js"
            broken.write_text("window.YTD.broken.part0 = {not valid json")
            with self.assertRaisesRegex(ValueError, "Cannot read JSON"):
                load(folder)


if __name__ == "__main__":
    unittest.main()
