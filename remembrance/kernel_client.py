"""The memorial app's only path to voice consent: ask the Remembrance consent kernel, and refuse on any doubt.

Memorial viewing stays with this app's own reviewed-sharing checks (consent.py). Voice actions are authorized
solely by the separate kernel in remembrance-consent/, which answers each request with a short-lived signed
token for one action on one profile. Anything other than an explicit, well-formed approval is a denial.

Standard library only, so the kernel's contract test can load this file directly.
"""
import base64
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import http.client
import json
import re
import time
import urllib.error
from urllib.parse import urlsplit
import urllib.request
import uuid

# Each voice action is requested under its single permitted purpose; callers never choose a purpose.
VOICE_ACTIONS = {'INITIATE_VOICE_SYNTHESIS': 'VOICE_SYNTHESIS', 'GENERATE_SCRIPT': 'SCRIPT_GENERATION',
                 'DELIVER_MESSAGE': 'VOICE_SYNTHESIS'}
ASSERTION_SECONDS = 300
MIN_KEY_BYTES = 32
MAX_RESPONSE_BYTES = 64 * 1024
MAX_SUBJECT_LENGTH = 255
REASON = re.compile(r'[A-Z][A-Z_]{0,63}')
CHECK_SUBJECT = 'remembrance-web-check'


class KernelDenied(Exception):
    """Every outcome other than an explicit, well-formed approval."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class KernelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class KernelConfig:
    url: str
    key: bytes = field(repr=False)
    issuer: str = 'remembrance-web'
    audience: str = 'remembrance-consent'
    timeout: float = 5.0

    @classmethod
    def from_env(cls, environ):
        """None when voice consent is not configured; an error when it is half or badly configured."""
        url, key = environ.get('REMEMBRANCE_CONSENT_KERNEL_URL', '').strip(), environ.get('REMEMBRANCE_AUTH_JWT_KEY', '')
        if not url and not key:
            return None
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise KernelConfigError('REMEMBRANCE_CONSENT_KERNEL_URL must be an http(s) URL without credentials, query or fragment.')
        if len(key.encode()) < MIN_KEY_BYTES:
            raise KernelConfigError(f'REMEMBRANCE_AUTH_JWT_KEY must be the kernel’s shared key, at least {MIN_KEY_BYTES} bytes.')
        return cls(url=url.rstrip('/'), key=key.encode(),
                   issuer=environ.get('REMEMBRANCE_AUTH_JWT_ISSUER') or cls.issuer,
                   audience=environ.get('REMEMBRANCE_AUTH_JWT_AUDIENCE') or cls.audience)


@dataclass(frozen=True)
class VoiceAuthorization:
    """Pass `token` to the service doing the work in its X-Consent-Authorization header."""
    action: str
    profile_id: str
    consent_grant_id: str
    jti: str
    expires_at: str
    token: str = field(repr=False)


def _encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def identity_assertion(config, user_id, *, now=None):
    """HS256 identity assertion the kernel verifies with the same shared key.

    No email claim: this app does not verify addresses, and the kernel trusts only verified ones. No roles:
    administrative kernel actions, such as authority review and death records, never originate here.
    """
    if not isinstance(user_id, str) or not user_id or user_id != user_id.strip() or len(user_id) > MAX_SUBJECT_LENGTH:
        raise KernelDenied('invalid_user')
    issued = int(time.time() if now is None else now)
    claims = {'sub': user_id, 'iss': config.issuer, 'aud': config.audience, 'iat': issued,
              'exp': issued + ASSERTION_SECONDS, 'actor_kind': 'human', 'roles': []}
    signing_input = '.'.join(_encode(json.dumps(part, separators=(',', ':')).encode())
                             for part in ({'alg': 'HS256', 'typ': 'JWT'}, claims))
    signature = hmac.new(config.key, signing_input.encode('ascii'), hashlib.sha256).digest()
    return f'{signing_input}.{_encode(signature)}'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never forward a bearer assertion to another location


_opener = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method, url, headers, body, timeout):
    try:
        with _opener.open(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as reply:
        with reply:
            return reply.code, reply.read(MAX_RESPONSE_BYTES + 1)


def _call(config, method, path, *, user_id=None, body=None, transport=None):
    headers = {'Accept': 'application/json'}
    if user_id is not None:
        headers['Authorization'] = 'Bearer ' + identity_assertion(config, user_id)
    if body is not None:
        headers['Content-Type'] = 'application/json'
    try:
        status, raw = (transport or urllib_transport)(method, config.url + path, headers, body, config.timeout)
    except (OSError, ValueError, http.client.HTTPException):
        raise KernelDenied('kernel_unreachable') from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise KernelDenied('invalid_response')
    return status, raw


def _approval(reply, action, profile):
    try:
        return (isinstance(reply, dict) and reply.get('allowed') is True and reply.get('action') == action
                and reply.get('profile_id') == profile and reply.get('token_type') == 'consent+jwt'
                and isinstance(reply.get('token'), str) and reply['token'].count('.') == 2
                and str(uuid.UUID(reply['consent_grant_id'])) == reply['consent_grant_id']
                and str(uuid.UUID(reply['jti'])) == reply['jti'] and bool(datetime.fromisoformat(reply['expires_at'])))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def authorize_voice(config, user_id, profile_id, action, *, transport=None):
    """Return the kernel's token for one voice action on one kernel profile, or raise KernelDenied."""
    if config is None:
        raise KernelDenied('kernel_not_configured')
    if action not in VOICE_ACTIONS:
        raise KernelDenied('unsupported_action')  # memorial viewing is this app's decision, never the kernel's
    try:
        profile = str(uuid.UUID(str(profile_id)))
    except ValueError:
        raise KernelDenied('invalid_profile') from None
    body = json.dumps({'action': action, 'profile_id': profile, 'purpose_code': VOICE_ACTIONS[action]}).encode()
    status, raw = _call(config, 'POST', '/consent/authorize', user_id=user_id, body=body, transport=transport)
    if status in (401, 429):
        raise KernelDenied('identity_rejected' if status == 401 else 'rate_limited')
    try:
        reply = json.loads(raw)
    except ValueError:
        raise KernelDenied('invalid_response') from None
    if status == 403 and isinstance(reply, dict):
        # A kernel decision carries `reason`; a purpose violation at its boundary carries `error.code`.
        error = reply.get('error')
        reason = reply.get('reason') or (error.get('code') if isinstance(error, dict) else None)
        raise KernelDenied(reason if isinstance(reason, str) and REASON.fullmatch(reason) else 'denied')
    if status != 200 or not _approval(reply, action, profile):
        raise KernelDenied('invalid_response')
    return VoiceAuthorization(action=action, profile_id=profile, consent_grant_id=reply['consent_grant_id'],
                              jti=reply['jti'], expires_at=reply['expires_at'], token=reply['token'])


def check(config, *, transport=None):
    """Confirm the kernel is up and accepts this app's identity assertions. Reads only; records nothing."""
    if config is None:
        raise KernelDenied('kernel_not_configured')
    if _call(config, 'GET', '/healthz', transport=transport)[0] != 200:
        raise KernelDenied('kernel_unhealthy')
    # An unknown grant is 404 to an authenticated caller and 401 when the assertion is not accepted.
    status, _ = _call(config, 'GET', f'/consent/grants/{uuid.uuid4()}', user_id=CHECK_SUBJECT, transport=transport)
    if status != 404:
        raise KernelDenied('identity_rejected' if status == 401 else 'invalid_response')
