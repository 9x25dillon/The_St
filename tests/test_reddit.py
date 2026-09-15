from pathlib import Path
import tempfile
import unittest

from mirror import parse_timestamp
from reddit import load, load_rows


POSTS_CSV = ("id,permalink,date,ip,subreddit,gildings,title,url,body\n"
             "1,/r/gardening/1,2024-01-01 00:00:00 UTC,0.0.0.0,gardening,0,Tomato tips,,"
             "Water deeply and mulch well\n")
COMMENTS_CSV = ("id,permalink,date,ip,subreddit,gildings,link,parent,body\n"
                "2,/r/gardening/2,2024-01-02 00:00:00 UTC,0.0.0.0,gardening,0,x,y,"
                "Totally agree about mulching\n")


class RedditTests(unittest.TestCase):
    def test_classifies_posts_and_comments_by_header_shape(self):
        records = load_rows([{"name": "posts.csv", "text": POSTS_CSV},
                              {"name": "comments.csv", "text": COMMENTS_CSV}])
        self.assertEqual([(r.text, r.source, r.detail) for r in records],
                         [("Tomato tips. Water deeply and mulch well", "post", "gardening"),
                          ("Totally agree about mulching", "comment", "gardening")])
        self.assertEqual(records[0].when, parse_timestamp("2024-01-01T00:00:00+00:00"))

    def test_unrecognized_csv_shape_is_skipped_not_guessed(self):
        other = "id,name\n1,unrelated\n"
        self.assertEqual(load_rows([{"name": "orders.csv", "text": other}]), [])

    def test_dedupes_identical_text_within_a_kind(self):
        records = load_rows([{"name": "posts.csv", "text": POSTS_CSV + POSTS_CSV.splitlines()[1] + "\n"}])
        self.assertEqual(len(records), 1)

    def test_invalid_inputs_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                load(str(Path(folder) / "missing"))
            with self.assertRaisesRegex(ValueError, "No CSV"):
                load(folder)
            (Path(folder) / "posts.csv").write_text(POSTS_CSV, encoding="utf-8-sig")
            records = load(folder)
            self.assertEqual(records[0].source, "post")


if __name__ == "__main__":
    unittest.main()
