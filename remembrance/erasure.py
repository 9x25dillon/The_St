"""Owner-requested, operator-reviewed permanent erasure, with a ledger kept outside the database."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import secrets

if __package__:
    from . import consent
else:
    import consent

DECLARATION_TEXT = ('I ask the operator to permanently erase this memorial after reviewing my request: its life story, '
                    'private and archived memories, media files, timeline, tributes, family access, reports, sharing grants '
                    'and nominations. Recording this request archives the memorial now. Erasure cannot be undone. Copies '
                    'already downloaded or shared cannot be recalled, and server backups expire on the operator’s published schedule.')
LEDGER_FORMAT = 'remembrance-erasure-ledger'
LEDGER_FIELDS = ('format', 'version', 'sequence', 'memorial_id', 'decided_at', 'actor', 'evidence_digest', 'previous_hash')
# Every table holding a memorial's data, children before their parents.
MEMORIAL_TABLES = ('candles', 'tributes', 'events', 'media', 'members', 'reports', 'audit',
                   'succession_requests', 'erasure_requests', 'consent_grants', 'consent_controls')
# Kept by design after erasure: opaque IDs, fixed codes, times and digests; never names or content.
RETAINED_TABLES = ('consent_events', 'erasures')
MEMORIAL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
HEX64 = re.compile(r'[0-9a-f]{64}')


class ErasureError(ValueError):
    pass


def owner_record(conn, mid, owner_id):
    memorial = conn.execute('SELECT * FROM memorials WHERE id=?', (mid,)).fetchone()
    if memorial is None or memorial['owner_id'] != owner_id or memorial['demo']:
        raise ErasureError('Only the current memorial owner can change this erasure request.')
    return memorial


def activity(conn, mid, event_type, actor_id, request_id, payload=None):
    consent.append_event(conn, mid, event_type, actor_id, payload={'request_id': request_id, **(payload or {})})
    conn.execute('INSERT INTO audit(memorial_id,actor_id,action,target_id,created) VALUES(?,?,?,?,?)',
                 (mid, actor_id, event_type.replace('_', '-'), request_id, consent.now()))


def request(conn, mid, owner_id, *, signed_name):
    signed_name = signed_name.strip()
    if not 1 <= len(signed_name) <= 120:
        raise ErasureError('Enter your full name to record this request.')
    with consent.transaction(conn):
        owner_record(conn, mid, owner_id)
        if conn.execute("SELECT 1 FROM erasure_requests WHERE memorial_id=? AND status='pending'", (mid,)).fetchone():
            raise ErasureError('An erasure request for this memorial is already waiting for review.')
        request_id, timestamp = secrets.token_hex(16), consent.now()
        conn.execute('INSERT INTO erasure_requests(id,memorial_id,requested_by,signed_name,declaration_text,status,created_at) '
                     'VALUES(?,?,?,?,?,?,?)', (request_id, mid, owner_id, signed_name, DECLARATION_TEXT, 'pending', timestamp))
        conn.execute('UPDATE memorials SET archived=1,updated=? WHERE id=?', (timestamp, mid))
        # Digest only the fixed text: a digest of the typed name would outlive the erasure in the history.
        activity(conn, mid, 'erasure_requested', owner_id, request_id, {'declaration_digest': consent.digest(DECLARATION_TEXT)})
    return request_id


def withdraw(conn, mid, request_id, owner_id):
    with consent.transaction(conn):
        owner_record(conn, mid, owner_id)
        row = conn.execute('SELECT status FROM erasure_requests WHERE id=? AND memorial_id=?', (request_id, mid)).fetchone()
        if row is None:
            raise ErasureError('Erasure request not found for this memorial.')
        if row['status'] != 'pending':
            return False
        conn.execute("UPDATE erasure_requests SET status='withdrawn',closed_at=?,closed_by=? WHERE id=?",
                     (consent.now(), owner_id, request_id))
        activity(conn, mid, 'erasure_withdrawn', owner_id, request_id)
    return True


def decline(conn, request_id, *, reviewer, reference):
    actor, evidence = consent.operator_identity(reviewer, reference)
    with consent.transaction(conn):
        row = conn.execute('SELECT * FROM erasure_requests WHERE id=?', (request_id,)).fetchone()
        if row is None:
            raise ErasureError('Erasure request not found.')
        if row['status'] == 'declined':
            return False
        if row['status'] != 'pending':
            raise ErasureError('Only a pending erasure request can be declined.')
        conn.execute("UPDATE erasure_requests SET status='declined',closed_at=?,closed_by=?,evidence_digest=? WHERE id=?",
                     (consent.now(), actor, evidence, request_id))
        activity(conn, row['memorial_id'], 'erasure_declined', actor, request_id, {'evidence_digest': evidence})
    return True


def preview(conn, mid, media_dir):
    """Counts for the operator's review. Contains no names or content."""
    memorial = conn.execute('SELECT owner_id,archived,demo FROM memorials WHERE id=?', (mid,)).fetchone()
    erased = conn.execute('SELECT * FROM erasures WHERE memorial_id=?', (mid,)).fetchone()
    if memorial is None:
        if erased is None:
            raise ErasureError('Memorial not found.')
        return {'memorial_id': mid, 'erased': dict(erased)}
    files = [media_dir / row['filename'] for row in conn.execute('SELECT filename FROM media WHERE memorial_id=?', (mid,))]
    pending = conn.execute("SELECT id FROM erasure_requests WHERE memorial_id=? AND status='pending'", (mid,)).fetchone()
    return {'memorial_id': mid, 'owner_id': memorial['owner_id'], 'archived': bool(memorial['archived']),
            'example': bool(memorial['demo']), 'pending_request': pending['id'] if pending else None,
            'erased': dict(erased) if erased else None,
            'rows': {table: conn.execute(f'SELECT COUNT(*) FROM {table} WHERE memorial_id=?', (mid,)).fetchone()[0]
                     for table in MEMORIAL_TABLES},
            'media_files': len(files), 'media_bytes': sum(path.stat().st_size for path in files if path.is_file()),
            'retained_history_events': conn.execute('SELECT COUNT(*) FROM consent_events WHERE memorial_id=?', (mid,)).fetchone()[0]}


