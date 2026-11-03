"""Pool mixin: request list and model statistics for the dashboard."""

from __future__ import annotations

import logging
from typing import Any

from pool_rules import model_hint

log = logging.getLogger("pool")


class ReportsMixin:
    def list_requests(self, limit: int = 15) -> list[dict[str, Any]]:
        """Tasks the owner started from the dashboard's task panel, newest first, with the agent's answer."""
        conn = self._db()
        try:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM tasks WHERE origin = 'panel' ORDER BY created_at DESC LIMIT ?", (limit,))]
        finally:
            conn.close()
        out = []
        for task_id in ids:
            t = self.task_view(task_id)
            if t is None:
                continue
            report = t["report"] or {}
            out.append({
                "id": t["id"], "status": t["status"], "goal": t["goal"], "model": t["model"], "role": t["role"],
                "account_id": t["account_id"], "created_at": t["created_at"], "started_at": t["started_at"],
                "updated_at": t["updated_at"], "repo": t["repo"], "uploads": t["uploads"],
                "summary": (report.get("summary") or "")[:8000],
                "error": report.get("error") or report.get("last_error") or report.get("reason"),
                "outputs": report.get("outputs"), "branch": report.get("branch") or t.get("branch"),
                "pushed": report.get("pushed"), "stage": (t.get("pipeline") or {}).get("stage"),
            })
        return out

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
