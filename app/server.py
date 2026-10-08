"""HTTP front for gemini-pool: MCP over JSON-RPC (POST /mcp), dashboard API and the dashboard page."""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from pool import ROLES, Pool

log = logging.getLogger("server")

PROTOCOL_VERSION = "2025-03-26"
DASHBOARD = Path(__file__).with_name("dashboard.html")
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
        "account_id": t["account_id"], "attempts": t["attempts"], "branch": t["branch"],
        "created_at": t["created_at"], "updated_at": t["updated_at"],
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

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode())

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return bool(_token) and hmac.compare_digest(auth.encode(), f"Bearer {_token}".encode())

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        if path in ("/", "/index.html"):
            # The page itself holds no data; every API call below needs the token.
            return self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
        if path.startswith("/api/"):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            if path == "/api/overview":
                return self._json(200, overview(_pool))
            match = re.fullmatch(r"/api/tasks/([0-9a-f]{12})", path)
            if match:
                task = _pool.get_task(match.group(1))
                return self._json(200, task) if task else self._json(404, {"error": "unknown task"})
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        match = re.fullmatch(r"/api/accounts/([A-Za-z0-9_-]{1,32})/quota", path)
        if match:
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            assert _pool is not None
            try:
                return self._json(200, _pool.check_quota(match.group(1)))
            except ValueError as exc:
                return self._json(404, {"error": str(exc)})
        if path != "/mcp":
            return self._send(404, b"not found", "text/plain")
        if not self._authorized():
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
