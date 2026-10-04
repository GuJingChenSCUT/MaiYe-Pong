"""Loopback-only Stop 1 application. Run with: python -m app.task_api --db PATH."""
from __future__ import annotations
import argparse
import json
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from app.auth import APIError, Auth
from app.social_auth import SocialAuth
from app.task_service import TaskService
from app.task_store import TaskStore, StorageUnavailable, encode
from app.agent_worker import AgentWorker
from slice04.domain import Rejected as DomainRejected
from reference.transaction_reference.kernel import Rejected as KernelRejected

WEB = Path(__file__).with_name("web")
STATIC_CONTENT_TYPES = {"index.html": "text/html", "app.js": "text/javascript", "styles.css": "text/css",
                        "assets/hong-kong-harbour.jpg": "image/jpeg",
                        "theme.js": "text/javascript",
                        "assets/hong-kong-day-v1.jpg": "image/jpeg",
                        "assets/hong-kong-night-v1.svg": "image/svg+xml",
                        "assets/maiyebang-logo-v1.png": "image/png",
                        "assets/maiyepong-logo-v2.png": "image/png",
                        "assets/google-g.png": "image/png",
                        "assets/wechat.svg": "image/svg+xml"}


class ApplicationServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store, *, start_worker=True, social_configs=None, social_exchange=None):
        if address[0] not in {"127.0.0.1", "localhost"}:
            raise ValueError("Stop 1 server must bind to loopback; public deployment is a separate review")
        self.store, self.auth, self.service = store, Auth(store), TaskService(store)
        self.worker = AgentWorker(self.service) if start_worker else None
        super().__init__(address, Handler)
        self.social = SocialAuth(store, origin=f"http://127.0.0.1:{self.server_address[1]}",
                                 configs=social_configs,
                                 **({"exchange": social_exchange} if social_exchange else {}))
        self.worker_thread = None
        if self.worker:
            self.worker_thread = threading.Thread(target=self.worker.serve, daemon=True)
            self.worker_thread.start()

    def server_close(self):
        if self.worker:
            self.worker.stopping.set()
        super().server_close()
        if self.worker_thread:
            self.worker_thread.join(timeout=3)

    def health(self):
        storage = self.store.health(probe=True)
        worker = self.worker.health() if self.worker else {"status": "external_unmonitored", "reason": None}
        if self.worker:
            worker["alive"] = bool(self.worker_thread and self.worker_thread.is_alive())
            if not worker["alive"]:
                worker.update(status="unavailable", reason="WORKER_NOT_RUNNING")
            elif time.time() - worker["last_progress_at"] > 240:
                worker.update(status="unavailable", reason="WORKER_PROGRESS_STALE")
        ready = storage["status"] == "available" and worker["status"] not in {"unavailable", "stopped"}
        return {"status": "ready" if ready else "unavailable", "storage": storage,
                "worker": worker, "automatic_payment_dispatch": False}

    def ensure_execution_ready(self):
        # Stopping an existing task remains possible while its worker is down.
        # A --no-worker process cannot attest to an externally supervised worker.
        if self.worker:
            worker = self.worker.health()
            if (not self.worker_thread or not self.worker_thread.is_alive()
                    or worker["status"] in {"unavailable", "stopped"}
                    or time.time() - worker["last_progress_at"] > 240):
                raise APIError(503, "WORKER_UNAVAILABLE", "研究服務暫時不可用；請稍後查詢原任務，仍可嘗試停止任務。")


