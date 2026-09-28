import io
from contextlib import closing
from flask.testing import FlaskClient
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
import zipfile
from PIL import Image
from remembrance.app import create_app


class TrackedClient(FlaskClient):
    responses = None

    def open(self, *args, **kwargs):
        response = super().open(*args, **kwargs)
        if self.responses is None:
            self.responses = []
        self.responses.append(response)
        return response

    def close_responses(self):
        for response in self.responses or []:
            response.close()


class MemorialTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'DATA_DIR': Path(self.tmp.name), 'SECRET_KEY': 'testing-only-secret-key-not-for-real-use', 'PUBLIC_URL': 'http://localhost'})
        self.app.test_client_class = TrackedClient
        self.owner = self.app.test_client()
        self.visitor = self.app.test_client()
        self.family = self.app.test_client()
        self.register(self.owner, 'owner@example.com')
        response = self.post(self.owner, '/memorials/new', name='June Example', born='1940-01-02', died='2024-03-04', biography='A remembered life.', visibility='public', authority='executor', authority_name='Test Owner', consent='yes')
        self.assertEqual(response.status_code, 302)
        self.mid = response.location.split('/')[2]
        self.base = '/m/' + self.mid

    def tearDown(self):
        for client in (self.owner, self.visitor, self.family):
            client.close_responses()
        self.tmp.cleanup()

    def token(self, client):
        client.get('/')
        with client.session_transaction() as s:
            return s['csrf']

    def post(self, client, path, **data):
        data['csrf'] = self.token(client)
        return client.post(path, data=data)

    def register(self, client, email):
        return self.post(client, '/register', email=email, name='Test Person', password='a strong test passphrase!', terms='yes')

    def query(self, sql, params=()):
        with closing(sqlite3.connect(Path(self.tmp.name) / 'remembrance.sqlite3')) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()

    def image(self):
        out = io.BytesIO()
        Image.new('RGB', (48, 48), '#779977').save(out, 'PNG')
        out.seek(0)
        return out

    def upload(self, vis='private', children=False):
        response = self.post(self.owner, self.base + '/upload', file=(self.image(), 'photo.png'), caption='A family photo', rights='yes', visibility=vis, portrait='yes', children='yes' if children else '')
        self.assertEqual(response.status_code, 302)
        return self.query('SELECT * FROM media ORDER BY rowid DESC')[0]['id']

