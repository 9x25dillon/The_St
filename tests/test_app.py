import http.client
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import Server, detect_source, import_data
from mirror import _read_history


class ImportTests(unittest.TestCase):
    def test_sources(self):
        result = import_data({'source':'notes','files':[{'name':'diary.md','text':'Gardens gardens and walks.'}]})
        self.assertEqual(result['terms'][0], ('gardens', 2))
        self.assertEqual(len(import_data({'source':'demo'})['records']), 60)
        result = import_data({'source':'tiktok','files':[{'text':json.dumps({'Interests':['Gardening']})}]})
        self.assertEqual(result['categories'], ['Gardening'])
        with self.assertRaises(ValueError):
            import_data({'source':'notes','files':[]})

    def test_new_sources_are_wired_correctly(self):
        result = import_data({'source':'youtube','files':[{'text':json.dumps(
            [{'title':'Searched for gardens','time':'2024-01-01T00:00:00Z'}])}]})
        self.assertEqual(result['records'][0]['source'], 'search')

        result = import_data({'source':'instagram','files':[{'text':json.dumps(
            {'topics_your_topics':[{'string_map_data':{'Name':{'value':'Cooking'}}}]})}]})
        self.assertEqual(result['categories'], ['Cooking'])

        result = import_data({'source':'spotify','files':[{'text':json.dumps(
            [{'ts':'2024-01-01T00:00:00Z','master_metadata_track_name':'A Song',
              'master_metadata_album_artist_name':'A Band'}])}]})
        self.assertEqual(result['records'][0]['text'], 'A Song — A Band')

        result = import_data({'source':'reddit','files':[{'name':'posts.csv','text':
            'id,permalink,date,ip,subreddit,gildings,title,url,body\n'
            '1,/r/x/1,2024-01-01 00:00:00 UTC,0.0.0.0,gardening,0,Tomato tips,,Water deeply\n'}]})
        self.assertEqual(result['records'][0]['source'], 'post')

        result = import_data({'source':'amazon','files':[{'name':'Retail.OrderHistory.1.csv','text':
            'Order Date,Product Name\n2024-01-01 00:00:00 UTC,A Nice Lamp\n'}]})
        self.assertEqual(result['records'][0]['text'], 'A Nice Lamp')

        result = import_data({'source':'usage','files':[{'text':json.dumps(
            [{'app':'Instagram','minutes':10,'date':'2024-01-01'}])}]})
        self.assertEqual(result['records'][0]['text'], 'Instagram: 10 minutes')

        result = import_data({'source':'x','files':[{'name':'search-history.js','text':
            'window.YTD.search_history.part0 = ' + json.dumps(
                [{'searchHistory':{'query':'gardening tips'}}]) + ';'}]})
        self.assertEqual(result['records'][0]['text'], 'gardening tips')

    def test_auto_detect_resolves_each_new_source_filename(self):
        cases = {
            'youtube': 'watch-history.json', 'instagram': 'your_topics.json',
            'spotify': 'Streaming_History_Audio_2024_1.json', 'reddit': 'posts.csv',
            'amazon': 'Retail.OrderHistory.1.csv', 'usage': 'usage.json',
            'tiktok': 'user_data.json', 'x': 'search-history.js',
        }
        for expected, name in cases.items():
            self.assertEqual(detect_source([{'name': name}]), expected, name)

    def test_sqlite_backup_reads_wal_and_empty_title_query(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'History'
            connection = sqlite3.connect(path)
            try:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('CREATE TABLE urls (url TEXT, title TEXT)')
                connection.execute("INSERT INTO urls VALUES ('https://example.com/?q=gardens', NULL)")
                connection.commit()
                result = _read_history(str(path), 'urls')
                self.assertEqual(result[0].text, 'gardens')
                self.assertEqual(connection.execute('SELECT count(*) FROM urls').fetchone()[0], 1)
            finally:
                connection.close()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = Server(('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        defaults = {'Content-Type':'application/json','X-Saint-Token':self.server.token}
        defaults.update(headers or {})
        connection.request(method, path, json.dumps(body) if body is not None else None, defaults)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, raw

    def test_sequential_imports_combine_into_one_session(self):
        self.request('POST', '/api/import', {'source': 'demo'})
        self.request('POST', '/api/import',
                     {'source': 'notes', 'files': [{'name': 'diary.md', 'text': 'Gardens and walks.'}]})
        state = json.loads(self.request('GET', '/api/state')[1])
        self.assertEqual(len(state['records']), 61)
        self.assertEqual({s['source']: s['count'] for s in state['sources']},
                         {'demo': 60, 'notes': 1})

    def test_failed_append_preserves_combined_session(self):
        self.request('POST', '/api/import', {'source': 'demo'})
        self.request('POST', '/api/import',
                     {'source': 'notes', 'files': [{'name': 'diary.md', 'text': 'Gardens and walks.'}]})
        status, _ = self.request('POST', '/api/import', {'source': 'notes', 'files': []})
        self.assertEqual(status, 400)
        state = json.loads(self.request('GET', '/api/state')[1])
        self.assertEqual(len(state['records']), 61)

    def test_auto_detect_routes_by_filename(self):
        status, body = self.request('POST', '/api/import',
            {'source': 'auto', 'files': [{'name': 'diary.md', 'text': 'Gardens and walks.'}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['detected_source'], 'notes')

    def test_auto_detect_ambiguous_files_rejected(self):
        status, body = self.request('POST', '/api/import',
            {'source': 'auto', 'files': [{'name': 'diary.md', 'text': 'x'}, {'name': 'posts.csv', 'text': 'x'}]})
        self.assertEqual(status, 400)
        self.assertIn('different sources', json.loads(body)['error'])

    def test_auto_detect_unrecognized_filename_rejected(self):
        status, body = self.request('POST', '/api/import',
            {'source': 'auto', 'files': [{'name': 'data.bin', 'text': 'x'}]})
        self.assertEqual(status, 400)
        self.assertIn('Choose one manually', json.loads(body)['error'])

    def test_import_clear_and_static_assets(self):
        for path in ('/', '/app.js', '/style.css'):
            self.assertEqual(self.request('GET', path)[0], 200)
        self.assertEqual(self.request('POST', '/api/import', {'source':'demo'})[0], 200)
        state = json.loads(self.request('GET', '/api/state')[1])
        self.assertEqual(len(state['records']), 60)
        self.assertEqual(self.request('POST', '/api/clear', {})[0], 200)
        self.assertEqual(json.loads(self.request('GET', '/api/state')[1])['records'], [])
        self.assertEqual(self.request('POST', '/api/analyze', {})[0], 400)

    def test_foreign_requests_and_malformed_input(self):
        self.assertEqual(self.request('GET','/api/state', headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('POST','/api/import',{'source':'demo'}, {'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('POST','/api/import',{'source':'demo'}, {'X-Saint-Token':'wrong'})[0],403)
        self.assertEqual(self.request('POST','/api/import',[])[0],400)
        self.assertEqual(self.request('GET','/../README.md')[0],404)

    def test_failed_import_preserves_current_session(self):
        self.request('POST', '/api/import', {'source':'demo'})
        status, _ = self.request('POST', '/api/import', {'source':'notes', 'files':[]})
        self.assertEqual(status, 400)
        self.assertEqual(len(json.loads(self.request('GET', '/api/state')[1])['records']), 60)

    def test_analysis_failure_is_recoverable(self):
        self.server.worker_busy = True
        with patch('app.semantic_map', side_effect=ImportError('missing dependency')):
            self.server.analyze(import_data({'source':'demo'}), 0)
        self.assertEqual(self.server.job['status'], 'error')
        self.assertFalse(self.server.worker_busy)
        self.assertIn('setup_local.py', self.server.job['error'])

    def test_finished_analysis_cannot_restore_cleared_data(self):
        snapshot = import_data({'source':'demo'})
        with patch('app.semantic_map', return_value={'points':[], 'stars':[]}):
            self.server.generation = 1
            self.server.worker_busy = True
            self.server.analyze(snapshot, 0)
        self.assertEqual(self.server.snapshot['records'], [])
        self.assertFalse(self.server.worker_busy)
        self.assertIsNone(self.server.snapshot['map'])
