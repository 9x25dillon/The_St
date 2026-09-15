import json
from pathlib import Path
import tempfile
import unittest

from mirror import parse_timestamp
from x import load, load_blobs, load_files


class XTests(unittest.TestCase):
    def test_unwraps_ytd_prefix_and_parses_saved_search(self):
        raw = ('window.YTD.saved_search.part0 = ' +
               json.dumps([{"savedSearch": {"savedSearchId": "1", "query": "gardening tips"}}]) + ';')
        expressed, categories = load_files([{"name": "saved-search.js", "text": raw}])
        self.assertEqual([(r.text, r.source) for r in expressed], [("gardening tips", "search")])

    def test_tweets_parse_classic_dates_and_separate_retweets(self):
        raw = 'window.YTD.tweets.part0 = ' + json.dumps([
            {"tweet": {"full_text": "Just planted new tomatoes", "created_at": "Mon Jan 01 00:00:00 +0000 2024"}},
            {"tweet": {"full_text": "RT @someone: their words", "created_at": "Mon Jan 01 00:00:00 +0000 2024"}},
        ])
        expressed, _ = load_files([{"name": "tweets.js", "text": raw}])
        self.assertEqual([(r.text, r.source) for r in expressed],
                         [("Just planted new tomatoes", "post"), ("RT @someone: their words", "repost")])
        self.assertEqual(expressed[0].when, parse_timestamp("2024-01-01T00:00:00Z"))

    def test_liked_posts_are_not_your_own_posts(self):
        raw = 'window.YTD.like.part0 = ' + json.dumps(
            [{"like": {"tweetId": "1", "fullText": "Someone else's tomato photo", "expandedUrl": ""}}])
        expressed, _ = load_files([{"name": "like.js", "text": raw}])
        self.assertEqual([(r.text, r.source) for r in expressed], [("Someone else's tomato photo", "like")])

    def test_personalization_interest_objects_and_shows(self):
        raw = 'window.YTD.personalization.part0 = ' + json.dumps([{"p13nData": {
            "demographics": {"languages": [{"language": "English", "isDisabled": False}]},
            "interests": {
                "interests": [{"name": "Gardening", "isDisabled": False},
                              {"name": "Cooking", "isDisabled": False},
                              {"name": "Turned off", "isDisabled": True}],
                "partnerInterests": [],
                "audienceAndAdvertisers": {"advertisers": ["@brand"], "numAudiences": "3"},
                "shows": ["A Garden Show"]},
            "locationHistory": ["Somewhere, Someplace"]}}])
        _, categories = load_files([{"name": "personalization.js", "text": raw}])
        self.assertEqual(categories, ["A Garden Show", "Cooking", "Gardening"])

    def test_dedupes_case_insensitive_within_source(self):
        blob = [{"savedSearch": {"query": "Gardens"}}, {"savedSearch": {"query": "gardens"}}]
        expressed, _ = load_blobs([blob])
        self.assertEqual(len(expressed), 1)

    def test_raw_json_without_wrapper_still_parses(self):
        raw = json.dumps([{"savedSearch": {"query": "no wrapper here"}}])
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