def checked_ledger(path, data_dir):
    path, data = Path(path).resolve(), Path(data_dir).resolve()
    if path == data or data in path.parents:
        raise ErasureError('Keep the erasure ledger outside the live data directory; backups and restores replace that directory.')
    return path


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify(entry, sequence, previous_hash):
    try:
        valid = (isinstance(entry, dict) and set(entry) == {*LEDGER_FIELDS, 'entry_hash'}
                 and entry['format'] == LEDGER_FORMAT and type(entry['version']) is int and entry['version'] == 1
                 and type(entry['sequence']) is int and entry['sequence'] == sequence
                 and entry['previous_hash'] == previous_hash
                 and isinstance(entry['memorial_id'], str) and MEMORIAL_ID.fullmatch(entry['memorial_id'])
                 and isinstance(entry['actor'], str) and entry['actor'].startswith('operator:')
                 and all(isinstance(entry[key], str) and HEX64.fullmatch(entry[key]) for key in ('evidence_digest', 'entry_hash'))
                 and isinstance(entry['decided_at'], str) and datetime.fromisoformat(entry['decided_at'])
                 and entry['entry_hash'] == consent.digest(consent.canonical({key: entry[key] for key in LEDGER_FIELDS})))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ErasureError(f'The erasure ledger failed verification at entry {sequence}.')


