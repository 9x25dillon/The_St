from pathlib import Path
import tempfile
import unittest

from mirror import parse_timestamp
from amazon import load, load_rows


ORDERS_CSV = ("Order ID,Order Date,Product Name,Currency,Unit Price\n"
              "112-1111111,2024-01-01 00:00:00 UTC,Tomato Seeds,USD,4.99\n"
              "112-2222222,2024-01-02 00:00:00 UTC,tomato seeds,USD,4.99\n"
              "112-3333333,2024-01-03 00:00:00 UTC,Garden Trowel,USD,12.50\n")


class AmazonTests(unittest.TestCase):
    def test_parses_orders_and_dedupes_case_insensitively(self):
        records = load_rows([{"name": "Retail.OrderHistory.1.csv", "text": ORDERS_CSV}])
        self.assertEqual([(r.text, r.source, r.detail) for r in records],
                         [("Tomato Seeds", "order", "amazon"),
                          ("Garden Trowel", "order", "amazon")])
        self.assertEqual(records[0].when, parse_timestamp("2024-01-01T00:00:00+00:00"))

    def test_unrecognized_csv_shape_is_skipped_not_guessed(self):
        other = "id,name\n1,unrelated\n"
        self.assertEqual(load_rows([{"name": "orders.csv", "text": other}]), [])

    def test_invalid_inputs_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                load(str(Path(folder) / "missing"))
            with self.assertRaisesRegex(ValueError, "No CSV"):
                load(folder)
            (Path(folder) / "Retail.OrderHistory.1.csv").write_text(ORDERS_CSV, encoding="utf-8-sig")
            records = load(folder)
            self.assertEqual(records[0].source, "order")


if __name__ == "__main__":
    unittest.main()
