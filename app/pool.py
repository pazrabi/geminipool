"""gemini-pool core: SQLite state, account scheduling, task execution."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import signal
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import git_ops

log = logging.getLogger("pool")

ROLES = ("developer", "reviewer", "debugger")
MAX_ATTEMPTS = 3
QUOTA_PAUSE_S = 5 * 3600
NETWORK_RETRY_S = 30
TASK_TIMEOUT_S = int(os.environ.get("POOL_TASK_TIMEOUT", "1800"))
TEST_TIMEOUT_S = int(os.environ.get("POOL_TEST_TIMEOUT", "900"))

QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|\b429\b|quota|rate.?limit|too many requests", re.I)
AUTH_RE = re.compile(r"\b401\b|UNAUTHENTICATED|API key not valid|PERMISSION_DENIED|no auth", re.I)
NETWORK_RE = re.compile(r"ECONNRESET|ETIMEDOUT|ENOTFOUND|fetch failed|socket hang up", re.I)

ROLE_TEXT = {
    "developer": (
        "Implement the goal below. Change only files that match the allowed patterns. "
        "Keep the change minimal. Do not push anything."
    ),
    "reviewer": (
        "Review the code for the goal below. Do NOT modify, create or delete any file. "
        "Answer with findings (problems, risks, suggestions), ordered by severity."
    ),
    "debugger": (
        "Find the root cause of the problem described below and fix it with a minimal change. "
        "Change only files that match the allowed patterns. Do not push anything."
    ),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    goal TEXT NOT NULL,
    repo TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    allowed_files TEXT NOT NULL,
    test_cmd TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    account_id TEXT,
    branch TEXT,
    report TEXT,
    not_before REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'ok',
    paused_until REAL,
    last_error TEXT,
    busy_task TEXT,
    last_used REAL NOT NULL DEFAULT 0
);
"""


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


Runner = Callable[..., Awaitable[RunResult]]


def build_prompt(task: dict[str, Any]) -> str:
    return (
        f"{ROLE_TEXT[task['role']]}\n\n"
        f"Goal:\n{task['goal']}\n\n"
        f"Allowed file patterns: {', '.join(task['allowed_files'])}\n"
        + (f"Tests that must pass: {task['test_cmd']}\n" if task["test_cmd"] else "")
    )


def extract_summary(stdout: str) -> str:
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return stdout[-2000:]
    if isinstance(data, dict):
        for key in ("response", "text", "result"):
            if isinstance(data.get(key), str):
                return data[key]
    return stdout[-2000:]