class Ledger:
    """Append-only JSON Lines; each entry chains to the previous one by SHA-256."""

    def __init__(self, fd):
        self.fd = fd
        raw = b''
        while chunk := os.pread(fd, 1 << 16, len(raw)):
            raw += chunk
        if raw and not raw.endswith(b'\n'):
            raise ErasureError('The erasure ledger ends with an incomplete entry, probably from an interrupted erasure. '
                               'Nothing was erased for it. Check it, remove only that final partial line, then retry.')
        self.entries, seen = [], set()
        for sequence, line in enumerate(raw.splitlines(), 1):
            try:
                entry = json.loads(line)
            except ValueError:
                raise ErasureError(f'The erasure ledger failed verification at entry {sequence}.') from None
            _verify(entry, sequence, self.entries[-1]['entry_hash'] if self.entries else consent.ZERO_HASH)
            if entry['memorial_id'] in seen:
                raise ErasureError(f'The erasure ledger repeats memorial {entry["memorial_id"]} at entry {sequence}.')
            seen.add(entry['memorial_id'])
            self.entries.append(entry)

    def entry_for(self, mid):
        return next((entry for entry in self.entries if entry['memorial_id'] == mid), None)

    def append(self, mid, actor, evidence):
        entry = {'format': LEDGER_FORMAT, 'version': 1, 'sequence': len(self.entries) + 1, 'memorial_id': mid,
                 'decided_at': consent.now(), 'actor': actor, 'evidence_digest': evidence,
                 'previous_hash': self.entries[-1]['entry_hash'] if self.entries else consent.ZERO_HASH}
        entry['entry_hash'] = consent.digest(consent.canonical(entry))
        remaining = memoryview((consent.canonical(entry) + '\n').encode())
        while remaining:
            remaining = remaining[os.write(self.fd, remaining):]
        os.fsync(self.fd)
        self.entries.append(entry)
        return entry


@contextmanager
def open_ledger(path, *, create):
    """Hold an exclusive lock so erasures and reapplies extend one chain in order."""
    try:
        fd = os.open(path, (os.O_RDWR | os.O_APPEND | os.O_CREAT) if create else os.O_RDONLY, 0o600)
    except FileNotFoundError:
        raise ErasureError(f'Erasure ledger not found at {path}. Check the path; never substitute an empty ledger.') from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if create:
            _fsync_directory(path.parent)
        yield Ledger(fd)
    finally:
        os.close(fd)


def create_ledger(conn, ledger_path, data_dir):
    """Start an empty ledger, only for a database that has never erased a memorial. Never overwrites."""
    path = checked_ledger(ledger_path, data_dir)
    if conn.execute('SELECT COUNT(*) FROM erasures').fetchone()[0]:
        raise ErasureError('This database records erasures. Retrieve the existing ledger instead of starting a new one.')
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        raise ErasureError(f'A ledger already exists at {path}; it was left unchanged.') from None
    _fsync_directory(path.parent)


def _check_registry(conn, ledger):
    """Every erasure this database recorded must appear, unchanged, in the ledger."""
    entries = {entry['memorial_id']: entry for entry in ledger.entries}
    missing = [row['memorial_id'] for row in conn.execute('SELECT * FROM erasures ORDER BY ledger_sequence')
               if (entry := entries.get(row['memorial_id'])) is None
               or (entry['sequence'], entry['entry_hash']) != (row['ledger_sequence'], row['ledger_hash'])]
    if missing:
        raise ErasureError(f'This database records {len(missing)} erasure(s) that this ledger lacks or contradicts, '
                           f'starting with memorial {missing[0]}. Use the complete ledger for this server.')


def _register(conn, entry):
    if not conn.execute('SELECT 1 FROM erasures WHERE memorial_id=?', (entry['memorial_id'],)).fetchone():
        conn.execute('INSERT INTO erasures(memorial_id,decided_at,erased_at,erased_by,evidence_digest,ledger_sequence,ledger_hash) '
                     'VALUES(?,?,?,?,?,?,?)', (entry['memorial_id'], entry['decided_at'], consent.now(), entry['actor'],
                                              entry['evidence_digest'], entry['sequence'], entry['entry_hash']))


