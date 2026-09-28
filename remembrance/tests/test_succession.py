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

from remembrance import app as app_module, consent, database, succession
from remembrance.scripts.backup import backup
from remembrance.scripts.restore import restore
from remembrance.tests.test_app import MemorialTestCase


class SuccessionTests(MemorialTestCase):
    def setUp(self):
        super().setUp()
        self.register(self.family, 'successor@example.com')
        self.owner_id = self.query('SELECT owner_id FROM memorials WHERE id=?', (self.mid,))[0]['owner_id']
        self.nominee_id = self.query('SELECT id FROM users WHERE email=?', ('successor@example.com',))[0]['id']

    def connection(self):
        conn = sqlite3.connect(Path(self.tmp.name) / 'remembrance.sqlite3', timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def nominate(self):
        response = self.post(self.owner, self.base + '/succession', email='successor@example.com', account_code=self.nominee_id, consent='yes')
        self.assertEqual(response.status_code, 302, response.text)
        return self.query("SELECT id FROM succession_requests WHERE status IN ('offered','accepted')")[0]['id']

    def accept(self, request_id):
        response = self.post(self.family, '/succession/' + request_id, action='accept', signed_name='Successor Person', authority='executor', consent='yes')
        self.assertEqual(response.status_code, 302, response.text)

    def transfer(self, request_id, success=True, **overrides):
        options = dict(from_owner_id=self.owner_id, to_account_id=self.nominee_id)
        options.update(overrides)
        result = self.app.test_cli_runner().invoke(args=['succession', 'transfer', request_id,
                                                       '--from-owner', options['from_owner_id'], '--to-account', options['to_account_id'],
                                                       '--reviewer', 'reviewer-1', '--reference', 'private-transfer-case'])
        if success:
            self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        else:
            self.assertNotEqual(result.exit_code, 0)
        return result

    def test_invitation_is_bound_to_account_and_reveals_no_private_content(self):
        fid = self.upload('private')
        self.post(self.owner, self.base + '/edit', name='June Example', biography='A private story never shown to nominees.', visibility='private')
        request_id = self.nominate()
        self.assertIn('Invitations to care', self.family.get('/dashboard').text)
        invite = self.family.get('/succession/' + request_id)
        self.assertEqual(invite.status_code, 200)
        self.assertIn('June Example', invite.text)
        self.assertNotIn('A private story never shown to nominees.', invite.text)
        self.assertNotIn(fid, invite.text)
        self.assertEqual(self.family.get(self.base).status_code, 404)
        self.assertEqual(self.family.get(self.base + '/manage').status_code, 403)
        self.assertEqual(self.family.get(self.base + '/export').status_code, 403)
        self.assertEqual(self.owner.get('/succession/' + request_id).status_code, 404)
        self.register(self.visitor, 'unrelated@example.com')
        self.assertEqual(self.visitor.get('/succession/' + request_id).status_code, 404)
        self.assertEqual(self.post(self.visitor, '/succession/' + request_id, action='decline').status_code, 404)
        self.accept(request_id)
        self.assertEqual(self.family.get('/media/' + fid).status_code, 404)
        self.assertEqual(self.family.get(self.base + '/manage').status_code, 403)
        self.assertEqual(self.query('SELECT owner_id FROM memorials WHERE id=?', (self.mid,))[0]['owner_id'], self.owner_id)

    def test_invalid_nominations_csrf_and_legacy_email_cannot_transfer(self):
        for changes in ({'consent': ''}, {'account_code': 'wrong'}, {'email': 'unknown@example.com'},
                        {'email': 'owner@example.com', 'account_code': self.owner_id}):
            data = dict(email='successor@example.com', account_code=self.nominee_id, consent='yes')
            data.update(changes)
            self.assertEqual(self.post(self.owner, self.base + '/succession', **data).status_code, 400)
        self.assertEqual(self.owner.post(self.base + '/succession').status_code, 403)
        self.assertEqual(self.post(self.family, self.base + '/succession', email='owner@example.com', account_code=self.owner_id, consent='yes').status_code, 403)
        self.assertEqual(len(self.query('SELECT * FROM succession_requests')), 0)
        with closing(self.connection()) as conn:
            conn.execute('UPDATE memorials SET successor=? WHERE id=?', ('successor@example.com', self.mid))
            conn.commit()
        self.assertIn('previously saved preference', self.owner.get(self.base + '/succession').text)
        self.transfer('missing', success=False)
        self.assertEqual(self.post(self.owner, self.base + '/settings', action='successor', successor='unknown@example.com').status_code, 400)

    def test_acceptance_requires_declaration_and_does_not_run_transfer(self):
        request_id = self.nominate()
        path = '/succession/' + request_id
        for changes in ({'consent': ''}, {'signed_name': ''}, {'authority': 'admin'}, {'action': 'transfer'}):
            data = dict(action='accept', consent='yes', signed_name='Successor', authority='executor')
            data.update(changes)
            self.assertEqual(self.post(self.family, path, **data).status_code, 400)
        self.assertEqual(self.family.post(path, data={'action': 'accept'}).status_code, 403)
        self.transfer(request_id, success=False)
        self.accept(request_id)
        self.accept(request_id)
        self.assertEqual(len(self.query("SELECT * FROM consent_events WHERE event_type='successor_accepted'")), 1)
        self.transfer(request_id, to_account_id=self.owner_id, success=False)
        self.transfer(request_id, from_owner_id=self.nominee_id, success=False)
        pending = self.app.test_cli_runner().invoke(args=['succession', 'pending'])
        self.assertEqual(json.loads(pending.output)['id'], request_id)
        shown = self.app.test_cli_runner().invoke(args=['succession', 'show', request_id])
        self.assertEqual(json.loads(shown.output)['declaration_text'], succession.DECLARATION_TEXT)

    def test_cancel_and_withdraw_acceptance_close_nomination(self):
        request_id = self.nominate()
        self.assertEqual(self.post(self.owner, self.base + '/succession', email='successor@example.com', account_code=self.nominee_id, consent='yes').status_code, 400)
        self.assertEqual(self.post(self.family, self.base + '/succession/' + request_id + '/cancel').status_code, 403)
        self.accept(request_id)
        self.post(self.family, '/succession/' + request_id, action='decline')
        self.transfer(request_id, success=False)
        self.assertNotIn('Invitations to care', self.family.get('/dashboard').text)
        new_id = self.nominate()
        self.assertNotEqual(request_id, new_id)
        self.assertEqual(self.post(self.owner, self.base + '/succession/' + request_id + '/cancel').status_code, 302)
        self.assertEqual(self.query('SELECT status FROM succession_requests WHERE id=?', (new_id,))[0]['status'], 'offered')
        self.post(self.owner, self.base + '/succession/' + new_id + '/cancel')
        self.assertEqual(self.post(self.family, '/succession/' + new_id, action='accept', signed_name='Nominee', authority='executor', consent='yes').status_code, 400)
        self.transfer(new_id, success=False)

    def test_transfer_preserves_content_and_rechecks_existing_sessions(self):
        fid = self.upload('private')
        self.post(self.owner, self.base + '/edit', name='June Example', biography='Preserved life story', visibility='private')
        self.post(self.owner, self.base + '/members', email='owner@example.com', account_code=self.owner_id, action='add')
        before = dict(self.query('SELECT * FROM memorials WHERE id=?', (self.mid,))[0])
        request_id = self.nominate()
        self.accept(request_id)
        self.transfer(request_id)
        after = self.query('SELECT * FROM memorials WHERE id=?', (self.mid,))[0]
        for key in ('id', 'name', 'biography', 'visibility', 'portrait_id', 'archived'):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after['owner_id'], self.nominee_id)
        self.assertEqual(after['authority_name'], 'Successor Person')
        self.assertEqual(after['successor'], '')
        self.assertEqual(len(self.query('SELECT * FROM members WHERE email=?', ('owner@example.com',))), 0)
        for suffix in ('/manage', '/edit', '/export', '/consent', '/succession'):
            self.assertEqual(self.owner.get(self.base + suffix).status_code, 403)
            self.assertEqual(self.family.get(self.base + suffix).status_code, 200)
        self.assertEqual(self.owner.get('/media/' + fid).status_code, 404)
        self.assertEqual(self.family.get('/media/' + fid).status_code, 200)
        self.assertEqual(self.post(self.owner, self.base + '/settings', action='archive').status_code, 403)
        self.assertEqual(self.post(self.family, self.base + '/edit', name='Cared for by successor', visibility='private').status_code, 302)
        self.assertIn('already completed', self.transfer(request_id).output)
        with closing(self.connection()) as conn:
            consent.verify_history(conn)

    def test_transfer_preserves_existing_consent_and_new_owner_can_revoke(self):
        self.post(self.owner, self.base + '/consent', signed_name='Original Owner', audience='public', activation='after_death', consent='yes')
        gid = self.query('SELECT id FROM consent_grants')[0]['id']
        runner = self.app.test_cli_runner()
        for args in (['verify', gid], ['confirm-death', self.mid, '--date', '2020-01-01']):
            result = runner.invoke(args=['consent', *args, '--reviewer', 'reviewer-1', '--reference', 'case'])
            self.assertEqual(result.exit_code, 0, result.output)
        grant_before = dict(self.query('SELECT * FROM consent_grants')[0])
        control_before = dict(self.query('SELECT * FROM consent_controls')[0])
        request_id = self.nominate()
        self.accept(request_id)
        self.transfer(request_id)
        self.assertEqual(grant_before, dict(self.query('SELECT * FROM consent_grants')[0]))
        self.assertEqual(control_before, dict(self.query('SELECT * FROM consent_controls')[0]))
        self.assertEqual(self.visitor.get(self.base).status_code, 200)
        self.assertEqual(self.post(self.owner, self.base + '/consent/' + gid + '/revoke').status_code, 403)
        self.assertEqual(self.post(self.family, self.base + '/consent/' + gid + '/revoke').status_code, 302)
        self.assertEqual(self.visitor.get(self.base).status_code, 404)

    def test_stale_owner_write_is_rolled_back_after_transfer(self):
        request_id = self.nominate()
        self.accept(request_id)
        original_ident = app_module.ident

        def transfer_before_insert():
            with closing(self.connection()) as conn:
                succession.transfer(conn, request_id, from_owner_id=self.owner_id, to_account_id=self.nominee_id, reviewer='reviewer-1', reference='case')
            return original_ident()

        with patch.object(app_module, 'ident', side_effect=transfer_before_insert):
            response = self.post(self.owner, self.base + '/events', year='2020', title='Stale edit', story='Must not be committed', visibility='public')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.query('SELECT * FROM events')), 0)
        self.assertEqual(len(self.query("SELECT * FROM audit WHERE action='event-added'")), 0)
        self.assertEqual(self.query('SELECT owner_id FROM memorials WHERE id=?', (self.mid,))[0]['owner_id'], self.nominee_id)

    def test_stale_upload_removes_file_after_transfer(self):
        request_id = self.nominate()
        self.accept(request_id)
        original_ident = app_module.ident

        def transfer_before_file_write():
            with closing(self.connection()) as conn:
                succession.transfer(conn, request_id, from_owner_id=self.owner_id, to_account_id=self.nominee_id, reviewer='reviewer-1', reference='case')
            return original_ident()

        with patch.object(app_module, 'ident', side_effect=transfer_before_file_write):
            response = self.post(self.owner, self.base + '/upload', file=(self.image(), 'stale.png'), caption='Stale', rights='yes', visibility='private')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.query('SELECT * FROM media')), 0)
        self.assertEqual(list((Path(self.tmp.name) / 'media').iterdir()), [])

    def test_audit_failure_rolls_back_transfer_and_nomination(self):
        with closing(self.connection()) as conn:
            conn.execute("CREATE TRIGGER fail_succession BEFORE INSERT ON consent_events BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.nominate()
        self.assertEqual(len(self.query('SELECT * FROM succession_requests')), 0)
        self.assertEqual(self.query('SELECT successor FROM memorials')[0]['successor'], '')
        with closing(self.connection()) as conn:
            conn.execute('DROP TRIGGER fail_succession')
        request_id = self.nominate()
        self.accept(request_id)
        with closing(self.connection()) as conn:
            conn.execute("CREATE TRIGGER fail_succession BEFORE INSERT ON consent_events BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        self.transfer(request_id, success=False)
        self.assertEqual(self.query('SELECT status FROM succession_requests')[0]['status'], 'accepted')
        self.assertEqual(self.query('SELECT owner_id FROM memorials')[0]['owner_id'], self.owner_id)

    def test_cancel_and_transfer_serialize(self):
        request_id = self.nominate()
        self.accept(request_id)
        barrier = threading.Barrier(2)

        def compete(operation):
            with closing(self.connection()) as conn:
                barrier.wait(timeout=5)
                try:
                    if operation == 'transfer':
                        return succession.transfer(conn, request_id, from_owner_id=self.owner_id, to_account_id=self.nominee_id, reviewer='reviewer-1', reference='case')
                    return succession.cancel(conn, self.mid, request_id, self.owner_id)
                except succession.SuccessionError:
                    return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [pool.submit(compete, operation) for operation in ('transfer', 'cancel')]
            self.assertEqual(sorted(result.result(timeout=10) for result in outcomes), [False, True])
        status = self.query('SELECT status FROM succession_requests')[0]['status']
        self.assertIn(status, ('completed', 'cancelled'))
        self.assertEqual(self.query('SELECT owner_id FROM memorials')[0]['owner_id'], self.nominee_id if status == 'completed' else self.owner_id)
        with closing(self.connection()) as conn:
            consent.verify_history(conn)

    def test_exports_and_server_restore_preserve_transfer_record(self):
        request_id = self.nominate()
        self.accept(request_id)
        self.transfer(request_id)
        response = self.family.get(self.base + '/export')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            payload = json.loads(archive.read('memorial.json'))
            self.assertEqual(payload['succession'][0]['previous_authority_name'], 'Test Owner')
            self.assertEqual(payload['succession'][0]['status'], 'completed')
            self.assertEqual(payload['consent_history'][-1]['event_type'], 'ownership_transferred')
            self.assertNotIn('private-transfer-case', json.dumps(payload))
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            archive, target = Path(folder) / 'backup.zip', Path(folder) / 'restored'
            backup(self.tmp.name, archive)
            restore(archive, target)
            restored = app_module.create_app({'TESTING': True, 'DATA_DIR': target, 'SECRET_KEY': 'restored-test-secret-not-for-real-use', 'PUBLIC_URL': 'http://localhost'})
            with restored.test_client() as client:
                with client.session_transaction() as session:
                    session['uid'] = self.owner_id
                self.assertEqual(client.get(self.base + '/manage').status_code, 403)
            with closing(sqlite3.connect(target / 'remembrance.sqlite3')) as conn:
                conn.row_factory = sqlite3.Row
                self.assertEqual(conn.execute('SELECT owner_id FROM memorials').fetchone()[0], self.nominee_id)
                consent.verify_history(conn)


class SuccessionMigrationTests(unittest.TestCase):
    def test_version_two_upgrade_preserves_consent_history_and_saved_preference(self):
        with tempfile.TemporaryDirectory() as folder:
            path, backup_path = Path(folder) / 'db.sqlite3', Path(folder) / 'before.sqlite3'
            with closing(sqlite3.connect(path)) as conn:
                conn.row_factory = sqlite3.Row
                conn.executescript((database.ROOT / 'schema.sql').read_text())
                conn.executescript((database.ROOT / 'migrations' / '002_consent.sql').read_text())
                conn.execute("INSERT INTO users VALUES('owner','owner@example.com','Owner','hash','2026-01-01')")
                conn.execute("INSERT INTO memorials(id,owner_id,name,authority,authority_name,consent_at,created,updated,successor) "
                             "VALUES('memorial','owner','Preserved','self','Owner','2026-01-01','2026-01-01','2026-01-01','legacy@example.com')")
                conn.commit()
                gid = consent.record_grant(conn, 'memorial', 'owner', signed_name='Owner', audience='family', activation='after_death')
                consent.verify_grant(conn, gid, reviewer='reviewer-1', reference='case')
                checkpoint = consent.verify_history(conn)
            self.assertTrue(database.upgrade(path, backup_path))
            with closing(sqlite3.connect(path)) as conn:
                conn.row_factory = sqlite3.Row
                self.assertEqual(database.version(conn), database.SCHEMA_VERSION)
                self.assertEqual(consent.verify_history(conn, checkpoint), checkpoint)
                self.assertEqual(conn.execute('SELECT successor FROM memorials').fetchone()[0], 'legacy@example.com')
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM succession_requests').fetchone()[0], 0)
            with closing(sqlite3.connect(backup_path)) as conn:
                self.assertEqual(database.version(conn), 2)


if __name__ == '__main__':
    unittest.main()
