from pathlib import Path
import json
import tempfile
import unittest

from mirror import parse_timestamp
from usage import load, load_blobs


class UsageTests(unittest.TestCase):
    def test_rows_become_synthesized_passages(self):
        rows = [{"app": "Instagram", "minutes": 47, "date": "2024-01-01"},
                {"app": "Instagram", "minutes": 47, "date": "2024-01-01"},  # exact dup, dropped
                {"app": "Spotify", "minutes": 12.5, "date": "2024-01-02"}]
        records = load_blobs([rows])
        self.assertEqual([(r.text, r.source, r.detail) for r in records],
                         [("Instagram: 47 minutes", "usage", "Instagram"),
                          ("Spotify: 12.5 minutes", "usage", "Spotify")])
        self.assertEqual(records[0].when, parse_timestamp("2024-01-01"))

    def test_invalid_rows_are_skipped_not_guessed(self):
        rows = [{"app": "", "minutes": 5, "date": "2024-01-01"},
                {"app": "X", "minutes": -1, "date": "2024-01-01"},
                {"app": "X", "date": "2024-01-01"},
                {"minutes": 5, "date": "2024-01-01"}]
        self.assertEqual(load_blobs([rows]), [])

    def test_load_from_folder_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                load(str(Path(folder) / "missing"))
            with self.assertRaisesRegex(ValueError, "No JSON"):
                load(folder)
            (Path(folder) / "usage.json").write_text(
                json.dumps([{"app": "Reading", "minutes": 30, "date": "2024-01-01"}]),
                encoding="utf-8-sig")
            records = load(folder)
            self.assertEqual(records[0].text, "Reading: 30 minutes")


if __name__ == "__main__":
    unittest.main()
