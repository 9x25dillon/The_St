from pathlib import Path
import tempfile
import unittest

from notes import load, parse_note


class NotesTests(unittest.TestCase):
    def test_prose_metadata_code_and_provenance(self):
        records = parse_note('journal.md', '---\nsecret: metadata\n---\n# Morning\n\nI watered the garden.\n\n```python\nprint("skip")\n```\n\nA quiet moment.')
        self.assertEqual([r.text for r in records], ['I watered the garden.', 'A quiet moment.'])
        self.assertEqual(records[0].detail, 'journal.md · Morning')

    def test_chunks_do_not_drop_words(self):
        text = ' '.join(f'word{i}' for i in range(301))
        result = parse_note('long.txt', text)
        self.assertEqual(len(result), 3)
        self.assertEqual(' '.join(r.text for r in result), text)

    def test_directory_skips_hidden_and_external_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'notes'
            root.mkdir()
            (root / 'entry.md').write_text('Personal reflection.')
            (root / '.hidden.md').write_text('Hidden.')
            outside = Path(folder) / 'outside.md'
            outside.write_text('Outside.')
            (root / 'linked.md').symlink_to(outside)
            self.assertEqual([r.text for r in load(str(root))], ['Personal reflection.'])

    def test_reject_empty_binary_and_unsupported(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                load(folder)
        for name, text in [('file.pdf', 'text'), ('file.txt', '\x00')]:
            with self.assertRaises(ValueError):
                parse_note(name, text)