class Pool:
    def __init__(
        self,
        data_dir: Path,
        runner: Runner,
        accounts: list[dict[str, Any]],
        push_token: str | None = None,
        allow_local_repos: bool = False,
    ):
        self.allow_local_repos = allow_local_repos
        self.data = Path(data_dir)
        self.repos = self.data / "repos"
        self.worktrees = self.data / "worktrees"
        self.profiles = self.data / "profiles"
        for d in (self.data, self.repos, self.worktrees, self.profiles):
            d.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data / "pool.db"
        self.nohooks = git_ops.install_nohooks(self.data / "nohooks")
        self.runner = runner
        self.push_token = push_token
        self.accounts_cfg = {a["id"]: a for a in accounts}
        self.running: set[asyncio.Task] = set()
        self._repo_lock = threading.Lock()
        self._init_db()
        self._sync_accounts()
        self._recover()

    # ---- storage -------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _exec(self, sql: str, params: tuple = ()) -> None:
        conn = self._db()
        try:
            conn.execute(sql, params)
        finally:
            conn.close()

    def _init_db(self) -> None:
        conn = self._db()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.close()

    def _sync_accounts(self) -> None:
        conn = self._db()
        for acc_id, cfg in self.accounts_cfg.items():
            conn.execute("INSERT OR IGNORE INTO accounts (id, role) VALUES (?, ?)", (acc_id, cfg["role"]))
            conn.execute("UPDATE accounts SET role = ?, enabled = 1 WHERE id = ?", (cfg["role"], acc_id))
        placeholders = ",".join("?" * len(self.accounts_cfg)) or "''"
        conn.execute(f"UPDATE accounts SET enabled = 0 WHERE id NOT IN ({placeholders})", tuple(self.accounts_cfg))
        conn.close()

    def _recover(self) -> None:
        """After a restart nothing is really running: requeue interrupted tasks and free accounts."""
        conn = self._db()
        conn.execute("UPDATE tasks SET status = 'queued', account_id = NULL WHERE status = 'running'")
        conn.execute("UPDATE accounts SET busy_task = NULL")
        conn.close()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        conn = self._db()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        conn.close()
        return self._task_dict(row) if row else None

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._db()
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [self._task_dict(r) for r in rows]

    def list_accounts(self) -> list[dict[str, Any]]:
        conn = self._db()
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["allowed_files"] = json.loads(d["allowed_files"])
        d["report"] = json.loads(d["report"]) if d["report"] else None
        return d

    # ---- submission ----------------------------------------------------

    def submit(
        self,
        role: str,
        goal: str,
        repo: str,
        allowed_files: list[str],
        base_branch: str = "main",
        test_cmd: str | None = None,
    ) -> str:
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if not goal or not goal.strip():
            raise ValueError("goal is empty")
        if not repo.startswith("https://") and not (self.allow_local_repos and Path(repo).is_dir()):
            raise ValueError("repo must be an https:// URL")
        if not allowed_files or not all(isinstance(p, str) and p for p in allowed_files):
            raise ValueError("allowed_files must be a non-empty list of glob patterns")
        with self._repo_lock:
            repo_path = git_ops.ensure_repo(repo, self.repos, self.push_token)
            git_ops.check_branch(repo_path, base_branch)
        task_id = uuid.uuid4().hex[:12]
        t = time.time()
        conn = self._db()
        conn.execute(
            "INSERT INTO tasks (id, role, goal, repo, base_branch, allowed_files, test_cmd, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (task_id, role, goal, repo, base_branch, json.dumps(allowed_files), test_cmd, t, t),
        )
        conn.close()
        return task_id

    # ---- scheduling ----------------------------------------------------

    def _claim_next(self) -> tuple[str, str] | None:
        conn = self._db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            t = time.time()
            tasks = conn.execute(
                "SELECT id, role FROM tasks WHERE status = 'queued' AND not_before <= ? ORDER BY created_at",
                (t,),
            ).fetchall()
            for task in tasks:
                acc = conn.execute(
                    "SELECT id FROM accounts WHERE enabled = 1 AND role = ? AND busy_task IS NULL"
                    " AND state != 'auth_error' AND (paused_until IS NULL OR paused_until <= ?)"
                    " ORDER BY last_used LIMIT 1",
                    (task["role"], t),
                ).fetchone()
                if acc is None:
                    continue
                conn.execute(
                    "UPDATE accounts SET busy_task = ?, last_used = ? WHERE id = ?", (task["id"], t, acc["id"])
                )
                conn.execute(
                    "UPDATE tasks SET status = 'running', account_id = ?, attempts = attempts + 1, updated_at = ?"
                    " WHERE id = ?",
                    (acc["id"], t, task["id"]),
                )
                conn.execute("COMMIT")
                return task["id"], acc["id"]
            conn.execute("COMMIT")
            return None
        finally:
            conn.close()

    async def dispatch_once(self) -> int:
        started = 0
        while True:
            claim = await asyncio.to_thread(self._claim_next)
            if claim is None:
                return started
            task_id, account_id = claim
            job = asyncio.create_task(self._execute(task_id, account_id))
            self.running.add(job)
            job.add_done_callback(self.running.discard)
            started += 1

    async def run_forever(self, interval: float = 1.0) -> None:
        while True:
            try:
                await self.dispatch_once()
            except Exception:
                log.exception("dispatch failed")
            await asyncio.sleep(interval)

    # ---- execution -----------------------------------------------------

    def _agent_env(self, account_id: str) -> dict[str, str]:
        """Environment for the Gemini CLI. Deliberately excludes GIT_PUSH_TOKEN."""
        home = self.profiles / account_id
        home.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "CI": "1",
        }
        key = os.environ.get(f"GEMINI_API_KEY_{account_id.upper()}") or os.environ.get("GEMINI_API_KEY")
        if key:
            env["GEMINI_API_KEY"] = key
        proxy = self.accounts_cfg.get(account_id, {}).get("proxy")
        if proxy:
            env["HTTPS_PROXY"] = proxy
        return env

    def _test_env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if not k.startswith(("GEMINI", "GIT_PUSH"))}

    async def _execute(self, task_id: str, account_id: str) -> None:
        try:
            await self._run(task_id, account_id)
        except Exception as exc:
            log.exception("task %s crashed", task_id)
            self._finish(task_id, "failed", {"error": f"internal: {exc}"})
        finally:
            self._exec("UPDATE accounts SET busy_task = NULL, last_used = ? WHERE id = ?", (time.time(), account_id))

    async def _run(self, task_id: str, account_id: str) -> None:
        task = self.get_task(task_id)
        assert task is not None
        base = task["base_branch"]
        repo_path = git_ops.repo_dir(self.repos, task["repo"])
        wt = self.worktrees / task_id
        branch = f"pool/{task_id}"
        await asyncio.to_thread(
            git_ops.prepare_worktree, repo_path, wt, task_id, base, self.push_token, self.nohooks
        )
        self._exec("UPDATE tasks SET branch = ? WHERE id = ?", (branch, task_id))

        res = await self.runner(
            prompt=build_prompt(task),
            workdir=wt,
            env=self._agent_env(account_id),
            timeout=TASK_TIMEOUT_S,
        )
        output = f"{res.stdout}\n{res.stderr}"

        if res.timed_out:
            return self._retry_or_fail(task, account_id, "timeout", output)
        if res.returncode != 0:
            if QUOTA_RE.search(output):
                self._pause(account_id, "quota", time.time() + QUOTA_PAUSE_S, output)
                return self._retry_or_fail(task, account_id, "quota limit", output, delay=0)
            if AUTH_RE.search(output):
                self._pause(account_id, "auth_error", None, output)
                return self._retry_or_fail(task, account_id, "account auth error", output, delay=0)
            if NETWORK_RE.search(output):
                return self._retry_or_fail(task, account_id, "network error", output)
            return self._finish(task_id, "failed", {
                "error": f"agent exited with code {res.returncode}",
                "tail": output[-2000:],
                "account": account_id,
            })

        self._mark_ok(account_id)
        await self._finish_success(task, account_id, extract_summary(res.stdout), wt, branch)

    async def _finish_success(
        self, task: dict[str, Any], account_id: str, summary: str, wt: Path, branch: str
    ) -> None:
        task_id, base = task["id"], task["base_branch"]
        changed, stats = await asyncio.to_thread(git_ops.stage_and_diff, wt, base)

        violations = []
        if task["role"] == "reviewer" and changed:
            violations.append(f"reviewer changed files: {', '.join(changed)}")
        outside = [f for f in changed if not any(fnmatch.fnmatch(f, p) for p in task["allowed_files"])]
        if outside:
            violations.append(f"files outside allowed patterns: {', '.join(outside)}")

        tests = None
        if task["test_cmd"]:
            passed, tail = await self._run_tests(wt, task["test_cmd"])
            tests = {"cmd": task["test_cmd"], "passed": passed, "tail": tail}

        pushed = False
        push_error = None
        if changed and not violations and task["role"] != "reviewer":
            await asyncio.to_thread(git_ops.commit, wt, f"pool {task_id}: {task['goal'][:60]}")
            if self.push_token:
                try:
                    await asyncio.to_thread(git_ops.push, wt, branch, self.push_token)
                    pushed = True
                except git_ops.GitError as exc:
                    push_error = str(exc)

        report = {
            "summary": summary,
            "changed_files": changed,
            "stats": stats,
            "violations": violations,
            "tests": tests,
            "branch": branch,
            "pushed": pushed,
            "push_error": push_error,
            "account": account_id,
            "attempts": task["attempts"],
        }
        self._finish(task_id, "failed" if violations else "done", report)

    async def _run_tests(self, wt: Path, cmd: str) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(wt),
            env=self._test_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), TEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            return False, "tests timed out"
        return proc.returncode == 0, out.decode(errors="replace")[-4000:]

    # ---- state transitions ---------------------------------------------

    def _finish(self, task_id: str, status: str, report: dict[str, Any]) -> None:
        self._exec(
            "UPDATE tasks SET status = ?, report = ?, account_id = COALESCE(account_id, ?), updated_at = ?"
            " WHERE id = ?",
            (status, json.dumps(report, ensure_ascii=False), report.get("account"), time.time(), task_id),
        )

    def _retry_or_fail(
        self, task: dict[str, Any], account_id: str, reason: str, output: str, delay: float = NETWORK_RETRY_S
    ) -> None:
        if task["attempts"] >= MAX_ATTEMPTS:
            return self._finish(task["id"], "failed", {"error": reason, "tail": output[-2000:], "account": account_id})
        self._exec(
            "UPDATE tasks SET status = 'queued', account_id = NULL, not_before = ?, report = ?, updated_at = ?"
            " WHERE id = ?",
            (time.time() + delay, json.dumps({"last_error": reason}), time.time(), task["id"]),
        )

    def _pause(self, account_id: str, state: str, until: float | None, output: str) -> None:
        self._exec(
            "UPDATE accounts SET state = ?, paused_until = ?, last_error = ? WHERE id = ?",
            (state, until, output[-300:], account_id),
        )

    def _mark_ok(self, account_id: str) -> None:
        self._exec(
            "UPDATE accounts SET state = 'ok', paused_until = NULL, last_error = NULL WHERE id = ?", (account_id,)
        )
