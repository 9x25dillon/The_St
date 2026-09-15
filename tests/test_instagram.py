import json
from pathlib import Path
import tempfile
import unittest

from instagram import fix_meta_text, load, load_blobs


class InstagramTests(unittest.TestCase):
    def test_topics_and_searches_with_dedup_and_timestamp(self):
        topics = {"topics_your_topics": [
            {"title": "", "media_map_data": {}, "string_map_data": {"Name": {"href": "", "value": "Cooking", "timestamp": 0}}},
            {"title": "", "media_map_data": {}, "string_map_data": {"Name": {"href": "", "value": "Cooking", "timestamp": 0}}},
        ]}
        searches = {"searches_keyword": [
            {"title": "", "media_map_data": {}, "string_map_data": {
                "Search": {"href": "", "value": "gardening tips", "timestamp": 1700000000},
                "Time": {"href": "", "value": "", "timestamp": 1700000000}}},
            {"title": "", "media_map_data": {}, "string_map_data": {
                "Search": {"href": "", "value": "GARDENING TIPS", "timestamp": 1700000100}}},
        ]}
        expressed, categories = load_blobs([topics, searches])
        self.assertEqual([(r.text, r.source, r.when) for r in expressed],
                         [("gardening tips", "search", 1700000000.0)])
        self.assertEqual(categories, ["Cooking"])

    def test_localized_field_names_and_profile_searches(self):
        topics = {"topics_your_topics": [{"string_map_data": {"Nome": {"href": "", "value": "Culinária", "timestamp": 0}}}]}
        people = {"searches_user": [{"string_map_data": {"Search": {"value": "a.friend", "timestamp": 1700000000}}}]}
        keyword = {"searches_keyword": [{"string_map_data": {"Pesquisa": {"value": "hortas", "timestamp": 1700000000}}}]}
        expressed, categories = load_blobs([topics, people, keyword])
        self.assertEqual(categories, ["Culinária"])
        self.assertEqual([r.text for r in expressed], ["hortas"])

    def test_meta_latin1_mojibake_is_repaired(self):
        garbled = "caf\u00c3\u00a9 \u00e2\u0080\u0099s"  # how Meta's JSON stores "café ’s"
        blob = {"searches_keyword": [{"string_map_data": {"Search": {"value": garbled}}}]}
        expressed, _ = load_blobs([blob])
        self.assertEqual(expressed[0].text, "café \u2019s")
        self.assertEqual(fix_meta_text("naïve"), "naïve")  # genuine Latin-1 text is left alone

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
