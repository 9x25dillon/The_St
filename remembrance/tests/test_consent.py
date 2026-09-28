from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from remembrance import consent, database
from remembrance.app import create_app
from remembrance.scripts.backup import backup
from remembrance.scripts.restore import restore
from remembrance.tests.test_app import MemorialTestCase


class ConsentTests(MemorialTestCase):
    def connection(self):
        conn = sqlite3.connect(Path(self.tmp.name) / 'remembrance.sqlite3', timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def grant(self, audience='public', activation='now'):
        response = self.post(self.owner, self.base + '/consent', signed_name='Recorded Owner',
                             audience=audience, activation=activation, consent='yes')
        self.assertEqual(response.status_code, 302, response.text)
        return self.query("SELECT * FROM consent_grants WHERE status!='revoked'")[0]['id']

    def operator(self, *args, success=True):
        result = self.app.test_cli_runner().invoke(args=['consent', *args, '--reviewer', 'reviewer-1', '--reference', 'private-case-123'])
        if success:
            self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        else:
            self.assertNotEqual(result.exit_code, 0)
        return result

    def add_family(self):
        self.register(self.family, 'family@example.com')
        uid = self.query('SELECT id FROM users WHERE email=?', ('family@example.com',))[0]['id']
        self.post(self.owner, self.base + '/members', email='family@example.com', account_code=uid, action='add')

    def test_opt_in_closes_every_visitor_surface_and_preserves_owner_access(self):
        fid = self.upload('public')
        self.add_family()
        self.assertEqual(self.visitor.get(self.base).status_code, 200)
        self.grant()
        for client in (self.visitor, self.family):
            for path in (self.base, '/media/' + fid, self.base + '/qr.svg', self.base + '/qr.png', self.base + '/plaque'):
                self.assertEqual(client.get(path).status_code, 404, path)
            self.assertEqual(self.post(client, self.base + '/candle').status_code, 404)
            self.assertEqual(self.post(client, self.base + '/tributes', name='Visitor', body='Memory', consent='yes').status_code, 404)
        self.assertNotIn('June Example', self.family.get('/dashboard').text)
        for path in (self.base, '/media/' + fid, self.base + '/manage', self.base + '/export', self.base + '/consent'):
            self.assertEqual(self.owner.get(path).status_code, 200, path)
        self.assertIn('Waiting for review.', self.owner.get(self.base + '/consent').text)
        self.assertIn('Sharing paused', self.owner.get('/dashboard').text)
        self.assertIn('Sharing paused', self.owner.get(self.base + '/manage').text)
        # Concerns remain reportable even while content is hidden; the form reveals no story.
        report = self.visitor.get(self.base + '/report')
        self.assertEqual(report.status_code, 200)
        self.assertNotIn('June Example', report.text)

    def test_review_preserves_family_and_item_permissions(self):
        public_file, private_file = self.upload('public'), self.upload('private')
        self.add_family()
        gid = self.grant(audience='family')
        self.operator('verify', gid)
        self.assertEqual(self.visitor.get(self.base).status_code, 404)
        self.assertEqual(self.family.get(self.base).status_code, 200)
        self.assertEqual(self.family.get('/media/' + public_file).status_code, 200)
        self.assertEqual(self.family.get('/media/' + private_file).status_code, 404)
        self.post(self.owner, self.base + '/edit', name='June Example', visibility='private')
        self.assertEqual(self.family.get(self.base).status_code, 404)
        self.post(self.owner, self.base + '/edit', name='June Example', visibility='family')
        self.assertEqual(self.family.get(self.base).status_code, 200)
        self.post(self.owner, self.base + '/members', email='family@example.com', action='remove')
        self.assertEqual(self.family.get(self.base).status_code, 404)

    def test_after_passing_requires_separate_review_and_can_be_corrected(self):
        gid = self.grant(activation='after_death')
        self.operator('verify', gid)
        self.assertEqual(self.visitor.get(self.base).status_code, 404)
        self.post(self.owner, self.base + '/edit', name='June Example', born='1940-01-02', died='2020-01-01', visibility='public')
        self.assertEqual(self.visitor.get(self.base).status_code, 404)
        for value in ('not-a-date', '9999-01-01', '1900-01-01'):
            self.operator('confirm-death', self.mid, '--date', value, success=False)
        self.operator('confirm-death', self.mid, '--date', '2020-01-01')
        self.assertEqual(self.visitor.get(self.base).status_code, 200)
        self.operator('clear-death', self.mid)
        self.assertEqual(self.visitor.get(self.base).status_code, 404)
        self.assertIn('Held until after passing.', self.owner.get(self.base + '/consent').text)

    def test_death_confirmation_alone_does_not_approve_grant(self):
        gid = self.grant(activation='after_death')
        self.operator('confirm-death', self.mid, '--date', '2020-01-01')
        self.assertEqual(self.visitor.get(self.base).status_code, 404)
        self.operator('verify', gid)
        self.assertEqual(self.visitor.get(self.base).status_code, 200)

    def test_revocation_is_idempotent_and_replacement_requires_review(self):
        fid = self.upload('public')
        gid = self.grant()
        self.operator('verify', gid)
        self.assertEqual(self.visitor.get('/media/' + fid).status_code, 200)
        for _ in range(2):
            self.assertEqual(self.post(self.owner, self.base + '/consent/' + gid + '/revoke').status_code, 302)
        self.assertEqual(len(self.query("SELECT * FROM consent_events WHERE event_type='grant_revoked'")), 1)
        self.assertEqual(self.visitor.get('/media/' + fid).status_code, 404)
        self.post(self.owner, self.base + '/settings', action='archive')
        self.post(self.owner, self.base + '/settings', action='restore')
        self.assertEqual(self.visitor.get(self.base).status_code, 404)
        self.operator('verify', gid, success=False)
        new_gid = self.grant()
        self.assertNotEqual(gid, new_gid)
        self.assertEqual(self.visitor.get(self.base).status_code, 404)
        self.operator('verify', new_gid)
        self.assertEqual(self.visitor.get(self.base).status_code, 200)
        self.post(self.owner, self.base + '/consent/' + gid + '/revoke')
        self.assertEqual(self.visitor.get(self.base).status_code, 200)
        self.assertIn('already revoked', self.owner.get(self.base + '/consent').text)
        self.operator('revoke', self.mid, new_gid)
        self.assertEqual(self.visitor.get(self.base).status_code, 404)

    def test_backup_restore_preserves_revoked_grant_and_history(self):
        gid = self.grant()
        self.operator('verify', gid)
        self.post(self.owner, self.base + '/consent/' + gid + '/revoke')
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            archive, target = Path(folder) / 'server.zip', Path(folder) / 'restored'
            backup(self.tmp.name, archive)
            restore(archive, target)
            restored = create_app({'TESTING': True, 'DATA_DIR': target, 'SECRET_KEY': 'restored-test-secret-not-for-real-use', 'PUBLIC_URL': 'http://localhost'})
            with restored.test_client() as client:
                self.assertEqual(client.get(self.base).status_code, 404)
                self.assertEqual(client.get('/healthz').json['schema'], 3)
            with closing(sqlite3.connect(target / 'remembrance.sqlite3')) as conn:
                conn.row_factory = sqlite3.Row
                self.assertEqual(conn.execute('SELECT status FROM consent_grants').fetchone()[0], 'revoked')
                self.assertEqual(consent.verify_history(conn)['profiles'][self.mid]['sequence'], 4)

    def test_owner_boundaries_csrf_and_invalid_forms(self):
        self.register(self.family, 'other@example.com')
        self.assertEqual(self.family.get(self.base + '/consent').status_code, 403)
        self.assertEqual(self.post(self.family, self.base + '/consent', signed_name='Other', audience='public', activation='now', consent='yes').status_code, 403)
        self.assertEqual(self.owner.post(self.base + '/consent').status_code, 403)
        for overrides in ({'consent': ''}, {'audience': 'all'}, {'activation': 'yesterday'}, {'signed_name': ''}):
            data = dict(signed_name='Owner', audience='public', activation='now', consent='yes')
            data.update(overrides)
            self.assertEqual(self.post(self.owner, self.base + '/consent', **data).status_code, 400)
        self.assertEqual(len(self.query('SELECT * FROM consent_controls')), 0)
        gid = self.grant()
        self.assertEqual(self.post(self.family, self.base + '/consent/' + gid + '/revoke').status_code, 403)
        self.assertEqual(self.post(self.owner, self.base + '/consent/missing/revoke').status_code, 400)
        self.assertEqual(self.post(self.owner, self.base + '/consent', signed_name='Owner', audience='public', activation='now', consent='yes').status_code, 400)
        self.assertEqual(len(self.query('SELECT * FROM consent_grants')), 1)

    def test_export_contains_consent_history_without_raw_case_reference(self):
        gid = self.grant()
        self.operator('verify', gid)
        response = self.owner.get(self.base + '/export')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            data = json.loads(archive.read('memorial.json'))
            self.assertEqual(data['consent']['grants'][0]['id'], gid)
            self.assertEqual(len(data['consent']['history']), 2)
            self.assertNotIn('private-case-123', json.dumps(data))
        for row in self.query('SELECT * FROM consent_events'):
            self.assertNotIn('Recorded Owner', row['payload'])

    def test_audit_failure_rolls_back_state_and_never_serves_media(self):
        with closing(self.connection()) as conn:
            conn.execute("CREATE TRIGGER fail_history BEFORE INSERT ON consent_events BEGIN SELECT RAISE(ABORT, 'test audit failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.grant()
        self.assertEqual(len(self.query('SELECT * FROM consent_grants')), 0)
        self.assertEqual(len(self.query('SELECT * FROM consent_controls')), 0)
        with closing(self.connection()) as conn:
            conn.execute('DROP TRIGGER fail_history')
        fid = self.upload('public')
        gid = self.grant()
        self.operator('verify', gid)
        with closing(self.connection()) as conn:
            conn.execute("CREATE TRIGGER fail_history BEFORE INSERT ON consent_events BEGIN SELECT RAISE(ABORT, 'test audit failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.visitor.get('/media/' + fid)
        with self.assertRaises(sqlite3.IntegrityError):
            self.post(self.owner, self.base + '/consent/' + gid + '/revoke')
        self.assertEqual(self.query('SELECT status FROM consent_grants')[0]['status'], 'verified')

    def test_history_triggers_hashes_and_off_host_checkpoint(self):
        gid = self.grant()
        self.operator('verify', gid)
        with closing(self.connection()) as conn:
            checkpoint = consent.verify_history(conn)
            for sql in ('UPDATE consent_events SET actor_id=\'changed\'', 'DELETE FROM consent_events',
                        'INSERT OR REPLACE INTO consent_events SELECT * FROM consent_events WHERE sequence=1'):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)
                conn.rollback()
            conn.execute('DROP TRIGGER consent_events_no_delete')
            conn.execute('DELETE FROM consent_events WHERE sequence=2')
            conn.commit()
            self.assertEqual(consent.verify_history(conn)['profiles'][self.mid]['sequence'], 1)
            with self.assertRaises(consent.ConsentError):
                consent.verify_history(conn, checkpoint)
            conn.execute('DROP TRIGGER consent_events_no_update')
            conn.execute("UPDATE consent_events SET payload='{}'")
            conn.commit()
            with self.assertRaises(consent.ConsentError):
                consent.verify_history(conn)

    def test_cli_checkpoint_and_pending_queue(self):
        gid = self.grant()
        runner = self.app.test_cli_runner()
        pending = runner.invoke(args=['consent', 'pending'])
        self.assertEqual(pending.exit_code, 0)
        self.assertEqual(json.loads(pending.output)['id'], gid)
        shown = runner.invoke(args=['consent', 'show', gid])
        self.assertEqual(shown.exit_code, 0)
        self.assertEqual(json.loads(shown.output)['declaration_text'], consent.DECLARATION_TEXT)
        path = Path(self.tmp.name) / 'checkpoint.json'
        result = runner.invoke(args=['consent', 'audit', '--output', str(path)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.operator('verify', gid)
        self.assertEqual(runner.invoke(args=['consent', 'audit', '--checkpoint', str(path)]).exit_code, 0)
        self.assertNotEqual(runner.invoke(args=['consent', 'audit', '--output', str(path)]).exit_code, 0)

    def test_authorization_and_revocation_serialize(self):
        gid = self.grant()
        self.operator('verify', gid)
        owner = self.query('SELECT owner_id FROM memorials WHERE id=?', (self.mid,))[0]['owner_id']
        barrier = threading.Barrier(2)

        def authorize():
            with closing(self.connection()) as conn:
                barrier.wait(timeout=5)
                return consent.authorize(conn, self.mid, role='public')

        def revoke():
            with closing(self.connection()) as conn:
                barrier.wait(timeout=5)
                return consent.revoke_grant(conn, self.mid, gid, owner_id=owner)

        with ThreadPoolExecutor(max_workers=2) as pool:
            authorization, revocation = pool.submit(authorize), pool.submit(revoke)
            result = authorization.result(timeout=10)
            self.assertTrue(revocation.result(timeout=10))
        events = [row['event_type'] for row in self.query('SELECT * FROM consent_events ORDER BY sequence')]
        if result.allowed:
            self.assertLess(events.index('authorized'), events.index('grant_revoked'))
        else:
            self.assertLess(events.index('grant_revoked'), events.index('denied'))
        with closing(self.connection()) as conn:
            self.assertFalse(consent.authorize(conn, self.mid, role='public').allowed)
            consent.verify_history(conn)

    def test_kernel_denies_unknown_uses_and_roles(self):
        gid = self.grant()
        self.operator('verify', gid)
        grant = self.query('SELECT * FROM consent_grants')[0]
        self.assertTrue(consent.decide(grant).allowed)
        for kwargs in ({'action': 'voice_synthesis'}, {'purpose': 'advertising'}, {'purpose': ''}, {'role': 'admin'}):
            self.assertFalse(consent.decide(grant, **kwargs).allowed)
        self.assertFalse(consent.decide(None).allowed)


class MigrationTests(unittest.TestCase):
    def test_version_one_requires_upgrade_and_backup_preserves_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path, backup = Path(folder) / 'remembrance.sqlite3', Path(folder) / 'before.sqlite3'
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript((database.ROOT / 'schema.sql').read_text())
                conn.execute("INSERT INTO users VALUES('existing','old@example.com','Existing','hash','2026-01-01')")
                conn.commit()
                with self.assertRaises(RuntimeError):
                    database.initialize(conn)
                self.assertEqual(database.version(conn), 1)
            self.assertTrue(database.upgrade(path, backup))
            self.assertFalse(database.upgrade(path, backup))
            for file, expected in ((path, 3), (backup, 1)):
                with closing(sqlite3.connect(file)) as conn:
                    self.assertEqual(database.version(conn), expected)
                    self.assertEqual(conn.execute('SELECT name FROM users').fetchone()[0], 'Existing')
                    self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_failed_migration_rolls_back_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path, backup = root / 'db.sqlite3', root / 'backup.sqlite3'
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript((database.ROOT / 'schema.sql').read_text())
            (root / 'migrations').mkdir()
            (root / 'migrations' / '002_broken.sql').write_text('CREATE TABLE partial (id INTEGER);\nTHIS IS INVALID;\nPRAGMA user_version=2;\n')
            with patch.object(database, 'ROOT', root), self.assertRaises(sqlite3.OperationalError):
                database.upgrade(path, backup)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(database.version(conn), 1)
                self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='partial'").fetchone())
            self.assertTrue(backup.is_file())

    def test_concurrent_fresh_startup_and_unknown_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'new.sqlite3'
            barrier = threading.Barrier(2)

            def initialize():
                with closing(sqlite3.connect(path, timeout=15)) as conn:
                    barrier.wait(timeout=5)
                    database.initialize(conn)
                    return database.version(conn)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [pool.submit(initialize) for _ in range(2)]
                self.assertEqual([result.result(timeout=10) for result in results], [3, 3])
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('PRAGMA user_version=99')
                with self.assertRaises(RuntimeError):
                    database.initialize(conn)
                self.assertEqual(database.version(conn), 99)


if __name__ == '__main__':
    unittest.main()
