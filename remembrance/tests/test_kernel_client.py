import base64
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from remembrance import kernel_client
from remembrance.app import create_app

KEY = b'shared-identity-assertion-key-0123456789'
PROFILE = '0f8fad5b-d9cb-469f-a165-70867728950e'
GRANT = '7c9e6679-7425-40de-944b-e07fc1f90ae7'
JTI = '16fd2706-8baf-433b-82eb-8c7fada847da'
CONFIG = kernel_client.KernelConfig(url='http://kernel.test', key=KEY)
APP_CONFIG = {'TESTING': True, 'SECRET_KEY': 'testing-only-secret-key-not-for-real-use', 'PUBLIC_URL': 'http://localhost'}


def decode(segment):
    return base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4))


def approval(action='INITIATE_VOICE_SYNTHESIS', **overrides):
    reply = {'allowed': True, 'action': action, 'profile_id': PROFILE, 'consent_grant_id': GRANT,
             'token': 'header.claims.signature', 'token_type': 'consent+jwt', 'jti': JTI,
             'expires_at': '2026-09-28T12:05:00Z', 'reason': None}
    reply.update(overrides)
    return reply


class FakeKernel:
    """Records each request, then answers with a fixed status and body or raises."""

    def __init__(self, status=200, body=None, error=None):
        self.status, self.error, self.requests = status, error, []
        self.body = body if isinstance(body, bytes) else json.dumps(approval() if body is None else body).encode()

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append({'method': method, 'url': url, 'headers': headers, 'body': body})
        if self.error:
            raise self.error
        return self.status, self.body


class KernelConfigTests(unittest.TestCase):
    ENV = {'REMEMBRANCE_CONSENT_KERNEL_URL': 'https://kernel.example/base/', 'REMEMBRANCE_AUTH_JWT_KEY': KEY.decode()}

    def test_disabled_until_configured_and_bad_settings_are_rejected(self):
        self.assertIsNone(kernel_client.KernelConfig.from_env({}))
        config = kernel_client.KernelConfig.from_env(self.ENV)
        self.assertEqual((config.url, config.key, config.issuer, config.audience),
                         ('https://kernel.example/base', KEY, 'remembrance-web', 'remembrance-consent'))
        self.assertNotIn(KEY.decode(), repr(config))
        custom = kernel_client.KernelConfig.from_env({**self.ENV, 'REMEMBRANCE_AUTH_JWT_ISSUER': 'web-2', 'REMEMBRANCE_AUTH_JWT_AUDIENCE': 'kernel-2'})
        self.assertEqual((custom.issuer, custom.audience), ('web-2', 'kernel-2'))
        for changes in ({'REMEMBRANCE_AUTH_JWT_KEY': ''}, {'REMEMBRANCE_CONSENT_KERNEL_URL': ''}, {'REMEMBRANCE_AUTH_JWT_KEY': 'too-short'},
                        {'REMEMBRANCE_CONSENT_KERNEL_URL': 'ftp://kernel.example'}, {'REMEMBRANCE_CONSENT_KERNEL_URL': 'https://user:pw@kernel.example'},
                        {'REMEMBRANCE_CONSENT_KERNEL_URL': 'https://kernel.example/?next=x'}, {'REMEMBRANCE_CONSENT_KERNEL_URL': 'kernel.example'}):
            with self.subTest(changes=changes), self.assertRaises(kernel_client.KernelConfigError):
                kernel_client.KernelConfig.from_env({**self.ENV, **changes})

    def test_app_refuses_to_start_half_configured_and_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as folder:
            half = {'REMEMBRANCE_CONSENT_KERNEL_URL': 'http://127.0.0.1:8790', 'REMEMBRANCE_AUTH_JWT_KEY': ''}
            with patch.dict('os.environ', half), self.assertRaises(kernel_client.KernelConfigError):
                create_app({**APP_CONFIG, 'DATA_DIR': Path(folder)})
            unset = {'REMEMBRANCE_CONSENT_KERNEL_URL': '', 'REMEMBRANCE_AUTH_JWT_KEY': ''}
            with patch.dict('os.environ', unset):
                self.assertIsNone(create_app({**APP_CONFIG, 'DATA_DIR': Path(folder)}).config['CONSENT_KERNEL'])


