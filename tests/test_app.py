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
        result = import_data({'source':'tiktok','files':[{'text':json.dumps(
            {'Ads and data': {'Ad Interests': {'AdInterestCategories': 'Gardening'}}})}]})
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

        result = import_data({'source':'x','files':[{'name':'saved-search.js','text':
            'window.YTD.saved_search.part0 = ' + json.dumps(
                [{'savedSearch':{'savedSearchId':'1','query':'gardening tips'}}]) + ';'}]})
        self.assertEqual(result['records'][0]['text'], 'gardening tips')

    def test_auto_detect_resolves_each_new_source_filename(self):
        cases = [
            ('youtube', 'watch-history.json'), ('youtube', 'watch-history.html'),
            ('instagram', 'your_topics.json'), ('instagram', 'recommended_topics.json'),
            ('spotify', 'Streaming_History_Audio_2024_1.json'), ('spotify', 'StreamingHistory_podcast_0.json'),
            ('spotify', 'SearchQueries.json'), ('spotify', 'Inferences.json'),
            ('reddit', 'posts.csv'), ('amazon', 'Retail.OrderHistory.1.csv'), ('amazon', 'Order History.csv'),
            ('usage', 'usage.json'), ('tiktok', 'user_data.json'), ('tiktok', 'user_data_tiktok.json'),
            ('x', 'saved-search.js'),
        ]
        for expected, name in cases:
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

    def test_reimporting_the_same_export_adds_nothing(self):
        export = {'source': 'tiktok', 'files': [{'text': json.dumps({
            'Your Activity': {'Searches': {'SearchList': [{'Date': '2024-01-01 00:00:00', 'SearchTerm': 'gardens'}]},
                              'Watch History': {'VideoList': [{'Date': '2024-01-01 00:00:00',
                                                               'Link': 'https://www.tiktokv.com/share/video/1/'}]}},
            'Ads and data': {'Ad Interests': {'AdInterestCategories': 'Gardening'}}})}]}
        first = json.loads(self.request('POST', '/api/import', export)[1])
        self.assertEqual((first['changed'], first['added'], first['skipped']), (True, 1, 0))
        self.server.snapshot['map'] = {'points': [], 'stars': []}  # a finished map survives a no-op import
        generation = self.server.generation
        again = json.loads(self.request('POST', '/api/import', export)[1])
        self.assertEqual((again['changed'], again['added'], again['skipped']), (False, 0, 1))
        self.assertEqual(self.server.generation, generation)
        state = json.loads(self.request('GET', '/api/state')[1])
        self.assertEqual((len(state['records']), state['categories'], state['watches']), (1, ['Gardening'], 1))
        self.assertEqual(state['sources'], [{'source': 'tiktok', 'count': 1}])
        self.assertIsNotNone(state['map'])
        self.assertNotIn('watch_times', state)
        self.assertEqual(state['records'][0]['origin'], 'tiktok')

    def test_overlapping_export_adds_only_new_entries(self):
        def youtube(*titles):
            return {'source': 'youtube', 'files': [{'text': json.dumps(
                [{'title': t, 'time': '2024-01-01T00:00:00Z'} for t in titles])}]}
        self.request('POST', '/api/import', youtube('Searched for gardens', 'Watched Roses'))
        result = json.loads(self.request('POST', '/api/import',
                                         youtube('Searched for gardens', 'Watched Roses', 'Watched Tulips'))[1])
        self.assertEqual((result['added'], result['skipped']), (1, 2))
        state = json.loads(self.request('GET', '/api/state')[1])
        self.assertEqual([r['text'] for r in state['records']], ['gardens', 'Roses', 'Tulips'])
        self.assertIsNone(state['map'])

    def test_repeated_passages_within_one_file_are_kept_once_per_repeat(self):
        note = {'source': 'notes', 'files': [{'name': 'diary.md', 'text': 'Coffee.\n\nCoffee.'}]}
        self.request('POST', '/api/import', note)
        self.request('POST', '/api/import', note)
        state = json.loads(self.request('GET', '/api/state')[1])
        self.assertEqual(len(state['records']), 2)

    def test_youtube_html_history_gets_a_clear_message(self):
        with self.assertRaisesRegex(ValueError, 'HTML version'):
            import_data({'source': 'youtube', 'files': [{'name': 'watch-history.html', 'text': '<html><body>'}]})

    def test_auto_detect_uses_the_whole_selection_when_files_arrive_one_at_a_time(self):
        status, body = self.request('POST', '/api/import', {
            'source': 'auto', 'names': ['diary.md', 'posts.csv'],
            'files': [{'name': 'diary.md', 'text': 'Gardens and walks.'}]})
        self.assertEqual(status, 400)
        self.assertIn('different sources', json.loads(body)['error'])

    def test_the_same_song_in_two_history_files_is_one_passage(self):
        def history(ts):
            return {'source': 'spotify', 'files': [{'name': 'Streaming_History_Audio_0.json', 'text': json.dumps(
                [{'ts': ts, 'ms_played': 200000, 'master_metadata_track_name': 'Weather',
                  'master_metadata_album_artist_name': 'The National'}])}]}
        self.request('POST', '/api/import', history('2024-01-01T00:00:00Z'))
        result = json.loads(self.request('POST', '/api/import', history('2025-06-01T00:00:00Z'))[1])
        self.assertEqual((result['changed'], result['skipped']), (False, 1))

    def test_session_keeps_the_newest_passages_that_fit(self):
        with patch('app.MAX_RECORDS', 3):
            rows = [{'app': 'Notes', 'minutes': i, 'date': f'2024-01-0{i}'} for i in range(1, 6)]
            result = json.loads(self.request('POST', '/api/import', {'source': 'usage', 'files': [
                {'name': 'usage.json', 'text': json.dumps(rows)}]})[1])
            self.assertEqual((result['added'], result['dropped']), (3, 2))
            state = json.loads(self.request('GET', '/api/state')[1])
            self.assertEqual([r['text'] for r in state['records']],
                             ['Notes: 3 minutes', 'Notes: 4 minutes', 'Notes: 5 minutes'])
            full = json.loads(self.request('POST', '/api/import', {'source': 'demo'})[1])
            self.assertEqual((full['changed'], full['added'], full['dropped']), (False, 0, 60))

    def test_newest_passages_win_whatever_order_a_source_arrives_in(self):
        def usage(*days):
            return {'source': 'usage', 'files': [{'name': 'usage.json', 'text': json.dumps(
                [{'app': 'Notes', 'minutes': day, 'date': f'2024-01-{day:02d}'} for day in days])}]}
        with patch('app.MAX_RECORDS', 3):
            self.request('POST', '/api/import', usage(4, 5, 6))
            older = json.loads(self.request('POST', '/api/import', usage(1, 2, 3))[1])
            self.assertEqual((older['changed'], older['added'], older['dropped']), (False, 0, 3))
            newer = json.loads(self.request('POST', '/api/import', usage(7, 8))[1])
            self.assertEqual((newer['changed'], newer['added'], newer['dropped']), (True, 2, 2))  # days 4 and 5 make room
            state = json.loads(self.request('GET', '/api/state')[1])
            self.assertEqual([r['text'] for r in state['records']],
                             ['Notes: 6 minutes', 'Notes: 7 minutes', 'Notes: 8 minutes'])
            self.assertEqual(state['sources'], [{'source': 'usage', 'count': 3}])

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
