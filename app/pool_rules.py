"""Pool rules: roles and prompts, error patterns, DB schema, pure helpers (no state)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

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

SCRATCH = "scratch"  # a task without a repository: the agent works on attached files and answers

ROLE_TEXT["scratch"] = (
    "You are an assistant working for the owner. Do what the goal below asks. This working folder is empty scratch "
    "space, not a repository: do not look for a project here. Files the owner attached are listed below (read them "
    "from there). Put your answer in your final message. If the goal asks you to create, convert, edit or write files "
    "for the owner, save them in the output folder given below and mention their names in your answer."
)

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


def build_prompt(task: dict[str, Any], materials: str = "", outbox: str = "") -> str:
    stage = task.get("stage")
    scratch = task.get("repo") == SCRATCH
    key = "scratch" if scratch else stage if stage in ("review", "rework") else task["role"]
    text = ROLE_TEXT[key].replace("{base}", task["base_branch"])
    allowed = "" if stage == "review" or scratch else f"Allowed file patterns: {', '.join(task['allowed_files'])}\n"
    if outbox:
        materials += (f"\nOutput folder for files meant for the owner: {outbox}\n"
                      + ("" if scratch else "(Repository changes stay in the working copy; only files for the owner go there, "
                                            "and only if the goal asks for such files.)\n"))
    return (
        f"{text}\n\n"
        f"Goal:\n{task['goal']}\n\n"
        + allowed
        + (f"Tests that must pass: {task['test_cmd']}\n" if task["test_cmd"] else "")
        + materials
    )


RESET_RE = re.compile(r"Resets? in\s+(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", re.I)
MAX_QUOTA_PAUSE_S = 8 * 86400


def quota_pause_seconds(text: str) -> int:
    """How long to keep an agent out after a quota error: the reset time Google names (e.g. "Resets in 100h21m6s"),
    capped, plus a minute; the default of five hours when the text names none."""
    m = RESET_RE.search(text or "")
    if m and any(m.groups()):
        d, h, mi, sec = (int(g or 0) for g in m.groups())
        seconds = d * 86400 + h * 3600 + mi * 60 + sec
        if seconds > 0:
            return min(seconds + 60, MAX_QUOTA_PAUSE_S)
    return QUOTA_PAUSE_S


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
    for prefix, name in (("cancelled", "cancelled"), ("internal", "internal"), ("quota", "quota"), ("account auth", "auth"),
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
