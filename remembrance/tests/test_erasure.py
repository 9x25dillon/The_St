from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from remembrance import app as app_module, consent, database, erasure
from remembrance.scripts.backup import backup
from remembrance.scripts.restore import restore
from remembrance.tests.test_app import MemorialTestCase

PERSONAL_TEXT = ('June Example', 'A remembered life.', 'A tribute to erase', 'Recorded Owner', 'family@example.com', 'private-erasure-case')


class ErasureTests(MemorialTestCase):
    def setUp(self):
        super().setUp()
        # The ledger must live outside the data directory that backups and restores replace.
        self.outside = tempfile.TemporaryDirectory()
        self.ledger = Path(self.outside.name) / 'erasure-ledger.jsonl'
        self.owner_id = self.query('SELECT owner_id FROM memorials WHERE id=?', (self.mid,))[0]['owner_id']

    def tearDown(self):
        super().tearDown()
        self.outside.cleanup()

    def connection(self, folder=None):
        conn = sqlite3.connect(Path(folder or self.tmp.name) / 'remembrance.sqlite3', timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def cli(self, *args, success=True, app=None):
        result = (app or self.app).test_cli_runner().invoke(args=['erasure', *args])
        if success:
            self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        else:
            self.assertNotEqual(result.exit_code, 0)
        return result

    def erase(self, mid=None, owner=None, ledger=None, success=True):
        return self.cli('memorial', mid or self.mid, '--owner', owner or self.owner_id, '--ledger', str(ledger or self.ledger),
                        '--reviewer', 'reviewer-1', '--reference', 'private-erasure-case', success=success)

    def reapply(self, *args, ledger=None, success=True, app=None):
        return self.cli('reapply', '--ledger', str(ledger or self.ledger), *args, success=success, app=app)

    def request_erasure(self, client=None, **overrides):
        data = dict(signed_name='Recorded Owner', consent='yes')
        data.update(overrides)
        return self.post(client or self.owner, self.base + '/erasure', **data)

    def new_memorial(self, name):
        response = self.post(self.owner, '/memorials/new', name=name, visibility='public', authority='self', authority_name='Owner', consent='yes')
        self.assertEqual(response.status_code, 302)
        return response.location.split('/')[2]

    def ledger_entries(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines()] if self.ledger.exists() else []

    def media_files(self, folder=None):
        return sorted(path.name for path in (Path(folder or self.tmp.name) / 'media').iterdir())

    def test_owner_request_archives_hides_and_can_be_withdrawn(self):
        self.assertEqual(self.owner.get(self.base + '/erasure').status_code, 200)
        self.assertEqual(self.visitor.get(self.base + '/erasure').status_code, 302)
        self.register(self.family, 'other@example.com')
        self.assertEqual(self.family.get(self.base + '/erasure').status_code, 403)
        self.assertEqual(self.request_erasure(self.family).status_code, 403)
        self.assertEqual(self.owner.post(self.base + '/erasure', data={'signed_name': 'Owner', 'consent': 'yes'}).status_code, 403)
        for overrides in ({'consent': ''}, {'signed_name': ''}, {'signed_name': 'x' * 121}):
            self.assertEqual(self.request_erasure(**overrides).status_code, 400)
        self.assertEqual(len(self.query('SELECT * FROM erasure_requests')), 0)
        self.assertEqual(self.request_erasure().status_code, 302)
        request = self.query('SELECT * FROM erasure_requests')[0]
        self.assertEqual((request['status'], request['signed_name']), ('pending', 'Recorded Owner'))
        self.assertEqual(self.visitor.get(self.base).status_code, 404)
        for path in (self.base, self.base + '/manage', self.base + '/export'):
            self.assertEqual(self.owner.get(path).status_code, 200, path)
        self.assertIn('Erasure requested', self.owner.get('/dashboard').text)
        self.assertIn('Waiting for operator review.', self.owner.get(self.base + '/erasure').text)
        self.assertEqual(self.request_erasure().status_code, 400)
        self.assertEqual(self.post(self.owner, self.base + '/settings', action='restore').status_code, 400)
        event = self.query("SELECT * FROM consent_events WHERE event_type='erasure_requested'")[0]
        self.assertEqual(json.loads(event['payload'])['request_id'], request['id'])
        self.assertNotIn('Recorded Owner', event['payload'])
        with zipfile.ZipFile(io.BytesIO(self.owner.get(self.base + '/export').data)) as archive:
            exported = json.loads(archive.read('memorial.json'))
        self.assertEqual(exported['erasure_requests'][0]['status'], 'pending')
        self.assertEqual(exported['consent_history'][-1]['event_type'], 'erasure_requested')
        withdraw = self.base + '/erasure/' + request['id'] + '/withdraw'
        self.assertEqual(self.post(self.family, withdraw).status_code, 403)
        self.assertEqual(self.post(self.owner, self.base + '/erasure/missing/withdraw').status_code, 400)
        for _ in range(2):
            self.assertEqual(self.post(self.owner, withdraw).status_code, 302)
        self.assertEqual(len(self.query("SELECT * FROM consent_events WHERE event_type='erasure_withdrawn'")), 1)
        self.assertEqual(self.query('SELECT status FROM erasure_requests')[0]['status'], 'withdrawn')
        self.assertEqual(self.visitor.get(self.base).status_code, 404)
        self.assertEqual(self.post(self.owner, self.base + '/settings', action='restore').status_code, 302)
        self.assertEqual(self.visitor.get(self.base).status_code, 200)
        with closing(self.connection()) as conn:
            consent.verify_history(conn)

    def test_operator_erasure_removes_every_record_and_file(self):
        public_file, private_file = self.upload('public'), self.upload('private')
        self.post(self.owner, self.base + '/events', year='1980', title='A chapter', story='Private chapter', visibility='private')
        self.assertEqual(self.post(self.visitor, self.base + '/tributes', name='Guest', body='A tribute to erase', consent='yes').status_code, 200)
        self.post(self.owner, self.base + '/tributes/' + self.query('SELECT id FROM tributes')[0]['id'], status='approved')
        self.post(self.visitor, self.base + '/candle')
        self.post(self.visitor, self.base + '/report', email='visitor@example.com', category='privacy', detail='Please remove this.')
        self.register(self.family, 'family@example.com')
        family_id = self.query('SELECT id FROM users WHERE email=?', ('family@example.com',))[0]['id']
        self.post(self.owner, self.base + '/members', email='family@example.com', account_code=family_id, action='add')
        self.post(self.owner, self.base + '/consent', signed_name='Recorded Owner', audience='public', activation='now', consent='yes')
        grant = self.app.test_cli_runner().invoke(args=['consent', 'verify', self.query('SELECT id FROM consent_grants')[0]['id'],
                                                        '--reviewer', 'reviewer-1', '--reference', 'case'])
        self.assertEqual(grant.exit_code, 0, grant.output)
        self.assertEqual(self.visitor.get(self.base).status_code, 200)
        self.post(self.owner, self.base + '/succession', email='family@example.com', account_code=family_id, consent='yes')
        self.request_erasure()
        kept_mid = self.new_memorial('Kept Memorial')
        kept = self.post(self.owner, '/m/' + kept_mid + '/upload', file=(self.image(), 'kept.png'), caption='Kept', rights='yes', visibility='public')
        self.assertEqual(kept.status_code, 302)
        kept_file = self.query('SELECT * FROM media WHERE memorial_id=?', (kept_mid,))[0]

        preview = json.loads(self.cli('preview', self.mid).output)
        self.assertEqual((preview['owner_id'], preview['media_files'], preview['archived']), (self.owner_id, 2, True))
        self.assertIsNotNone(preview['pending_request'])
        self.assertTrue(all(preview['rows'].values()), preview['rows'])
        for text in PERSONAL_TEXT:
            self.assertNotIn(text, json.dumps(preview))

        self.assertIn('Memorial erased', self.erase().output)
        for table in erasure.MEMORIAL_TABLES:
            self.assertEqual(self.query(f'SELECT COUNT(*) FROM {table} WHERE memorial_id=?', (self.mid,))[0][0], 0, table)
        self.assertEqual(self.query('SELECT COUNT(*) FROM memorials WHERE id=?', (self.mid,))[0][0], 0)
        self.assertEqual(self.media_files(), [kept_file['filename']])
        self.assertEqual(self.visitor.get('/m/' + kept_mid).status_code, 200)
        self.assertEqual(self.visitor.get('/media/' + kept_file['id']).status_code, 200)
        for client in (self.owner, self.visitor, self.family):
            for path in (self.base, '/media/' + public_file, '/media/' + private_file, self.base + '/qr.svg', self.base + '/plaque', self.base + '/report'):
                self.assertEqual(client.get(path).status_code, 404, path)
        for suffix in ('/manage', '/export', '/consent', '/succession', '/erasure'):
            self.assertEqual(self.owner.get(self.base + suffix).status_code, 404, suffix)
        self.assertNotIn('June Example', self.owner.get('/dashboard').text)
        self.assertNotIn('Invitations to care', self.family.get('/dashboard').text)

        history = self.query('SELECT * FROM consent_events WHERE memorial_id=? ORDER BY sequence', (self.mid,))
        self.assertEqual(history[-1]['event_type'], 'memorial_erased')
        removed = json.loads(history[-1]['payload'])['removed']
        self.assertEqual((removed['memorials'], removed['media_files'], removed['tributes']), (1, 2, 1))
        for row in history:
            for text in PERSONAL_TEXT:
                self.assertNotIn(text, row['payload'])
        with closing(self.connection()) as conn:
            consent.verify_history(conn)
        entries = self.ledger_entries()
        self.assertEqual([(entry['sequence'], entry['memorial_id']) for entry in entries], [(1, self.mid)])
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('private-erasure-case', self.ledger.read_text())
        listed = [json.loads(line) for line in self.cli('list').output.splitlines()]
        self.assertEqual([(row['memorial_id'], row['ledger_hash']) for row in listed], [(self.mid, entries[0]['entry_hash'])])
        self.assertEqual(self.cli('pending').output, '')

    def test_erasure_is_bound_to_the_reviewed_case_and_idempotent(self):
        fid = self.upload('public')
        self.register(self.family, 'other@example.com')
        other_id = self.query('SELECT id FROM users WHERE email=?', ('other@example.com',))[0]['id']
        self.erase(owner=other_id, success=False)
        self.erase(mid='missing', success=False)
        for inside in (Path(self.tmp.name) / 'ledger.jsonl', Path(self.tmp.name) / 'media' / 'ledger.jsonl'):
            self.assertIn('outside the live data directory', self.erase(ledger=inside, success=False).output)
        self.cli('memorial', self.mid, '--owner', self.owner_id, '--ledger', str(self.ledger),
                 '--reviewer', 'not an id', '--reference', 'case', success=False)
        self.assertEqual(self.ledger_entries(), [])
        self.assertEqual(self.visitor.get('/media/' + fid).status_code, 200)
        self.erase()
        self.assertIn('already erased', self.erase().output)
        self.assertEqual(len(self.ledger_entries()), 1)
        self.assertEqual(len(self.query("SELECT * FROM consent_events WHERE event_type='memorial_erased'")), 1)
        self.assertEqual(json.loads(self.cli('preview', self.mid).output)['erased']['ledger_sequence'], 1)
        # The fictional example can be erased as well, and is not silently recreated afterwards.
        runner = self.app.test_cli_runner()
        self.assertEqual(runner.invoke(args=['seed-demo']).exit_code, 0)
        self.erase(mid='eleanor-example', owner='example-owner')
        self.assertIn('will not be recreated', runner.invoke(args=['seed-demo']).output)
        self.assertEqual(self.visitor.get('/m/eleanor-example').status_code, 404)
        self.assertEqual([entry['sequence'] for entry in self.ledger_entries()], [1, 2])

    def test_init_ledger_never_overwrites_or_replaces_a_lost_ledger(self):
        self.cli('init-ledger', '--ledger', str(self.ledger))
        self.assertEqual((self.ledger.read_bytes(), self.ledger.stat().st_mode & 0o777), (b'', 0o600))
        self.assertIn('already exists', self.cli('init-ledger', '--ledger', str(self.ledger), success=False).output)
        self.assertIn('"ledger_entries": 0', self.reapply('--check').output)
        self.cli('init-ledger', '--ledger', str(Path(self.tmp.name) / 'inside.jsonl'), success=False)
        self.erase()
        self.assertIn('records erasures', self.cli('init-ledger', '--ledger', str(self.ledger), success=False).output)
        self.assertEqual(len(self.ledger_entries()), 1)
        replacement = Path(self.outside.name) / 'replacement.jsonl'
        self.assertIn('records erasures', self.cli('init-ledger', '--ledger', str(replacement), success=False).output)
        self.assertFalse(replacement.exists())

    def test_reapply_after_restore_erases_what_the_backup_brought_back(self):
        fid = self.upload('public')
        filename = self.query('SELECT filename FROM media')[0]['filename']
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            archive, target = Path(folder) / 'backup.zip', Path(folder) / 'restored'
            backup(self.tmp.name, archive)
            self.erase()
            restore(archive, target)
            restored = app_module.create_app({'TESTING': True, 'DATA_DIR': target, 'SECRET_KEY': 'restored-test-secret-not-for-real-use', 'PUBLIC_URL': 'http://localhost'})
            with restored.test_client() as client:
                self.assertEqual(client.get(self.base).status_code, 200)
            self.assertIn(self.mid, self.reapply('--check', success=False, app=restored).output)
            self.assertTrue((target / 'media' / filename).is_file())
            self.reapply(ledger=Path(folder) / 'missing.jsonl', success=False, app=restored)
            summary = json.loads(self.reapply(app=restored).output)
            self.assertEqual((summary['ledger_entries'], summary['erased'], summary['outstanding']), (1, 1, []))
            self.assertFalse((target / 'media' / filename).exists())
            with restored.test_client() as client:
                self.assertEqual(client.get(self.base).status_code, 404)
                self.assertEqual(client.get('/media/' + fid).status_code, 404)
            with closing(self.connection(target)) as conn:
                consent.verify_history(conn)
                self.assertEqual(erasure.registry(conn)[0]['ledger_hash'], self.ledger_entries()[0]['entry_hash'])
                event = conn.execute("SELECT payload FROM consent_events WHERE event_type='memorial_erased'").fetchone()
                self.assertTrue(json.loads(event['payload'])['reapplied'])
            self.assertEqual(json.loads(self.reapply(app=restored).output)['erased'], 0)
            self.reapply('--check', app=restored)
        self.reapply('--check')

    def test_tampered_truncated_or_incomplete_ledgers_are_refused(self):
        second, third = self.new_memorial('Second'), self.new_memorial('Third')
        self.erase()
        self.erase(mid=second)
        original = self.ledger.read_bytes()
        lines = original.splitlines(keepends=True)
        altered = json.loads(lines[0])
        altered['evidence_digest'] = '0' * 64
        self.ledger.write_bytes(json.dumps(altered).encode() + b'\n' + lines[1])
        self.assertIn('failed verification at entry 1', self.reapply(success=False).output)
        self.erase(mid=third, success=False)
        self.ledger.write_bytes(lines[0])
        self.assertIn('lacks or contradicts', self.reapply(success=False).output)
        self.erase(mid=third, success=False)
        self.assertIn('lacks or contradicts', self.erase(mid=third, ledger=Path(self.outside.name) / 'fresh.jsonl', success=False).output)
        self.ledger.write_bytes(original + lines[1][:40])
        self.assertIn('incomplete entry', self.reapply(success=False).output)
        self.ledger.write_bytes(original)
        self.erase(mid=third)
        self.assertEqual([entry['sequence'] for entry in self.ledger_entries()], [1, 2, 3])
        self.assertEqual(len(self.query('SELECT * FROM memorials')), 0)
        self.reapply('--check')

    def test_failed_erasure_rolls_back_and_the_recorded_decision_completes_later(self):
        self.upload('public')
        files = self.media_files()
        with closing(self.connection()) as conn:
            conn.execute("CREATE TRIGGER fail_history BEFORE INSERT ON consent_events BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        self.erase(success=False)
        self.assertEqual(self.visitor.get(self.base).status_code, 200)
        self.assertEqual(self.media_files(), files)
        self.assertEqual(len(self.ledger_entries()), 1)
        self.assertIn(self.mid, self.reapply('--check', success=False).output)
        with closing(self.connection()) as conn:
            conn.execute('DROP TRIGGER fail_history')
        self.erase()
        self.assertEqual(len(self.ledger_entries()), 1)
        payload = json.loads(self.query("SELECT payload FROM consent_events WHERE event_type='memorial_erased'")[0]['payload'])
        self.assertTrue(payload['reapplied'])
        self.assertEqual(self.media_files(), [])
        self.reapply('--check')

    def test_pending_request_blocks_transfer_until_the_operator_declines_it(self):
        self.register(self.family, 'successor@example.com')
        nominee = self.query('SELECT id FROM users WHERE email=?', ('successor@example.com',))[0]['id']
        self.post(self.owner, self.base + '/succession', email='successor@example.com', account_code=nominee, consent='yes')
        request_id = self.query('SELECT id FROM succession_requests')[0]['id']
        self.post(self.family, '/succession/' + request_id, action='accept', signed_name='Successor', authority='executor', consent='yes')
        self.request_erasure()
        erasure_id = self.query('SELECT id FROM erasure_requests')[0]['id']
        runner = self.app.test_cli_runner()
        transfer = ['succession', 'transfer', request_id, '--from-owner', self.owner_id, '--to-account', nominee,
                    '--reviewer', 'reviewer-1', '--reference', 'transfer-case']
        blocked = runner.invoke(args=transfer)
        self.assertNotEqual(blocked.exit_code, 0)
        self.assertIn('pending erasure request', blocked.output)
        pending = json.loads(self.cli('pending').output)
        self.assertEqual((pending['id'], pending['memorial_id'], pending['archived']), (erasure_id, self.mid, 1))
        self.assertEqual(json.loads(self.cli('show', erasure_id).output)['signed_name'], 'Recorded Owner')
        self.cli('decline', 'missing', '--reviewer', 'reviewer-1', '--reference', 'case', success=False)
        for _ in range(2):
            self.cli('decline', erasure_id, '--reviewer', 'reviewer-1', '--reference', 'dispute-case')
        self.assertEqual(len(self.query("SELECT * FROM consent_events WHERE event_type='erasure_declined'")), 1)
        self.assertEqual(self.post(self.owner, self.base + '/erasure/' + erasure_id + '/withdraw').status_code, 302)
        self.assertEqual(self.query('SELECT status FROM erasure_requests')[0]['status'], 'declined')
        self.assertEqual(runner.invoke(args=transfer).exit_code, 0)
        self.assertEqual(self.query('SELECT owner_id FROM memorials')[0]['owner_id'], nominee)
        with closing(self.connection()) as conn:
            consent.verify_history(conn)

    def test_upload_racing_an_erasure_leaves_no_orphan_file(self):
        original_ident = app_module.ident

        def erase_before_file_write():
            with closing(self.connection()) as conn:
                erasure.erase(conn, self.mid, owner_id=self.owner_id, reviewer='reviewer-1', reference='case',
                              ledger_path=self.ledger, data_dir=self.tmp.name)
            return original_ident()

        with patch.object(app_module, 'ident', side_effect=erase_before_file_write), self.assertRaises(sqlite3.IntegrityError):
            self.post(self.owner, self.base + '/upload', file=(self.image(), 'late.png'), caption='Late', rights='yes', visibility='public')
        self.assertEqual(self.media_files(), [])
        self.assertEqual(len(self.query('SELECT * FROM media')), 0)
        self.assertEqual(len(self.query('SELECT * FROM memorials')), 0)

    def test_every_table_with_memorial_data_is_erased_or_deliberately_retained(self):
        with closing(self.connection()) as conn:
            tables = [row['name'] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            holding = {table for table in tables if any(column['name'] == 'memorial_id' for column in conn.execute(f'PRAGMA table_info({table})'))}
            referencing = {table for table in tables for key in conn.execute(f'PRAGMA foreign_key_list({table})') if key['table'] == 'memorials'}
        self.assertEqual(holding | referencing, set(erasure.MEMORIAL_TABLES) | set(erasure.RETAINED_TABLES))


class ErasureMigrationTests(unittest.TestCase):
    def test_version_three_upgrade_adds_erasure_tables_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path, backup_path = Path(folder) / 'db.sqlite3', Path(folder) / 'before.sqlite3'
            with closing(sqlite3.connect(path)) as conn:
                conn.row_factory = sqlite3.Row
                conn.executescript((database.ROOT / 'schema.sql').read_text())
                for name in ('002_consent.sql', '003_succession.sql'):
                    conn.executescript((database.ROOT / 'migrations' / name).read_text())
                conn.execute("INSERT INTO users VALUES('owner','owner@example.com','Owner','hash','2026-01-01')")
                conn.execute("INSERT INTO memorials(id,owner_id,name,authority,authority_name,consent_at,created,updated) "
                             "VALUES('memorial','owner','Preserved','self','Owner','2026-01-01','2026-01-01','2026-01-01')")
                conn.commit()
                consent.record_grant(conn, 'memorial', 'owner', signed_name='Owner', audience='public', activation='now')
                checkpoint = consent.verify_history(conn)
            self.assertTrue(database.upgrade(path, backup_path))
            with closing(sqlite3.connect(path)) as conn:
                conn.row_factory = sqlite3.Row
                self.assertEqual(database.version(conn), 4)
                self.assertEqual(consent.verify_history(conn, checkpoint), checkpoint)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM erasure_requests').fetchone()[0], 0)
                self.assertEqual(erasure.registry(conn), [])
            with closing(sqlite3.connect(backup_path)) as conn:
                self.assertEqual(database.version(conn), 3)


if __name__ == '__main__':
    unittest.main()