class MemorialTests(MemorialTestCase):
    def test_account_and_private_defaults(self):
        r = self.post(self.owner, '/memorials/new', name='Private Person', authority='self', authority_name='A Person', consent='yes')
        mid = r.location.split('/')[2]
        self.assertEqual(self.visitor.get('/m/' + mid).status_code, 404)
        self.assertEqual(self.owner.get('/m/' + mid).status_code, 200)
        self.assertEqual(self.visitor.get(self.base).status_code, 200)
        self.assertEqual(self.visitor.get(self.base + '/manage').status_code, 302)
        self.assertEqual(self.visitor.get(self.base + '/export').status_code, 302)
        self.assertNotIn('a strong test passphrase!', self.query('SELECT password FROM users')[0]['password'])

    def test_csrf_origin_and_host(self):
        self.assertEqual(self.owner.post(self.base + '/candle').status_code, 403)
        token = self.token(self.owner)
        r = self.owner.post(self.base + '/candle', data={'csrf': token}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.visitor.get('/', headers={'Host': 'evil.example'}).status_code, 400)
        r = self.visitor.get(self.base)
        self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", r.headers['Content-Security-Policy'])

    def test_media_family_visibility_and_revocation(self):
        fid = self.upload('family')
        self.register(self.family, 'family@example.com')
        self.assertEqual(self.family.get('/media/' + fid).status_code, 404)
        uid = self.query('SELECT id FROM users WHERE email=?', ('family@example.com',))[0]['id']
        invalid = self.post(self.owner, self.base + '/members', email='family@example.com', account_code='wrong', action='add')
        self.assertEqual(invalid.status_code, 400)
        r = self.post(self.owner, self.base + '/members', email='family@example.com', account_code=uid, action='add')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.family.get('/media/' + fid).status_code, 200)
        self.assertEqual(self.visitor.get('/media/' + fid).status_code, 404)
        self.assertEqual(self.family.get(self.base + '/manage').status_code, 403)
        self.assertEqual(self.post(self.family, self.base + '/settings', action='archive').status_code, 403)
        self.post(self.owner, self.base + '/members', email='family@example.com', action='remove')
        self.assertEqual(self.family.get('/media/' + fid).status_code, 404)

    def test_children_archive_restore_and_profile_gate(self):
        fid = self.upload('public', children=True)
        self.assertEqual(self.query('SELECT visibility FROM media')[0]['visibility'], 'family')
        self.post(self.owner, self.base + '/media/' + fid, action='visibility', visibility='public')
        self.assertEqual(self.visitor.get('/media/' + fid).status_code, 200)
        self.post(self.owner, self.base + '/media/' + fid, action='archive')
        self.assertEqual(self.visitor.get('/media/' + fid).status_code, 404)
        self.assertEqual(self.owner.get('/media/' + fid).status_code, 200)
        self.post(self.owner, self.base + '/media/' + fid, action='restore')
        self.assertEqual(self.visitor.get('/media/' + fid).status_code, 200)
        self.post(self.owner, self.base + '/settings', action='archive')
        self.assertEqual(self.visitor.get('/media/' + fid).status_code, 404)
        self.assertEqual(self.visitor.get(self.base + '/qr.svg').status_code, 404)
        self.post(self.owner, self.base + '/settings', action='restore')
        self.assertEqual(self.visitor.get('/media/' + fid).status_code, 200)

    def test_moderation_xss_withdrawal(self):
        text = '<script>alert("xss")</script> A tender memory.'
        r = self.post(self.visitor, self.base + '/tributes', name='Guest', body=text, consent='yes')
        self.assertEqual(r.status_code, 200)
        token = re.search(r'<code class="withdrawal-code">([^<]+)', r.text)[1]
        tid = self.query('SELECT id FROM tributes')[0]['id']
        self.assertNotIn('A tender memory.', self.visitor.get(self.base).text)
        self.post(self.owner, self.base + '/tributes/' + tid, status='approved')
        page = self.visitor.get(self.base).text
        self.assertIn('A tender memory.', page)
        self.assertNotIn('<script>alert', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertEqual(self.post(self.visitor, '/withdraw', token='wrong').status_code, 400)
        self.assertEqual(self.post(self.visitor, '/withdraw', token=token).status_code, 302)
        self.assertEqual(len(self.query('SELECT * FROM tributes')), 0)
        self.assertNotIn('A tender memory.', self.visitor.get(self.base).text)

    def test_qr_and_export_offline(self):
        fid = self.upload('private')
        self.post(self.owner, self.base + '/events', year='1980', title='A chapter', story='Private event', visibility='private')
        qr = self.visitor.get(self.base + '/qr.svg')
        self.assertEqual(qr.status_code, 200)
        self.assertIn(b'<svg', qr.data)
        png = self.visitor.get(self.base + '/qr.png?download=1')
        self.assertTrue(png.data.startswith(b'\x89PNG'))
        self.assertIn('attachment', png.headers['Content-Disposition'])
        response = self.owner.get(self.base + '/export')
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as z:
            payload = json.loads(z.read('memorial.json'))
            self.assertEqual(payload['memorial']['id'], self.mid)
            self.assertEqual(payload['version'], 1)
            self.assertEqual(payload['media'][0]['id'], fid)
            self.assertIn('media/' + payload['media'][0]['filename'], z.namelist())
            html = z.read('index.html').decode()
            self.assertIn('Private event', html)
            self.assertNotIn('src="http', html)
            self.assertIn('PRIVATE FAMILY ARCHIVE', html)
        response.close()

    def test_input_validation_and_safe_uploads(self):
        r = self.post(self.owner, self.base + '/upload', file=(io.BytesIO(b'<script>evil</script>'), 'evil.svg'), caption='Bad', visibility='public', rights='yes')
        self.assertEqual(r.status_code, 400)
        r = self.post(self.owner, self.base + '/upload', file=(io.BytesIO(b'not a png'), 'bad.png'), caption='Bad', visibility='public', rights='yes')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.post(self.owner, self.base + '/events', year='oops', title='Invalid').status_code, 400)
        self.assertEqual(self.post(self.owner, self.base + '/edit', name='No', born='2024-03-04', died='1940-01-02').status_code, 400)
        self.assertEqual(self.post(self.owner, self.base + '/edit', name='No', visibility='anything').status_code, 400)
        self.assertEqual(self.post(self.owner, self.base + '/edit', name='No', born='2024-99-99').status_code, 400)

    def test_import_is_atomic_and_private(self):
        payload = {'format':'remembrance-import','version':1,'memories':[{'year':1988,'title':'Imported title','story':'From an authorized export'}]}
        r = self.post(self.owner, self.base + '/import', file=(io.BytesIO(json.dumps(payload).encode()), 'memories.json'), consent='yes')
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('Imported title', self.visitor.get(self.base).text)
        row = self.query('SELECT * FROM events')[0]
        self.assertEqual(row['visibility'], 'private')
        self.post(self.owner, self.base + '/events/' + row['id'] + '/visibility', visibility='public')
        self.assertIn('Imported title', self.visitor.get(self.base).text)
        payload['memories'].append({'year':False,'title':'Invalid'})
        self.assertEqual(self.post(self.owner, self.base + '/import', file=(io.BytesIO(json.dumps(payload).encode()), 'memories.json'), consent='yes').status_code, 400)
        self.assertEqual(len(self.query('SELECT * FROM events')), 1)

    def test_candles_no_public_counters_and_report(self):
        for _ in range(2):
            self.assertEqual(self.post(self.visitor, self.base + '/candle').status_code, 302)
        self.assertEqual(len(self.query('SELECT * FROM candles')), 1)
        r = self.post(self.visitor, self.base + '/report', email='visitor@example.com', category='privacy', detail='Please remove this picture.')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.query('SELECT * FROM reports')), 1)
        self.assertIn('Please remove this picture.', self.owner.get(self.base + '/manage').text)
        self.assertNotIn('visitor@example.com', self.visitor.get(self.base).text)

    def test_data_survives_restart_and_routes_render(self):
        second = create_app({'TESTING':True,'DATA_DIR':Path(self.tmp.name),'PUBLIC_URL':'http://localhost','SECRET_KEY':'testing-only-secret-key-not-for-real-use'})
        self.assertIn('June Example', second.test_client().get(self.base).text)
        for path in ('/', '/install', '/privacy', '/withdraw', '/register', '/login', '/healthz', '/sw.js', '/static/manifest.webmanifest', self.base, self.base + '/plaque', self.base + '/report'):
            self.assertEqual(self.visitor.get(path).status_code, 200, path)
        for path in ('/dashboard', self.base + '/manage', self.base + '/edit', self.base + '/import'):
            self.assertEqual(self.owner.get(path).status_code, 200, path)
        self.assertEqual(self.post(self.owner, '/logout').status_code, 302)
        self.assertEqual(self.owner.get('/dashboard').status_code, 302)
        self.assertEqual(self.post(self.owner, '/login', email='owner@example.com', password='wrong').status_code, 401)
        self.assertEqual(self.post(self.owner, '/login', email='owner@example.com', password='a strong test passphrase!').status_code, 302)

    def test_cross_owner_and_demo_are_protected(self):
        self.register(self.family, 'other@example.com')
        self.assertEqual(self.post(self.family, self.base + '/edit', name='Stolen').status_code, 403)
        self.assertEqual(self.family.get(self.base + '/export').status_code, 403)
        runner = self.app.test_cli_runner()
        self.assertEqual(runner.invoke(args=['seed-demo']).exit_code, 0)
        self.assertEqual(self.visitor.get('/m/eleanor-example').status_code, 200)
        self.assertEqual(self.post(self.visitor, '/m/eleanor-example/tributes', name='No', body='No', consent='yes').status_code, 400)


if __name__ == '__main__':
    unittest.main()
