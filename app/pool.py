"""gemini-pool core: SQLite state, account scheduling, task execution."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import git_ops
import housekeeping

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
    model TEXT,
    started_at REAL,
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
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    account_id TEXT,
    task_id TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS quota (
    account_id TEXT PRIMARY KEY,
    checked_at REAL NOT NULL,
    ok INTEGER NOT NULL,
    output TEXT
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
        quota_checker: Callable[[dict[str, str]], tuple[bool, str]] | None = None,
        models_lister: Callable[[dict[str, str]], tuple[bool, str]] | None = None,
        require_login: bool = False,
        max_concurrent: int = 0,
    ):
        # require_login: an account without an agy login is not given tasks (it would only fail them).
        # max_concurrent: cap on tasks running at once, to protect a small server; 0 means no cap.
        self.require_login = require_login
        self.max_concurrent = max_concurrent
        self._cfg_lock = threading.Lock()
        self.allow_local_repos = allow_local_repos
        self.quota_checker = quota_checker
        self.models_lister = models_lister
        self._models_cache: tuple[float, dict[str, str]] | None = None
        # Maintenance mode: no new task is started while it is on. It lives in memory only, so the restart
        # that a deploy causes switches it off again by itself and the queue never stays stuck.
        self.draining = False
        self._loop: asyncio.AbstractEventLoop | None = None
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
        self.disk_low = False
        self._last_space_attempt = 0.0
        self._init_db()
        self._sync_accounts()
        self._recover()
        self.housekeep()

    # ---- disk space ----------------------------------------------------

    def housekeep(self, aggressive: bool = False) -> dict[str, int]:
        try:
            return housekeeping.cleanup(self, aggressive)
        except Exception:
            log.exception("housekeeping failed")
            return {"stripped": 0, "retired": 0}

    def _disk_ok(self) -> bool:
        """False while there is too little free space to run a task safely (a full disk breaks SQLite)."""
        if housekeeping.free_bytes(self.data) >= housekeeping.MIN_FREE_BYTES:
            self.disk_low = False
            return True
        if time.time() - self._last_space_attempt > 60:  # the cleanup is not free, so not every second
            self._last_space_attempt = time.time()
            if housekeeping.ensure_space(self):
                self.disk_low = False
                return True
        if not self.disk_low:
            free_mb = housekeeping.free_bytes(self.data) // (1024 * 1024)
            log.error("disk space is low: %d MB free; not starting tasks", free_mb)
            try:
                self._event("disk_low", None, None, f"Free space on the volume: {free_mb} MB. New tasks wait.")
            except Exception:
                pass  # recording the event needs disk space too
        self.disk_low = True
        return False

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
        # Databases created before these columns existed get them added in place.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for column in ("model TEXT", "started_at REAL"):
            if column.split()[0] not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column}")
        conn.close()

    def _sync_accounts(self) -> None:
        conn = self._db()
        for acc_id, cfg in self.accounts_cfg.items():
            conn.execute("INSERT OR IGNORE INTO accounts (id, role) VALUES (?, ?)", (acc_id, cfg["role"]))
            conn.execute(
                "UPDATE accounts SET role = ?, enabled = ? WHERE id = ?",
                (cfg["role"], int(cfg.get("enabled", True)), acc_id),
            )
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
        rows = conn.execute(
            "SELECT a.*, q.checked_at AS quota_checked_at, q.ok AS quota_ok, q.output AS quota_text"
            " FROM accounts a LEFT JOIN quota q ON q.account_id = a.id ORDER BY a.id"
        ).fetchall()
        conn.close()
        accounts = [dict(r) for r in rows]
        for acc in accounts:
            acc["model"] = self._model_for(acc["id"])
            acc["logged_in"] = self.login_present(acc["id"])
        return accounts

    # ---- account management ---------------------------------------------

    def login_present(self, account_id: str) -> bool:
        """True once agy has stored a Google login in the account's profile."""
        return (self.profiles / account_id / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").is_file()

    def _save_accounts(self) -> None:
        path = self.data / "accounts.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self.accounts_cfg.values()), indent=2, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(path)

    def add_account(self, account_id: str, role: str = "developer") -> dict[str, Any]:
        """Register a new agent live. It takes no tasks until its Google login is done (see scripts/add_account.sh)."""
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", account_id or ""):
            raise ValueError("id must be 1-16 characters: lowercase letters, digits, - or _, starting with a letter")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        with self._cfg_lock:
            if account_id in self.accounts_cfg:
                raise ValueError(f"account {account_id} already exists")
            self.accounts_cfg[account_id] = {"id": account_id, "role": role}
            (self.profiles / account_id).mkdir(parents=True, exist_ok=True)
            self._save_accounts()
            self._sync_accounts()
        return next(a for a in self.list_accounts() if a["id"] == account_id)

    def set_account_enabled(self, account_id: str, enabled: bool) -> dict[str, Any]:
        with self._cfg_lock:
            if account_id not in self.accounts_cfg:
                raise ValueError("unknown account")
            self.accounts_cfg[account_id]["enabled"] = bool(enabled)
            self._save_accounts()
            self._sync_accounts()
        return next(a for a in self.list_accounts() if a["id"] == account_id)

    def remove_account(self, account_id: str) -> None:
        """Forget an agent and delete its profile (its Google login). Refused while it is working."""
        with self._cfg_lock:
            if account_id not in self.accounts_cfg:
                raise ValueError("unknown account")
            conn = self._db()
            try:
                row = conn.execute("SELECT busy_task FROM accounts WHERE id = ?", (account_id,)).fetchone()
                if row is not None and row["busy_task"]:
                    raise ValueError("the account is running a task; wait for it to finish")
                conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
                conn.execute("DELETE FROM quota WHERE account_id = ?", (account_id,))
            finally:
                conn.close()
            del self.accounts_cfg[account_id]
            self._save_accounts()
            shutil.rmtree(self.profiles / account_id, ignore_errors=True)

    def _model_for(self, account_id: str) -> str | None:
        """Model pinned for this account (accounts.json "model"), else the global AGY_MODEL, else None = agy default."""
        return self.accounts_cfg.get(account_id, {}).get("model") or os.environ.get("AGY_MODEL") or None

    def list_models(self, account_id: str) -> dict[str, Any]:
        """Models agy offers to this account. Blocking: call from a worker thread."""
        if account_id not in self.accounts_cfg:
            raise ValueError("unknown account")
        if self.models_lister is None:
            raise ValueError("model list is not available")
        ok, text = self.models_lister(self._agent_env(account_id))
        if ok:
            text = "\n".join(
                line for line in text.splitlines() if line.split("\t")[0].strip().startswith("gemini")
            )
        return {"account_id": account_id, "ok": ok, "text": text}

    def model_catalog(self) -> dict[str, str]:
        """Model id -> display name, as reported by `agy models` (cached 10 minutes). Empty if unavailable."""
        cached = self._models_cache
        if cached is not None and time.time() - cached[0] < 600:
            return cached[1]
        if self.models_lister is None or not self.accounts_cfg:
            return {}
        ok, text = self.models_lister(self._agent_env(next(iter(self.accounts_cfg))))
        catalog = {}
        if ok:
            for line in text.splitlines():
                parts = line.split("\t")
                # Only Gemini models are offered; agy also lists Claude and GPT models, which the pool ignores.
                if len(parts) == 2 and parts[0].strip().startswith("gemini"):
                    catalog[parts[0].strip()] = parts[1].strip()
        if catalog:
            self._models_cache = (time.time(), catalog)
        return catalog

    def _check_model(self, model: str) -> None:
        catalog = self.model_catalog()
        # If the list cannot be fetched, let agy decide rather than blocking every task.
        if catalog and model not in catalog:
            raise ValueError(f"unknown model '{model}'. Available: {', '.join(catalog)}")

    def set_model(self, account_id: str, model: str | None) -> dict[str, Any]:
        """Pin a model for one account (None = agy default) and persist it to accounts.json."""
        if account_id not in self.accounts_cfg:
            raise ValueError("unknown account")
        if model:
            if model not in self.model_catalog():
                raise ValueError("unknown model")
            self.accounts_cfg[account_id]["model"] = model
        else:
            self.accounts_cfg[account_id].pop("model", None)
        with self._cfg_lock:
            self._save_accounts()
        return {"account_id": account_id, "model": self._model_for(account_id)}

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._db()
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def _event(self, kind: str, account_id: str | None, task_id: str | None, message: str) -> None:
        conn = self._db()
        try:
            conn.execute(
                "INSERT INTO events (ts, kind, account_id, task_id, message) VALUES (?, ?, ?, ?, ?)",
                (time.time(), kind, account_id, task_id, (message or "").strip()[-1500:]),
            )
            conn.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 1000")
        finally:
            conn.close()

    def check_quota(self, account_id: str) -> dict[str, Any]:
        """Ask agy for the account's usage (/usage). Blocking: call from a worker thread."""
        if account_id not in self.accounts_cfg:
            raise ValueError("unknown account")
        if self.quota_checker is None:
            raise ValueError("quota check is not available")
        ok, text = self.quota_checker(self._agent_env(account_id))
        checked_at = time.time()
        self._exec(
            "INSERT INTO quota (account_id, checked_at, ok, output) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(account_id) DO UPDATE SET checked_at = excluded.checked_at,"
            " ok = excluded.ok, output = excluded.output",
            (account_id, checked_at, int(ok), text),
        )
        if not ok:
            self._event("quota_check_failed", account_id, None, text)
        return {"account_id": account_id, "checked_at": checked_at, "ok": ok, "text": text}

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
        model: str | None = None,
    ) -> str:
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if not goal or not goal.strip():
            raise ValueError("goal is empty")
        if not repo.startswith("https://") and not (self.allow_local_repos and Path(repo).is_dir()):
            raise ValueError("repo must be an https:// URL")
        if not allowed_files or not all(isinstance(p, str) and p for p in allowed_files):
            raise ValueError("allowed_files must be a non-empty list of glob patterns")
        model = (model or "").strip() or None
        if model:
            self._check_model(model)
        with self._repo_lock:
            repo_path = git_ops.ensure_repo(repo, self.repos, self.push_token)
            git_ops.check_branch(repo_path, base_branch)
        task_id = uuid.uuid4().hex[:12]
        t = time.time()
        conn = self._db()
        conn.execute(
            "INSERT INTO tasks (id, role, goal, repo, base_branch, allowed_files, test_cmd, model, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (task_id, role, goal, repo, base_branch, json.dumps(allowed_files), test_cmd, model, t, t),
        )
        conn.close()
        return task_id

    # ---- scheduling ----------------------------------------------------

    def set_draining(self, draining: bool) -> dict[str, Any]:
        self.draining = bool(draining)
        return self.maintenance_status()

    def maintenance_status(self) -> dict[str, Any]:
        conn = self._db()
        try:
            running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
        finally:
            conn.close()
        return {"draining": self.draining, "running": running, "safe_to_deploy": self.draining and running == 0,
                "disk": housekeeping.disk_info(self.data)}

    def _claim_next(self) -> tuple[str, str] | None:
        if self.draining or not self._disk_ok():
            return None
        usable = [a for a in list(self.accounts_cfg) if not self.require_login or self.login_present(a)]
        if not usable:
            return None
        conn = self._db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            t = time.time()
            if self.max_concurrent:
                busy = conn.execute("SELECT COUNT(*) FROM accounts WHERE busy_task IS NOT NULL").fetchone()[0]
                if busy >= self.max_concurrent:
                    conn.execute("COMMIT")
                    return None
            tasks = conn.execute(
                "SELECT id, role FROM tasks WHERE status = 'queued' AND not_before <= ? ORDER BY created_at",
                (t,),
            ).fetchall()
            marks = ",".join("?" * len(usable))
            for task in tasks:
                acc = conn.execute(
                    "SELECT id FROM accounts WHERE enabled = 1 AND role = ? AND busy_task IS NULL"
                    f" AND id IN ({marks})"
                    " AND state != 'auth_error' AND (paused_until IS NULL OR paused_until <= ?)"
                    " ORDER BY last_used LIMIT 1",
                    (task["role"], *usable, t),
                ).fetchone()
                if acc is None:
                    continue
                conn.execute(
                    "UPDATE accounts SET busy_task = ?, last_used = ? WHERE id = ?", (task["id"], t, acc["id"])
                )
                conn.execute(
                    "UPDATE tasks SET status = 'running', account_id = ?, attempts = attempts + 1, updated_at = ?,"
                    " started_at = ? WHERE id = ?",
                    (acc["id"], t, t, task["id"]),
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

    def recover_task(self, task_id: str) -> dict[str, Any]:
        """Redo the finishing steps (checks, tests, commit, push) for a task that failed with an internal error.

        The agent is not run again, so no quota is spent: its work is still in the task's working copy.
        """
        task = self.get_task(task_id)
        if task is None:
            raise ValueError("unknown task")
        if task["status"] != "failed" or not str((task["report"] or {}).get("error", "")).startswith("internal"):
            raise ValueError("only tasks that failed with an internal pool error can be recovered")
        wt = self.worktrees / task_id
        if not wt.is_dir():
            raise ValueError("the task's working copy no longer exists")
        account_id = task["account_id"] or next(iter(self.accounts_cfg))
        now = time.time()
        self._exec("UPDATE tasks SET status = 'running', started_at = ?, updated_at = ? WHERE id = ?", (now, now, task_id))
        coro = self._recover_finish(task, account_id, wt)
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)  # runs in the background; the dashboard shows progress
        else:
            asyncio.run(coro)
        return self.get_task(task_id) or task

    async def _recover_finish(self, task: dict[str, Any], account_id: str, wt: Path) -> None:
        note = "(result recovered after an internal pool error; the agent's own answer was not saved)"
        try:
            await self._finish_success(
                task, account_id, note, wt, f"pool/{task['id']}", task["model"] or self._model_for(account_id)
            )
        except Exception as exc:
            log.exception("recovery of task %s failed", task["id"])
            self._event("internal", account_id, task["id"], f"recovery failed: {exc}")
            self._finish(task["id"], "failed", {"error": f"internal: {exc}", "account": account_id})

    async def run_forever(self, interval: float = 1.0) -> None:
        self._loop = asyncio.get_running_loop()
        last_housekeeping = time.time()
        while True:
            try:
                await self.dispatch_once()
                if time.time() - last_housekeeping > housekeeping.INTERVAL_S:
                    last_housekeeping = time.time()
                    await asyncio.to_thread(self.housekeep)
            except Exception:
                log.exception("dispatch failed")
            await asyncio.sleep(interval)

    # ---- execution -----------------------------------------------------

    def _agent_env(self, account_id: str) -> dict[str, str]:
        """Environment for agy. HOME is the account's profile (that is where its Google login lives).
        Deliberately excludes GIT_PUSH_TOKEN."""
        home = self.profiles / account_id
        home.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "CI": "1",
        }
        proxy = self.accounts_cfg.get(account_id, {}).get("proxy")
        if proxy:
            env["HTTPS_PROXY"] = proxy
        return env

    def _test_env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if not k.startswith(("GEMINI", "AGY", "GIT_PUSH"))}

    async def _execute(self, task_id: str, account_id: str) -> None:
        try:
            await self._run(task_id, account_id)
        except Exception as exc:
            log.exception("task %s crashed", task_id)
            self._event("internal", account_id, task_id, str(exc))
            self._finish(task_id, "failed", {"error": f"internal: {exc}"})
        finally:
            # Free the space first: on a full disk the database write below would fail otherwise.
            housekeeping.strip_heavy(self.worktrees / task_id)  # virtualenvs etc. are rebuilt by the next test run
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

        model = task["model"] or self._model_for(account_id)  # the task's own model beats the account's
        res = await self.runner(
            prompt=build_prompt(task),
            workdir=wt,
            env=self._agent_env(account_id),
            timeout=TASK_TIMEOUT_S,
            model=model,
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
            self._event("agent_error", account_id, task_id, f"exit code {res.returncode}\n{output}")
            return self._finish(task_id, "failed", {
                "error": f"agent exited with code {res.returncode}",
                "tail": output[-2000:],
                "account": account_id,
            })

        self._mark_ok(account_id)
        await self._finish_success(task, account_id, extract_summary(res.stdout), wt, branch, model)

    async def _finish_success(
        self, task: dict[str, Any], account_id: str, summary: str, wt: Path, branch: str, model: str | None
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
                    self._event("push_error", account_id, task_id, push_error)
        for violation in violations:
            self._event("violation", account_id, task_id, violation)

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
            "model": model,
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
        self._event(reason, account_id, task["id"], output)
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
