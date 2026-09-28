"""Remembrance: a self-hostable, server-rendered memorial application."""
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timezone, date
from functools import wraps
from urllib.parse import urlsplit

import click
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from PIL import Image, ImageOps, UnidentifiedImageError
import qrcode
import qrcode.image.svg
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import BadRequest, SecurityError

if __package__:
    from . import consent, database
else:
    import consent
    import database

ROOT = Path(__file__).resolve().parent
Image.MAX_IMAGE_PIXELS = 30_000_000


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def ident():
    return secrets.token_hex(8)


def create_app(config=None):
    app = Flask(__name__)
    data = Path(os.environ.get('REMEMBRANCE_DATA', ROOT / 'data'))
    app.config.update(DATA_DIR=data, MAX_CONTENT_LENGTH=32 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('REMEMBRANCE_SECURE', '0') == '1',
                      PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
                      PUBLIC_URL=os.environ.get('PUBLIC_URL', 'http://127.0.0.1:8787').rstrip('/'),
                      SUPPORT_EMAIL=os.environ.get('SUPPORT_EMAIL', ''),
                      MAX_MEDIA_BYTES=int(os.environ.get('MAX_MEDIA_BYTES', 1024 * 1024 * 1024)))
    if config:
        app.config.update(config)
    data = Path(app.config['DATA_DIR'])
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    (data / 'media').mkdir(exist_ok=True, mode=0o700)
    secret = os.environ.get('SECRET_KEY') or app.config.get('SECRET_KEY')
    if not secret:
        secret_path = data / '.secret'
        try:
            with secret_path.open('x') as f:
                os.chmod(secret_path, 0o600)
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        secret = secret_path.read_text().strip()
    app.config['SECRET_KEY'] = secret
    if len(secret) < 32:
        raise ValueError('SECRET_KEY must contain at least 32 random characters')
    if os.environ.get('TRUST_PROXY') == '1':
        # Enable only when the listener is reachable solely through one trusted proxy.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    origin = urlsplit(app.config['PUBLIC_URL'])
    if origin.scheme not in ('http', 'https') or not origin.hostname or origin.path or origin.query or origin.fragment or origin.username:
        raise ValueError('PUBLIC_URL must be an http(s) origin with no path, credentials, query, or fragment')
    if app.config['SESSION_COOKIE_SECURE'] and origin.scheme != 'https':
        raise ValueError('Secure deployment requires an https PUBLIC_URL')
    app.config['TRUSTED_HOSTS'] = [origin.hostname]

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(data / 'remembrance.sqlite3', timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
            g.db.execute('PRAGMA journal_mode=WAL')
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        conn = g.pop('db', None)
        if conn:
            conn.close()

    with app.app_context():
        database.initialize(db())

    def audit(mid, action, target=None):
        db().execute('INSERT INTO audit(memorial_id,actor_id,action,target_id,created) VALUES(?,?,?,?,?)',
                     (mid, session.get('uid'), action, target, now()))

    def field(name, maximum=200, required=False):
        value = request.form.get(name, '').strip()
        if len(value) > maximum or (required and not value):
            abort(400, f'{name.replace("_", " ").capitalize()} is required or exceeds {maximum} characters.')
        return value

    def email_field(name='email', required=True):
        value = field(name, 254, required).lower()
        if value and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            abort(400, 'Please enter a valid email address.')
        return value

    def visibility():
        value = field('visibility') or 'private'
        if value not in ('public', 'family', 'private'):
            abort(400, 'Invalid visibility.')
        return value

    def rate_limit(kind, limit=10, period=3600):
        # No raw visitor addresses are retained; do not trust forwarded addresses.
        key = hmac.new(secret.encode(), f'{kind}:{request.remote_addr}'.encode(), hashlib.sha256).hexdigest()
        window = int(time.time()) // period
        db().execute('DELETE FROM rate_limits WHERE window < ?', (window - 2,))
        db().execute('INSERT INTO rate_limits VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET '
                     'hits=CASE WHEN window=excluded.window THEN hits+1 ELSE 1 END, window=excluded.window', (key, window))
        hits = db().execute('SELECT hits FROM rate_limits WHERE key=?', (key,)).fetchone()[0]
        db().commit()
        if hits > limit:
            abort(429, 'Please wait before trying again.')

    @app.before_request
    def prepare():
        if request.path.startswith('/static/') or request.path in ('/healthz', '/sw.js'):
            return
        session.setdefault('csrf', secrets.token_urlsafe(32))
        g.user = db().execute('SELECT id,name,email FROM users WHERE id=?', (session.get('uid'),)).fetchone()
        if request.method == 'POST':
            source = request.headers.get('Origin')
            if source and source != app.config['PUBLIC_URL']:
                abort(403, 'This request came from another site.')
            token = request.form.get('csrf', '')
            if not hmac.compare_digest(token, session['csrf']):
                abort(403, 'Your form expired. Refresh the page and try again.')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if not request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store'
        if app.config['SESSION_COOKIE_SECURE']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.context_processor
    def context():
        return dict(current_user=getattr(g, 'user', None), csrf=session.get('csrf', ''),
                    public_url=app.config['PUBLIC_URL'], support_email=app.config['SUPPORT_EMAIL'])

    def login_required(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not getattr(g, 'user', None):
                return redirect(url_for('login'))
            return fn(*args, **kwargs)
        return wrapped

    def get_memorial(mid, owner=False):
        m = db().execute('SELECT * FROM memorials WHERE id=?', (mid,)).fetchone()
        if not m:
            abort(404)
        role = 'public'
        if g.user and g.user['id'] == m['owner_id']:
            role = 'owner'
        elif g.user and db().execute('SELECT 1 FROM members WHERE memorial_id=? AND email=?', (mid, g.user['email'])).fetchone():
            role = 'family'
        if owner and role != 'owner':
            abort(403, 'Only the memorial owner can do this.')
        if role != 'owner' and (m['archived'] or m['visibility'] == 'private' or (m['visibility'] == 'family' and role != 'family')):
            abort(404)
        if role != 'owner' and not sharing_allowed(mid, role):
            abort(404)
        return m, role

    def sharing_allowed(mid, role):
        if not db().execute('SELECT 1 FROM consent_controls WHERE memorial_id=?', (mid,)).fetchone():
            return True
        return consent.authorize(db(), mid, role=role, actor_id=g.user['id'] if g.user else None).allowed

    def sharing_label(m):
        if m['archived']:
            return 'Archived'
        if m['visibility'] == 'private':
            return 'Private'
        control = db().execute('SELECT * FROM consent_controls WHERE memorial_id=?', (m['id'],)).fetchone()
        if control:
            grant = consent.current_grant(db(), m['id'])
            if not consent.decide(grant, death_confirmed=bool(control['death_confirmed_at']), role='family').allowed:
                return 'Sharing paused'
            if grant['audience'] == 'family':
                return 'Family'
        return m['visibility'].capitalize()

    def visible(item, role):
        return role == 'owner' or (not item['archived'] and (item['visibility'] == 'public' or (role == 'family' and item['visibility'] == 'family')))

    def owned_action(mid):
        m, _ = get_memorial(mid, owner=True)
        if m['demo']:
            abort(403, 'The example memorial is read-only.')
        return m

    @app.route('/')
    def home():
        examples = db().execute("SELECT * FROM memorials WHERE demo=1 AND visibility='public' AND archived=0").fetchall()
        return render_template('home.html', examples=examples)

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            rate_limit('register', 10)
            email = email_field()
            name = field('name', 100, True)
            password = field('password', 256, True)
            if len(password) < 12:
                abort(400, 'Use a password of at least 12 characters.')
            if request.form.get('terms') != 'yes':
                abort(400, 'Please accept the terms and privacy notice.')
            uid = ident()
            try:
                db().execute('INSERT INTO users VALUES(?,?,?,?,?)', (uid, email, name, generate_password_hash(password), now()))
                db().commit()
            except sqlite3.IntegrityError:
                flash('Unable to create that account. Try signing in instead.', 'error')
                return render_template('auth.html', mode='register'), 400
            session.clear()
            session.update(uid=uid, csrf=secrets.token_urlsafe(32))
            session.permanent = True
            return redirect(url_for('dashboard'))
        return render_template('auth.html', mode='register')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            rate_limit('login', 30)
            user = db().execute('SELECT * FROM users WHERE email=?', (email_field(),)).fetchone()
            password = field('password', 256, True)
            # Comparable password work for unknown and known accounts.
            valid = check_password_hash(user['password'] if user else app.config['DUMMY_HASH'], password)
            if not user or not valid:
                flash('Email or password was not recognized.', 'error')
                return render_template('auth.html', mode='login'), 401
            session.clear()
            session.update(uid=user['id'], csrf=secrets.token_urlsafe(32))
            session.permanent = True
            return redirect(url_for('dashboard'))
        return render_template('auth.html', mode='login')

    app.config['DUMMY_HASH'] = generate_password_hash(secrets.token_urlsafe(24))

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('home'))

    @app.get('/dashboard')
    @login_required
    def dashboard():
        own = db().execute('SELECT * FROM memorials WHERE owner_id=? ORDER BY created DESC', (g.user['id'],)).fetchall()
        own = [dict(m, sharing_label=sharing_label(m)) for m in own]
        shared = db().execute('SELECT m.* FROM memorials m JOIN members f ON f.memorial_id=m.id '
                             "WHERE f.email=? AND m.visibility IN ('public','family') AND m.archived=0", (g.user['email'],)).fetchall()
        shared = [m for m in shared if m['owner_id'] == g.user['id'] or sharing_allowed(m['id'], 'family')]
        return render_template('dashboard.html', memorials=own, shared=shared)

    def memorial_fields():
        values = {key: field(key, limit, key == 'name') for key, limit in
                  [('name', 120), ('born', 10), ('died', 10), ('tagline', 240), ('biography', 30000), ('location', 200)]}
        for key in ('born', 'died'):
            if values[key]:
                try:
                    date.fromisoformat(values[key])
                except ValueError:
                    abort(400, 'Dates must use YYYY-MM-DD.')
        if values['born'] and values['died'] and values['born'] > values['died']:
            abort(400, 'The birth date must be before the death date.')
        values['visibility'] = visibility()
        return values

    @app.route('/memorials/new', methods=['GET', 'POST'])
    @login_required
    def new_memorial():
        if request.method == 'POST':
            rate_limit('create-memorial', 20)
            fields = memorial_fields()
            authority = field('authority', 80, True)
            authority_name = field('authority_name', 120, True)
            if request.form.get('consent') != 'yes' or authority not in ('executor', 'family-authorized', 'self'):
                abort(400, 'Please confirm your authority and consent to create this memorial.')
            mid = ident()
            db().execute('INSERT INTO memorials(id,owner_id,name,born,died,tagline,biography,location,visibility,'
                         'authority,authority_name,consent_at,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (mid, g.user['id'], *fields.values(), authority, authority_name, now(), now(), now()))
            audit(mid, 'created; consent-v1')
            db().commit()
            flash('Your memorial has been created. Add memories at your own pace.')
            return redirect(url_for('manage', mid=mid))
        return render_template('edit.html', m=None)

    @app.route('/m/<mid>/edit', methods=['GET', 'POST'])
    @login_required
    def edit(mid):
        m = owned_action(mid)
        if request.method == 'POST':
            values = memorial_fields()
            db().execute('UPDATE memorials SET name=?,born=?,died=?,tagline=?,biography=?,location=?,visibility=?,updated=? WHERE id=?',
                         (*values.values(), now(), mid))
            audit(mid, 'profile-updated')
            db().commit()
            flash('Your changes have been saved.')
            return redirect(url_for('manage', mid=mid))
        return render_template('edit.html', m=m)

    @app.get('/m/<mid>')
    def memorial(mid):
        m, role = get_memorial(mid)
        media = [x for x in db().execute('SELECT * FROM media WHERE memorial_id=? AND archived=0 ORDER BY created', (mid,)) if visible(x, role)]
        events = [x for x in db().execute('SELECT * FROM events WHERE memorial_id=? AND archived=0 ORDER BY year,id', (mid,)) if visible(x, role)]
        tributes = db().execute("SELECT * FROM tributes WHERE memorial_id=? AND status='approved' ORDER BY created DESC", (mid,)).fetchall()
        portrait = next((x for x in media if x['id'] == m['portrait_id']), None)
        return render_template('memorial.html', m=m, role=role, media=media, events=events, tributes=tributes, portrait=portrait)

    @app.get('/m/<mid>/manage')
    @login_required
    def manage(mid):
        m = owned_action(mid)
        rows = lambda table: db().execute(f'SELECT * FROM {table} WHERE memorial_id=?', (mid,)).fetchall()
        return render_template('manage.html', m=m, media=rows('media'), events=rows('events'),
                               tributes=rows('tributes'), members=rows('members'),
                               audit=db().execute('SELECT * FROM audit WHERE memorial_id=? ORDER BY id DESC LIMIT 30', (mid,)).fetchall(),
                               reports=rows('reports'), sharing_label=sharing_label(m),
                               sharing_control=db().execute('SELECT * FROM consent_controls WHERE memorial_id=?', (mid,)).fetchone())

    @app.route('/m/<mid>/consent', methods=['GET', 'POST'])
    @login_required
    def consent_page(mid):
        m = owned_action(mid)
        if request.method == 'POST':
            rate_limit('consent-grant', 20)
            if request.form.get('consent') != 'yes':
                abort(400, 'Confirm the sharing permission before recording a grant.')
            consent.record_grant(db(), mid, g.user['id'], signed_name=field('signed_name', 120, True),
                                 audience=field('audience'), activation=field('activation'))
            flash('Your grant is recorded. Visitor access is paused until the operator reviews it and the release conditions are met.')
            return redirect(url_for('consent_page', mid=mid))
        control = db().execute('SELECT * FROM consent_controls WHERE memorial_id=?', (mid,)).fetchone()
        grant = consent.current_grant(db(), mid)
        decision = consent.decide(grant, death_confirmed=bool(control and control['death_confirmed_at']),
                                  role='family' if grant and grant['audience'] == 'family' else 'public')
        return render_template('consent.html', m=m, control=control, grant=grant, decision=decision, declaration=consent.DECLARATION_TEXT,
                               grants=db().execute('SELECT * FROM consent_grants WHERE memorial_id=? ORDER BY created_at DESC', (mid,)).fetchall(),
                               history=db().execute('SELECT * FROM consent_events WHERE memorial_id=? ORDER BY sequence DESC LIMIT 30', (mid,)).fetchall())

    @app.post('/m/<mid>/consent/<gid>/revoke')
    @login_required
    def revoke_consent(mid, gid):
        owned_action(mid)
        changed = consent.revoke_grant(db(), mid, gid, owner_id=g.user['id'])
        flash('Grant revoked. Visitors can no longer open this memorial or its media. Copies already downloaded cannot be recalled.'
              if changed else 'That grant was already revoked. Check the current sharing status below.')
        return redirect(url_for('consent_page', mid=mid))

    @app.errorhandler(consent.ConsentError)
    def consent_error(error):
        return render_template('error.html', error=BadRequest(str(error))), 400

    @app.post('/m/<mid>/upload')
    @login_required
    def upload(mid):
        owned_action(mid)
        rate_limit('upload', 100)
        f = request.files.get('file')
        if not f or not f.filename:
            abort(400, 'Choose a photo, video, or audio file.')
        if request.form.get('rights') != 'yes':
            abort(400, 'Please confirm permission to share this media.')
        caption = field('caption', 500, True)
        vis = visibility()
        if request.form.get('children') == 'yes' and vis == 'public':
            vis = 'family'
            flash('Media featuring children was saved for family only.')
        content = f.read()
        if not content:
            abort(400, 'This file is empty.')
        current_size = sum(p.stat().st_size for p in (data / 'media').iterdir() if p.is_file())
        if current_size + len(content) > app.config['MAX_MEDIA_BYTES']:
            abort(413, 'This server’s media storage limit has been reached. Contact its operator.')
        ext = Path(f.filename).suffix.lower()
        if ext in ('.jpg', '.jpeg', '.png', '.webp'):
            try:
                with Image.open(io.BytesIO(content)) as source:
                    source.load()
                    clean = ImageOps.exif_transpose(source).convert('RGB')
                    clean.thumbnail((3200, 3200))
                    out = io.BytesIO()
                    clean.save(out, 'JPEG', quality=88)
                    content = out.getvalue()
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                abort(400, 'The image could not be read safely.')
            ext, mime = '.jpg', 'image/jpeg'
        else:
            valid = {'.mp4': ('video/mp4', content[4:8] == b'ftyp'),
                     '.webm': ('video/webm', content.startswith(b'\x1aE\xdf\xa3')),
                     '.mp3': ('audio/mpeg', content.startswith(b'ID3') or (len(content) > 1 and content[0] == 255 and content[1] & 224 == 224)),
                     '.wav': ('audio/wav', content.startswith(b'RIFF') and content[8:12] == b'WAVE'),
                     '.m4a': ('audio/mp4', content[4:8] == b'ftyp')}
            if ext not in valid or not valid[ext][1]:
                abort(400, 'Supported files: JPEG, PNG, WebP, MP4, WebM, MP3, WAV, and M4A. Maximum 32 MB per request.')
            mime = valid[ext][0]
        fid = ident()
        filename = fid + ext
        path = data / 'media' / filename
        path.write_bytes(content)
        try:
            db().execute('INSERT INTO media VALUES(?,?,?,?,?,?,?,?,0)',
                         (fid, mid, filename, Path(f.filename).name[:200], mime, caption, vis, now()))
            if request.form.get('portrait') == 'yes' and mime.startswith('image/'):
                db().execute('UPDATE memorials SET portrait_id=? WHERE id=?', (fid, mid))
            audit(mid, 'media-uploaded', fid)
            db().commit()
        except Exception:
            path.unlink(missing_ok=True)
            raise
        flash('Your memory has been added.')
        return redirect(url_for('manage', mid=mid) + '#media')

    @app.get('/media/<fid>')
    def media_file(fid):
        item = db().execute('SELECT * FROM media WHERE id=?', (fid,)).fetchone()
        if not item:
            abort(404)
        _, role = get_memorial(item['memorial_id'])
        if not visible(item, role):
            abort(404)
        return send_file(data / 'media' / item['filename'], mimetype=item['mime'], conditional=True)

    @app.post('/m/<mid>/media/<fid>')
    @login_required
    def update_media(mid, fid):
        owned_action(mid)
        item = db().execute('SELECT * FROM media WHERE id=? AND memorial_id=?', (fid, mid)).fetchone()
        if not item:
            abort(404)
        action = field('action')
        if action in ('archive', 'restore'):
            db().execute('UPDATE media SET archived=? WHERE id=?', (int(action == 'archive'), fid))
        elif action == 'visibility':
            db().execute('UPDATE media SET visibility=? WHERE id=?', (visibility(), fid))
        elif action == 'portrait' and item['mime'].startswith('image/') and not item['archived']:
            db().execute('UPDATE memorials SET portrait_id=? WHERE id=?', (fid, mid))
        else:
            abort(400)
        audit(mid, 'media-' + action, fid)
        db().commit()
        return redirect(url_for('manage', mid=mid) + '#media')

    @app.post('/m/<mid>/events')
    @login_required
    def add_event(mid):
        owned_action(mid)
        try:
            year = int(field('year', 4, True))
            if not 1 <= year <= 9999:
                raise ValueError()
        except ValueError:
            abort(400, 'Enter a year from 1 to 9999.')
        eid = ident()
        db().execute('INSERT INTO events VALUES(?,?,?,?,?,?,0)', (eid, mid, year, field('title', 200, True), field('story', 5000), visibility()))
        audit(mid, 'event-added', eid)
        db().commit()
        return redirect(url_for('manage', mid=mid) + '#timeline')

    @app.post('/m/<mid>/events/<eid>')
    @login_required
    def archive_event(mid, eid):
        owned_action(mid)
        if not db().execute('SELECT 1 FROM events WHERE id=? AND memorial_id=?', (eid, mid)).fetchone():
            abort(404)
        action = field('action')
        if action not in ('archive', 'restore'):
            abort(400)
        db().execute('UPDATE events SET archived=? WHERE id=?', (int(action == 'archive'), eid))
        audit(mid, 'event-' + action, eid)
        db().commit()
        return redirect(url_for('manage', mid=mid) + '#timeline')

    @app.post('/m/<mid>/tributes')
    def tribute(mid):
        m, _ = get_memorial(mid)
        if m['demo'] or m['archived']:
            abort(400, 'Contributions are disabled on this memorial.')
        rate_limit('tribute', 15)
        if request.form.get('consent') != 'yes':
            abort(400, 'Please consent to displaying this tribute after review.')
        token, tid = secrets.token_urlsafe(32), ident()
        db().execute('INSERT INTO tributes VALUES(?,?,?,?,?,?,?)',
                     (tid, mid, field('name', 100, True), field('body', 5000, True), 'pending', hashlib.sha256(token.encode()).hexdigest(), now()))
        audit(mid, 'tribute-submitted', tid)
        db().commit()
        return render_template('receipt.html', token=token, tid=tid, m=m)

    @app.route('/withdraw', methods=['GET', 'POST'])
    def withdraw():
        if request.method == 'POST':
            rate_limit('withdraw', 30)
            token = field('token', 100, True)
            row = db().execute('SELECT * FROM tributes WHERE withdrawal_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not row:
                abort(400, 'That withdrawal code was not found.')
            db().execute('DELETE FROM tributes WHERE id=?', (row['id'],))
            audit(row['memorial_id'], 'tribute-erased', row['id'])
            db().commit()
            flash('Your tribute and display name have been erased from the live database. Backups expire according to the operator’s retention schedule.')
            return redirect(url_for('home'))
        return render_template('withdraw.html')

    @app.post('/m/<mid>/tributes/<tid>')
    @login_required
    def moderate(mid, tid):
        owned_action(mid)
        status = field('status')
        if status not in ('approved', 'archived'):
            abort(400)
        changed = db().execute('UPDATE tributes SET status=? WHERE id=? AND memorial_id=?', (status, tid, mid)).rowcount
        if not changed:
            abort(404)
        audit(mid, 'tribute-' + status, tid)
        db().commit()
        flash('The tribute has been ' + status + '.')
        return redirect(url_for('manage', mid=mid) + '#tributes')

    @app.post('/m/<mid>/candle')
    def candle(mid):
        m, _ = get_memorial(mid)
        if m['demo'] or m['archived']:
            abort(400, 'Candles are disabled on this example.')
        rate_limit('candle', 60)
        visitor = session.setdefault('visitor', secrets.token_urlsafe(24))
        digest = hmac.new(secret.encode(), f'{visitor}:{mid}:{date.today()}'.encode(), hashlib.sha256).hexdigest()
        db().execute('DELETE FROM candles WHERE day < date(\'now\',\'-30 days\')')
        db().execute('INSERT OR IGNORE INTO candles VALUES(?,?,?)', (mid, digest, date.today().isoformat()))
        db().commit()
        flash('A candle is lit in their memory. Take a quiet moment here.')
        return redirect(url_for('memorial', mid=mid) + '#remember')

    @app.post('/m/<mid>/members')
    @login_required
    def member(mid):
        owned_action(mid)
        email = email_field()
        action = field('action')
        if action == 'remove':
            db().execute('DELETE FROM members WHERE memorial_id=? AND email=?', (mid, email))
        elif action == 'add':
            # Existing accounts only: an unverified email must never confer private access.
            code = field('account_code', 16, True)
            if not db().execute('SELECT 1 FROM users WHERE email=? AND id=?', (email, code)).fetchone():
                abort(400, 'The account email and code did not match. Ask your relative for the code shown on their My memorials page.')
            db().execute('INSERT OR IGNORE INTO members VALUES(?,?)', (mid, email))
        else:
            abort(400)
        audit(mid, 'family-access-' + action)
        db().commit()
        flash('Family access updated. No email was sent; share the memorial link directly.')
        return redirect(url_for('manage', mid=mid) + '#access')

    @app.post('/m/<mid>/settings')
    @login_required
    def settings(mid):
        owned_action(mid)
        action = field('action')
        if action == 'successor':
            successor = email_field('successor', required=False)
            db().execute('UPDATE memorials SET successor=? WHERE id=?', (successor, mid))
        elif action in ('archive', 'restore'):
            db().execute('UPDATE memorials SET archived=? WHERE id=?', (int(action == 'archive'), mid))
        else:
            abort(400)
        audit(mid, 'memorial-' + action)
        db().commit()
        flash('Settings saved.')
        return redirect(url_for('manage', mid=mid) + '#access')

    @app.route('/m/<mid>/report', methods=['GET', 'POST'])
    def report(mid):
        # A report can be filed for a hidden memorial without revealing its details.
        if not db().execute('SELECT 1 FROM memorials WHERE id=?', (mid,)).fetchone():
            abort(404)
        if request.method == 'POST':
            rate_limit('report', 10)
            category = field('category')
            if category not in ('privacy', 'copyright', 'authority', 'other'):
                abort(400)
            rid = ident()
            db().execute('INSERT INTO reports VALUES(?,?,?,?,?,?,?)', (rid, mid, email_field(), category, field('detail', 5000, True), now(), 'open'))
            db().commit()
            flash(f'Your request is recorded. Reference: {rid}. The memorial owner and server operator can review it. For urgent requests, contact the operator shown in the privacy notice.')
            return redirect(url_for('policy'))
        return render_template('report.html', mid=mid)

    @app.post('/m/<mid>/reports/<rid>')
    @login_required
    def resolve_report(mid, rid):
        owned_action(mid)
        if not db().execute("UPDATE reports SET status='reviewed' WHERE id=? AND memorial_id=?", (rid, mid)).rowcount:
            abort(404)
        audit(mid, 'report-reviewed', rid)
        db().commit()
        flash('Marked reviewed. Contact the requester separately to communicate the outcome.')
        return redirect(url_for('manage', mid=mid))

    def qr_image(mid, svg=False):
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=12, border=4)
        qr.add_data(app.config['PUBLIC_URL'] + '/m/' + mid)
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathFillImage) if svg else qr.make_image(fill_color='black', back_color='white')
        out = io.BytesIO()
        img.save(out)
        out.seek(0)
        return out

    @app.get('/m/<mid>/qr.<fmt>')
    def qr_download(mid, fmt):
        get_memorial(mid)
        if fmt not in ('png', 'svg'):
            abort(404)
        return send_file(qr_image(mid, fmt == 'svg'), mimetype='image/svg+xml' if fmt == 'svg' else 'image/png',
                         as_attachment=request.args.get('download') == '1', download_name=f'remembrance-{mid}.{fmt}')

    @app.get('/m/<mid>/plaque')
    def plaque(mid):
        m, _ = get_memorial(mid)
        return render_template('plaque.html', m=m, local=urlsplit(app.config['PUBLIC_URL']).hostname in ('localhost', '127.0.0.1'))

    @app.get('/m/<mid>/export')
    @login_required
    def export(mid):
        m = owned_action(mid)
        rate_limit('export', 10)
        payload = dict(format='remembrance-archive', version=1, exported_at=now(), memorial=dict(m))
        for table in ('media', 'events', 'tributes', 'members', 'audit'):
            payload[table] = [dict(x) for x in db().execute(f'SELECT * FROM {table} WHERE memorial_id=?', (mid,))]
        for t in payload['tributes']:
            t.pop('withdrawal_hash', None)
        control = db().execute('SELECT * FROM consent_controls WHERE memorial_id=?', (mid,)).fetchone()
        if control:
            payload['consent'] = {'control': dict(control),
                                  'grants': [dict(x) for x in db().execute('SELECT * FROM consent_grants WHERE memorial_id=? ORDER BY created_at', (mid,))],
                                  'history': [dict(x) for x in db().execute('SELECT * FROM consent_events WHERE memorial_id=? ORDER BY sequence', (mid,))]}
        out = tempfile.TemporaryFile()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('memorial.json', json.dumps(payload, indent=2, ensure_ascii=False))
            z.writestr('index.html', render_template('archive.html', m=m, media=payload['media'], events=payload['events'], tributes=payload['tributes']))
            z.writestr('README.txt', 'Private owner archive. Includes private and archived memories and account email addresses. Store securely. Open index.html offline; memorial.json is the portable data format. Media paths are relative. No external service is required. Erasure cannot recall copies already exported.\n')
            for item in payload['media']:
                z.write(data / 'media' / item['filename'], 'media/' + item['filename'])
        out.seek(0)
        response = send_file(out, mimetype='application/zip', as_attachment=True, download_name=f'remembrance-{mid}.zip')
        response.call_on_close(out.close)
        return response

    @app.route('/m/<mid>/import', methods=['GET', 'POST'])
    @login_required
    def import_memories(mid):
        owned_action(mid)
        if request.method == 'POST':
            f = request.files.get('file')
            if not f:
                abort(400, 'Choose a Remembrance JSON file.')
            try:
                payload = json.load(f)
                if not isinstance(payload, dict) or payload.get('format') != 'remembrance-import' or payload.get('version') != 1:
                    raise ValueError()
                items = payload['memories']
                if not isinstance(items, list) or not 1 <= len(items) <= 100:
                    raise ValueError()
                cleaned = []
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError()
                    title, story = item.get('title'), item.get('story', '')
                    year = item.get('year')
                    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200 or not isinstance(story, str) or len(story) > 5000 or type(year) is not int or not 1 <= year <= 9999:
                        raise ValueError()
                    cleaned.append((ident(), mid, year, title.strip(), story, 'private'))
            except (ValueError, KeyError, TypeError, UnicodeError):
                abort(400, 'Invalid import. Use the documented format with 1–100 memories, a year, title, and optional story.')
            if request.form.get('consent') != 'yes':
                abort(400, 'Confirm that you are authorized to import these memories.')
            db().executemany('INSERT INTO events VALUES(?,?,?,?,?,?,0)', cleaned)
            audit(mid, 'private-memories-imported')
            db().commit()
            flash(f'{len(cleaned)} memories imported as private timeline entries. Review them before changing visibility.')
            return redirect(url_for('manage', mid=mid) + '#timeline')
        return render_template('import.html', mid=mid)

    @app.post('/m/<mid>/events/<eid>/visibility')
    @login_required
    def event_visibility(mid, eid):
        owned_action(mid)
        if not db().execute('UPDATE events SET visibility=? WHERE id=? AND memorial_id=?', (visibility(), eid, mid)).rowcount:
            abort(404)
        audit(mid, 'event-visibility', eid)
        db().commit()
        return redirect(url_for('manage', mid=mid) + '#timeline')

    @app.get('/privacy')
    def policy():
        return render_template('policy.html')

    @app.get('/install')
    def install():
        return render_template('install.html')

    @app.get('/sw.js')
    def service_worker():
        return send_file(ROOT / 'static' / 'sw.js', mimetype='application/javascript')

    @app.get('/healthz')
    def health():
        db().execute('SELECT 1').fetchone()
        return {'status': 'ok', 'schema': database.version(db())}

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def error_page(error):
        if isinstance(error, SecurityError):
            return 'Untrusted request host.', 400, {'Content-Type': 'text/plain'}
        return render_template('error.html', error=error), error.code

    @app.cli.command('seed-demo')
    def seed_demo():
        """Create one clearly fictional, read-only example, without login credentials."""
        uid, mid = 'example-owner', 'eleanor-example'
        if db().execute('SELECT 1 FROM memorials WHERE id=?', (mid,)).fetchone():
            click.echo('Example already exists.')
            return
        db().execute('INSERT INTO users VALUES(?,?,?,?,?)', (uid, 'example@invalid.example', 'Example family', generate_password_hash(secrets.token_urlsafe(64)), now()))
        db().execute('INSERT INTO memorials(id,owner_id,name,born,died,tagline,biography,location,visibility,authority,authority_name,consent_at,created,updated,demo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)',
                     (mid, uid, 'Eleanor Rose Bennett', '1942-04-18', '2024-09-06', 'A life rooted in love. A spirit that helped others bloom.',
                      'Eleanor believed the most meaningful things in life were often the smallest: a handwritten note, an open door, a little more time in the garden.\n\nA teacher, a mother, and a wonderfully patient listener, she made people feel at home wherever she was. Sunday afternoons meant a crowded kitchen, something warm from the oven, and stories that grew a little longer each year.\n\nHer garden was never quite finished. There was always another seed to plant, another cutting to give a neighbor. We carry that generosity with us — in the things we grow and the people we care for.',
                      'A fictional memorial for exploring Remembrance', 'public', 'example', 'Fictional example', now(), now(), now()))
        for year, title, story in [(1942, 'The beginning of a beautiful life', 'Born into a home filled with music, books, and room for one more at the table.'),
                                    (1964, 'A classroom of her own', 'Began a lifelong love of teaching, helping children find confidence in their own words.'),
                                    (1968, 'A love that felt like home', 'Married her best friend and began a new chapter together.'),
                                    (1995, 'A garden for everyone', 'Helped neighbors turn an empty patch of land into a shared garden.')]:
            db().execute('INSERT INTO events VALUES(?,?,?,?,?,?,0)', (ident(), mid, year, title, story, 'public'))
        db().execute('INSERT INTO tributes VALUES(?,?,?,?,?,?,?)', (ident(), mid, 'A fictional family tribute', 'She taught us that love is something you practice, in the little things, every day. We still hear her laughter around the kitchen table.', 'approved', secrets.token_hex(32), now()))
        db().commit()
        click.echo('Created /m/eleanor-example (fictional, read-only).')

    @app.cli.command('reset-password')
    @click.argument('email')
    @click.password_option(confirmation_prompt=True)
    def reset_password(email, password):
        """Operator-assisted recovery, after independently verifying identity."""
        if len(password) < 12:
            raise click.ClickException('Use at least 12 characters.')
        if not db().execute('UPDATE users SET password=? WHERE email=?', (generate_password_hash(password), email.lower())).rowcount:
            raise click.ClickException('Account not found.')
        db().commit()
        click.echo('Password updated. Rotate SECRET_KEY to revoke all existing sessions if compromise is suspected.')

    @app.cli.command('reports')
    def list_reports():
        """Review reports through server access; outputs personal information."""
        for row in db().execute("SELECT * FROM reports WHERE status='open' ORDER BY created"):
            click.echo(json.dumps(dict(row)))

    @app.cli.group('consent')
    def consent_commands():
        """Operator review of memorial sharing, after independent evidence checks."""

    def operator_options(fn):
        fn = click.option('--reviewer', required=True, help='Opaque operator ID, not a personal name.')(fn)
        return click.option('--reference', required=True, help='Private case reference; only its digest is stored.')(fn)

    def consent_operation(fn, *args, **kwargs):
        try:
            return fn(db(), *args, **kwargs)
        except consent.ConsentError as error:
            raise click.ClickException(str(error)) from error

    @consent_commands.command('pending')
    def pending_consents():
        """List grant references awaiting review; no identity documents are stored here."""
        for row in db().execute("SELECT id,memorial_id,authority,audience,activation,created_at FROM consent_grants WHERE status='pending' ORDER BY created_at"):
            click.echo(json.dumps(dict(row)))

    @consent_commands.command('show')
    @click.argument('grant_id')
    def show_consent(grant_id):
        """Read the recorded declaration for review. Output includes the signer's name."""
        grant = db().execute('SELECT * FROM consent_grants WHERE id=?', (grant_id,)).fetchone()
        if grant is None:
            raise click.ClickException('Grant not found.')
        click.echo(json.dumps(dict(grant), indent=2))

    @consent_commands.command('verify')
    @click.argument('grant_id')
    @operator_options
    def verify_consent(grant_id, reviewer, reference):
        """Record completed authority/consent review. This is not automated verification."""
        consent_operation(consent.verify_grant, grant_id, reviewer=reviewer, reference=reference)
        click.echo('Grant reviewed. Release time, audience and existing visibility still apply.')

    @consent_commands.command('confirm-death')
    @click.argument('memorial_id')
    @click.option('--date', 'death_date', required=True, help='Independently confirmed date, YYYY-MM-DD.')
    @operator_options
    def confirm_consent_death(memorial_id, death_date, reviewer, reference):
        """Record independently reviewed evidence of passing for an opted-in memorial."""
        consent_operation(consent.confirm_death, memorial_id, death_date=death_date, reviewer=reviewer, reference=reference)
        click.echo('Confirmation recorded. Reviewed grants marked after passing may now allow sharing.')

    @consent_commands.command('clear-death')
    @click.argument('memorial_id')
    @operator_options
    def clear_consent_death(memorial_id, reviewer, reference):
        """Correct a mistaken confirmation; after-passing grants stop allowing access."""
        consent_operation(consent.clear_death, memorial_id, reviewer=reviewer, reference=reference)
        click.echo('Confirmation cleared. After-passing grants are suspended.')

    @consent_commands.command('revoke')
    @click.argument('memorial_id')
    @click.argument('grant_id')
    @operator_options
    def operator_revoke_consent(memorial_id, grant_id, reviewer, reference):
        """Revoke a grant after an operator review, for example an authority dispute."""
        changed = consent_operation(consent.revoke_grant, memorial_id, grant_id, reviewer=reviewer, reference=reference)
        click.echo('Grant revoked; future visitor requests are blocked.' if changed else 'That grant was already revoked; check whether a replacement grant exists.')

    @consent_commands.command('audit')
    @click.option('--checkpoint', type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option('--output', type=click.Path(dir_okay=False, path_type=Path))
    def consent_audit(checkpoint, output):
        """Verify chains, optionally against an earlier off-host checkpoint. Never overwrites output."""
        try:
            prior = json.loads(checkpoint.read_text()) if checkpoint else None
            heads = consent_operation(consent.verify_history, checkpoint=prior)
            content = json.dumps(heads, indent=2) + '\n'
            if output:
                with os.fdopen(os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as stream:
                    stream.write(content)
                click.echo(f'Verified checkpoint written to {output}. Keep a copy off this host.')
            else:
                click.echo(content, nl=False)
        except (OSError, json.JSONDecodeError) as error:
            raise click.ClickException(str(error)) from error

    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=8787, debug=False)
