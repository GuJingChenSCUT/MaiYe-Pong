"""Isolated loopback credential setup tests; only a fake key is used."""
import http.client
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('secret_input', Path(__file__).with_name('secret-input.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'isolated/deepseek.key.dpapi'
        self.server = module.SetupServer(0, self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f'127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def request(self, method='GET', path='/', headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def session(self):
        code, headers, body = self.request()
        self.assertEqual(code, 200)
        token = re.search(rb"'X-CSRF-Token':\"([^\"]+)\"", body).group(1).decode()
        return {'Cookie': headers['Set-Cookie'].split(';')[0], 'Origin': 'http://' + self.host,
                'X-CSRF-Token': token, 'Content-Type': 'application/json'}

    def test_page_masks_input_and_has_no_external_resources_or_key(self):
        code, headers, body = self.request()
        self.assertEqual(code, 200)
        self.assertIn(b'type="password"', body)
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertIn('no-store', headers['Cache-Control'])
        self.assertNotIn(b'https://', body)
        self.assertFalse(self.path.exists())

    def test_wrong_host_and_cross_site_get_are_rejected(self):
        self.assertEqual(self.request(headers={'Host': 'attacker.invalid'})[0], 403)
        self.assertEqual(self.request(headers={'Origin': 'https://attacker.invalid'})[0], 403)
        self.assertEqual(self.request(headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_no_secret_read_endpoint(self):
        for route in ('/key', '/save', '/deepseek.key.dpapi', '/status', '/?key=fake'):
            self.assertEqual(self.request(path=route)[0], 404)

    def test_origin_and_csrf_required_before_encryption(self):
        headers = self.session()
        with patch.object(module, 'encrypt_dpapi') as encrypt:
            for key, value in [('Origin', 'https://attacker.invalid'), ('X-CSRF-Token', 'incorrect'),
                               ('Cookie', 'myb_setup=unknown')]:
                candidate = {**headers, key: value}
                self.assertEqual(self.request('POST', '/save', candidate, '{"key":"fake"}')[0], 403)
            missing = dict(headers); del missing['Origin']
            self.assertEqual(self.request('POST', '/save', missing, '{"key":"fake"}')[0], 403)
        encrypt.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_invalid_key_does_not_save(self):
        headers = self.session()
        with patch.object(module, 'encrypt_dpapi') as encrypt:
            self.assertEqual(self.request('POST', '/save', headers, '{"key":"invalid whitespace"}')[0], 400)
        encrypt.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_fake_key_dpapi_roundtrip_is_confined_to_temporary_directory(self):
        fake = 'offline-test-placeholder-not-a-real-key-0000'
        code, _, body = self.request('POST', '/save', self.session(), json.dumps({'key': fake}))
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {'status': 'saved'})
        encrypted = self.path.read_text(encoding='ascii')
        self.assertNotIn(fake, encrypted)
        # Mirror start-local.ps1's decode, but report only assertion status.
        script = "Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop; $s = [Console]::In.ReadToEnd() | ConvertTo-SecureString; $p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try {[Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($p))} finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p); $s.Dispose()}"
        encoded = module.base64.b64encode(script.encode('utf-16le')).decode('ascii')
        executable = Path(module.os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
                                input=encrypted.encode('ascii'), capture_output=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertTrue(result.returncode == 0 and result.stdout.decode('ascii') == fake, 'DPAPI compatibility assertion failed')

    def test_encryption_failure_preserves_previous_encrypted_file_and_hides_details(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('previous-test-encrypted-placeholder')
        with patch.object(module, 'encrypt_dpapi', side_effect=ValueError('fake private provider detail')):
            code, _, body = self.request('POST', '/save', self.session(),
                                        '{"key":"offline-test-placeholder-000000"}')
        self.assertEqual(code, 400)
        self.assertNotIn(b'private', body)
        self.assertEqual(self.path.read_text(), 'previous-test-encrypted-placeholder')


if __name__ == '__main__':
    unittest.main()
