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
from youtube import load_blobs as load_youtube_blobs
from instagram import load_blobs as load_instagram_blobs
from spotify import load_blobs as load_spotify_blobs
from reddit import load_rows as load_reddit_rows
from amazon import load_rows as load_amazon_rows
from usage import load_blobs as load_usage_blobs
from x import load_files as load_x_files

ASSETS = Path(__file__).parent / 'web'
MAX_REQUEST = 8_000_000
STOP = set('the and that this with from have was were are for but not you your my our had has will into just about they them then when what some more been very can'.split())

# Auto-detect: filename patterns for the standard export tools ship, checked in order.
# A file that matches none of these needs a manual pick from the dropdown -- auto-detect
# never guesses from content alone, only from names these tools consistently use.
_SOURCE_FILENAME_PATTERNS = [
    (re.compile(r'\.(md|markdown|txt)$', re.I), 'notes'),
    (re.compile(r'(watch-history|search-history)\.json$', re.I), 'youtube'),
    (re.compile(r'your_topics|word_or_phrase_searches|ads_viewed|ads_and_topics', re.I), 'instagram'),
    (re.compile(r'streaming_history_audio|streaminghistory', re.I), 'spotify'),
    (re.compile(r'posts\.csv$|comments\.csv$', re.I), 'reddit'),
    (re.compile(r'retail\.orderhistory', re.I), 'amazon'),
    (re.compile(r'usage|screen.?time', re.I), 'usage'),
    (re.compile(r'user_data\.json$|tiktok', re.I), 'tiktok'),
    (re.compile(r'\.js$', re.I), 'x'),
]


def detect_source(files):
    if not isinstance(files, list) or not files:
        raise ValueError('Choose one or more files to auto-detect a source.')
    guesses = set()
    for file in files:
        name = file.get('name', '') if isinstance(file, dict) else ''
        for pattern, source in _SOURCE_FILENAME_PATTERNS:
            if pattern.search(name):
                guesses.add(source)
                break
    if len(guesses) == 1:
        return guesses.pop()
    if not guesses:
        raise ValueError('Could not detect a source from these filenames. '
                         'Choose one manually from the dropdown.')
    raise ValueError(f"These files look like different sources ({', '.join(sorted(guesses))}). "
                     'Import one source at a time.')


def word_terms(record_dicts):
    words = Counter(word for r in record_dicts for word in re.findall(r"[^\W\d_]{3,}", r['text'].lower())
                    if word not in STOP)
    return words.most_common(16)


def summarize(records, categories=(), watches=0):
    record_dicts = [asdict(r) for r in records]
    return {'records': record_dicts, 'categories': list(categories), 'watches': watches,
            'terms': word_terms(record_dicts), 'map': None, 'sources': []}


def merge_snapshot(existing, incoming, source_name):
    records = existing['records'] + incoming['records']
    if len(records) > MAX_RECORDS:
        raise ValueError('Combined session exceeds 5,000 passages. Clear the session or import fewer files.')
    sources = [dict(entry) for entry in existing.get('sources', [])]
    added = len(incoming['records'])
    for entry in sources:
        if entry['source'] == source_name:
            entry['count'] += added
            break
    else:
        sources.append({'source': source_name, 'count': added})
    return {'records': records,
            'categories': existing['categories'] + incoming['categories'],
            'watches': existing['watches'] + incoming['watches'],
            'terms': word_terms(records), 'map': None, 'sources': sources}


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
    elif source == 'youtube':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose YouTube Takeout JSON export files.')
        try:
            blobs = [json.loads(f['text'].lstrip('﻿')) for f in files]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Each YouTube file must contain valid JSON.') from exc
        records, categories, watches = load_youtube_blobs(blobs), [], 0
    elif source == 'instagram':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose Instagram JSON export files.')
        try:
            blobs = [json.loads(f['text'].lstrip('﻿')) for f in files]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Each Instagram file must contain valid JSON.') from exc
        records, categories, watches = (*load_instagram_blobs(blobs), 0)
    elif source == 'spotify':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose Spotify streaming history JSON files.')
        try:
            blobs = [json.loads(f['text'].lstrip('﻿')) for f in files]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Each Spotify file must contain valid JSON.') from exc
        records, categories, watches = load_spotify_blobs(blobs), [], 0
    elif source == 'reddit':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose Reddit posts.csv / comments.csv export files.')
        records, categories, watches = load_reddit_rows(files), [], 0
    elif source == 'amazon':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose an Amazon order history CSV file.')
        records, categories, watches = load_amazon_rows(files), [], 0
    elif source == 'usage':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose a usage.json screen-time export file.')
        try:
            blobs = [json.loads(f['text'].lstrip('﻿')) for f in files]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Each usage file must contain valid JSON.') from exc
        records, categories, watches = load_usage_blobs(blobs), [], 0
    elif source == 'x':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose X/Twitter export .js files.')
        records, categories, watches = (*load_x_files(files), 0)
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
    import signal_score as sig
    texts = [r['text'] for r in snapshot['records']]
    timestamps = [r.get('when') for r in snapshot['records']]
    vectors = embed(texts, offline=True)
    mid = umap.UMAP(n_components=15, metric='cosine', random_state=7).fit_transform(vectors)
    estimator = hdbscan.HDBSCAN(min_cluster_size=10, min_samples=3).fit(mid)
    reducer = umap.UMAP(n_components=2, metric='cosine', random_state=7).fit(vectors)

    # Local "profile health" scoring -- same mid-dimensional vectors used for clustering,
    # never the 2D display projection. See signal_score.py for what each term means and
    # what's deliberately not implemented yet.
    labels = estimator.labels_
    anchor = sig.anchor_strength(mid, labels)
    divergence = sig.divergence_penalty(mid, labels)
    recency = sig.recency_decay(timestamps)
    noise = sig.noise_exposure(estimator.outlier_scores_, labels)
    iws = sig.injection_weight(anchor, divergence, recency, noise)
    snr = sig.signal_to_noise(iws)
    homogenization = sig.homogenization_index(labels)
    health = {'snr': snr, 'homogenization': homogenization,
              'profile_health': sig.profile_health(snr, homogenization)}

    stars = []
    if snapshot['categories']:
        cv = embed(snapshot['categories'], offline=True)
        positions = reducer.transform(cv)
        # Cosine similarity in the original embedding space, not the 2D picture.
        nearest = np.argmax(cv @ vectors.T, axis=1)
        stars = [{'text': text, 'x': float(pos[0]), 'y': float(pos[1]),
                  'nearest': texts[int(index)], 'similarity': float(cv[i] @ vectors[index])}
                 for i, (text, pos, index) in enumerate(zip(snapshot['categories'], positions, nearest))]
    points = [{'x': float(pos[0]), 'y': float(pos[1]), 'label': int(label),
               'outlier': float(score) if np.isfinite(score) else None,
               'iws': float(w), 'signal': bool(w >= 0.35)}
              for pos, label, score, w in zip(reducer.embedding_, labels, estimator.outlier_scores_, iws)]
    return {'points': points, 'stars': stars, 'health': health}


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
                source = data.get('source')
                resolved = detect_source(data.get('files')) if source == 'auto' else source
                incoming = import_data({**data, 'source': resolved})
                with self.server.lock:
                    merged = merge_snapshot(self.server.snapshot, incoming, resolved)
                    self.server.generation += 1
                    self.server.snapshot = merged
                    self.server.job = {'status': 'idle'}
                self.reply(200, {'ok': True, 'detected_source': resolved})
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
