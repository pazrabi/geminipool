#!/usr/bin/env python3
"""Prints one line per finished pool task, so Claude Code's Monitor tool can wake the session for a review.

Usage: pool_watch.py <task_id> [<task_id> ...]
Exits by itself once every given task has finished (done or failed). Needs only Python 3 and the pool token.

The lines carry service fields only. Free text from the agent (its answer, test output, error messages) is
deliberately left out: it is untrusted and must not reach the reviewing model as if it were an instruction.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://193-41-86-92.sslip.io"
DEFAULT_TOKEN_FILE = "~/projects/.gemini-pool-mcp-token"
FINISHED = ("done", "failed", "needs_lead")
TASK_ID = re.compile(r"[0-9a-f]{12}")
FAILURE_REASONS = (("internal", "internal"), ("quota", "quota"), ("account auth", "auth"),
                   ("network", "network"), ("timeout", "timeout"), ("agent exited", "agent_error"))
# Reasons the pool itself gives for a pipeline that needs the lead (fixed words, never free text).
PIPELINE_REASONS = {"max_rounds", "review_no_verdict", "review_failed", "no_changes", "violation", "internal", "quota",
                    "auth", "network", "timeout", "agent_error"}


def safe(value, limit=120):
    """Keep a field to harmless characters so it cannot smuggle text into the notification."""
    return re.sub(r"[^A-Za-z0-9:/._+-]", "?", str(value or ""))[:limit] or "-"


def failure_reason(report):
    report = report or {}
    if report.get("reason") in PIPELINE_REASONS:
        return report["reason"]
    if report.get("violations"):
        return "violation"
    error = str(report.get("error") or "")
    for prefix, name in FAILURE_REASONS:
        if error.startswith(prefix):
            return name
    return "unknown"


def describe(task):
    report = task.get("report") or {}
    parts = ["POOL", "id=" + safe(task["id"]), "status=" + safe(task["status"]),
             "repo=" + safe(task.get("repo")), "base=" + safe(task.get("base_branch")),
             "branch=" + safe(task.get("branch"))]
    if task["status"] == "done":
        tests = report.get("tests")
        parts += ["pushed=" + str(bool(report.get("pushed"))),
                  "tests=" + ("none" if not tests else "passed" if tests.get("passed") else "failed"),
                  "files=" + str(len(report.get("changed_files") or []))]
    else:
        parts.append("reason=" + failure_reason(report))
    pipeline = task.get("pipeline")
    if pipeline:  # the first task stands for the whole develop-review-rework pipeline
        parts.append("rounds=%d" % int(report.get("rounds") or pipeline.get("round") or 1))
        if task["status"] == "done":
            parts.append("verdict=approved")
        if pipeline.get("created_at") and pipeline.get("updated_at"):
            parts.append("duration=%ds" % round(pipeline["updated_at"] - pipeline["created_at"]))
    else:
        started = task.get("started_at")
        if started and task.get("updated_at"):
            parts.append("duration=%ds" % round(task["updated_at"] - started))
    parts.append("model=" + safe(report.get("model") or task.get("model") or "default"))
    return " ".join(parts)


def fetch(url, token, task_id):
    request = urllib.request.Request("%s/api/tasks/%s" % (url, task_id), headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_ids", nargs="+")
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between checks")
    args = parser.parse_args(argv)

    url = os.environ.get("PUBLIC_URL", DEFAULT_URL).rstrip("/")
    token = os.environ.get("POOL_TOKEN") or Path(
        os.environ.get("POOL_TOKEN_FILE", DEFAULT_TOKEN_FILE)).expanduser().read_text().strip()
    pending = []
    for task_id in args.task_ids:
        if TASK_ID.fullmatch(task_id):
            pending.append(task_id)
        else:
            print("POOL id=%s status=unknown" % safe(task_id), flush=True)

    failures = 0
    while pending:
        for task_id in list(pending):
            try:
                task = fetch(url, token, task_id)
                failures = 0
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    print("POOL_WATCH_ERROR auth: the pool token was rejected", flush=True)
                    return 2
                if exc.code == 404:
                    print("POOL id=%s status=unknown" % task_id, flush=True)
                    pending.remove(task_id)
                    continue
                failures += 1
                continue
            except (urllib.error.URLError, OSError, ValueError):
                failures += 1
                if failures == 30:  # one notice after a long outage, then keep trying quietly
                    print("POOL_WATCH_ERROR the pool has been unreachable for a while; still retrying", flush=True)
                continue
            if task["status"] in FINISHED:
                print(describe(task), flush=True)
                pending.remove(task_id)
        if pending:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
