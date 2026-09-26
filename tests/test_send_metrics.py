"""Exercise the real sender against loopback TLS and plaintext redirect traps."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

SENDER = Path(__file__).resolve().parents[1] / 'scripts/send_metrics.py'
TOKEN = 'sender-test-token'
PAYLOAD = b'{"metrics":[{"name":"cpu","fields":{"usage_active":1}}]}'


class SenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        directory = Path(cls.temp.name)
        cls.cert, key = directory / 'cert.pem', directory / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                        '-keyout', str(key), '-out', str(cls.cert)], check=True, capture_output=True)
        cls.requests = []
        cls.status = 204
        cls.location = ''

        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                cls.requests.append((self.path, self.headers.get('Authorization'), payload))
                self.send_response(cls.status)
                if cls.location:
                    self.send_header('Location', cls.location)
                # Deliberately echo a credential in an error body: the sender
                # must not put arbitrary response content into Telegraf's logs.
                body = TOKEN.encode()
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST

        cls.https = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, key)
        cls.https.socket = context.wrap_socket(cls.https.socket, server_side=True)
        for server in (cls.https, cls.http):
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.url = f'https://127.0.0.1:{cls.https.server_port}/metrics?private=value'

    @classmethod
    def tearDownClass(cls):
        for server in (cls.https, cls.http):
            server.shutdown()
            server.server_close()
        cls.temp.cleanup()

    def setUp(self):
        self.requests.clear()
        type(self).status = 204
        type(self).location = ''

    def send(self, url=None, *, trusted=True, token=TOKEN, payload=PAYLOAD, check=False):
        env = dict(os.environ, SERVER_MONITOR_TOKEN=token)
        env.pop('SSL_CERT_FILE', None)
        env.pop('SSL_CERT_DIR', None)
        if trusted:
            env['SSL_CERT_FILE'] = str(self.cert)
        argv = [sys.executable, str(SENDER), '--url', url or self.url]
        if check:
            argv.append('--check')
        result = subprocess.run(argv, input=payload, capture_output=True, env=env, timeout=15)
        self.assertNotIn(TOKEN.encode(), result.stdout + result.stderr)
        self.assertNotIn(b'private=value', result.stdout + result.stderr)
        return result

    def test_verified_https_preserves_body_headers_and_query(self):
        self.assertEqual(self.send().returncode, 0)
        self.assertEqual(self.requests, [('/metrics?private=value', 'Bearer ' + TOKEN, PAYLOAD)])

    def test_docker_identity_health_and_boolean_fields_reach_receiver_unchanged(self):
        payload = (SENDER.parents[1] / 'examples/docker-payload.json').read_bytes()
        self.assertEqual(self.send(payload=payload).returncode, 0)
        self.assertEqual(self.requests, [('/metrics?private=value', 'Bearer ' + TOKEN, payload)])

    def test_every_redirect_is_rejected_without_contacting_its_target(self):
        for scheme, port in (('http', self.http.server_port), ('https', self.https.server_port)):
            for code in (301, 302, 303, 307, 308):
                with self.subTest(scheme=scheme, code=code):
                    self.requests.clear()
                    type(self).status = code
                    type(self).location = f'{scheme}://127.0.0.1:{port}/stolen?token={TOKEN}'
                    result = self.send()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b'redirects are disabled', result.stderr)
                    self.assertEqual(len(self.requests), 1)
                    self.assertEqual(self.requests[0][0], '/metrics?private=value')

    def test_untrusted_certificate_is_rejected_before_sending_credentials(self):
        result = self.send(trusted=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'certificate verification failed', result.stderr)
        self.assertEqual(self.requests, [])

    def test_invalid_endpoints_and_credentials_do_not_send(self):
        for url in (self.url.replace('https:', 'http:'), self.url + '#fragment',
                    self.url.replace('127.0.0.1', 'user:password@127.0.0.1')):
            self.assertNotEqual(self.send(url).returncode, 0)
        self.assertNotEqual(self.send(token='bad\ntoken').returncode, 0)
        self.assertEqual(self.requests, [])

    def test_non_success_statuses_fail_for_telegraf_retry_without_echoing_body(self):
        for code in (400, 401, 403, 429, 500, 503):
            with self.subTest(code=code):
                type(self).status = code
                result = self.send()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'HTTP {code}'.encode(), result.stderr)

    def test_check_does_not_send_and_empty_or_oversized_payloads_are_rejected(self):
        self.assertEqual(self.send(check=True).returncode, 0)
        for payload in (b'', b'x' * (16 * 1024 * 1024 + 1)):
            self.assertNotEqual(self.send(payload=payload).returncode, 0)
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