def _apply(conn, entry, media_dir, *, reapplied):
    """Delete the memorial inside the caller's write transaction, then its files just before commit.

    A crash after the files are gone but before commit leaves a recorded decision that erase or reapply completes.
    """
    mid = entry['memorial_id']
    filenames = [row['filename'] for row in conn.execute('SELECT filename FROM media WHERE memorial_id=?', (mid,))]
    if any(Path(name).name != name or name in ('', '.', '..') for name in filenames):
        raise ErasureError('A stored media filename is unsafe; erasure stopped for manual review.')
    removed = {table: conn.execute(f'DELETE FROM {table} WHERE memorial_id=?', (mid,)).rowcount for table in MEMORIAL_TABLES}
    removed['memorials'] = conn.execute('DELETE FROM memorials WHERE id=?', (mid,)).rowcount
    removed['media_files'] = len(filenames)
    consent.append_event(conn, mid, 'memorial_erased', entry['actor'],
                         payload={'evidence_digest': entry['evidence_digest'], 'ledger_sequence': entry['sequence'],
                                  'ledger_hash': entry['entry_hash'], 'reapplied': reapplied, 'removed': removed})
    _register(conn, entry)
    for name in filenames:
        (media_dir / name).unlink(missing_ok=True)
    if filenames:
        _fsync_directory(media_dir)


def erase(conn, mid, *, owner_id, reviewer, reference, ledger_path, data_dir):
    """Record the decision in the ledger, then erase. Returns False when already erased."""
    actor, evidence = consent.operator_identity(reviewer, reference)
    path = checked_ledger(ledger_path, data_dir)
    with open_ledger(path, create=True) as ledger, consent.transaction(conn):
        _check_registry(conn, ledger)
        memorial = conn.execute('SELECT owner_id FROM memorials WHERE id=?', (mid,)).fetchone()
        if memorial is None:
            if conn.execute('SELECT 1 FROM erasures WHERE memorial_id=?', (mid,)).fetchone():
                return False
            raise ErasureError('Memorial not found. If a restore removed it, run erasure reapply.')
        if memorial['owner_id'] != owner_id:
            raise ErasureError('The memorial and owner account must both match the reviewed case.')
        entry = ledger.entry_for(mid)
        reapplied = entry is not None
        _apply(conn, entry or ledger.append(mid, actor, evidence), Path(data_dir) / 'media', reapplied=reapplied)
    return True


def _state(conn, mid):
    return (conn.execute('SELECT 1 FROM memorials WHERE id=?', (mid,)).fetchone() is not None,
            conn.execute('SELECT 1 FROM erasures WHERE memorial_id=?', (mid,)).fetchone() is not None)


def reapply(conn, ledger_path, data_dir, *, check=False):
    """Erase again anything a restored backup brought back. Idempotent; check=True changes nothing."""
    path = checked_ledger(ledger_path, data_dir)
    summary = {'ledger_entries': 0, 'outstanding': [], 'erased': 0, 'recorded': 0}
    with open_ledger(path, create=False) as ledger:
        _check_registry(conn, ledger)
        summary['ledger_entries'] = len(ledger.entries)
        for entry in ledger.entries:
            if check:
                exists, registered = _state(conn, entry['memorial_id'])
                if exists or not registered:
                    summary['outstanding'].append(entry['memorial_id'])
                continue
            with consent.transaction(conn):
                exists, registered = _state(conn, entry['memorial_id'])
                if exists:
                    _apply(conn, entry, Path(data_dir) / 'media', reapplied=True)
                    summary['erased'] += 1
                elif not registered:
                    _register(conn, entry)
                    summary['recorded'] += 1
    return summary


def registry(conn):
    return [dict(row) for row in conn.execute('SELECT * FROM erasures ORDER BY ledger_sequence')]
