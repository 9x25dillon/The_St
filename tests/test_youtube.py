import json
from pathlib import Path
import tempfile
import unittest

from mirror import parse_timestamp
from youtube import load, load_blobs


class YouTubeTests(unittest.TestCase):
    def test_classifies_by_own_prefix_and_dedupes(self):
        blob = [
            {"title": "Searched for  gardening tips ",
             "titleUrl": "https://www.youtube.com/results?search_query=gardening+tips",
             "time": "2024-01-01T00:00:00Z"},
            {"title": "Watched How to prune roses",
             "titleUrl": "https://www.youtube.com/watch?v=abc",
             "time": "2024-01-02T00:00:00Z"},
            {"title": "Watched a video that has been removed",
             "time": "2024-01-03T00:00:00Z"},
            {"title": "Searched for gardening tips", "time": "2024-01-04T00:00:00Z"},
        ]
        records = load_blobs([blob])
        self.assertEqual([(r.text, r.source) for r in records],
                          [("gardening tips", "search"), ("How to prune roses", "watch")])
        self.assertEqual(records[0].when, parse_timestamp("2024-01-01T00:00:00Z"))
        self.assertEqual(records[1].detail, "https://www.youtube.com/watch?v=abc")

    def test_google_ads_are_kept_apart_and_bare_links_skipped(self):
        blob = [
            {"header": "YouTube", "title": "Watched Spring Seed Sale", "titleUrl": "https://www.youtube.com/watch?v=ad1",
             "time": "2024-01-01T00:00:00.123Z", "products": ["YouTube"],
             "details": [{"name": "From Google Ads"}], "activityControls": ["Web & App Activity"]},
            {"header": "YouTube", "title": "Watched https://www.youtube.com/watch?v=gone",
             "titleUrl": "https://www.youtube.com/watch?v=gone", "time": "2024-01-01T00:00:00.123Z"},
            {"header": "YouTube", "title": "Watched Composting basics", "time": "2024-01-02T00:00:00.456Z",
             "subtitles": [{"name": "A Channel", "url": "https://www.youtube.com/channel/x"}]},
        ]
        self.assertEqual([(r.text, r.source) for r in load_blobs([blob])],
                         [("Spring Seed Sale", "ad"), ("Composting basics", "watch")])

    def test_watch_and_search_files_combine_in_either_order(self):
        watch = [{"title": "Watched A quiet garden tour", "time": "2024-01-01T00:00:00Z"}]
        search = [{"title": "Searched for quiet gardens", "time": "2024-01-02T00:00:00Z"}]
        combined = load_blobs([search, watch])
        self.assertEqual({(r.text, r.source) for r in combined},
                          {("A quiet garden tour", "watch"), ("quiet gardens", "search")})

    def test_load_from_folder_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                load(str(Path(folder) / "missing"))
            with self.assertRaisesRegex(ValueError, "No JSON"):
                load(folder)
            (Path(folder) / "watch-history.json").write_text(json.dumps(
                [{"title": "Watched Local file test", "time": "2024-01-01T00:00:00Z"}]),
                encoding="utf-8-sig")
            records = load(folder)
            self.assertEqual(records[0].text, "Local file test")
            broken = Path(folder) / "broken.json"
            broken.write_text("{")
            with self.assertRaisesRegex(ValueError, "Cannot read JSON"):
                load(folder)


if __name__ == "__main__":
    unittest.main()
