#!/usr/bin/env python3
"""Loopback-only, in-memory interface for The Saint. Standard library only."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import math
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
MAX_REQUEST = 64_000_000  # one file (up to 32 MB) per request, with room for JSON escaping; real Spotify history files run ~12.8 MB
MAX_IMPORT_RECORDS = 100_000  # parsing safety bound; the session itself keeps MAX_RECORDS
STOP = set('the and that this with from have was were are for but not you your my our had has will into just about they them then when what some more been very can '
           'there their these those would could should didn doesn don isn wasn aren weren also than only which who how why where because while'.split())
# Links and @handles are URL fragments and other people's names, not words from the passage.
NOT_WORDS = re.compile(r"https?://\S+|@\w+")
WORD = re.compile(r"[^\W\d_]{3,}")

# Auto-detect: filename patterns for the standard export tools ship, checked in order.
# A file that matches none of these needs a manual pick from the dropdown -- auto-detect
# never guesses from content alone, only from names these tools consistently use.
_SOURCE_FILENAME_PATTERNS = [
    (re.compile(r'\.(md|markdown|txt)$', re.I), 'notes'),
    (re.compile(r'(watch-history|search-history)\.(json|html)$', re.I), 'youtube'),
    (re.compile(r'your_topics|recommended_topics|word_or_phrase_searches|ads_viewed|ads_and_topics', re.I), 'instagram'),
    (re.compile(r'streaming_history_audio|streaminghistory|searchqueries\.json$|inferences\.json$', re.I), 'spotify'),
    (re.compile(r'posts\.csv$|comments\.csv$', re.I), 'reddit'),
    (re.compile(r'retail\.orderhistory|order history\.csv$', re.I), 'amazon'),
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


def words(text):
    return [word for word in WORD.findall(NOT_WORDS.sub(' ', text).lower()) if word not in STOP]


def word_terms(record_dicts):
    return Counter(word for r in record_dicts for word in words(r['text'])).most_common(16)


# The four multiplied factors of a passage's score (signal_score.injection_weight), in the order
# the page lists them; ties for "lowest" go to the earlier one.
FACTORS = ('fit', 'clarity', 'recency', 'typical')


def held_back_by(factor_rows, signals):
    """For passages flagged as noise, how many have each factor as their lowest -- what most
    often keeps passages below the signal threshold. [(factor, count)], most common first."""
    lowest = Counter(min(FACTORS, key=lambda name: row[name])
                     for row, signal in zip(factor_rows, signals) if not signal)
    return sorted(lowest.items(), key=lambda item: (-item[1], FACTORS.index(item[0])))


def _month(ts):
    moment = datetime.fromtimestamp(ts, tz=timezone.utc)
    return moment.year * 12 + moment.month - 1


def activity_axis(records, max_buckets=60):
    """Shared time axis for island activity charts: the session's dated span in whole months,
    grouped into at most `max_buckets` equal buckets. None when nothing is dated."""
    months = [_month(r['when']) for r in records if r.get('when') is not None]
    if not months:
        return None
    first, span = min(months), max(months) - min(months) + 1
    size = -(-span // max_buckets)
    return {'first_month': first, 'bucket_months': size, 'buckets': -(-span // size)}


def recent_trend(island_times, session_times, min_dated=10):
    """Is this island a bigger or smaller part of the most recent quarter of the session's
    dated history than of the session overall? Compared with the session's own recent share,
    so an export that simply has more recent data doesn't make every island look like it's
    growing. None when there are too few dated passages to say."""
    if len(island_times) < min_dated or len(session_times) < 2 or max(session_times) <= min(session_times):
        return None
    start, end = min(session_times), max(session_times)
    since = end - (end - start) / 4
    island_recent = sum(t >= since for t in island_times) / len(island_times)
    session_recent = sum(t >= since for t in session_times) / len(session_times)
    ratio = island_recent / session_recent
    return {'label': 'growing' if ratio >= 1.5 else 'fading' if ratio <= 0.5 else 'steady',
            'island_recent': island_recent, 'session_recent': session_recent, 'since': since}


def island_summaries(records, labels, centrality, signals, stars=(), top_terms=6, top_passages=3):
    """Describe each behavioral island for the page, largest first. Pure Python.

    labels: HDBSCAN label per record (-1 = unclustered). centrality: per record, how close it
    sits to its island's typical meaning (higher = more representative). signals: per-record
    signal flag. stars: (assigned label, index of its closest passage) pairs.

    Words are c-TF-IDF: how much of this island's wording a word makes up, weighted by how
    rare the word is across all passages (unclustered ones included), counting only words in
    2+ of the island's passages. Checked on the sample journal and a real 515-tweet archive:
    plain frequency headlined words every island shares and one-off link fragments; this
    doesn't. Ties keep the order words first appear, so a card reads like its passages."""
    counts, passages_with, first_seen, total = {}, {}, {}, Counter()
    for label, passage_words in zip(labels, (words(r['text']) for r in records)):
        counts.setdefault(label, Counter()).update(passage_words)
        passages_with.setdefault(label, Counter()).update(set(passage_words))
        for word in passage_words:
            first_seen.setdefault((label, word), len(first_seen))
        total.update(passage_words)
    average_words = sum(total.values()) / max(len(counts), 1)
    members = {}
    for index, label in enumerate(labels):
        if label != -1:
            members.setdefault(label, []).append(index)
    axis = activity_axis(records)
    session_times = [r['when'] for r in records if r.get('when') is not None]
    summaries = []
    for label, indices in members.items():
        size = sum(counts[label].values()) or 1
        scored = [(word, n / size * math.log(1 + average_words / total[word]))
                  for word, n in counts[label].items() if passages_with[label][word] >= 2]
        scored.sort(key=lambda item: (-round(item[1], 12), first_seen[(label, item[0])]))
        dated = [records[i]['when'] for i in indices if records[i].get('when') is not None]
        activity = None
        if axis and dated:
            activity = [0] * axis['buckets']
            for ts in dated:
                activity[(_month(ts) - axis['first_month']) // axis['bucket_months']] += 1
        summaries.append({
            'label': label, 'size': len(indices), 'share': len(indices) / len(records),
            'terms': [word for word, _ in scored[:top_terms]],
            'sources': Counter(records[i].get('origin') or 'unknown' for i in indices).most_common(),
            'kinds': Counter(records[i]['source'] for i in indices).most_common(),
            'signal_share': sum(1 for i in indices if signals[i]) / len(indices),
            'first_when': min(dated) if dated else None, 'last_when': max(dated) if dated else None,
            'activity': activity, 'trend': recent_trend(dated, session_times),
            'central': sorted(indices, key=lambda i: -centrality[i])[:top_passages],
            'labels': sorted({text for text, index in stars if labels[index] == label}),
        })
    summaries.sort(key=lambda island: (-island['size'], island['label']))
    return summaries


def summarize(records, categories=(), watch_times=()):
    record_dicts = [asdict(r) for r in records]
    # watch_times stays server-side (see PRIVATE_SNAPSHOT_KEYS); clients only need the count.
    return {'records': record_dicts, 'categories': sorted(set(categories)), 'watches': len(watch_times),
            'watch_times': list(watch_times), 'terms': word_terms(record_dicts), 'map': None, 'sources': []}


PRIVATE_SNAPSHOT_KEYS = {'watch_times'}


# Sources whose entries are told apart by date as well as text: a usage row is one app on one
# day, so "Instagram: 47 minutes" on two days is two entries.
DATED_IDENTITY_SOURCES = {'usage'}


def _record_key(record):
    # Every other adapter already collapses repeats of the same text within an import and keeps
    # the first time it saw them; matching that here means the same song arriving in two
    # Spotify history files (sent as separate imports) is still one passage.
    when = record['when'] if record.get('origin') in DATED_IDENTITY_SOURCES else None
    return record.get('origin'), record['source'], record['text'].lower(), record['detail'], when


def _not_already_present(existing, incoming, key):
    """Incoming items not already covered by existing, compared as multisets: an item that
    appears n times in the session absorbs up to n copies from an import. Re-importing the
    same export adds nothing, an overlapping newer export adds only what's new, and a
    passage that legitimately repeats within one file keeps its repeats."""
    remaining = Counter(key(item) for item in existing)
    added = []
    for item in incoming:
        k = key(item)
        if remaining[k]:
            remaining[k] -= 1
        else:
            added.append(item)
    return added


def _newest(records, count):
    """The `count` most recent records, in their original order. Undated records rank after
    dated ones and keep file order among themselves."""
    ranked = sorted(range(len(records)), key=lambda i: (records[i]['when'] is not None, records[i]['when'] or 0),
                    reverse=True)
    return [records[i] for i in sorted(ranked[:count])]


def merge_snapshot(existing, incoming, source_name):
    """Combine an import into the session. Returns (snapshot, added, skipped, dropped): skipped
    entries were already in the session; dropped counts this source's older passages (new or
    already present) left out to stay under MAX_RECORDS. When the import brings nothing new,
    snapshot is `existing` itself, so a finished map is kept rather than discarded and rebuilt."""
    tagged = [{**record, 'origin': source_name} for record in incoming['records']]
    new_records = _not_already_present(existing['records'], tagged, _record_key)
    skipped = len(tagged) - len(new_records)
    records = existing['records'] + new_records
    dropped = 0
    if len(records) > MAX_RECORDS:
        # Trim this source's passages -- already-present and incoming together -- to the newest
        # that fit, so a multi-file export lands on the same result in any file order. Other
        # sources' passages are never evicted by this import.
        same = [r for r in records if r.get('origin') == source_name]
        capacity = max(MAX_RECORDS - (len(records) - len(same)), 0)
        kept = {id(r) for r in _newest(same, capacity)}
        dropped = len(same) - len(kept)
        records = [r for r in records if r.get('origin') != source_name or id(r) in kept]
        new_records = [r for r in new_records if id(r) in kept]
    new_watches = _not_already_present(existing['watch_times'], incoming['watch_times'], lambda t: t)
    categories = sorted(set(existing['categories']) | set(incoming['categories']))
    added = len(new_records)
    if not new_records and not new_watches and len(categories) == len(existing['categories']):
        return existing, 0, skipped, dropped
    sources = [dict(entry) for entry in existing.get('sources', [])]
    count = sum(1 for r in records if r.get('origin') == source_name)
    for entry in sources:
        if entry['source'] == source_name:
            entry['count'] = count
            break
    else:
        sources.append({'source': source_name, 'count': count})
    watch_times = existing['watch_times'] + new_watches
    return ({'records': records, 'categories': categories, 'watches': len(watch_times),
             'watch_times': watch_times, 'terms': word_terms(records), 'map': None, 'sources': sources},
            added, skipped, dropped)


def import_data(data):
    source = data.get('source')
    categories, watch_times = [], []
    if source == 'notes':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose between 1 and 500 note files.')
        records = []
        for file in files:
            if not isinstance(file, dict) or not isinstance(file.get('name'), str) or not isinstance(file.get('text'), str):
                raise ValueError('Invalid note file.')
            records.extend(parse_note(file['name'], file['text']))
            if len(records) > MAX_IMPORT_RECORDS:
                raise ValueError('Choose fewer notes at once.')
    elif source == 'tiktok':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose JSON export files.')
        try:
            blobs = [json.loads(f['text'].lstrip('\ufeff')) for f in files]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Each TikTok file must contain valid JSON.') from exc
        export = load_blobs(blobs)
        records, categories, watch_times = export.expressed, export.ad_categories, export.watch_times
    elif source == 'youtube':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose YouTube Takeout JSON export files.')
        if any(isinstance(f, dict) and isinstance(f.get('text'), str) and f['text'].lstrip().startswith('<') for f in files):
            raise ValueError('This is the HTML version of your YouTube history. Export it again from '
                             'Google Takeout with the history format set to JSON.')
        try:
            blobs = [json.loads(f['text'].lstrip('﻿')) for f in files]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Each YouTube file must contain valid JSON.') from exc
        records = load_youtube_blobs(blobs)
    elif source == 'instagram':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose Instagram JSON export files.')
        try:
            blobs = [json.loads(f['text'].lstrip('﻿')) for f in files]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Each Instagram file must contain valid JSON.') from exc
        records, categories = load_instagram_blobs(blobs)
    elif source == 'spotify':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose Spotify streaming history JSON files.')
        try:
            blobs = [json.loads(f['text'].lstrip('﻿')) for f in files]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Each Spotify file must contain valid JSON.') from exc
        records, categories = load_spotify_blobs(blobs)
    elif source == 'reddit':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose Reddit posts.csv / comments.csv export files.')
        records = load_reddit_rows(files)
    elif source == 'amazon':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose an Amazon order history CSV file.')
        records = load_amazon_rows(files)
    elif source == 'usage':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose a usage.json screen-time export file.')
        try:
            blobs = [json.loads(f['text'].lstrip('﻿')) for f in files]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('Each usage file must contain valid JSON.') from exc
        records = load_usage_blobs(blobs)
    elif source == 'x':
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('Choose X/Twitter export .js files.')
        records, categories = load_x_files(files)
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
    if not records and not categories and not watch_times:
        raise ValueError('No usable entries found in this source.')
    if len(records) > MAX_IMPORT_RECORDS:
        raise ValueError('This file has more entries than the app can read at once. Choose a smaller export file.')
    return summarize(records, categories, watch_times)


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
    recency = sig.usage_peak_recency(timestamps, [r.get('origin') for r in snapshot['records']])
    noise = sig.noise_exposure(estimator.outlier_scores_, labels)
    iws = sig.injection_weight(anchor, divergence, recency, noise)
    signals = [bool(w >= sig.SIGNAL_THRESHOLD) for w in iws]
    # Per-passage factors (as measured, before softening) so the page can say why a passage is
    # or isn't signal; the page applies health['floor'] to show what each one counts as.
    factor_rows = [{'fit': round(float(a), 3), 'clarity': round(float(d), 3), 'recency': round(float(r), 3),
                    'typical': round(float(1 - n), 3)} for a, d, r, n in zip(anchor, divergence, recency, noise)]
    snr = sig.signal_to_noise(iws)
    homogenization = sig.homogenization_index(labels)
    health = {'snr': snr, 'homogenization': homogenization,
              'profile_health': sig.profile_health(snr, homogenization),
              'threshold': sig.SIGNAL_THRESHOLD, 'floor': sig.FACTOR_FLOOR,
              'held_back_by': held_back_by(factor_rows, signals)}

    # How typical each passage is of its island: cosine similarity to the island's mean
    # embedding, in the original embedding space (like the star matching below), not in the
    # 15D or 2D projections. Unclustered passages get -1.
    centrality = np.full(len(texts), -1.0)
    for label in set(labels.tolist()) - {-1}:
        members = labels == label
        center = vectors[members].mean(axis=0)
        centrality[members] = vectors[members] @ (center / (np.linalg.norm(center) or 1.0))

    stars, star_pairs = [], []
    if snapshot['categories']:
        cv = embed(snapshot['categories'], offline=True)
        positions = reducer.transform(cv)
        # Cosine similarity in the original embedding space, not the 2D picture.
        nearest = np.argmax(cv @ vectors.T, axis=1)
        stars = [{'text': text, 'x': float(pos[0]), 'y': float(pos[1]),
                  'nearest': texts[int(index)], 'similarity': float(cv[i] @ vectors[index])}
                 for i, (text, pos, index) in enumerate(zip(snapshot['categories'], positions, nearest))]
        star_pairs = list(zip(snapshot['categories'], nearest.tolist()))
    points = [{'x': float(pos[0]), 'y': float(pos[1]), 'label': int(label),
               'outlier': float(score) if np.isfinite(score) else None,
               'iws': float(w), 'signal': signal, 'factors': factors}
              for pos, label, score, w, signal, factors
              in zip(reducer.embedding_, labels, estimator.outlier_scores_, iws, signals, factor_rows)]
    islands = island_summaries(snapshot['records'], labels.tolist(), centrality.tolist(), signals, star_pairs)
    return {'points': points, 'stars': stars, 'health': health, 'islands': islands,
            'unclustered': int((labels == -1).sum()), 'activity_axis': activity_axis(snapshot['records'])}


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
                public = {k: v for k, v in self.server.snapshot.items() if k not in PRIVATE_SNAPSHOT_KEYS}
                self.reply(200, {'token': self.server.token, **public,
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
                return self.reply(413, {'error': 'Choose a smaller file (maximum 32 MB per file).'})
            if self.headers.get('Content-Type') != 'application/json':
                return self.reply(415, {'error': 'JSON required.'})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object.')
            if self.path == '/api/import':
                source = data.get('source')
                # The page sends large selections one file per request, with every selected
                # name in `names`, so auto-detect still sees (and checks) the whole selection.
                names = data.get('names')
                selection = [{'name': n} for n in names if isinstance(n, str)] if isinstance(names, list) else data.get('files')
                resolved = detect_source(selection) if source == 'auto' else source
                incoming = import_data({**data, 'source': resolved})
                with self.server.lock:
                    merged, added, skipped, dropped = merge_snapshot(self.server.snapshot, incoming, resolved)
                    changed = merged is not self.server.snapshot
                    if changed:
                        self.server.generation += 1
                        self.server.snapshot = merged
                        self.server.job = {'status': 'idle'}
                self.reply(200, {'ok': True, 'detected_source': resolved, 'changed': changed,
                                 'added': added, 'skipped': skipped, 'dropped': dropped})
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
