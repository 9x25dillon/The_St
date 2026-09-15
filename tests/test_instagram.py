import json
from pathlib import Path
import tempfile
import unittest

from instagram import load, load_blobs


class InstagramTests(unittest.TestCase):
    def test_topics_and_searches_with_dedup_and_timestamp(self):
        blob = {
            "topics_your_topics": [
                {"string_map_data": {"Name": {"value": "Cooking"}}},
                {"string_map_data": {"Name": {"value": "Cooking"}}},
            ],
            "searches_user_search_history": [
                {"string_map_data": {"Search": {"value": "gardening tips"},
                                      "Time": {"value": "", "timestamp": 1700000000}}},
                {"string_map_data": {"Search": {"value": "GARDENING TIPS"}}},
            ],
        }
        expressed, categories = load_blobs([blob])
        self.assertEqual([(r.text, r.source, r.when) for r in expressed],
                         [("gardening tips", "search", 1700000000.0)])
        self.assertEqual(categories, ["Cooking"])

    def test_ignores_not_stored_and_non_string_map_nodes(self):
        blob = {"other": [{"unrelated": {"value": "x"}}],
                "searches": [{"string_map_data": {"Search": {"value": "not_stored"}}}]}
        expressed, categories = load_blobs([blob])
        self.assertEqual(expressed, [])
        self.assertEqual(categories, [])

    def test_invalid_inputs_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                load(str(Path(folder) / "missing"))
            with self.assertRaisesRegex(ValueError, "No JSON"):
                load(folder)
            path = Path(folder) / "broken.json"
            path.write_text("{")
            with self.assertRaisesRegex(ValueError, "Cannot read JSON"):
                load(folder)


if __name__ == "__main__":
    unittest.main()
