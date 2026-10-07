"""Runs one agent task with the Antigravity CLI (agy) in print mode."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path

from pool import RunResult

AGY_BIN = os.environ.get("AGY_BIN", "agy")


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
