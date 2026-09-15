"""Shared parsing cases (tests/parity_cases.json) that the desktop app and the hand-ported
Android adapter must both satisfy; tests/android_smoke.mjs runs the same file on a device.
Files are synthetic but shaped like verified real exports."""
import json
from pathlib import Path
import unittest

from app import detect_source, import_data, merge_snapshot, summarize

CASES = json.loads((Path(__file__).parent / 'parity_cases.json').read_text(encoding='utf-8'))


class ParityTests(unittest.TestCase):
    def test_cases(self):
        for case in CASES:
            with self.subTest(case['name']):
                try:
                    source = detect_source(case['files'])
                    snapshot = merge_snapshot(summarize([]), import_data({'source': source, 'files': case['files']}), source)[0]
                except ValueError as exc:
                    self.assertIn(case.get('expect_error', 'an unexpected error'), str(exc))
                    continue
                self.assertNotIn('expect_error', case)
                expect = case['expect']
                self.assertEqual(source, expect['detected_source'])
                self.assertEqual([[r['text'], r['source']] for r in snapshot['records']], expect['records'])
                if 'when' in expect:  # whole seconds: Java keeps epoch seconds, Python keeps fractions
                    self.assertEqual([None if r['when'] is None else int(r['when']) for r in snapshot['records']], expect['when'])
                self.assertEqual(snapshot['categories'], expect.get('categories', []))
                self.assertEqual(snapshot['watches'], expect.get('watches', 0))
                if 'terms' in expect:
                    self.assertEqual([list(term) for term in snapshot['terms']], expect['terms'])


if __name__ == '__main__':
    unittest.main()
