import json
from pathlib import Path
import tempfile
import unittest

from tiktok import _parse_date, load


class ExportTests(unittest.TestCase):
    def test_timezone_offsets_represent_same_instant(self):
        self.assertEqual(_parse_date("2025-01-01 12:00:00+02:00"),
                         _parse_date("2025-01-01T10:00:00Z"))
        self.assertEqual(_parse_date("1970-01-01 00:00:00"), 0)
        self.assertIsNone(_parse_date("invalid"))

    def test_nested_export_and_deduplication(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "export.json"
            path.write_text(json.dumps({"Activity": [
                {"SearchTerm": " Gardens "}, {"SearchTerm": "gardens"},
                {"Hashtag": "#plants"}, {"Comment": "Lovely flowers"},
                {"Date": "1970-01-01 00:00:00", "Link": "https://www.tiktok.com/video/1"},
                {"Interests": ["Gardening", "Gardening"]},
            ]}), encoding="utf-8-sig")
            result = load(folder)
        self.assertEqual([r.text for r in result.expressed],
                         ["Gardens", "plants", "Lovely flowers"])
        self.assertEqual(result.watch_times, [0])
        self.assertEqual(result.ad_categories, ["Gardening"])

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
