"""gemini-pool core: SQLite state, account scheduling, task execution."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import shutil
import signal
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import git_ops
import housekeeping
import uploads as uploads_mod
from pool_accounts import AccountsMixin
from pool_pipelines import PipelinesMixin
from pool_reports import ReportsMixin
from pool_rules import (
    AUTH_RE,
    MAX_ATTEMPTS,
    MAX_QUOTA_PAUSE_S,
    MAX_ROUNDS_DEFAULT,
    NETWORK_RE,
    NETWORK_RETRY_S,
    QUOTA_PAUSE_S,
    QUOTA_RE,
    ROLES,
    RunResult,
    Runner,
    SCHEMA,
    SCRATCH,
    TASK_TIMEOUT_S,
    TEST_TIMEOUT_S,
    build_prompt,
    extract_summary,
    model_hint,
    patterns_overlap,
    quota_pause_seconds,
)  # noqa: F401  (some are re-exported for server and tests)

log = logging.getLogger("pool")

class Pool(AccountsMixin, PipelinesMixin, ReportsMixin):
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
        egress: bool = False,
    ):
        # egress: give each new agent its own local proxy port, which the egress service maps to its own IPv6 address.
        self.egress = egress
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
        self.uploads = uploads_mod.UploadStore(self.data / "uploads", self._db, self._busy_upload_ids)
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
        for column in ("model TEXT", "started_at REAL", "pipeline_id TEXT", "stage TEXT",
                       "round INTEGER NOT NULL DEFAULT 1", "base_ref TEXT", "parent_id TEXT", "uploads TEXT",
                       "save_to TEXT", "origin TEXT"):
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
            if AUTH_RE.search(text):  # no login, or Google refuses the account: it must not be given tasks
                self._pause(account_id, "auth_error", None, text)
        elif self.accounts_state(account_id) == "auth_error":
            self._mark_ok(account_id)
        return {"account_id": account_id, "checked_at": checked_at, "ok": ok, "text": text}

    def accounts_state(self, account_id: str) -> str | None:
        conn = self._db()
        try:
            row = conn.execute("SELECT state FROM accounts WHERE id = ?", (account_id,)).fetchone()
        finally:
            conn.close()
        return row["state"] if row else None

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["allowed_files"] = json.loads(d["allowed_files"])
        d["uploads"] = json.loads(d["uploads"]) if d.get("uploads") else []
        d["report"] = json.loads(d["report"]) if d["report"] else None
        return d

    def _busy_upload_ids(self) -> set[str]:
        """Uploads that queued or running tasks need: attached materials and uploaded projects."""
        conn = self._db()
        try:
            rows = conn.execute(
                "SELECT repo, uploads FROM tasks WHERE status IN ('queued', 'running')"
            ).fetchall()
        finally:
            conn.close()
        busy: set[str] = set()
        root = str(self.uploads.root)
        for r in rows:
            if r["uploads"]:
                busy.update(json.loads(r["uploads"]))
            if r["repo"].startswith(root + os.sep):
                busy.add(Path(r["repo"]).name)
        return busy

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
        review: bool = False,
        review_model: str | None = None,
        max_rounds: int = MAX_ROUNDS_DEFAULT,
        uploads: list[str] | None = None,
        save_to: str | None = None,
        origin: str | None = None,
    ) -> str:
        return self.submit_with_info(role, goal, repo, allowed_files, base_branch, test_cmd, model,
                                     review, review_model, max_rounds, uploads, save_to, origin)["task_id"]

    def submit_with_info(
        self,
        role: str,
        goal: str,
        repo: str,
        allowed_files: list[str],
        base_branch: str = "main",
        test_cmd: str | None = None,
        model: str | None = None,
        review: bool = False,
        review_model: str | None = None,
        max_rounds: int = MAX_ROUNDS_DEFAULT,
        uploads: list[str] | None = None,
        save_to: str | None = None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        """Queue a task. With review=True it becomes a pipeline: develop, then an automatic review by a reviewer
        agent, with up to max_rounds of rework until the reviewer approves. Returns the task id and any warnings."""
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if not goal or not goal.strip():
            raise ValueError("goal is empty")
        scratch = repo == SCRATCH
        if scratch:
            if review:
                raise ValueError("a task without a repository cannot be reviewed")
            allowed_files = allowed_files or ["*"]
            if role == "reviewer":
                raise ValueError("a reviewer needs a repository")
        if save_to and not self.uploads.exists(save_to):
            raise ValueError(f"unknown upload {save_to} for save_to")
        uploads = list(dict.fromkeys(uploads or []))
        for upload_id in uploads:
            if not self.uploads.exists(upload_id):
                raise ValueError(f"unknown upload {upload_id}")
        if repo.startswith("upload:"):
            # A project the owner uploaded: it becomes a git repository on the server and tasks work on it.
            try:
                repo = str(self.uploads.make_project(repo[len("upload:"):]))
            except uploads_mod.UploadError as exc:
                raise ValueError(str(exc))
        elif not scratch and not repo.startswith("https://") and not (self.allow_local_repos and Path(repo).is_dir()):
            raise ValueError("repo must be an https:// URL, upload:<id> for an uploaded project, or scratch for files only")
        if not allowed_files or not all(isinstance(p, str) and p for p in allowed_files):
            raise ValueError("allowed_files must be a non-empty list of glob patterns")
        model = (model or "").strip() or None
        if model:
            self._check_model(model)
        review_model = (review_model or "").strip() or None
        if review:
            if role == "reviewer":
                raise ValueError("a reviewer task cannot itself be reviewed")
            if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 5:
                raise ValueError("max_rounds must be a whole number from 1 to 5")
            if review_model:
                self._check_model(review_model)
        warnings = [] if scratch else self._overlap_warnings(repo, allowed_files)
        if review and not self._has_usable_reviewer():
            warnings.append("no reviewer agent is available yet: the review will wait in the queue")
        if not scratch:
            with self._repo_lock:
                repo_path = git_ops.ensure_repo(repo, self.repos, self.push_token)
                git_ops.check_branch(repo_path, base_branch)
        task_id = uuid.uuid4().hex[:12]
        t = time.time()
        conn = self._db()
        try:
            conn.execute(
                "INSERT INTO tasks (id, role, goal, repo, base_branch, allowed_files, test_cmd, model, status,"
                " pipeline_id, stage, round, uploads, save_to, origin, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, 1, ?, ?, ?, ?, ?)",
                (task_id, role, goal, repo, base_branch, json.dumps(allowed_files), test_cmd, model,
                 task_id if review else None, "develop" if review else None,
                 json.dumps(uploads) if uploads else None, save_to or None, origin, t, t),
            )
            if review:
                conn.execute(
                    "INSERT INTO pipelines (id, goal, review_model, max_rounds, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (task_id, goal, review_model, max_rounds, t, t),
                )
        finally:
            conn.close()
        return {"task_id": task_id, "pipeline": bool(review), "warnings": warnings}

    def _has_usable_reviewer(self) -> bool:
        for acc in self.list_accounts():
            if acc["role"] == "reviewer" and acc["enabled"] and acc["state"] != "auth_error" and (
                not self.require_login or acc["logged_in"]
            ):
                return True
        return False

    def _overlap_warnings(self, repo: str, allowed: list[str]) -> list[str]:
        """Tell the lead when new work may touch the same files as work still in flight in the same repository."""
        conn = self._db()
        try:
            rows = conn.execute(
                "SELECT id, allowed_files FROM tasks WHERE repo = ? AND role != 'reviewer' AND ("
                " status IN ('queued', 'running') OR pipeline_id IN (SELECT id FROM pipelines WHERE status = 'active'))",
                (repo,),
            ).fetchall()
        finally:
            conn.close()
        out, seen = [], set()
        for row in rows:
            hit = patterns_overlap(allowed, json.loads(row["allowed_files"]))
            if hit and row["id"] not in seen:
                seen.add(row["id"])
                out.append(f"may conflict with task {row['id']} (still in flight): both can change {hit[0]} / {hit[1]}")
        return out

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

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Cancel a task that has not started. A running task cannot be stopped safely, so it is refused."""
        task = self.get_task(task_id)
        if task is None:
            raise ValueError("unknown task")
        if task["status"] != "queued":
            raise ValueError(f"only a task waiting in the queue can be cancelled (this one is {task['status']})")
        conn = self._db()
        try:  # the check and the change in one statement, so a task that just started is not touched
            changed = conn.execute(
                "UPDATE tasks SET status = 'failed', account_id = NULL, updated_at = ?, report = ?"
                " WHERE id = ? AND status = 'queued'",
                (time.time(), json.dumps({"error": "cancelled by the owner", "account": None}), task_id),
            ).rowcount
        finally:
            conn.close()
        if not changed:
            raise ValueError("the task has just started and can no longer be cancelled")
        self._event("cancelled", None, task_id, "Task cancelled by the owner while it waited in the queue.")
        self._after_terminal(task_id)  # a pipeline whose waiting stage is cancelled ends here
        return self.task_view(task_id) or task

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
            shutil.rmtree(self.uploads.outbox(task_id), ignore_errors=True)  # whatever an unfinished attempt left
            self._exec("UPDATE accounts SET busy_task = NULL, last_used = ? WHERE id = ?", (time.time(), account_id))

    async def _run(self, task_id: str, account_id: str) -> None:
        task = self.get_task(task_id)
        assert task is not None
        base = task["base_branch"]
        repo_path = git_ops.repo_dir(self.repos, task["repo"])
        wt = self.worktrees / task_id
        branch = f"pool/{task_id}"
        scratch = task["repo"] == SCRATCH
        if scratch:
            shutil.rmtree(wt, ignore_errors=True)
            wt.mkdir(parents=True)
            branch = None
        else:
            await asyncio.to_thread(
                git_ops.prepare_worktree, repo_path, wt, task_id, base, self.push_token, self.nohooks, task["base_ref"]
            )
            self._exec("UPDATE tasks SET branch = ? WHERE id = ?", (branch, task_id))
        outbox = None
        if task.get("stage") != "review":
            outbox = self.uploads.outbox(task_id)
            shutil.rmtree(outbox, ignore_errors=True)
            outbox.mkdir(parents=True)

        model = task["model"] or self._model_for(account_id)  # the task's own model beats the account's
        materials = await asyncio.to_thread(self.uploads.prompt_block, task["uploads"])
        await asyncio.to_thread(self.uploads.touch, task["uploads"])
        res = await self.runner(
            prompt=build_prompt(task, materials, str(outbox) if outbox else ""),
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
                self._pause(account_id, "quota", time.time() + quota_pause_seconds(output), output)
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
        if scratch:
            return await self._finish_scratch(task, account_id, extract_summary(res.stdout), model)
        await self._finish_success(task, account_id, extract_summary(res.stdout), wt, branch, model)

    async def _finish_success(
        self, task: dict[str, Any], account_id: str, summary: str, wt: Path, branch: str, model: str | None
    ) -> None:
        task_id, base = task["id"], task["base_branch"]
        # Test runs by the agent leave virtualenvs and caches behind; they must not count as changes.
        await asyncio.to_thread(housekeeping.strip_heavy, wt)
        ref = task["base_ref"] or f"origin/{base}"
        changed, stats = await asyncio.to_thread(git_ops.stage_and_diff, wt, ref)

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
        head = await asyncio.to_thread(git_ops.head_sha, wt)
        outputs = None
        if task.get("stage") != "review" and not violations:
            outputs = await asyncio.to_thread(self.uploads.adopt_outbox, task_id, task["goal"], task.get("save_to"))

        report = {
            "summary": summary,
            "changed_files": changed,
            "stats": stats,
            "violations": violations,
            "tests": tests,
            "branch": branch,
            "head": head,
            "pushed": pushed,
            "push_error": push_error,
            "outputs": outputs,
            "account": account_id,
            "model": model,
            "attempts": task["attempts"],
        }
        self._finish(task_id, "failed" if violations else "done", report)

    async def _finish_scratch(self, task: dict[str, Any], account_id: str, summary: str, model: str | None) -> None:
        outputs = await asyncio.to_thread(self.uploads.adopt_outbox, task["id"], task["goal"], task["save_to"])
        self._finish(task["id"], "done", {
            "summary": summary, "changed_files": [], "stats": "", "violations": [], "tests": None, "branch": None,
            "head": None, "pushed": False, "push_error": None, "outputs": outputs,
            "account": account_id, "model": model, "attempts": task["attempts"],
        })

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

    # ---- pipelines: develop -> review -> rework -> approved ---------------------------------------------------------

    # ---- state transitions ---------------------------------------------

    def _finish(self, task_id: str, status: str, report: dict[str, Any]) -> None:
        self._exec(
            "UPDATE tasks SET status = ?, report = ?, account_id = COALESCE(account_id, ?), updated_at = ?"
            " WHERE id = ?",
            (status, json.dumps(report, ensure_ascii=False), report.get("account"), time.time(), task_id),
        )
        if status in ("done", "failed"):
            try:
                self._after_terminal(task_id)
            except Exception:  # a pipeline problem must never lose the task's own result
                log.exception("pipeline step after task %s failed", task_id)

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