class IdentityAssertionTests(unittest.TestCase):
    def test_claims_are_minimal_and_signed_with_the_shared_key(self):
        token = kernel_client.identity_assertion(CONFIG, 'a1b2c3d4e5f60718', now=1_790_000_000)
        header, claims, signature = token.split('.')
        self.assertEqual(json.loads(decode(header)), {'alg': 'HS256', 'typ': 'JWT'})
        self.assertEqual(json.loads(decode(claims)), {'sub': 'a1b2c3d4e5f60718', 'iss': 'remembrance-web', 'aud': 'remembrance-consent',
                                                      'iat': 1_790_000_000, 'exp': 1_790_000_300, 'actor_kind': 'human', 'roles': []})
        self.assertEqual(decode(signature), hmac.new(KEY, f'{header}.{claims}'.encode(), hashlib.sha256).digest())
        for user_id in ('', ' padded', 'x' * 256, None):
            with self.subTest(user_id=user_id), self.assertRaises(kernel_client.KernelDenied):
                kernel_client.identity_assertion(CONFIG, user_id)


class AuthorizeVoiceTests(unittest.TestCase):
    def authorize(self, kernel, config=CONFIG, profile_id=PROFILE, action='INITIATE_VOICE_SYNTHESIS'):
        with self.assertRaises(kernel_client.KernelDenied) as denied:
            kernel_client.authorize_voice(config, 'user-1', profile_id, action, transport=kernel)
        return denied.exception.reason

    def test_each_voice_action_uses_its_fixed_purpose_and_returns_a_checked_token(self):
        for action, purpose in kernel_client.VOICE_ACTIONS.items():
            kernel = FakeKernel(body=approval(action))
            result = kernel_client.authorize_voice(CONFIG, 'user-1', PROFILE.upper(), action, transport=kernel)
            [sent] = kernel.requests
            self.assertEqual((sent['method'], sent['url']), ('POST', 'http://kernel.test/consent/authorize'))
            self.assertEqual(json.loads(sent['body']), {'action': action, 'profile_id': PROFILE, 'purpose_code': purpose})
            self.assertTrue(sent['headers']['Authorization'].startswith('Bearer '))
            self.assertEqual((result.action, result.profile_id, result.consent_grant_id, result.jti, result.token),
                             (action, PROFILE, GRANT, JTI, 'header.claims.signature'))
            self.assertNotIn('header.claims.signature', repr(result))

    def test_requests_this_app_decides_never_reach_the_kernel(self):
        for overrides, expected in (({'config': None}, 'kernel_not_configured'), ({'action': 'VIEW_MEMORIAL'}, 'unsupported_action'),
                                    ({'action': 'EXPORT_DATA'}, 'unsupported_action'), ({'action': 'FINANCIAL'}, 'unsupported_action'),
                                    ({'profile_id': 'not-a-uuid'}, 'invalid_profile'), ({'profile_id': None}, 'invalid_profile')):
            kernel = FakeKernel()
            with self.subTest(overrides=overrides):
                self.assertEqual(self.authorize(kernel, **overrides), expected)
                self.assertEqual(kernel.requests, [])

    def test_anything_but_a_well_formed_approval_is_a_denial(self):
        cases = [
            (FakeKernel(error=TimeoutError()), 'kernel_unreachable'),
            (FakeKernel(error=ConnectionRefusedError()), 'kernel_unreachable'),
            (FakeKernel(error=http.client.RemoteDisconnected('closed')), 'kernel_unreachable'),
            (FakeKernel(error=http.client.IncompleteRead(b'')), 'kernel_unreachable'),
            (FakeKernel(401, {'error': {'code': 'UNAUTHENTICATED'}}), 'identity_rejected'),
            (FakeKernel(429, {'error': {'code': 'RATE_LIMITED'}}), 'rate_limited'),
            (FakeKernel(403, approval(allowed=False, token=None, reason='NOT_AUTHORIZED')), 'NOT_AUTHORIZED'),
            (FakeKernel(403, {'error': {'code': 'PURPOSE_VIOLATION'}}), 'PURPOSE_VIOLATION'),
            (FakeKernel(403, {'reason': '<script>'}), 'denied'),
            (FakeKernel(403, {'error': 'plain text'}), 'denied'),
            (FakeKernel(403, b'<html>forbidden</html>'), 'invalid_response'),
            (FakeKernel(500, {'error': {'code': 'INTERNAL'}}), 'invalid_response'),
            (FakeKernel(302, b''), 'invalid_response'),
            (FakeKernel(200, b'not json'), 'invalid_response'),
            (FakeKernel(200, [approval()]), 'invalid_response'),
            (FakeKernel(200, approval(allowed=False)), 'invalid_response'),
            (FakeKernel(200, approval(allowed='true')), 'invalid_response'),
            (FakeKernel(200, approval(action='GENERATE_SCRIPT')), 'invalid_response'),
            (FakeKernel(200, approval(profile_id=GRANT)), 'invalid_response'),
            (FakeKernel(200, approval(token=None)), 'invalid_response'),
            (FakeKernel(200, approval(token='opaque')), 'invalid_response'),
            (FakeKernel(200, approval(token_type='bearer')), 'invalid_response'),
            (FakeKernel(200, approval(consent_grant_id='grant')), 'invalid_response'),
            (FakeKernel(200, approval(jti=None)), 'invalid_response'),
            (FakeKernel(200, approval(expires_at='soon')), 'invalid_response'),
            (FakeKernel(200, json.dumps(approval(padding=' ' * kernel_client.MAX_RESPONSE_BYTES)).encode()), 'invalid_response'),
        ]
        for kernel, expected in cases:
            with self.subTest(status=kernel.status, body=kernel.body[:60], error=kernel.error):
                self.assertEqual(self.authorize(kernel), expected)


