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
        records = load_blobs([blob])
        self.assertEqual(records[0].text, "Weather — The National")
        self.assertEqual(records[0].source, "track")
        self.assertEqual(records[0].detail, "spotify")
        self.assertEqual(records[0].when, parse_timestamp("2024-01-01T00:00:00Z"))

    def test_older_basic_format_fields(self):
        blob = [{"endTime": "2024-01-02 00:00:00", "trackName": "Skinny Love",
                 "artistName": "Bon Iver", "msPlayed": 150000}]
        records = load_blobs([blob])
        self.assertEqual(records[0].text, "Skinny Love — Bon Iver")
        self.assertEqual(records[0].when, parse_timestamp("2024-01-02T00:00:00"))

    def test_podcast_episode_fallback(self):
        blob = [{"ts": "2024-01-03T00:00:00Z", "master_metadata_track_name": None,
                 "episode_name": "Episode 12", "episode_show_name": "A Great Show"}]
        records = load_blobs([blob])
        self.assertEqual(records[0].text, "Episode 12 — A Great Show")
        self.assertEqual(records[0].source, "podcast")

    def test_entry_with_no_usable_fields_is_skipped(self):
        blob = [{"ts": "2024-01-01T00:00:00Z"}]
        self.assertEqual(load_blobs([blob]), [])

    def test_dedup_is_case_insensitive(self):
        blob = [{"ts": "2024-01-01T00:00:00Z", "master_metadata_track_name": "Weather",
                 "master_metadata_album_artist_name": "The National"},
                {"ts": "2024-01-02T00:00:00Z", "master_metadata_track_name": "WEATHER",
                 "master_metadata_album_artist_name": "the national"}]
        self.assertEqual(len(load_blobs([blob])), 1)

    def test_load_from_folder_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                load(str(Path(folder) / "missing"))
            with self.assertRaisesRegex(ValueError, "No JSON"):
                load(folder)
            (Path(folder) / "Streaming_History_Audio_2024_0.json").write_text(json.dumps(
                [{"ts": "2024-01-01T00:00:00Z", "master_metadata_track_name": "Local Test",
                  "master_metadata_album_artist_name": "Someone"}]), encoding="utf-8-sig")
            records = load(folder)
            self.assertEqual(records[0].text, "Local Test — Someone")
            broken = Path(folder) / "broken.json"
            broken.write_text("{")
            with self.assertRaisesRegex(ValueError, "Cannot read JSON"):
                load(folder)


if __name__ == "__main__":
    unittest.main()
