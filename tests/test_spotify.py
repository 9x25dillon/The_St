import json
from pathlib import Path
import tempfile
import unittest

from mirror import parse_timestamp
from spotify import load, load_blobs


class SpotifyTests(unittest.TestCase):
    def test_modern_extended_format_fields(self):
        blob = [{"ts": "2024-01-01T00:00:00Z", "master_metadata_track_name": "Weather",
                 "master_metadata_album_artist_name": "The National", "ms_played": 200000}]
        records, _ = load_blobs([blob])
        self.assertEqual(records[0].text, "Weather — The National")
        self.assertEqual(records[0].source, "track")
        self.assertEqual(records[0].detail, "spotify")
        self.assertEqual(records[0].when, parse_timestamp("2024-01-01T00:00:00Z"))

    def test_older_basic_format_fields(self):
        blob = [{"endTime": "2024-01-02 00:00:00", "trackName": "Skinny Love",
                 "artistName": "Bon Iver", "msPlayed": 150000}]
        records, _ = load_blobs([blob])
        self.assertEqual(records[0].text, "Skinny Love — Bon Iver")
        self.assertEqual(records[0].when, parse_timestamp("2024-01-02T00:00:00"))

    def test_podcast_episode_fallback(self):
        blob = [{"ts": "2024-01-03T00:00:00Z", "master_metadata_track_name": None,
                 "episode_name": "Episode 12", "episode_show_name": "A Great Show"}]
        records, _ = load_blobs([blob])
        self.assertEqual(records[0].text, "Episode 12 — A Great Show")
        self.assertEqual(records[0].source, "podcast")

    def test_account_data_podcasts_searches_and_inferences(self):
        podcasts = [{"endTime": "2024-01-05 08:30", "podcastName": "Garden Talk",
                     "episodeName": "Soil 101", "msPlayed": 1800000}]
        searches = [{"platform": "IPHONE", "searchTime": "2024-01-06T10:00:00.000Z[UTC]",
                     "searchQuery": "compost", "searchInteractionURIs": []},
                    {"platform": "IPHONE", "searchTime": "2024-01-06T10:01:00.000Z[UTC]",
                     "searchQuery": 1979, "searchInteractionURIs": []}]
        inferences = {"inferences": ["1P_Custom_Gardeners", "3P_Home_Improvement"]}
        records, categories = load_blobs([podcasts, searches, inferences])
        self.assertEqual([(r.text, r.source) for r in records],
                         [("Soil 101 — Garden Talk", "podcast"), ("compost", "search"), ("1979", "search")])
        self.assertEqual(records[1].when, parse_timestamp("2024-01-06T10:00:00Z"))
        self.assertEqual(categories, ["1P Custom Gardeners", "3P Home Improvement"])

    def test_short_plays_are_skips_not_taste(self):
        blob = [{"ts": "2024-01-01T00:00:00Z", "master_metadata_track_name": "Skipped Song",
                 "master_metadata_album_artist_name": "A Band", "ms_played": 4200, "skipped": True},
                {"ts": "2024-01-01T00:00:00Z", "audiobook_title": "A Long Book",
                 "audiobook_chapter_title": "Chapter 1", "ms_played": 600000}]
        records, _ = load_blobs([blob])
        self.assertEqual([(r.text, r.source) for r in records], [("Chapter 1 — A Long Book", "audiobook")])

    def test_entry_with_no_usable_fields_is_skipped(self):
        blob = [{"ts": "2024-01-01T00:00:00Z"}]
        self.assertEqual(load_blobs([blob]), ([], []))

    def test_dedup_is_case_insensitive(self):
        blob = [{"ts": "2024-01-01T00:00:00Z", "master_metadata_track_name": "Weather",
                 "master_metadata_album_artist_name": "The National"},
                {"ts": "2024-01-02T00:00:00Z", "master_metadata_track_name": "WEATHER",
                 "master_metadata_album_artist_name": "the national"}]
        self.assertEqual(len(load_blobs([blob])[0]), 1)

    def test_load_from_folder_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                load(str(Path(folder) / "missing"))
            with self.assertRaisesRegex(ValueError, "No JSON"):
                load(folder)
            (Path(folder) / "Streaming_History_Audio_2024_0.json").write_text(json.dumps(
                [{"ts": "2024-01-01T00:00:00Z", "master_metadata_track_name": "Local Test",
                  "master_metadata_album_artist_name": "Someone"}]), encoding="utf-8-sig")
            records, _ = load(folder)
            self.assertEqual(records[0].text, "Local Test — Someone")
            broken = Path(folder) / "broken.json"
            broken.write_text("{")
            with self.assertRaisesRegex(ValueError, "Cannot read JSON"):
                load(folder)


if __name__ == "__main__":
    unittest.main()
