"""Git operations for gemini-pool: clones, worktrees, staging, commits, pushes."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _auth_header(token: str) -> str:
    raw = f"x-access-token:{token}".encode()
    return "Authorization: Basic " + base64.b64encode(raw).decode()


def _git(args: list[str], cwd: Path | None = None, token: str | None = None, check: bool = True) -> str:
    cmd = ["git"]
    if token:
        cmd += ["-c", f"http.extraHeader={_auth_header(token)}"]
    cmd += args
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=600)
    if check and proc.returncode != 0:
        # The error text never contains the token: git prints only its own stderr.
        detail = (proc.stderr.strip() or proc.stdout.strip())[-800:]  # some git messages go to stdout
        raise GitError(f"git {' '.join(a for a in args if not a.startswith('-c'))[:60]} failed: {detail}")
    return proc.stdout.strip()


def repo_dir(repos: Path, url: str) -> Path:
    name = url.rstrip("/").split("/")[-1].removesuffix(".git") or "repo"
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return repos / f"{name}-{digest}"


def install_nohooks(path: Path) -> Path:
    """Hooks directory used by agent worktrees. Its pre-push hook refuses every push."""
    path.mkdir(parents=True, exist_ok=True)
    hook = path / "pre-push"
    hook.write_text("#!/bin/sh\necho 'push is disabled for pool agents' >&2\nexit 1\n")
    hook.chmod(0o755)
    return path


def ensure_repo(url: str, repos: Path, token: str | None) -> Path:
    d = repo_dir(repos, url)
    if not (d / ".git").exists():
        _git(["clone", "--quiet", url, str(d)], token=token)
    else:
        _git(["fetch", "--quiet", "--prune", "origin"], cwd=d, token=token)
    _git(["config", "extensions.worktreeConfig", "true"], cwd=d)
    return d


def check_branch(d: Path, base: str) -> None:
    try:
        _git(["rev-parse", "--verify", "--quiet", f"origin/{base}"], cwd=d)
    except GitError:
        raise ValueError(f"base branch '{base}' not found in repository")


def prepare_worktree(d: Path, wt: Path, task_id: str, base: str, token: str | None, nohooks: Path) -> None:
    branch = f"pool/{task_id}"
    # A requeued task starts from scratch: drop the previous attempt's worktree and branch.
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], cwd=d, check=False)
        shutil.rmtree(wt, ignore_errors=True)
    _git(["branch", "-D", branch], cwd=d, check=False)
    _git(["worktree", "prune"], cwd=d, check=False)
    _git(["fetch", "--quiet", "origin", base], cwd=d, token=token)
    _git(["worktree", "add", "-q", "-b", branch, str(wt), f"origin/{base}"], cwd=d)
    _git(["config", "--worktree", "core.hooksPath", str(nohooks)], cwd=wt)


def stage_and_diff(wt: Path, base: str) -> tuple[list[str], str]:
    """Stage everything and list the files that differ from the base branch."""
    _git(["add", "-A"], cwd=wt)
    files = [f for f in _git(["diff", "--cached", "--name-only", f"origin/{base}"], cwd=wt).splitlines() if f]
    stats = _git(["diff", "--cached", "--shortstat", f"origin/{base}"], cwd=wt)
    return files, stats


def commit(wt: Path, message: str) -> None:
    """Commit what is staged. If the agent already committed its own work there is nothing left to commit."""
    if not _git(["diff", "--cached", "--name-only"], cwd=wt):
        return
    _git(["-c", "user.name=gemini-pool", "-c", "user.email=pool@localhost", "commit", "-q", "-m", message], cwd=wt)


def push(wt: Path, branch: str, token: str) -> None:
    # Pool's own push: the hook directory is bypassed here, and the token is only sent for this call.
    _git(["-c", "core.hooksPath=/dev/null", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=wt, token=token)
