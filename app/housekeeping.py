"""Keeps the pool's volume from filling up.

Task working copies pile up on /data and test commands drop rebuildable bulk (virtualenvs, node_modules) into
them. Left alone that fills the volume, after which SQLite cannot write ("disk I/O error") and the pool stops.
Three defences: strip the rebuildable bulk, retire old working copies, and stop taking tasks when space is low.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import git_ops

if TYPE_CHECKING:
    from pool import Pool

log = logging.getLogger("housekeeping")

# Anything here is rebuilt by the next test run, so removing it never loses work.
HEAVY_DIRS = frozenset({".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox"})
KEEP_PUSHED_S = 24 * 3600        # a pushed task's working copy is only a convenience
KEEP_UNPUSHED_S = 14 * 86400     # unpushed work exists only here, and failed-internal tasks can be recovered
KEEP_ORPHAN_S = 24 * 3600
MIN_FREE_BYTES = int(os.environ.get("POOL_MIN_FREE_MB", "120")) * 1024 * 1024
INTERVAL_S = 600


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def disk_info(path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    return {"total": usage.total, "free": usage.free, "low": usage.free < MIN_FREE_BYTES}


def strip_heavy(worktree: Path) -> int:
    """Remove rebuildable directories from a working copy. Returns how many were removed."""
    removed = 0
    for current, dirs, _files in os.walk(worktree, topdown=True):
        if ".git" in dirs:
            dirs.remove(".git")  # never look inside git's own data
        for name in [d for d in dirs if d in HEAVY_DIRS]:
            shutil.rmtree(Path(current) / name, ignore_errors=True)
            dirs.remove(name)
            removed += 1
    return removed


def strip_all(worktrees: Path) -> int:
    """For process start, before the database is touched: at that moment no task is running."""
    if not worktrees.is_dir():
        return 0
    return sum(strip_heavy(wt) for wt in worktrees.iterdir() if wt.is_dir())


def _retire(pool: "Pool", wt: Path, task: dict[str, Any] | None, pushed: bool) -> None:
    repo_dir = git_ops.repo_dir(pool.repos, task["repo"]) if task else None
    if repo_dir is not None and repo_dir.is_dir():
        with git_ops.repo_lock(repo_dir):
            git_ops._git(["worktree", "remove", "--force", str(wt)], cwd=repo_dir, check=False)
            shutil.rmtree(wt, ignore_errors=True)
            git_ops._git(["worktree", "prune"], cwd=repo_dir, check=False)
            if pushed:  # the branch lives on GitHub; an unpushed one is the only copy and stays
                git_ops._git(["branch", "-D", f"pool/{task['id']}"], cwd=repo_dir, check=False)
    else:
        shutil.rmtree(wt, ignore_errors=True)


def cleanup(pool: "Pool", aggressive: bool = False) -> dict[str, int]:
    """Strip rebuildable bulk from idle working copies and retire old ones. `aggressive` ignores the age limit
    for pushed tasks (their work is safe on GitHub); it is used when the disk is nearly full."""
    now = time.time()
    stripped = retired = 0
    if not pool.worktrees.is_dir():
        return {"stripped": 0, "retired": 0}
    for wt in sorted(p for p in pool.worktrees.iterdir() if p.is_dir()):
        task = pool.get_task(wt.name)
        if task is not None and task["status"] in ("running", "queued"):
            continue  # in use, or about to be re-run
        idle_for = now - wt.stat().st_mtime  # read before stripping: removing a subfolder bumps the mtime
        stripped += strip_heavy(wt)
        if task is None:
            if idle_for > KEEP_ORPHAN_S:
                _retire(pool, wt, None, False)
                retired += 1
            continue
        if task.get("pipeline_id") and pool.pipeline_active(task["pipeline_id"]):
            continue  # a later stage may still start from this commit
        report = task["report"] or {}
        age = now - task["updated_at"]
        pushed = bool(report.get("pushed"))
        recoverable = task["status"] == "failed" and str(report.get("error", "")).startswith("internal")
        keep_for = KEEP_UNPUSHED_S if (not pushed or recoverable) else (0 if aggressive else KEEP_PUSHED_S)
        if age > keep_for:
            _retire(pool, wt, task, pushed)
            retired += 1
    if stripped or retired:
        log.info("housekeeping: stripped %d heavy dirs, retired %d working copies", stripped, retired)
    return {"stripped": stripped, "retired": retired}


def ensure_space(pool: "Pool") -> bool:
    """True if there is room to start a task. Tries an aggressive cleanup first when space is low."""
    if free_bytes(pool.data) >= MIN_FREE_BYTES:
        return True
    cleanup(pool, aggressive=True)
    return free_bytes(pool.data) >= MIN_FREE_BYTES
