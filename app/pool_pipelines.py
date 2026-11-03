"""Pool mixin: develop-review-rework pipelines and task views."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import git_ops
from pool_rules import FINDINGS_LIMIT, failure_reason, parse_verdict, strip_verdict

log = logging.getLogger("pool")


class PipelinesMixin:
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
            " stage, round, base_ref, parent_id, uploads, save_to, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, role, goal, parent["repo"], parent["base_branch"], json.dumps(allowed), parent["test_cmd"], model,
             pipe["id"], stage, round_no, base_ref, parent["id"],
             json.dumps(parent["uploads"]) if parent.get("uploads") else None, parent.get("save_to"), t, t),
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
