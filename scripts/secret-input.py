"""Loopback-only, write-only DeepSeek credential setup; never log request data."""
from __future__ import annotations

import argparse
import base64
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
import time


ENCRYPT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
$plain = [Console]::In.ReadToEnd()
$secure = $null
try {
    $secure = ConvertTo-SecureString -String $plain -AsPlainText -Force
    $encrypted = ConvertFrom-SecureString -SecureString $secure
    [Console]::Out.Write($encrypted)
} finally {
    $plain = $null
    if ($null -ne $secure) { $secure.Dispose() }
}
"""

PAGE = """<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>買嘢幫 · 連接 DeepSeek</title>
<style nonce="__NONCE__">
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#edf3ef;color:#153d32;font:16px/1.6 system-ui,sans-serif;padding:24px}
main{width:min(100%,520px);background:white;padding:36px;border-radius:22px;box-shadow:0 18px 70px #1c4a3020}small{font-size:13px;letter-spacing:.07em;color:#537666}h1{font-size:28px;line-height:1.25;margin:14px 0 12px}p{color:#53655c;margin:0 0 24px}label{display:block;font-weight:600;margin-bottom:8px}input{width:100%;border:1px solid #b8cfc0;border-radius:10px;padding:14px;font:inherit;background:#f9fcf9}input:focus{outline:3px solid #b9ead3;border-color:#12664c}button{border:0;border-radius:10px;background:#12664c;color:white;padding:14px 18px;width:100%;font:600 16px system-ui;margin-top:18px;cursor:pointer}button:disabled{opacity:.6;cursor:wait}.note{font-size:13px;margin-top:18px;margin-bottom:0}.status{margin-top:20px;min-height:25px;font-weight:600}.foot{border-top:1px solid #e2eae5;padding-top:20px;margin-top:25px;font-size:13px}
</style><main><small>買嘢幫 / 本機設定</small><h1>連接 DeepSeek</h1>
<p>輸入 API key，讓買嘢幫使用你指定的模型進行商品研究。</p>
<form id="setup" autocomplete="off"><label for="key">DeepSeek API key</label>
<input id="key" name="key" type="password" autocomplete="new-password" spellcheck="false" autocapitalize="off" maxlength="512" required placeholder="貼上你的 API key">
<button id="save" type="submit">加密儲存</button></form>
<p class="note">僅傳送到這部電腦，並以目前 Windows 帳戶加密保存。這一步不呼叫模型，也不會付款。</p>
<div id="status" class="status" role="status" aria-live="polite"></div>
<p class="foot">儲存後可關閉此頁。模型驗收及服務重啟由團隊在下一步執行；模型供應商可能會就實際 API 使用計費。</p></main>
<script nonce="__NONCE__">'use strict';
const form=document.getElementById('setup'),field=document.getElementById('key'),button=document.getElementById('save'),status=document.getElementById('status');
form.addEventListener('submit',async event=>{event.preventDefault();button.disabled=true;status.textContent='正在加密儲存…';
let body=JSON.stringify({key:field.value});field.value='';
try{const response=await fetch('/save',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':__CSRF__},body});body='';const result=await response.json();
if(response.ok&&result.status==='saved'){status.textContent='已加密儲存。可以關閉此頁。';form.hidden=true;}
else{status.textContent=result.error==='INVALID_KEY'?'格式不正確，請重新貼上完整的 API key。':'未能儲存，請重新整理此頁後再試。';}}
catch{status.textContent='連線未完成，請重新整理此頁後再試。';}
finally{body='';field.value='';button.disabled=false;}});
</script></html>"""


def encrypt_dpapi(key: str) -> str:
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    encoded = base64.b64encode(ENCRYPT_SCRIPT.encode('utf-16le')).decode('ascii')
    result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
                             '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded], input=key.encode('utf-8'),
                            capture_output=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    encrypted = result.stdout.decode('ascii', errors='strict').strip()
    if result.returncode != 0 or not re.fullmatch(r'[0-9a-fA-F]{100,20000}', encrypted):
        raise RuntimeError('ENCRYPTION_FAILED')
    return encrypted


def save_secret(path: Path, key: str):
    encrypted = encrypt_dpapi(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', dir=path.parent,
                                         prefix='.deepseek-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, secret_path: Path):
        super().__init__(('127.0.0.1', port), SetupHandler)
        self.secret_path = secret_path
        self.sessions = {}
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()

    def handle_error(self, request, client_address):
        # Never dump handler locals, request bodies or headers to stderr.
        pass


class SetupHandler(BaseHTTPRequestHandler):
    server: SetupServer
    protocol_version = 'HTTP/1.0'
    server_version = 'LocalSetup'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def log_message(self, format, *args):
        pass

    @property
    def authority(self):
        return f'127.0.0.1:{self.server.server_port}'

    def host_valid(self):
        return self.headers.get_all('Host', []) == [self.authority]

    def respond(self, code: int, payload: dict | bytes, *, nonce=None, cookie=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if isinstance(payload, bytes) else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; connect-src 'self'; " + (f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'" if nonce else "style-src 'none'; script-src 'none'"))
        if cookie:
            self.send_header('Set-Cookie', f'myb_setup={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=900')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.host_valid():
            return self.respond(403, {'error': 'HOST_REJECTED'})
        if self.path != '/':
            return self.respond(404, {'error': 'NOT_FOUND'})
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self.respond(403, {'error': 'ORIGIN_REJECTED'})
        origin = self.headers.get('Origin')
        if origin is not None and origin != 'http://' + self.authority:
            return self.respond(403, {'error': 'ORIGIN_REJECTED'})
        sid, csrf, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.server.lock:
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v[1] > time.monotonic()}
            if len(self.server.sessions) >= 64:
                return self.respond(429, {'error': 'TOO_MANY_SESSIONS'})
            self.server.sessions[sid] = (csrf, time.monotonic() + 900)
        body = PAGE.replace('__NONCE__', nonce).replace('__CSRF__', json.dumps(csrf)).encode('utf-8')
        self.respond(200, body, nonce=nonce, cookie=sid)

    def do_POST(self):
        if not self.host_valid():
            return self.respond(403, {'error': 'HOST_REJECTED'})
        if self.headers.get_all('Origin', []) != ['http://' + self.authority]:
            return self.respond(403, {'error': 'ORIGIN_REJECTED'})
        if self.path != '/save':
            return self.respond(404, {'error': 'NOT_FOUND'})
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            sid = cookies['myb_setup'].value
        except Exception:
            return self.respond(403, {'error': 'CSRF_REJECTED'})
        with self.server.lock:
            session = self.server.sessions.get(sid)
        csrf_headers = self.headers.get_all('X-CSRF-Token', [])
        if (not session or session[1] <= time.monotonic() or len(csrf_headers) != 1
                or not secrets.compare_digest(csrf_headers[0], session[0])):
            return self.respond(403, {'error': 'CSRF_REJECTED'})
        lengths = self.headers.get_all('Content-Length', [])
        if (self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1
                or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 4096):
            return self.respond(400, {'error': 'BODY_REJECTED'})
        if self.headers.get_all('Content-Type', []) != ['application/json']:
            return self.respond(415, {'error': 'CONTENT_TYPE_REJECTED'})
        try:
            raw = self.rfile.read(int(lengths[0]))
            data = json.loads(raw.decode('utf-8'))
            raw = b''
            if (not isinstance(data, dict) or set(data) != {'key'} or
                    not isinstance(data['key'], str) or
                    not re.fullmatch(r'[A-Za-z0-9._-]{16,512}', data['key'])):
                return self.respond(400, {'error': 'INVALID_KEY'})
            with self.server.write_lock:
                save_secret(self.server.secret_path, data['key'])
            data.clear()
        except (ValueError, UnicodeError):
            return self.respond(400, {'error': 'INVALID_KEY'})
        except Exception:
            return self.respond(500, {'error': 'SAVE_FAILED'})
        self.respond(200, {'status': 'saved'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8769)
    args = parser.parse_args()
    path = Path(os.environ['LOCALAPPDATA']) / 'MaiYeBang/deepseek.key.dpapi'
    server = SetupServer(args.port, path)
    print(f'Local credential setup: http://127.0.0.1:{server.server_port} (pid {os.getpid()})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
