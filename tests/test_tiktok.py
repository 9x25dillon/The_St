import json
from pathlib import Path
import tempfile
import unittest

from tiktok import _parse_date, load, load_blobs


class ExportTests(unittest.TestCase):
    def test_timezone_offsets_represent_same_instant(self):
        self.assertEqual(_parse_date("2025-01-01 12:00:00+02:00"),
                         _parse_date("2025-01-01T10:00:00Z"))
        self.assertEqual(_parse_date("1970-01-01 00:00:00"), 0)
        self.assertIsNone(_parse_date("invalid"))

    def test_nested_export_and_deduplication(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "user_data_tiktok.json"
            path.write_text(json.dumps({
                "Your Activity": {
                    "Searches": {"SearchList": [{"Date": "1970-01-01 00:00:00", "SearchTerm": " Gardens "},
                                                {"Date": "1970-01-01 00:00:00", "SearchTerm": "gardens"}]},
                    "Hashtag": {"HashtagList": [{"HashtagName": "#plants", "HashtagLink": ""}]},
                    "Watch History": {"VideoList": [
                        {"Date": "1970-01-01 00:00:00", "Link": "https://www.tiktokv.com/share/video/1/"}]},
                },
                "Comment": {"Comments": {"CommentsList": [
                    {"date": "1970-01-01 00:00:00", "comment": "Lovely flowers", "photo": "N/A", "url": ""}]}},
                "Ads and data": {"Ad Interests": {"AdInterestCategories": "Gardening | Gardening | Cooking"}},
            }), encoding="utf-8-sig")
            result = load(folder)
        self.assertEqual([r.text for r in result.expressed],
                         ["Gardens", "plants", "Lovely flowers"])
        self.assertEqual(result.watch_times, [0])
        self.assertEqual(result.ad_categories, ["Cooking", "Gardening"])

    def test_only_watch_history_counts_as_watches(self):
        # Real exports: likes, favorites and shares use the same {Date, Link} shape, favorite
        # effects/hashtags/sounds link to m.tiktok.com, and your own posts sit under Post.
        entry = {"Date": "2024-01-01 00:00:00", "Link": "https://www.tiktokv.com/share/video/9/"}
        result = load_blobs([{
            "Activity": {
                "Video Browsing History": {"VideoList": [entry, entry]},
                "Like List": {"ItemFavoriteList": [{"date": "2024-01-01 00:00:00", "link": entry["Link"]}]},
                "Favorite Videos": {"FavoriteVideoList": [entry]},
                "Favorite Sounds": {"FavoriteSoundList": [
                    {"Date": "2024-01-01 00:00:00", "Link": "https://m.tiktok.com/h5/share/music/1.html"}]},
                "Share History": {"ShareHistoryList": [dict(entry, SharedContent="video", Method="copy")]},
            },
            "Post": {"Posts": {"VideoList": [dict(entry, Title="My tomato harvest")]}},
            "App Settings": {"Settings": {"SettingsMap": {"Interests": "Cooking"}}},
        }])
        self.assertEqual(len(result.watch_times), 2)
        self.assertEqual([(r.text, r.source) for r in result.expressed], [("My tomato harvest", "caption")])
        self.assertEqual(result.ad_categories, [])  # self-chosen settings interests are not assigned labels

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
