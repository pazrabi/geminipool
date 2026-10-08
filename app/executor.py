"""Runs one agent task with the Antigravity CLI (agy) in print mode."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path

from pool import RunResult

AGY_BIN = os.environ.get("AGY_BIN", "agy")


def agy_quota(env: dict[str, str]) -> tuple[bool, str]:
    """Run agy's /usage in print mode for one account. Blocking; returns (ok, output)."""
    try:
        proc = subprocess.run(
            [AGY_BIN, "-p", "/usage"],
            cwd=env["HOME"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return False, "agy did not answer within 90 seconds"
    text = f"{proc.stdout}\n{proc.stderr}".strip()
    return proc.returncode == 0, text[-4000:]


async def agy_runner(
    *,
    prompt: str,
    workdir: Path,
    env: dict[str, str],
    timeout: float,
) -> RunResult:
    args = [AGY_BIN, "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"]
    model = os.environ.get("AGY_MODEL")
    if model:
        args += ["--model", model]
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(workdir),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        return RunResult(returncode=-1, stdout="", stderr="timeout", timed_out=True)
    return RunResult(
        returncode=proc.returncode,
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace"),
    )
