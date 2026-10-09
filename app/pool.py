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
AUTH_RE = re.compile(
    r"\b401\b|UNAUTHENTICATED|API key not valid|PERMISSION_DENIED|no auth|not eligible|Verify your account|"
    r"Eligibility check failed|Authentication required|visit the URL to log in",
    re.I,
)
NETWORK_RE = re.compile(r"ECONNRESET|ETIMEDOUT|ENOTFOUND|fetch failed|socket hang up", re.I)

MAX_ROUNDS_DEFAULT = 3
FINDINGS_LIMIT = 6000
VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVE|CHANGES)\b", re.I)

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
    # Pipeline stages. The reviewer's answer ends with a machine-read verdict line.
    "review": (
        "You are the code reviewer. The code to review is already in this working copy (HEAD). Compare it with the "
        "base: `git diff origin/{base}...HEAD`. Do NOT modify, create, delete, commit or push anything. Check that the "
        "change does what the task asks, look for bugs, missed cases, security problems and broken project rules (if the "
        "repository root has CLAUDE.md or a docs/*GUIDE* file, follow it), and whether tests cover the change. Run the "
        "project's tests (command below). Write the findings in Russian: file:line, the problem, how to fix it, the most "
        "severe first. Finish with exactly one last line: `VERDICT: APPROVE` if there is nothing blocking, otherwise "
        "`VERDICT: CHANGES`."
    ),
    "rework": (
        "A reviewer found problems in the code that is already in this working copy (it starts from the previous "
        "attempt). Fix every blocking finding listed in the goal below, keep the rest of the change, and re-run the "
        "tests. Change only files that match the allowed patterns. Do not push anything."
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
    pipeline_id TEXT,
    stage TEXT,
    round INTEGER NOT NULL DEFAULT 1,
    base_ref TEXT,
    parent_id TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    account_id TEXT,
    branch TEXT,
    report TEXT,
    not_before REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pipelines (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    review_model TEXT,
    max_rounds INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'active',
    reason TEXT,
    final_task_id TEXT,
    summary TEXT,
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
    stage = task.get("stage")
    key = stage if stage in ("review", "rework") else task["role"]
    text = ROLE_TEXT[key].replace("{base}", task["base_branch"])
    allowed = "" if stage == "review" else f"Allowed file patterns: {', '.join(task['allowed_files'])}\n"
    return (
        f"{text}\n\n"
        f"Goal:\n{task['goal']}\n\n"
        + allowed
        + (f"Tests that must pass: {task['test_cmd']}\n" if task["test_cmd"] else "")
    )


def parse_verdict(text: str) -> str | None:
    """The reviewer's last `VERDICT: APPROVE|CHANGES` line, or None if there is none."""
    found = VERDICT_RE.findall(text or "")
    return found[-1].upper() if found else None


def strip_verdict(text: str) -> str:
    return "\n".join(line for line in (text or "").splitlines() if not VERDICT_RE.search(line)).strip()


def failure_reason(report: dict[str, Any] | None) -> str:
    report = report or {}
    if report.get("violations"):
        return "violation"
    error = str(report.get("error") or "")
    for prefix, name in (("internal", "internal"), ("quota", "quota"), ("account auth", "auth"),
                         ("network", "network"), ("timeout", "timeout"), ("agent exited", "agent_error")):
        if error.startswith(prefix):
            return name
    return "unknown"


def _literal_prefix(pattern: str) -> str:
    cut = min([i for i in (pattern.find(c) for c in "*?[") if i >= 0], default=len(pattern))
    return pattern[:cut]


def patterns_overlap(a: list[str], b: list[str]) -> tuple[str, str] | None:
    """A cheap check whether two sets of file globs can touch the same files: the fixed beginnings must nest."""
    for x in a:
        for y in b:
            px, py = _literal_prefix(x), _literal_prefix(y)
            if px.startswith(py) or py.startswith(px):
                return x, y
    return None


_MODEL_RE = re.compile(r"gemini-(\d+(?:\.\d+)?)-(flash|pro)(?:-(low|medium|high))?$")
_MODEL_HINTS = {
    ("flash", "low"): "fastest and lightest: mechanical edits, renames, docs, small well-specified changes",
    ("flash", "medium"): "fast, a little reasoning: ordinary tasks with a clear specification",
    ("flash", "high"): "fast with deep reasoning: typical feature work and tests; a good default developer",
    ("pro", "low"): "stronger than Flash with modest reasoning: tricky logic and careful review without long deliberation",
    ("pro", "high"): "strongest and slowest: complex or risky changes (money, security, database schema), hard reviews",
}


def model_hint(model_id: str) -> dict[str, Any]:
    """Guidance read off the model id (family, generation, reasoning effort). Heuristics, not measurements."""
    m = _MODEL_RE.match(model_id)
    if not m:
        return {"family": None, "generation": None, "effort": None, "hint": None}
    generation, family, effort = float(m.group(1)), m.group(2), m.group(3)
    return {"family": family, "generation": generation, "effort": effort, "hint": _MODEL_HINTS.get((family, effort))}


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
                       "round INTEGER NOT NULL DEFAULT 1", "base_ref TEXT", "parent_id TEXT"):
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
            acc["egress"] = self._egress_label(acc["id"])
        return accounts

    # ---- account management ---------------------------------------------

    def _egress_label(self, account_id: str) -> dict[str, Any]:
        proxy = str(self.accounts_cfg.get(account_id, {}).get("proxy") or "")
        m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/?", proxy)
        if m:
            return {"mode": "own_ipv6", "port": int(m.group(1))}
        if proxy:
            return {"mode": "external_proxy", "host": proxy.split("@")[-1].split("//")[-1]}
        return {"mode": "server_ip"}

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
            if self.egress:
                self.accounts_cfg[account_id]["proxy"] = f"http://127.0.0.1:{self._next_egress_port()}"
            (self.profiles / account_id).mkdir(parents=True, exist_ok=True)
            self._save_accounts()
            self._sync_accounts()
        return next(a for a in self.list_accounts() if a["id"] == account_id)

    def _next_egress_port(self) -> int:
        """A port above every one ever handed out, so a removed agent's address is never given to someone else."""
        state = self.data / "egress_ports.json"
        try:
            last = int(json.loads(state.read_text()).get("last", 3100))
        except (OSError, ValueError):
            last = 3100
        for cfg in self.accounts_cfg.values():
            m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/?", str(cfg.get("proxy") or ""))
            if m:
                last = max(last, int(m.group(1)))
        port = last + 1
        if port > 3999:
            raise ValueError("no free egress ports left")
        state.write_text(json.dumps({"last": port}))
        return port

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
        review: bool = False,
        review_model: str | None = None,
        max_rounds: int = MAX_ROUNDS_DEFAULT,
    ) -> str:
        return self.submit_with_info(role, goal, repo, allowed_files, base_branch, test_cmd, model,
                                     review, review_model, max_rounds)["task_id"]

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
    ) -> dict[str, Any]:
        """Queue a task. With review=True it becomes a pipeline: develop, then an automatic review by a reviewer
        agent, with up to max_rounds of rework until the reviewer approves. Returns the task id and any warnings."""
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
        review_model = (review_model or "").strip() or None
        if review:
            if role == "reviewer":
                raise ValueError("a reviewer task cannot itself be reviewed")
            if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 5:
                raise ValueError("max_rounds must be a whole number from 1 to 5")
            if review_model:
                self._check_model(review_model)
        warnings = self._overlap_warnings(repo, allowed_files)
        if review and not self._has_usable_reviewer():
            warnings.append("no reviewer agent is available yet: the review will wait in the queue")
        with self._repo_lock:
            repo_path = git_ops.ensure_repo(repo, self.repos, self.push_token)
            git_ops.check_branch(repo_path, base_branch)
        task_id = uuid.uuid4().hex[:12]
        t = time.time()
        conn = self._db()
        try:
            conn.execute(
                "INSERT INTO tasks (id, role, goal, repo, base_branch, allowed_files, test_cmd, model, status,"
                " pipeline_id, stage, round, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, 1, ?, ?)",
                (task_id, role, goal, repo, base_branch, json.dumps(allowed_files), test_cmd, model,
                 task_id if review else None, "develop" if review else None, t, t),
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
            git_ops.prepare_worktree, repo_path, wt, task_id, base, self.push_token, self.nohooks, task["base_ref"]
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

    # ---- pipelines: develop -> review -> rework -> approved ---------------------------------------------------------

    def get_pipeline(self, pipeline_id: str) -> dict[str, Any] | None:
        conn = self._db()
        try:
            row = conn.execute("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        d = dict(row)
        d["summary"] = json.loads(d["summary"]) if d["summary"] else None
        return d

    def pipeline_active(self, pipeline_id: str) -> bool:
        pipe = self.get_pipeline(pipeline_id)
        return bool(pipe and pipe["status"] == "active")

    def _pipeline_tasks(self, pipeline_id: str) -> list[dict[str, Any]]:
        conn = self._db()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE pipeline_id = ? ORDER BY created_at, rowid", (pipeline_id,)
            ).fetchall()
        finally:
            conn.close()
        return [self._task_dict(r) for r in rows]

    def _insert_stage_task(self, pipe: dict[str, Any], parent: dict[str, Any], role: str, stage: str, goal: str,
                           base_ref: str, round_no: int, model: str | None, allowed: list[str]) -> str:
        task_id = uuid.uuid4().hex[:12]
        t = time.time()
        self._exec(
            "INSERT INTO tasks (id, role, goal, repo, base_branch, allowed_files, test_cmd, model, status, pipeline_id,"
            " stage, round, base_ref, parent_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (task_id, role, goal, parent["repo"], parent["base_branch"], json.dumps(allowed), parent["test_cmd"], model,
             pipe["id"], stage, round_no, base_ref, parent["id"], t, t),
        )
        return task_id

    def _has_child(self, parent_id: str, stage: str) -> bool:
        conn = self._db()
        try:
            return conn.execute(
                "SELECT 1 FROM tasks WHERE parent_id = ? AND stage = ?", (parent_id, stage)
            ).fetchone() is not None
        finally:
            conn.close()

    def _after_terminal(self, task_id: str) -> None:
        """Called whenever a task reaches done or failed. For pipeline tasks this decides what happens next."""
        task = self.get_task(task_id)
        if task is None or not task.get("pipeline_id"):
            return
        pipe = self.get_pipeline(task["pipeline_id"])
        tasks = self._pipeline_tasks(task["pipeline_id"])
        if pipe is None or not tasks or tasks[-1]["id"] != task_id:
            return  # an older task cannot steer a pipeline that has moved on
        report = task["report"] or {}
        stage = task["stage"]
        if pipe["status"] != "active":  # e.g. a failed task was recovered and now succeeded
            self._exec("UPDATE pipelines SET status = 'active', reason = NULL, summary = NULL, updated_at = ? WHERE id = ?",
                       (time.time(), pipe["id"]))
        if stage in ("develop", "rework"):
            if task["status"] != "done":
                return self._end_pipeline(pipe, "failed", failure_reason(report), task)
            if not report.get("changed_files"):
                return self._end_pipeline(pipe, "needs_lead", "no_changes", task)
            if not self._has_child(task_id, "review"):
                self._queue_review(pipe, task, report)
        elif stage == "review":
            dev = self.get_task(task["parent_id"]) if task.get("parent_id") else None
            if dev is None:
                return self._end_pipeline(pipe, "needs_lead", "review_failed", task)
            if task["status"] != "done":
                return self._end_pipeline(pipe, "needs_lead", "review_failed", dev, review=task)
            verdict = parse_verdict(report.get("summary", ""))
            tests = report.get("tests")
            tests_failed = bool(tests) and not tests.get("passed")
            if verdict is None:
                return self._end_pipeline(pipe, "needs_lead", "review_no_verdict", dev, review=task)
            if verdict == "APPROVE" and not tests_failed:
                return self._end_pipeline(pipe, "approved", None, dev, review=task)
            if task["round"] >= pipe["max_rounds"]:
                return self._end_pipeline(pipe, "needs_lead", "max_rounds", dev, review=task)
            if not self._has_child(task_id, "rework"):
                self._queue_rework(pipe, task, dev, tests_failed, tests)

    def _queue_review(self, pipe: dict[str, Any], dev: dict[str, Any], report: dict[str, Any]) -> None:
        goal = f"Review of {dev['id']} (round {dev['round']}): {pipe['goal']}"
        self._insert_stage_task(pipe, dev, "reviewer", "review", goal, report["head"], dev["round"],
                                pipe["review_model"], [])
        if not self._has_usable_reviewer():
            try:
                self._event("no_reviewer", None, dev["id"], "The change waits for review, but no reviewer agent is available.")
            except Exception:
                pass

    def _queue_rework(self, pipe: dict[str, Any], review: dict[str, Any], dev: dict[str, Any],
                      tests_failed: bool, tests: dict[str, Any] | None) -> None:
        report = review["report"] or {}
        findings = strip_verdict(report.get("summary", ""))
        if tests_failed:
            findings += f"\n\nThe test command also failed ({tests.get('cmd')}). Its output ended with:\n{tests.get('tail', '')[-1500:]}"
        findings = findings[:FINDINGS_LIMIT]
        goal = f"{pipe['goal']}\n\nReviewer findings to fix (review {review['round']} of {pipe['max_rounds']}):\n{findings}"
        self._insert_stage_task(pipe, review, dev["role"], "rework", goal, review["base_ref"], review["round"] + 1,
                                dev["model"], dev["allowed_files"])

    def _end_pipeline(self, pipe: dict[str, Any], status: str, reason: str | None, dev: dict[str, Any],
                      review: dict[str, Any] | None = None) -> None:
        summary = self._pipeline_summary(pipe, status, reason, dev, review)
        self._exec(
            "UPDATE pipelines SET status = ?, reason = ?, final_task_id = ?, summary = ?, updated_at = ? WHERE id = ?",
            (status, reason, dev["id"], json.dumps(summary, ensure_ascii=False), time.time(), pipe["id"]),
        )

    def _pipeline_summary(self, pipe: dict[str, Any], status: str, reason: str | None, dev: dict[str, Any],
                          review: dict[str, Any] | None) -> dict[str, Any]:
        """The report the lead reads: what was built, how the review went, which branch to take."""
        dev_report = dev["report"] or {}
        review_report = (review["report"] or {}) if review else {}
        files, stats = dev_report.get("changed_files") or [], dev_report.get("stats")
        head = dev_report.get("head")
        if head:  # everything the pipeline changed, not only the last round
            try:
                repo_path = git_ops.repo_dir(self.repos, dev["repo"])
                files, stats = git_ops.diff_summary(repo_path, f"origin/{dev['base_branch']}", head)
            except Exception:
                log.exception("could not summarise the pipeline diff")
        return {
            "status": status,
            "reason": reason,
            "verdict": parse_verdict(review_report.get("summary", "")) if review else None,
            "rounds": (review or dev)["round"],
            "max_rounds": pipe["max_rounds"],
            "final_task": dev["id"],
            "branch": dev.get("branch"),
            "head": head,
            "pushed": bool(dev_report.get("pushed")),
            "changed_files": files,
            "stats": stats,
            "violations": dev_report.get("violations") or [],
            "summary": dev_report.get("summary"),
            "review_summary": strip_verdict(review_report.get("summary", ""))[:4000] if review else None,
            "tests": review_report.get("tests") or dev_report.get("tests"),
            "dev_tests": dev_report.get("tests"),
            "account": dev_report.get("account"),
            "model": dev_report.get("model"),
            "reviewer_model": review_report.get("model") if review else None,
            "reviewer_account": review_report.get("account") if review else None,
            "error": dev_report.get("error"),
        }

    def _pipeline_stage(self, pipe: dict[str, Any], tasks: list[dict[str, Any]]) -> tuple[str, int, bool]:
        """(stage, round, waiting): where the pipeline is now and whether it is queued rather than running."""
        last = tasks[-1] if tasks else None
        if pipe["status"] != "active":
            return pipe["status"], (last["round"] if last else 1), False
        if last is None:
            return "develop", 1, True
        waiting = last["status"] == "queued"
        if last["stage"] == "review":
            return ("awaiting_review" if waiting else "review"), last["round"], waiting
        return ("rework" if last["stage"] == "rework" else "develop"), last["round"], waiting

    def pipeline_view(self, pipe: dict[str, Any]) -> dict[str, Any]:
        tasks = self._pipeline_tasks(pipe["id"])
        stage, round_no, waiting = self._pipeline_stage(pipe, tasks)
        steps = []
        for t in tasks:
            report = t["report"] or {}
            steps.append({
                "task_id": t["id"], "stage": t["stage"], "round": t["round"], "status": t["status"],
                "account_id": t["account_id"], "model": report.get("model") or t["model"],
                "started_at": t["started_at"], "updated_at": t["updated_at"], "created_at": t["created_at"],
                "verdict": parse_verdict(report.get("summary", "")) if t["stage"] == "review" else None,
            })
        return {
            "id": pipe["id"], "goal": pipe["goal"], "status": pipe["status"], "reason": pipe["reason"],
            "stage": stage, "round": round_no, "waiting": waiting, "max_rounds": pipe["max_rounds"],
            "review_model": pipe["review_model"], "steps": steps,
            "created_at": pipe["created_at"], "updated_at": pipe["updated_at"],
        }

    def list_pipelines(self, limit: int = 12) -> list[dict[str, Any]]:
        conn = self._db()
        try:
            ids = [r["id"] for r in conn.execute("SELECT id FROM pipelines ORDER BY created_at DESC LIMIT ?", (limit,))]
        finally:
            conn.close()
        return [self.pipeline_view(p) for p in (self.get_pipeline(i) for i in ids) if p]

    def effective_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """The task as the lead should see it. The first task of a pipeline stands for the whole pipeline: it is
        'running' until the review loop ends, then 'done' (approved), 'needs_lead' or 'failed'."""
        pid = task.get("pipeline_id")
        if not pid or pid != task["id"]:
            return task
        pipe = self.get_pipeline(pid)
        if pipe is None:
            return task
        view = self.pipeline_view(pipe)
        status = {"active": "running", "approved": "done"}.get(pipe["status"], pipe["status"])
        report = dict(pipe["summary"] or {})
        report["pipeline"] = {k: v for k, v in view.items() if k != "summary"}
        out = {**task, "status": status, "report": report, "pipeline": view}
        if report.get("branch"):
            out["branch"] = report["branch"]
        return out

    def task_view(self, task_id: str) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        return self.effective_task(task) if task else None

    # ---- models: guidance and measured history --------------------------------------------------------------------

    def model_stats(self) -> dict[str, dict[str, Any]]:
        """Per model: finished tasks, share that succeeded, and the typical run time. Measured, not guessed."""
        buckets: dict[str, dict[str, Any]] = {}
        for t in self.list_tasks(500):
            if t["status"] not in ("done", "failed") or not t["started_at"]:
                continue
            report = t["report"] or {}
            model = report.get("model") or t["model"]
            if not model:
                continue
            b = buckets.setdefault(model, {"tasks": 0, "done": 0, "seconds": []})
            b["tasks"] += 1
            b["done"] += t["status"] == "done"
            b["seconds"].append(max(0.0, t["updated_at"] - t["started_at"]))
        out = {}
        for model, b in buckets.items():
            secs = sorted(b["seconds"])
            out[model] = {"tasks": b["tasks"], "done_share": round(b["done"] / b["tasks"], 2),
                          "median_seconds": round(secs[len(secs) // 2])}
        return out

    def model_guide(self) -> list[dict[str, Any]]:
        stats = self.model_stats()
        return [{"id": mid, "name": name, **model_hint(mid), "stats": stats.get(mid)}
                for mid, name in self.model_catalog().items()]

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
