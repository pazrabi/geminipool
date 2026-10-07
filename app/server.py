"""HTTP front for gemini-pool: MCP over JSON-RPC at POST /mcp and a read-only status page."""

from __future__ import annotations

import hmac
import html
import json
import logging
from http.server import BaseHTTPRequestHandler
from typing import Any

from pool import ROLES, Pool

log = logging.getLogger("server")

PROTOCOL_VERSION = "2025-03-26"
_pool: Pool | None = None
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
        "name": "list_accounts",
        "description": "Accounts with role, state, pause end and current task.",
        "inputSchema": _OBJ,
    },
]


def configure(pool: Pool, token: str) -> None:
    global _pool, _token
    _pool = pool
    _token = token


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
                "serverInfo": {"name": "gemini-pool", "version": "0.1.0"},
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


def render_status(pool: Pool) -> str:
    acc_rows = "".join(
        f"<tr><td>{html.escape(a['id'])}</td><td>{html.escape(a['role'])}</td><td>{html.escape(a['state'])}</td>"
        f"<td>{'enabled' if a['enabled'] else 'disabled'}</td><td>{html.escape(a['busy_task'] or '-')}</td></tr>"
        for a in pool.list_accounts()
    )
    task_rows = "".join(
        f"<tr><td>{html.escape(t['id'])}</td><td>{html.escape(t['role'])}</td><td>{html.escape(t['status'])}</td>"
        f"<td>{t['attempts']}</td><td>{html.escape(t['account_id'] or '-')}</td></tr>"
        for t in pool.list_tasks(20)
    )
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>gemini-pool</title>
<style>body{{font:14px system-ui;margin:16px}}table{{border-collapse:collapse;margin-bottom:24px}}
td,th{{border:1px solid #ccc;padding:4px 8px;text-align:left}}</style>
<h2>Accounts</h2><table><tr><th>id</th><th>role</th><th>state</th><th>enabled</th><th>busy task</th></tr>{acc_rows}</table>
<h2>Recent tasks</h2><table><tr><th>id</th><th>role</th><th>status</th><th>attempts</th><th>account</th></tr>{task_rows}</table>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "gemini-pool"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep Railway logs readable
        log.info("%s %s", self.command, self.path)

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._send(200, b"ok", "text/plain")
        if self.path in ("/", "/status") and _pool is not None:
            return self._send(200, render_status(_pool).encode(), "text/html; charset=utf-8")
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if self.path != "/mcp":
            return self._send(404, b"not found", "text/plain")
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {_token}"
        if not _token or not hmac.compare_digest(auth.encode(), expected.encode()):
            return self._json(401, {"error": "unauthorized"})
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
