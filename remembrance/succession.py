"""Account-bound nominations and operator-reviewed transfer of memorial care."""
import secrets

if __package__:
    from . import consent
else:
    import consent

DECLARATION_TEXT = ('I am willing to care for this memorial if the operator confirms my authority and completes the transfer. '
                    'I will respect the recorded wishes, sharing permissions and other people’s rights. '
                    'Accepting this nomination does not give me access to private memories or permission to edit them.')
ACTIVE = ('offered', 'accepted')


class SuccessionError(ValueError):
    pass


def owner_record(conn, mid, actor_id):
    memorial = conn.execute('SELECT * FROM memorials WHERE id=?', (mid,)).fetchone()
    if memorial is None or memorial['owner_id'] != actor_id or memorial['demo']:
        raise SuccessionError('Only the current memorial owner can change this nomination.')
    return memorial


def activity(conn, mid, event_type, actor_id, request_id, payload):
    consent.append_event(conn, mid, event_type, actor_id, payload={'request_id': request_id, **payload})
    conn.execute('INSERT INTO audit(memorial_id,actor_id,action,target_id,created) VALUES(?,?,?,?,?)',
                 (mid, actor_id, event_type.replace('_', '-'), request_id, consent.now()))


def nominate(conn, mid, owner_id, *, email, account_code):
    with consent.transaction(conn):
        memorial = owner_record(conn, mid, owner_id)
        nominee = conn.execute('SELECT id FROM users WHERE email=? AND id=?', (email.strip().lower(), account_code.strip())).fetchone()
        if nominee is None or nominee['id'] == owner_id:
            raise SuccessionError('Choose another existing account and confirm its email and account code directly.')
        if conn.execute("SELECT 1 FROM succession_requests WHERE memorial_id=? AND status IN ('offered','accepted')", (mid,)).fetchone():
            raise SuccessionError('Cancel the current nomination before naming someone else.')
        owner = conn.execute('SELECT name FROM users WHERE id=?', (owner_id,)).fetchone()
        request_id, timestamp = secrets.token_hex(16), consent.now()
        conn.execute('INSERT INTO succession_requests(id,memorial_id,from_owner_id,nominee_id,memorial_name,inviter_name,status,created_at) '
                     'VALUES(?,?,?,?,?,?,?,?)', (request_id, mid, owner_id, nominee['id'], memorial['name'], owner['name'], 'offered', timestamp))
        conn.execute('UPDATE memorials SET successor=?,updated=? WHERE id=?', (email.strip().lower(), timestamp, mid))
        activity(conn, mid, 'successor_nominated', owner_id, request_id, {'nominee_id': nominee['id']})
    return request_id


def respond(conn, request_id, nominee_id, *, action, signed_name='', authority=''):
    if action not in ('accept', 'decline'):
        raise SuccessionError('Choose whether to accept or decline this nomination.')
    signed_name = signed_name.strip()
    if action == 'accept' and (not 1 <= len(signed_name) <= 120 or authority not in ('executor', 'family-authorized')):
        raise SuccessionError('Enter your full name and the basis of your authority for review.')
    with consent.transaction(conn):
        invitation = conn.execute('SELECT * FROM succession_requests WHERE id=? AND nominee_id=?', (request_id, nominee_id)).fetchone()
        if invitation is None:
            raise SuccessionError('Nomination not found for this account.')
        owner_record(conn, invitation['memorial_id'], invitation['from_owner_id'])
        if invitation['status'] not in ACTIVE:
            raise SuccessionError('This nomination is closed. Ask the owner to record a new one if needed.')
        if action == 'accept':
            if invitation['status'] == 'accepted':
                return False
            conn.execute("UPDATE succession_requests SET status='accepted',accepted_at=?,accepted_name=?,accepted_authority=?,declaration_text=? WHERE id=?",
                         (consent.now(), signed_name, authority, DECLARATION_TEXT, request_id))
            payload = {'authority': authority, 'declaration_digest': consent.digest(consent.canonical({'signed_name': signed_name, 'text': DECLARATION_TEXT}))}
        else:
            conn.execute("UPDATE succession_requests SET status='declined',closed_at=? WHERE id=?", (consent.now(), request_id))
            conn.execute("UPDATE memorials SET successor='' WHERE id=?", (invitation['memorial_id'],))
            payload = {}
        activity(conn, invitation['memorial_id'], 'successor_accepted' if action == 'accept' else 'successor_declined', nominee_id, request_id, payload)
    return True


def cancel(conn, mid, request_id, owner_id):
    with consent.transaction(conn):
        owner_record(conn, mid, owner_id)
        invitation = conn.execute('SELECT * FROM succession_requests WHERE id=? AND memorial_id=? AND from_owner_id=?',
                                  (request_id, mid, owner_id)).fetchone()
        if invitation is None:
            raise SuccessionError('Nomination not found for this owner and memorial.')
        if invitation['status'] in ('cancelled', 'declined'):
            return False
        if invitation['status'] == 'completed':
            raise SuccessionError('This transfer has already completed.')
        conn.execute("UPDATE succession_requests SET status='cancelled',closed_at=? WHERE id=?", (consent.now(), request_id))
        conn.execute("UPDATE memorials SET successor='' WHERE id=?", (mid,))
        activity(conn, mid, 'successor_cancelled', owner_id, request_id, {})
    return True


def transfer(conn, request_id, *, from_owner_id, to_account_id, reviewer, reference):
    actor, evidence = consent.operator_identity(reviewer, reference)
    with consent.transaction(conn):
        invitation = conn.execute('SELECT * FROM succession_requests WHERE id=?', (request_id,)).fetchone()
        if invitation is None or invitation['from_owner_id'] != from_owner_id or invitation['nominee_id'] != to_account_id:
            raise SuccessionError('The nomination, source owner and destination account must all match the reviewed case.')
        if invitation['status'] == 'completed':
            return False
        if invitation['status'] != 'accepted':
            raise SuccessionError('Only an accepted, current nomination can be transferred.')
        memorial = owner_record(conn, invitation['memorial_id'], from_owner_id)
        if not invitation['accepted_name'] or invitation['accepted_authority'] not in ('executor', 'family-authorized') or invitation['declaration_text'] != DECLARATION_TEXT:
            raise SuccessionError('The nominee must record a valid acceptance declaration before transfer.')
        timestamp = consent.now()
        conn.execute("UPDATE memorials SET owner_id=?,authority=?,authority_name=?,consent_at=?,successor='',updated=? WHERE id=?",
                     (to_account_id, invitation['accepted_authority'], invitation['accepted_name'], invitation['accepted_at'], timestamp, memorial['id']))
        # Remove self-granted family access from the former owner; other family grants remain.
        conn.execute('DELETE FROM members WHERE memorial_id=? AND email=(SELECT email FROM users WHERE id=?)', (memorial['id'], from_owner_id))
        conn.execute("UPDATE succession_requests SET status='completed',closed_at=?,completed_by=?,evidence_digest=?,previous_authority=?,"
                     'previous_authority_name=?,previous_consent_at=? WHERE id=?',
                     (timestamp, actor, evidence, memorial['authority'], memorial['authority_name'], memorial['consent_at'], request_id))
        activity(conn, memorial['id'], 'ownership_transferred', actor, request_id,
                 {'from_owner_id': from_owner_id, 'to_account_id': to_account_id, 'evidence_digest': evidence})
    return True
