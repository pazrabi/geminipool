"""HTTP front for gemini-pool: MCP over JSON-RPC (POST /mcp), dashboard API and the dashboard page."""

from __future__ import annotations

import base64
import hmac
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import sso
import uploads as uploads_mod
from dashboard_api import _task_summary, overview, uploads_limits
from mcp_rpc import TOOLS, handle_rpc  # noqa: F401  (TOOLS is used by tests)
from oauth import OAuth, OAuthError, redirect_matches
from pool import Pool

log = logging.getLogger("server")

WEB_DIR = Path(__file__).with_name("web")
DASHBOARD = WEB_DIR / "dashboard.html"
# public static files of the dashboard page (the page itself holds no data; every API call needs the token)
DASHBOARD_ASSETS = {
    "/dashboard.css": (WEB_DIR / "dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": (WEB_DIR / "dashboard.js", "text/javascript; charset=utf-8"),
}
_pool: Pool | None = None
_oauth: OAuth | None = None
_token: str = ""



def configure(pool: Pool, token: str, oauth: OAuth | None = None) -> None:
    global _pool, _token, _oauth
    _pool = pool
    _token = token
    _oauth = oauth

class Handler(BaseHTTPRequestHandler):
    server_version = "gemini-pool"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep Railway logs readable
        log.info("%s %s", self.command, self.path)

    def _send(self, code: int, body: bytes, ctype: str = "application/json", headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code: int, obj: Any, headers: dict[str, str] | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), headers=headers)

    def _bearer(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def _authorized(self) -> bool:
        """The static pool token: used by Claude Code and the dashboard."""
        return bool(_token) and hmac.compare_digest(self._bearer().encode(), _token.encode())

    def _sso_session(self) -> None:
        """The dashboard asks here when it holds no token. A valid single sign-on cookie (signed with a secret only
        this server and DroidHub know) means the owner already logged in once, so the dashboard gets the pool token
        instead of showing its own login form. Nothing else accepts the cookie: MCP and the API keep requiring the token."""
        secret = os.environ.get("POOL_SSO_SECRET", "")
        name = os.environ.get("POOL_SSO_COOKIE", "molokohub_sso")
        user = sso.user_from_cookie_header(self.headers.get("Cookie", ""), name, secret)
        # A page on another site must not be able to trigger this, even if the browser would send the cookie.
        if user is None or self.headers.get("Sec-Fetch-Site", "same-origin") == "cross-site":
            return self._json(401, {"error": "unauthorized"})
        log.info("sso session for %s", user)  # _send already marks every answer no-store
        return self._json(200, {"token": _token, "user": user, "logout_url": os.environ.get("POOL_SSO_LOGOUT_URL", "")})

    def _mcp_authorized(self) -> bool:
        """MCP also accepts the access tokens issued by the OAuth flow (Claude custom connectors)."""
        return self._authorized() or (_oauth is not None and _oauth.validate_access(self._bearer()))

    def _base_url(self) -> str:
        configured = os.environ.get("PUBLIC_URL", "").rstrip("/")
        if configured:
            return configured
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        return f"{proto}://{self.headers.get('Host', 'localhost')}"

    def _unauthorized_mcp(self) -> None:
        # Claude only starts OAuth after a 401 that points at the protected resource metadata.
        meta = f"{self._base_url()}/.well-known/oauth-protected-resource"
        self._json(401, {"error": "unauthorized"}, {"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})

    def _read_body(self, limit: int = 100_000) -> bytes:
        return self.rfile.read(min(int(self.headers.get("Content-Length") or 0), limit))

    def _form(self) -> dict[str, str]:
        parsed = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"), keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _oauth_error(self, exc: OAuthError) -> None:
        headers = {"WWW-Authenticate": 'Basic realm="oauth"'} if exc.status == 401 else None
        self._json(exc.status, {"error": exc.error, "error_description": exc.description}, headers)

    # ---- OAuth endpoints -----------------------------------------------------

    def _discovery(self, path: str) -> bool:
        base = self._base_url()
        if path.startswith("/.well-known/oauth-protected-resource"):
            self._json(200, {
                "resource": f"{base}/mcp",
                "authorization_servers": [base],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["mcp"],
            })
            return True
        if path.startswith("/.well-known/oauth-authorization-server"):
            self._json(200, {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                "scopes_supported": ["mcp", "offline_access"],
            })
            return True
        return False

    def _consent(self, params: dict[str, str], error: str = "", code: int = 200) -> None:
        client = _oauth.get_client(params.get("client_id", "")) if _oauth else None
        redirect = params.get("redirect_uri", "")
        if (
            client is None
            or not redirect_matches(client["redirect_uris"], redirect)
            or params.get("response_type") != "code"
            or params.get("code_challenge_method") != "S256"
            or not params.get("code_challenge")
        ):
            return self._send(400, "Некорректный запрос авторизации.".encode(), "text/plain; charset=utf-8")
        hidden = "".join(
            f'<input type="hidden" name="{k}" value="{html.escape(params.get(k, ""))}">'
            for k in ("client_id", "redirect_uri", "response_type", "state", "code_challenge", "code_challenge_method",
                      "scope", "resource")
        )
        page = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Подключение к Gemini Pool</title>
<style>:root{{color-scheme:light dark}}body{{font:16px/1.5 system-ui,sans-serif;max-width:440px;margin:10vh auto;padding:0 16px}}
input{{width:100%;box-sizing:border-box;font:inherit;padding:10px;margin:8px 0 14px}}
button{{font:inherit;padding:10px 18px;cursor:pointer}}.err{{color:#c8382d}}.mono{{font-family:ui-monospace,monospace}}</style>
<h2>Подключение к Gemini Pool</h2>
<p>Приложение <b>{html.escape(client["name"] or "MCP client")}</b> просит доступ к вашему пулу: ставить задачи и
читать результаты.</p>
<p>После подтверждения вы вернётесь на <span class="mono">{html.escape(urllib.parse.urlsplit(redirect).hostname or "")}</span>.</p>
<form method="post" action="/authorize">{hidden}
<label>Токен пула<input type="password" name="pool_token" autocomplete="off" required autofocus></label>
<div class="err">{html.escape(error)}</div>
<button type="submit">Разрешить</button></form></html>"""
        self._send(code, page.encode(), "text/html; charset=utf-8",
                   {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"})

    def _authorize_submit(self) -> None:
        form = self._form()
        assert _oauth is not None
        # Re-validates client, redirect and PKCE parameters; renders the 400 page if they are wrong.
        client = _oauth.get_client(form.get("client_id", ""))
        redirect = form.get("redirect_uri", "")
        if client is None or not redirect_matches(client["redirect_uris"], redirect) or form.get(
            "code_challenge_method"
        ) != "S256" or form.get("response_type") != "code" or not form.get("code_challenge"):
            return self._consent(form)
        verdict = _oauth.check_owner_token(form.get("pool_token", ""))
        if verdict == "locked":
            return self._consent(form, "Слишком много неверных попыток. Попробуйте через 10 минут.", 429)
        if verdict != "ok":
            return self._consent(form, "Неверный токен.", 200)
        code = _oauth.create_code(client["client_id"], redirect, form["code_challenge"])
        query = {"code": code}
        if form.get("state"):
            query["state"] = form["state"]
        sep = "&" if urllib.parse.urlsplit(redirect).query else "?"
        self._send(302, b"", "text/plain", {"Location": redirect + sep + urllib.parse.urlencode(query)})

    def _token_endpoint(self) -> None:
        assert _oauth is not None
        form = self._form()
        client_id, secret = form.get("client_id", ""), form.get("client_secret")
        basic = self.headers.get("Authorization", "")
        if basic.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(basic[6:]).decode().partition(":")
                client_id, secret = urllib.parse.unquote(user), urllib.parse.unquote(pw)
            except (ValueError, UnicodeDecodeError):
                return self._oauth_error(OAuthError("invalid_client", "malformed Basic header", 401))
        try:
            grant = form.get("grant_type")
            if grant == "authorization_code":
                result = _oauth.exchange_code(client_id, secret, form.get("code", ""),
                                              form.get("redirect_uri", ""), form.get("code_verifier", ""))
            elif grant == "refresh_token":
                result = _oauth.refresh(client_id, secret, form.get("refresh_token", ""))
            else:
                raise OAuthError("unsupported_grant_type", "use authorization_code or refresh_token")
        except OAuthError as exc:
            return self._oauth_error(exc)
        self._json(200, result)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        if path.startswith("/.well-known/") and self._discovery(path):
            return
        if path == "/authorize" and _oauth is not None:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return self._consent({k: v[0] for k, v in query.items()})
        if path == "/mcp":
            # No server-initiated stream: unauthenticated callers get the OAuth challenge, others a 405.
            if not self._mcp_authorized():
                return self._unauthorized_mcp()
            return self._send(405, b'{"error": "use POST"}', headers={"Allow": "POST"})
        if path in ("/", "/index.html"):
            # The page itself holds no data; every API call below needs the token.
            return self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8",
                              {"Content-Security-Policy": "frame-ancestors 'self' https://molokohub.site"})
        if path in DASHBOARD_ASSETS:
            asset, ctype = DASHBOARD_ASSETS[path]
            return self._send(200, asset.read_bytes(), ctype)
        if path == "/api/sso/session":
            return self._sso_session()
        if path.startswith("/api/"):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            if path == "/api/overview":
                return self._json(200, overview(_pool))
            if path == "/api/requests":
                return self._json(200, {"requests": _pool.list_requests()})
            if path == "/api/models":
                return self._json(200, {"models": _pool.model_guide()})
            if path == "/api/uploads":
                return self._json(200, {"uploads": _pool.uploads.list(), "limits": uploads_limits(_pool)})
            one = re.fullmatch(r"/api/uploads/([0-9a-f]{12})", path)
            if one:
                try:
                    return self._json(200, {**_pool.uploads.get(one.group(1)),
                                            "file_names": _pool.uploads.file_list(one.group(1), 500)})
                except uploads_mod.UploadError as exc:
                    return self._json(404, {"error": str(exc)})
            download = re.fullmatch(r"/api/uploads/([0-9a-f]{12})/download", path)
            if download:
                return self._download(download.group(1))
            models = re.fullmatch(r"/api/accounts/([A-Za-z0-9_-]{1,32})/models", path)
            if models:
                try:
                    return self._json(200, _pool.list_models(models.group(1)))
                except ValueError as exc:
                    return self._json(404, {"error": str(exc)})
            match = re.fullmatch(r"/api/tasks/([0-9a-f]{12})", path)
            if match:
                task = _pool.task_view(match.group(1))
                return self._json(200, task) if task else self._json(404, {"error": "unknown task"})
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if _oauth is not None and path in ("/register", "/authorize", "/token"):
            if path == "/authorize":
                return self._authorize_submit()
            if path == "/token":
                return self._token_endpoint()
            try:
                meta = json.loads(self._read_body() or b"{}")
                if not isinstance(meta, dict):
                    raise ValueError("body must be a JSON object")
                return self._json(201, _oauth.register(meta))
            except OAuthError as exc:
                return self._oauth_error(exc)
            except ValueError:
                return self._json(400, {"error": "invalid_client_metadata", "error_description": "invalid JSON"})
        match = re.fullmatch(r"/api/accounts/([A-Za-z0-9_-]{1,32})/quota", path)
        if match:
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            try:
                return self._json(200, _pool.check_quota(match.group(1)))
            except ValueError as exc:
                return self._json(404, {"error": str(exc)})
        if path == "/api/accounts" or re.fullmatch(r"/api/accounts/([A-Za-z0-9_-]{1,32})/enabled", path):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            try:
                body = json.loads(self._read_body(10_000) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                if path == "/api/accounts":
                    return self._json(201, _pool.add_account(str(body.get("id", "")), str(body.get("role", "developer"))))
                if not isinstance(body.get("enabled"), bool):
                    raise ValueError('body must be {"enabled": true|false}')
                account_id = path.split("/")[3]
                return self._json(200, _pool.set_account_enabled(account_id, body["enabled"]))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
        if path == "/api/uploads" or re.fullmatch(r"/api/uploads/[0-9a-f]{12}", path):
            return self._uploads_post(path)
        if path == "/api/requests":
            return self._request_post()
        cancel = re.fullmatch(r"/api/tasks/([0-9a-f]{12})/cancel", path)
        if cancel:
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            try:
                return self._json(200, _task_summary(_pool.cancel_task(cancel.group(1))))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
        recover = re.fullmatch(r"/api/tasks/([0-9a-f]{12})/recover", path)
        if recover:
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            try:
                return self._json(200, _task_summary(_pool.recover_task(recover.group(1))))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
        if path == "/api/drain":
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            try:
                body = json.loads(self._read_body(10_000) or b"{}")
                if not isinstance(body, dict) or not isinstance(body.get("drain"), bool):
                    raise ValueError('body must be {"drain": true|false}')
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            log.info("maintenance mode %s", "on" if body["drain"] else "off")
            return self._json(200, _pool.set_draining(body["drain"]))
        model_match = re.fullmatch(r"/api/accounts/([A-Za-z0-9_-]{1,32})/model", path)
        if model_match:
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 10_000)) or b"{}")
                model = body.get("model")
                if model is not None and not isinstance(model, str):
                    raise ValueError("model must be a string or null")
                return self._json(200, _pool.set_model(model_match.group(1), model))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
        if path != "/mcp":
            return self._send(404, b"not found", "text/plain")
        if not self._mcp_authorized():
            return self._unauthorized_mcp()
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return self._json(413, {"error": "too large"})
        try:
            msg = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid json"})
        resp = handle_rpc(_pool, msg)
        if resp is None:
            return self._send(202, b"")
        self._json(200, resp)

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        match = re.fullmatch(r"/api/uploads/([0-9a-f]{12})/file", parsed.path)
        if not match:
            return self._send(404, b"not found", "text/plain")
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        assert _pool is not None
        rel = urllib.parse.parse_qs(parsed.query).get("path", [""])[0]
        try:
            length = int(self.headers.get("Content-Length") or -1)
            if length < 0:
                raise uploads_mod.UploadError("Content-Length is required")
            self.close_connection = True  # a rejected upload leaves unread bytes behind; do not reuse the connection
            return self._json(200, _pool.uploads.add_file(match.group(1), rel, self.rfile, length))
        except uploads_mod.UploadError as exc:
            return self._json(400, {"error": str(exc)})

    def _request_post(self) -> None:
        """A task typed into the dashboard: files only (scratch) or a project from the storage."""
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        assert _pool is not None
        try:
            body = json.loads(self._read_body(100_000) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            goal = body.get("goal")
            if not isinstance(goal, str) or not goal.strip() or len(goal) > 20_000:
                raise ValueError("write what to do (up to 20000 characters)")
            role = body.get("role", "developer")
            if role not in ("developer", "debugger"):
                raise ValueError("role must be developer or debugger")
            model = body.get("model")
            if model is not None and not isinstance(model, str):
                raise ValueError("model must be a string")
            attach = body.get("uploads", [])
            if not isinstance(attach, list) or not all(isinstance(i, str) for i in attach) or len(attach) > 30:
                raise ValueError("uploads must be a list of ids")
            for key in ("project", "save_to"):
                if body.get(key) is not None and not isinstance(body[key], str):
                    raise ValueError(f"{key} must be an upload id or null")
            project = body.get("project")
            info = _pool.submit_with_info(
                role=role, goal=goal.strip(), repo=f"upload:{project}" if project else "scratch", allowed_files=["*"],
                base_branch="main", model=model or None, review=bool(body.get("review")) and bool(project),
                uploads=attach, save_to=body.get("save_to"), origin="panel")
            return self._json(201, {"task_id": info["task_id"], "warnings": info["warnings"]})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})

    def _uploads_post(self, path: str) -> None:
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        assert _pool is not None
        try:
            body = json.loads(self._read_body(20_000) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            if "pinned" in body and not isinstance(body["pinned"], bool):
                raise ValueError("pinned must be true or false")
            for key in ("name", "note"):
                if key in body and not isinstance(body[key], str):
                    raise ValueError(f"{key} must be a string")
            if path == "/api/uploads":
                return self._json(201, _pool.uploads.create(
                    body.get("name", ""), body.get("note", ""), body.get("pinned", False)))
            return self._json(200, _pool.uploads.update(
                path.rsplit("/", 1)[1], pinned=body.get("pinned"),
                note=body.get("note"), name=body.get("name")))
        except (ValueError, uploads_mod.UploadError) as exc:
            return self._json(400, {"error": str(exc)})

    def _download(self, upload_id: str) -> None:
        """A zip of the upload, or of one branch of an uploaded project (?branch=pool/<id>)."""
        assert _pool is not None
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        branch = query.get("branch", [""])[0]
        try:
            root = _pool.uploads.path(upload_id)
            _pool.uploads.get(upload_id)
            with tempfile.TemporaryDirectory(dir=_pool.data) as tmp:
                out = Path(tmp) / "a.zip"
                if branch:
                    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch:
                        raise uploads_mod.UploadError("bad branch name")
                    r = subprocess.run(["git", "archive", "--format=zip", "-o", str(out), branch], cwd=root,
                                       capture_output=True, text=True, timeout=300)
                    if r.returncode != 0:
                        raise uploads_mod.UploadError("branch not found in this upload")
                else:
                    base = Path(tmp) / "src"
                    shutil.copytree(root, base, symlinks=True, ignore=shutil.ignore_patterns(".git"))
                    shutil.make_archive(str(out)[:-4], "zip", base)
                size = out.stat().st_size
                name = f"{_pool.uploads.get(upload_id)['name']}{'-' + branch.replace('/', '_') if branch else ''}.zip"
                ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{ascii_name}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(out, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
        except uploads_mod.UploadError as exc:
            self._json(404, {"error": str(exc)})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        one_file = re.fullmatch(r"/api/uploads/([0-9a-f]{12})/file", parsed.path)
        if one_file:
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            try:
                _pool.uploads.delete_file(one_file.group(1), urllib.parse.parse_qs(parsed.query).get("path", [""])[0])
            except uploads_mod.UploadError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"removed": True})
        up = re.fullmatch(r"/api/uploads/([0-9a-f]{12})", parsed.path)
        if up:
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            try:
                _pool.uploads.delete(up.group(1))
            except uploads_mod.UploadError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"removed": up.group(1)})
        match = re.fullmatch(r"/api/accounts/([A-Za-z0-9_-]{1,32})", self.path.split("?", 1)[0])
        if not match:
            return self._send(404, b"not found", "text/plain")
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        assert _pool is not None
        try:
            _pool.remove_account(match.group(1))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(200, {"removed": match.group(1)})