class StandInKernel(BaseHTTPRequestHandler):
    """Just enough of the kernel's HTTP surface to exercise the real transport and `flask kernel check`."""
    seen = []

    def log_message(self, *args):
        pass

    def reply(self, status, payload, location=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        if location:
            self.send_header('Location', location)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self):
        scheme, _, token = self.headers.get('Authorization', '').partition(' ')
        signing_input, _, signature = token.rpartition('.')
        expected = base64.urlsafe_b64encode(hmac.new(KEY, signing_input.encode(), hashlib.sha256).digest()).rstrip(b'=').decode()
        return scheme == 'Bearer' and bool(signing_input) and hmac.compare_digest(signature, expected)

    def do_GET(self):
        self.seen.append(('GET', self.path, 'Authorization' in self.headers))
        if self.path == '/healthz':
            self.reply(200, {'status': 'ok'})
        elif self.path.startswith('/consent/grants/'):
            self.reply(404, {'error': {'code': 'NOT_FOUND'}}) if self.authenticated() else self.reply(401, {'error': {'code': 'UNAUTHENTICATED'}})
        else:
            self.reply(404, {})

    def do_POST(self):
        self.seen.append(('POST', self.path, 'Authorization' in self.headers))
        self.reply(307, {}, location='/elsewhere')


class LiveTransportTests(unittest.TestCase):
    def setUp(self):
        StandInKernel.seen = []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), StandInKernel)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def check(self, kernel):
        app = create_app({**APP_CONFIG, 'DATA_DIR': Path(self.tmp.name), 'CONSENT_KERNEL': kernel})
        return app.test_cli_runner().invoke(args=['kernel', 'check'])

    def test_check_confirms_the_shared_key_with_reads_only(self):
        result = self.check(kernel_client.KernelConfig(url=self.url, key=KEY))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('accepts this app', result.output)
        self.assertEqual([(method, path.split('/')[1], credentials) for method, path, credentials in StandInKernel.seen],
                         [('GET', 'healthz', False), ('GET', 'consent', True)])
        wrong = self.check(kernel_client.KernelConfig(url=self.url, key=b'a-different-key-that-is-long-enough-0000'))
        self.assertNotEqual(wrong.exit_code, 0)
        self.assertIn('identity_rejected', wrong.output)
        self.assertIn('kernel_not_configured', self.check(None).output)

    def test_real_transport_never_follows_redirects_and_reports_an_unreachable_kernel(self):
        live = kernel_client.KernelConfig(url=self.url, key=KEY)
        with self.assertRaises(kernel_client.KernelDenied) as denied:
            kernel_client.authorize_voice(live, 'user-1', PROFILE, 'GENERATE_SCRIPT')
        self.assertEqual(denied.exception.reason, 'invalid_response')
        self.assertEqual(StandInKernel.seen, [('POST', '/consent/authorize', True)])
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            closed = kernel_client.KernelConfig(url=f'http://127.0.0.1:{probe.getsockname()[1]}', key=KEY, timeout=2)
        with self.assertRaises(kernel_client.KernelDenied) as denied:
            kernel_client.authorize_voice(closed, 'user-1', PROFILE, 'GENERATE_SCRIPT')
        self.assertEqual(denied.exception.reason, 'kernel_unreachable')


if __name__ == '__main__':
    unittest.main()
