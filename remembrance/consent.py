"""Opt-in, reviewed consent for memorial sharing. No synthetic-voice authorization."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import re
import secrets

ZERO_HASH = '0' * 64
DECLARATION_TEXT = ('I authorize viewing of this memorial and its current and future original media, stories and tributes '
                    'by the audience and at the release time selected in this grant. Existing visibility settings may further '
                    'restrict access. This permission excludes voice synthesis, generated messages, advertising and model '
                    'training. I can revoke this grant to pause future visitor access; copies already downloaded cannot be recalled.')
EVENT_FIELDS = ('memorial_id', 'sequence', 'event_type', 'actor_id', 'grant_id',
                'payload', 'created_at', 'previous_hash')


class ConsentError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str


def decide(grant, *, death_confirmed=False, role='public', action='memorial_view', purpose='memorial'):
    """Pure gate. Existing profile/item visibility must also permit the request."""
    if action != 'memorial_view' or purpose != 'memorial':
        return Decision(False, 'unsupported_use')
    if role not in ('public', 'family'):
        return Decision(False, 'unsupported_role')
    if grant is None:
        return Decision(False, 'no_grant')
    if grant['status'] == 'revoked':
        return Decision(False, 'revoked')
    if grant['status'] != 'verified':
        return Decision(False, 'awaiting_review')
    if grant['scope'] != action or grant['policy_version'] != 1:
        return Decision(False, 'unsupported_use')
    if grant['activation'] not in ('now', 'after_death'):
        return Decision(False, 'invalid_activation')
    if grant['activation'] == 'after_death' and not death_confirmed:
        return Decision(False, 'awaiting_death_confirmation')
    if grant['audience'] not in ('family', 'public') or (grant['audience'] == 'family' and role != 'family'):
        return Decision(False, 'audience_restricted')
    return Decision(True, 'allowed')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


@contextmanager
def transaction(conn, *, write=True):
    if conn.in_transaction:
        raise RuntimeError('Consent operations require their own transaction')
    conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def append_event(conn, mid, kind, actor, grant_id=None, payload=None):
    if not conn.in_transaction:
        raise RuntimeError('Consent history must be appended inside a transaction')
    previous = conn.execute('SELECT sequence,event_hash FROM consent_events WHERE memorial_id=? '
                            'ORDER BY sequence DESC LIMIT 1', (mid,)).fetchone()
    event = dict(memorial_id=mid, sequence=previous['sequence'] + 1 if previous else 1,
                 event_type=kind, actor_id=actor, grant_id=grant_id, payload=canonical(payload or {}),
                 created_at=now(), previous_hash=previous['event_hash'] if previous else ZERO_HASH)
    event['event_hash'] = digest(canonical(event))
    conn.execute('INSERT INTO consent_events(memorial_id,sequence,event_type,actor_id,grant_id,payload,'
                 'created_at,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?)', tuple(event.values()))


def current_grant(conn, mid):
    return conn.execute("SELECT * FROM consent_grants WHERE memorial_id=? AND status!='revoked'", (mid,)).fetchone()


def record_grant(conn, mid, owner_id, *, signed_name, audience, activation):
    signed_name = signed_name.strip()
    if not 1 <= len(signed_name) <= 120 or audience not in ('family', 'public') or activation not in ('now', 'after_death'):
        raise ConsentError('Choose a valid audience and release time, and enter your full name.')
    with transaction(conn):
        memorial = conn.execute('SELECT * FROM memorials WHERE id=?', (mid,)).fetchone()
        if memorial is None or memorial['owner_id'] != owner_id or memorial['demo']:
            raise ConsentError('Only the memorial owner can record this grant.')
        if current_grant(conn, mid):
            raise ConsentError('Revoke the current grant before recording a replacement.')
        gid, timestamp = secrets.token_hex(16), now()
        conn.execute('INSERT OR IGNORE INTO consent_controls(memorial_id,enabled_at) VALUES(?,?)', (mid, timestamp))
        conn.execute('INSERT INTO consent_grants(id,memorial_id,created_by,authority,signed_name,scope,policy_version,declaration_text,'
                     'audience,activation,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (gid, mid, owner_id, memorial['authority'], signed_name, 'memorial_view', 1, DECLARATION_TEXT,
                      audience, activation, 'pending', timestamp))
        append_event(conn, mid, 'grant_recorded', owner_id, gid,
                     {'audience': audience, 'activation': activation, 'scope': 'memorial_view', 'policy_version': 1,
                      'authority': memorial['authority'], 'declaration_digest': digest(canonical({'signed_name': signed_name, 'text': DECLARATION_TEXT}))})
    return gid


def operator_identity(reviewer, reference):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', reviewer):
        raise ConsentError('Use an opaque reviewer ID with 1–64 letters, digits, dots, underscores or hyphens.')
    reference = reference.strip()
    if not 1 <= len(reference) <= 200:
        raise ConsentError('Provide a case reference of 1–200 characters; store evidence separately.')
    return 'operator:' + reviewer, digest(reference)


def verify_grant(conn, gid, *, reviewer, reference):
    actor, evidence = operator_identity(reviewer, reference)
    with transaction(conn):
        grant = conn.execute('SELECT * FROM consent_grants WHERE id=?', (gid,)).fetchone()
        if grant is None or grant['status'] != 'pending':
            raise ConsentError('Only a pending grant can be verified.')
        conn.execute("UPDATE consent_grants SET status='verified',verified_at=?,verified_by=?,evidence_digest=? WHERE id=?",
                     (now(), actor, evidence, gid))
        append_event(conn, grant['memorial_id'], 'grant_verified', actor, gid, {'evidence_digest': evidence})


def revoke_grant(conn, mid, gid, *, owner_id=None, reviewer=None, reference=None):
    if owner_id is not None:
        actor, payload = owner_id, {}
    else:
        actor, evidence = operator_identity(reviewer or '', reference or '')
        payload = {'evidence_digest': evidence}
    with transaction(conn):
        memorial = conn.execute('SELECT * FROM memorials WHERE id=?', (mid,)).fetchone()
        if memorial is None or memorial['demo'] or (owner_id is not None and memorial['owner_id'] != owner_id):
            raise ConsentError('Memorial not found or owner does not match.')
        grant = conn.execute('SELECT * FROM consent_grants WHERE id=? AND memorial_id=?', (gid, mid)).fetchone()
        if grant is None:
            raise ConsentError('Grant not found for this memorial.')
        if grant['status'] == 'revoked':
            return False
        conn.execute("UPDATE consent_grants SET status='revoked',revoked_at=?,revoked_by=? WHERE id=?", (now(), actor, gid))
        append_event(conn, mid, 'grant_revoked', actor, gid, payload)
    return True


def confirm_death(conn, mid, *, death_date, reviewer, reference):
    actor, evidence = operator_identity(reviewer, reference)
    try:
        parsed = date.fromisoformat(death_date)
        if parsed > datetime.now(timezone.utc).date():
            raise ValueError()
    except (ValueError, TypeError):
        raise ConsentError('Use a valid date of passing that is not in the future.') from None
    with transaction(conn):
        control = conn.execute('SELECT * FROM consent_controls WHERE memorial_id=?', (mid,)).fetchone()
        memorial = conn.execute('SELECT born FROM memorials WHERE id=?', (mid,)).fetchone()
        if control is None:
            raise ConsentError('This memorial has not enabled reviewed sharing.')
        if control['death_confirmed_at']:
            raise ConsentError('Clear the existing death confirmation before replacing it.')
        if memorial['born'] and parsed.isoformat() < memorial['born']:
            raise ConsentError('The date of passing must not precede the date of birth.')
        conn.execute('UPDATE consent_controls SET death_date=?,death_confirmed_at=?,death_confirmed_by=?,'
                     'death_evidence_digest=? WHERE memorial_id=?', (parsed.isoformat(), now(), actor, evidence, mid))
        append_event(conn, mid, 'death_confirmed', actor, payload={'evidence_digest': evidence, 'date_digest': digest(parsed.isoformat())})


def clear_death(conn, mid, *, reviewer, reference):
    actor, evidence = operator_identity(reviewer, reference)
    with transaction(conn):
        control = conn.execute('SELECT * FROM consent_controls WHERE memorial_id=?', (mid,)).fetchone()
        if control is None or not control['death_confirmed_at']:
            raise ConsentError('There is no death confirmation to clear.')
        conn.execute('UPDATE consent_controls SET death_date=NULL,death_confirmed_at=NULL,death_confirmed_by=NULL,'
                     'death_evidence_digest=NULL WHERE memorial_id=?', (mid,))
        append_event(conn, mid, 'death_confirmation_cleared', actor, payload={'evidence_digest': evidence})


def authorize(conn, mid, *, role, actor_id=None, action='memorial_view', purpose='memorial'):
    """Serialize grant lookup, decision and audit append against revocation."""
    with transaction(conn):
        control = conn.execute('SELECT * FROM consent_controls WHERE memorial_id=?', (mid,)).fetchone()
        if control is None:
            result = Decision(False, 'no_grant')
            grant = None
        else:
            grant = current_grant(conn, mid)
            result = decide(grant, death_confirmed=bool(control['death_confirmed_at']), role=role, action=action, purpose=purpose)
        append_event(conn, mid, 'authorized' if result.allowed else 'denied', actor_id,
                     grant['id'] if grant else None, {'reason': result.code})
    return result


def verify_history(conn, checkpoint=None):
    """Return heads for off-host storage; an earlier checkpoint also detects truncation."""
    if checkpoint is not None:
        if not isinstance(checkpoint, dict) or checkpoint.get('format') != 'remembrance-consent-checkpoint' or checkpoint.get('version') != 1 or not isinstance(checkpoint.get('profiles'), dict):
            raise ConsentError('Unsupported consent checkpoint.')
        for mid, head in checkpoint['profiles'].items():
            if not isinstance(head, dict) or type(head.get('sequence')) is not int or head['sequence'] < 1 or not isinstance(head.get('hash'), str) or not re.fullmatch(r'[0-9a-f]{64}', head['hash']):
                raise ConsentError('Invalid consent checkpoint head.')
    expected = checkpoint['profiles'] if checkpoint else {}
    heads, matched = {}, set()
    with transaction(conn, write=False):
        for row in conn.execute('SELECT * FROM consent_events ORDER BY memorial_id,sequence'):
            mid = row['memorial_id']
            previous = heads.get(mid, {'sequence': 0, 'hash': ZERO_HASH})
            calculated = digest(canonical({key: row[key] for key in EVENT_FIELDS}))
            if row['sequence'] != previous['sequence'] + 1 or row['previous_hash'] != previous['hash'] or row['event_hash'] != calculated:
                raise ConsentError(f'Consent history failed verification for {mid} at event {row["sequence"]}.')
            heads[mid] = {'sequence': row['sequence'], 'hash': row['event_hash']}
            if mid in expected and row['sequence'] == expected[mid]['sequence']:
                if row['event_hash'] != expected[mid]['hash']:
                    raise ConsentError(f'Consent history differs from the checkpoint for {mid}.')
                matched.add(mid)
        if set(expected) != matched:
            raise ConsentError('Consent history is missing events recorded in the checkpoint.')
    return {'format': 'remembrance-consent-checkpoint', 'version': 1, 'profiles': heads}
