"""Dashboard API payloads: the overview, task summaries and upload limits."""

from __future__ import annotations

import time
from typing import Any

import uploads as uploads_mod
from pool import Pool


def _task_summary(t: dict[str, Any]) -> dict[str, Any]:
    report = t["report"] or {}
    tests = report.get("tests") or {}
    note = (report.get("error") or report.get("last_error") or "; ".join(report.get("violations") or [])
            or report.get("reason") or None)
    return {
        "id": t["id"], "role": t["role"], "status": t["status"], "goal": t["goal"], "repo": t["repo"],
        "account_id": t["account_id"], "attempts": t["attempts"], "branch": t["branch"], "model": t["model"],
        "created_at": t["created_at"], "updated_at": t["updated_at"], "started_at": t["started_at"],
        "pushed": report.get("pushed"), "tests_passed": tests.get("passed") if tests else None,
        "note": note,
        "pipeline_id": t.get("pipeline_id"), "stage": t.get("stage"), "round": t.get("round"),
    }


def uploads_limits(pool: Pool) -> dict[str, Any]:
    return {"max_file": uploads_mod.MAX_FILE_BYTES, "max_total": uploads_mod.MAX_TOTAL_BYTES,
            "used": pool.uploads.total_bytes(), "ttl_days": uploads_mod.TTL_S // 86400}


def overview(pool: Pool) -> dict[str, Any]:
    raw = pool.list_tasks(60)
    tasks = [pool.effective_task(t) for t in raw]
    # queued/running show real activity; done/failed count units of work (a pipeline once, not once per stage)
    counts = {s: 0 for s in ("queued", "running", "done", "failed")}
    for r in raw:
        if r["status"] in ("queued", "running"):
            counts[r["status"]] += 1
    for t in tasks:
        if t.get("pipeline_id") and t["pipeline_id"] != t["id"]:
            continue
        status = {"needs_lead": "failed"}.get(t["status"], t["status"])
        if status in ("done", "failed"):
            counts[status] += 1
    return {
        "now": time.time(),
        "accounts": pool.list_accounts(),
        "tasks": [_task_summary(t) for t in tasks],
        "pipelines": pool.list_pipelines(50),
        "events": pool.list_events(50),
        "counts": counts,
        **pool.maintenance_status(),
    }