class Handler(BaseHTTPRequestHandler):
    server_version = "HacKU-Stop1"
    def log_message(self, format, *args):
        # Access codes, draft text and credentials are never logged by this server.
        return

    def _reply(self, status, data, *, content_type="application/json; charset=utf-8", cookie=None, location=None):
        raw = encode(data).encode() if not isinstance(data, bytes) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            for value in ([cookie] if isinstance(cookie, str) else cookie):
                self.send_header("Set-Cookie", value)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "TRANSFER_ENCODING_UNSUPPORTED")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(400, "INVALID_CONTENT_LENGTH")
        if not 0 <= size <= 65536:
            raise APIError(413, "BODY_TOO_LARGE")
        if size and self.headers.get_content_type() != "application/json":
            raise APIError(415, "JSON_REQUIRED")
        try:
            self._body_read = True
            body = json.loads(self.rfile.read(size) or b"{}", parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite")))
        except (ValueError, UnicodeError):
            raise APIError(400, "INVALID_JSON")
        if not isinstance(body, dict):
            raise APIError(400, "OBJECT_BODY_REQUIRED")
        return body

    def _discard_pending_body(self):
        # Early auth/availability rejection still consumes a small declared body.
        # Closing an unread POST can otherwise reset the connection on Windows,
        # hiding the actual 403/503 from the caller. Never wait without a bound.
        if self._body_read or self.headers.get("Transfer-Encoding"):
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        if not 0 < size <= 65536:
            return
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(1)
            self.rfile.read(size)
        except (OSError, ValueError):
            pass
        finally:
            self.connection.settimeout(previous_timeout)
            self._body_read = True

    def _origin(self, *, oauth_callback=False):
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        if host not in allowed_hosts:
            raise APIError(403, "HOST_REJECTED")
        origin = self.headers.get("Origin")
        if not oauth_callback and origin and origin != "http://" + host:
            raise APIError(403, "ORIGIN_REJECTED")
        if not oauth_callback and self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise APIError(403, "CROSS_SITE_REJECTED")

    def _token(self, name="hacku_session"):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            raise APIError(401, "INVALID_SESSION_COOKIE")
        return cookie[name].value if name in cookie else ""

    def _handle(self):
        path = urlsplit(self.path).path
        method = self.command
        auth_parts = path.strip("/").split("/")
        auth_route = (len(auth_parts) == 5 and auth_parts[:3] == ["api", "v1", "auth"]
                      and auth_parts[3] in {"google", "wechat"})
        callback = auth_route and auth_parts[4] == "callback" and method == "GET"
        self._origin(oauth_callback=callback)
        asset = "index.html" if path == "/" else path.removeprefix("/")
        if method == "GET" and asset in STATIC_CONTENT_TYPES:
            file = WEB / asset
            # Windows registry MIME mappings can report .js as text/plain, which
            # browsers correctly reject under nosniff. These assets are known.
            content_type = STATIC_CONTENT_TYPES[asset]
            if content_type.startswith("text/"):
                content_type += "; charset=utf-8"
            return self._reply(200, file.read_bytes(), content_type=content_type)
        if method == "GET" and path == "/api/v1/config":
            return self._reply(200, self.server.service.config())
        if method == "GET" and path == "/api/v1/health":
            health = self.server.health()
            return self._reply(200 if health["status"] == "ready" else 503, health)
        if method == "GET" and path == "/api/v1/auth/providers":
            return self._reply(200, {"providers": self.server.social.status()})
        if auth_route:
            provider, action = auth_parts[3:]
            cookie_name = "hacku_oauth_" + provider
            if method == "POST" and action == "start":
                if self.headers.get("Host") != urlsplit(self.server.social.origin).netloc:
                    raise APIError(400, "LOGIN_CANONICAL_ORIGIN_REQUIRED", "請使用 127.0.0.1 網址登入。")
                if self._body():
                    raise APIError(400, "LOGIN_START_BODY_MUST_BE_EMPTY")
                result, browser = self.server.social.start(provider)
                return self._reply(200, result, cookie=f"{cookie_name}={browser}; HttpOnly; SameSite=Lax; Path=/api/v1/auth/{provider}; Max-Age=300")
            if callback:
                clear_cookie = f"{cookie_name}=; HttpOnly; SameSite=Lax; Path=/api/v1/auth/{provider}; Max-Age=0"
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=12)
                    if any(len(values) != 1 for values in query.values()):
                        raise APIError(400, "LOGIN_CALLBACK_AMBIGUOUS")
                    params = {key: values[0] for key, values in query.items()}
                    token, _ = self.server.social.finish(provider, params=params, browser=self._token(cookie_name))
                    # Rotate an existing session instead of silently retaining a
                    # privileged local identity or linking its task history.
                    old_token = self._token()
                    if old_token:
                        self.server.auth.logout(old_token)
                    return self._reply(303, b"", cookie=[clear_cookie,
                        f"hacku_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"], location="/?login=success")
                except (APIError, ValueError):
                    # An unrelated/forged callback must not erase another tab's
                    # valid browser binding. Consumed attempts cannot be reused;
                    # the short-lived cookie expires or is replaced on restart.
                    return self._reply(303, b"", location="/?login=retry")
        if method == "POST" and path == "/api/v1/session":
            body = self._body()
            if set(body) != {"access_code"}:
                raise APIError(400, "ACCESS_CODE_ONLY")
            token, result = self.server.auth.login(body["access_code"])
            return self._reply(200, result, cookie=f"hacku_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800")
        token = self._token()
        identity = self.server.auth.session(token, csrf=self.headers.get("X-CSRF-Token"), mutate=method != "GET")
        actor = identity["actor"]
        if path == "/api/v1/session":
            if method == "GET":
                return self._reply(200, identity)
            if method == "DELETE":
                self.server.auth.logout(token)
                return self._reply(200, {"logged_out": True}, cookie="hacku_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        if path == "/api/v1/tasks" and method == "GET":
            return self._reply(200, self.server.service.list(actor))
        if path == "/api/v1/intents" and method == "POST":
            self.server.ensure_execution_ready()
            data = self.server.service.mutation(actor, "create", None, self.headers.get("Idempotency-Key"), self._body())
            return self._reply(200, data)
        parts = path.strip("/").split("/")
        if len(parts) in {4, 5} and parts[:3] == ["api", "v1", "tasks"]:
            task_id = parts[3]
            if len(parts) == 4 and method == "GET":
                return self._reply(200, self.server.service.get(actor, task_id))
            if len(parts) == 5 and method == "POST":
                action = {"answers": "answers", "runs": "run", "approvals": "approve", "stop": "stop"}.get(parts[4])
                if action:
                    if action != "stop":
                        self.server.ensure_execution_ready()
                    data = self.server.service.mutation(actor, action, task_id, self.headers.get("Idempotency-Key"), self._body())
                    return self._reply(200, data)
        raise APIError(404, "ROUTE_NOT_FOUND")

    def _dispatch(self):
        self._body_read = False
        try:
            self._handle()
        except APIError as error:
            self._discard_pending_body()
            self._reply(error.status, {"error": error.code, "message": error.message})
        except (DomainRejected, KernelRejected) as error:
            self._reply(409, {"error": str(error), "message": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except StorageUnavailable:
            self._discard_pending_body()
            self._reply(503, {"error": "STORAGE_UNAVAILABLE",
                "message": "暫時無法確認或保存本次操作；停止亦未確認。請待服務恢復後查詢原任務，切勿另建付款。"})
        except Exception:
            self._reply(500, {"error": "INTERNAL_ERROR", "message": "服務未能完成本次操作，請查閱原任務狀態。"})

    do_GET = do_POST = do_DELETE = _dispatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Persistent local database file shared with the worker")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bootstrap", action="store_true", help="Provision and print new local buyer/merchant/operator access codes")
    parser.add_argument("--no-worker", action="store_true", help="Use a separately started durable research worker")
    args = parser.parse_args()
    store = TaskStore(args.db)
    if args.bootstrap:
        auth = Auth(store)
        for role, merchant in (("buyer", None), ("merchant", "fixture-merchant-A"), ("operator", None)):
            code = auth.provision(actor_id="local-" + role, role=role, merchant_id=merchant)
            print(f"Local {role} access code (keep private): {code}", flush=True)
    server = ApplicationServer(("127.0.0.1", args.port), store, start_worker=not args.no_worker)
    print(f"HacKU local application: http://127.0.0.1:{server.server_address[1]} — synthetic goods / LocalPSP; no automatic payment dispatch", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
