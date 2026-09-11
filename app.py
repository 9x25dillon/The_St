#!/usr/bin/env python3
"""Loopback-only, in-memory interface for The Saint. Standard library only."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading

from mirror import Record, read_chrome, read_firefox
from notes import parse_note, MAX_RECORDS
from tiktok import load_blobs

ASSETS = Path(__file__).parent / 'web'
MAX_REQUEST = 8_000_000
STOP = set('the and that this with from have was were are for but not you your my our had has will into just about they them then when what some more been very can'.split())


def summarize(records, categories=(), watches=0):
    words = Counter(word for r in records for word in re.findall(r"[^\W\d_]{3,}", r.text.lower())
                    if word not in STOP)
    return {'records': [asdict(r) for r in records], 'categories': list(categories),
            'watches': watches, 'terms': words.most_common(16), 'map': None}


def import_data(data):
    source = data.get('source')
    categories, watches = [], 0
    if source == 'notes':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose between 1 and 500 note files.')
        records = []
        for file in files:
            if not isinstance(file, dict) or not isinstance(file.get('name'), str) or not isinstance(file.get('text'), str):
                raise ValueError('Invalid note file.')
            records.extend(parse_note(file['name'], file['text']))
            if len(records) > MAX_RECORDS:
                raise ValueError('Choose fewer notes (maximum 5,000 passages).')
    elif source == 'tiktok':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose JSON export files.')
        try:
            blobs = [json.loads(f['text'].lstrip('\ufeff')) for f in files]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Each TikTok file must contain valid JSON.') from exc
        export = load_blobs(blobs)
        records, categories, watches = export.expressed, export.ad_categories, len(export.watch_times)
    elif source in ('firefox', 'chrome'):
        records = (read_firefox if source == 'firefox' else read_chrome)()
    elif source == 'demo':
        themes = [('Garden', 'I planted tomatoes and watered the herbs. The garden feels peaceful.'),
                  ('Music', 'I practiced piano chords and recorded a new melody.'),
                  ('Walking', 'I walked through the woods and enjoyed the quiet trail.'),
                  ('Making', 'I built a small wooden shelf and sketched my next project.'),
                  ('Friends', 'I cooked dinner with friends and enjoyed our conversation.')]
        records = [Record(f'{text} Reflection {i + 1}.', 'note', f'Sample journal · {title}')
                   for title, text in themes for i in range(12)]
    else:
        raise ValueError('Unknown source.')
    if not records and not categories and not watches:
        raise ValueError('No usable entries found in this source.')
    if len(records) > MAX_RECORDS:
        raise ValueError('Source exceeds 5,000 passages. Choose a smaller export.')
    return summarize(records, categories, watches)


def semantic_map(snapshot):
    import numpy as np
    import umap
    import hdbscan
    from mirror import embed
    texts = [r['text'] for r in snapshot['records']]
    vectors = embed(texts, offline=True)
    mid = umap.UMAP(n_components=15, metric='cosine', random_state=7).fit_transform(vectors)
    estimator = hdbscan.HDBSCAN(min_cluster_size=10, min_samples=3).fit(mid)
    reducer = umap.UMAP(n_components=2, metric='cosine', random_state=7).fit(vectors)
    stars = []
    if snapshot['categories']:
        cv = embed(snapshot['categories'], offline=True)
        positions = reducer.transform(cv)
        # Cosine similarity in the original embedding space, not the 2D picture.
        nearest = np.argmax(cv @ vectors.T, axis=1)
        stars = [{'text': text, 'x': float(pos[0]), 'y': float(pos[1]),
                  'nearest': texts[int(index)], 'similarity': float(cv[i] @ vectors[index])}
                 for i, (text, pos, index) in enumerate(zip(snapshot['categories'], positions, nearest))]
    return {'points': [{'x': float(pos[0]), 'y': float(pos[1]), 'label': int(label),
                        'outlier': float(score) if np.isfinite(score) else None}
                       for pos, label, score in zip(reducer.embedding_, estimator.labels_, estimator.outlier_scores_)],
            'stars': stars}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.snapshot = summarize([])
        self.job = {'status': 'idle'}
        self.generation = 0
        self.worker_busy = False

    def analyze(self, snapshot, generation):
        try:
            result = semantic_map(snapshot)
            error = None
        except Exception as exc:
            result = None
            error = ('Semantic analysis could not run. Run python setup_local.py, then '
                     'python setup_local.py --download-model, and restart the app. '
                     f'Failure: {type(exc).__name__}.')
        with self.lock:
            self.worker_busy = False
            if generation == self.generation:
                if error:
                    self.job = {'status': 'error', 'error': error}
                else:
                    self.snapshot['map'] = result
                    self.job = {'status': 'done'}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass  # No personal data or request paths in logs.

    def reply(self, status, body, content_type='application/json'):
        raw = json.dumps(body).encode() if content_type == 'application/json' else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def allowed(self):
        expected = f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected:
            self.reply(403, {'error': 'Use the printed 127.0.0.1 address.'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != f'http://{expected}':
            self.reply(403, {'error': 'Cross-origin request rejected.'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.reply(403, {'error': 'Cross-site request rejected.'})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == '/api/state':
            with self.server.lock:
                self.reply(200, {'token': self.server.token, **self.server.snapshot,
                                 'job': self.server.job,
                                 'semantic_installed': all(importlib.util.find_spec(m) is not None
                                     for m in ('numpy', 'umap', 'hdbscan', 'sentence_transformers'))})
        elif self.path in ('/', '/app.js', '/style.css'):
            filename, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                              '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                              '/style.css': ('style.css', 'text/css; charset=utf-8')}[self.path]
            self.reply(200, (ASSETS / filename).read_bytes(), mime)
        else:
            self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        if not self.allowed():
            return
        if not secrets.compare_digest(self.headers.get('X-Saint-Token', ''), self.server.token):
            return self.reply(403, {'error': 'Session expired. Reload this page.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_REQUEST:
                return self.reply(413, {'error': 'Choose a smaller import (maximum 8 MB). '})
            if self.headers.get('Content-Type') != 'application/json':
                return self.reply(415, {'error': 'JSON required.'})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object.')
            if self.path == '/api/import':
                snapshot = import_data(data)
                with self.server.lock:
                    self.server.generation += 1
                    self.server.snapshot = snapshot
                    self.server.job = {'status': 'idle'}
                self.reply(200, {'ok': True})
            elif self.path == '/api/clear':
                with self.server.lock:
                    self.server.generation += 1
                    self.server.snapshot = summarize([])
                    self.server.job = {'status': 'idle'}
                self.reply(200, {'ok': True})
            elif self.path == '/api/analyze':
                with self.server.lock:
                    if self.server.worker_busy:
                        return self.reply(409, {'error': 'An analysis is still running. Please wait.'})
                    if len(self.server.snapshot['records']) < 30:
                        raise ValueError('Import at least 30 passages for semantic clustering.')
                    self.server.worker_busy = True
                    self.server.job = {'status': 'running'}
                    threading.Thread(target=self.server.analyze,
                                     args=(self.server.snapshot, self.server.generation), daemon=True).start()
                self.reply(202, {'ok': True})
            else:
                self.reply(404, {'error': 'Not found'})
        except (ValueError, OSError, RecursionError, sqlite3.Error) as exc:
            self.reply(400, {'error': str(exc)})
        except Exception:
            self.reply(500, {'error': 'Import failed. Check the selected files and try again.'})


def main():
    parser = argparse.ArgumentParser(description='The Saint — local personal data explorer')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    try:
        server = Server(('127.0.0.1', args.port))
    except (OSError, OverflowError) as exc:
        parser.exit(1, f'Cannot start server: {exc}. Try --port 8766.\n')
    print(f'The Saint is ready at http://127.0.0.1:{server.server_port}', flush=True)
    print('Imports stay in process memory. Ctrl+C stops the server.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
