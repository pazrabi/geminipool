"""HTTP front for gemini-pool: MCP over JSON-RPC (POST /mcp), dashboard API and the dashboard page."""

from __future__ import annotations

import base64
import hmac
import html
import json
import logging
import os
import re
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from oauth import OAuth, OAuthError, redirect_matches
from pool import ROLES, Pool

log = logging.getLogger("server")

PROTOCOL_VERSION = "2025-03-26"
DASHBOARD = Path(__file__).with_name("dashboard.html")
_pool: Pool | None = None
_oauth: OAuth | None = None
_token: str = ""

_OBJ = {"type": "object"}
TOOLS = [
    {
        "name": "submit_task",
        "description": "Queue a narrow development task. Returns task_id; the result is a git branch pool/<task_id> plus a report.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": list(ROLES)},
                "goal": {"type": "string"},
                "repo": {"type": "string", "description": "https://github.com/owner/repo.git"},
                "allowed_files": {"type": "array", "items": {"type": "string"}, "description": "glob patterns"},
                "base_branch": {"type": "string", "default": "main"},
                "test_cmd": {"type": "string", "description": "shell command run in the worktree"},
                "model": {
                    "type": "string",
                    "description": "optional model id (see list_models); overrides the agent's own model for this task",
                },
            },
            "required": ["role", "goal", "repo", "allowed_files"],
        },
    },
    {
        "name": "task_status",
        "description": "Status of one task: queued, running, done or failed.",
        "inputSchema": {**_OBJ, "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "task_result",
        "description": "Full report of a finished task: summary, changed files, violations, test result, branch.",
        "inputSchema": {**_OBJ, "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "list_tasks",
        "description": "Recent tasks, newest first.",
        "inputSchema": {**_OBJ, "properties": {"limit": {"type": "integer", "default": 20}}},
    },
    {
        "name": "list_models",
        "description": "Model ids that can be passed as `model` to submit_task, with display names.",
        "inputSchema": _OBJ,
    },
    {
        "name": "list_accounts",
        "description": "Accounts with role, state, pause end and current task.",
        "inputSchema": _OBJ,
    },
]


def configure(pool: Pool, token: str, oauth: OAuth | None = None) -> None:
    global _pool, _token, _oauth
    _pool = pool
    _token = token
    _oauth = oauth


def _call_tool(name: str, args: dict[str, Any]) -> Any:
    assert _pool is not None
    if name == "submit_task":
        task_id = _pool.submit(
            role=args["role"],
            goal=args["goal"],
            repo=args["repo"],
            allowed_files=args["allowed_files"],
            base_branch=args.get("base_branch", "main"),
            test_cmd=args.get("test_cmd"),
            model=args.get("model"),
        )
        return {"task_id": task_id, "status": "queued"}
    if name == "task_status":
        task = _pool.get_task(args["task_id"])
        if task is None:
            raise ValueError("unknown task_id")
        return {k: task[k] for k in ("id", "status", "attempts", "account_id", "branch", "updated_at")}
    if name == "task_result":
        task = _pool.get_task(args["task_id"])
        if task is None:
            raise ValueError("unknown task_id")
        return {"id": task["id"], "status": task["status"], "report": task["report"]}
    if name == "list_tasks":
        return _pool.list_tasks(int(args.get("limit", 20)))
    if name == "list_accounts":
        return _pool.list_accounts()
    if name == "list_models":
        return [{"id": k, "name": v} for k, v in _pool.model_catalog().items()]
    raise ValueError(f"unknown tool {name}")


def handle_rpc(msg: dict[str, Any]) -> dict[str, Any] | None:
    rid = msg.get("id")
    if rid is None:  # notification, e.g. notifications/initialized
        return None
    method = msg.get("method")
    params = msg.get("params") or {}
    try:
        if method == "initialize":
            result: Any = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "gemini-pool", "version": "0.2.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                data = _call_tool(params.get("name", ""), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}]}
            except (ValueError, KeyError) as exc:
                result = {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    except Exception:
        log.exception("rpc %s failed", method)
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "internal error"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


# ---- dashboard API --------------------------------------------------------


def _task_summary(t: dict[str, Any]) -> dict[str, Any]:
    report = t["report"] or {}
    tests = report.get("tests") or {}
    note = report.get("error") or report.get("last_error") or "; ".join(report.get("violations") or []) or None
    return {
        "id": t["id"], "role": t["role"], "status": t["status"], "goal": t["goal"], "repo": t["repo"],
        "account_id": t["account_id"], "attempts": t["attempts"], "branch": t["branch"], "model": t["model"],
        "created_at": t["created_at"], "updated_at": t["updated_at"], "started_at": t["started_at"],
        "pushed": report.get("pushed"), "tests_passed": tests.get("passed") if tests else None,
        "note": note,
    }


def overview(pool: Pool) -> dict[str, Any]:
    tasks = pool.list_tasks(60)
    counts = {s: 0 for s in ("queued", "running", "done", "failed")}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {
        "now": time.time(),
        "accounts": pool.list_accounts(),
        "tasks": [_task_summary(t) for t in tasks],
        "events": pool.list_events(50),
        "counts": counts,
    }


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
            return self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
        if path.startswith("/api/"):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            if path == "/api/overview":
                return self._json(200, overview(_pool))
            models = re.fullmatch(r"/api/accounts/([A-Za-z0-9_-]{1,32})/models", path)
            if models:
                try:
                    return self._json(200, _pool.list_models(models.group(1)))
                except ValueError as exc:
                    return self._json(404, {"error": str(exc)})
            match = re.fullmatch(r"/api/tasks/([0-9a-f]{12})", path)
            if match:
                task = _pool.get_task(match.group(1))
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
        resp = handle_rpc(msg)
        if resp is None:
            return self._send(202, b"")
        self._json(200, resp)
