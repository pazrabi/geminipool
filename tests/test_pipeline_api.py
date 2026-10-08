"""The pipeline as the lead sees it: MCP tools and the dashboard payload, over real HTTP."""

import asyncio
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "app"))

import server  # noqa: E402
from pool import Pool  # noqa: E402
from test_pipeline import approve, changes, writes  # noqa: E402
from test_pool import FakeAgent, git  # noqa: E402

TOKEN = "p" * 40


class PipelineApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        remote = self.tmp / "remote"
        remote.mkdir()
        git(remote, "init", "-q", "-b", "main")
        (remote / "README.md").write_text("hi\n")
        git(remote, "add", "-A")
        git(remote, "commit", "-q", "-m", "init")
        self.url = str(remote)
        self.agent = FakeAgent()
        self.pool = Pool(self.tmp / "data", runner=self.agent, allow_local_repos=True,
                         accounts=[{"id": "acc1", "role": "developer"}, {"id": "rev1", "role": "reviewer"}],
                         models_lister=lambda env: (True, "gemini-3.8-flash-low\tFlash low\ngemini-3.1-pro-high\tPro high"))
        server.configure(self.pool, TOKEN, None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def http(self, method, path, obj=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request(method, path, json.dumps(obj) if obj is not None else None,
                     {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def tool(self, name, **args):
        status, data = self.http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                  "params": {"name": name, "arguments": args}})
        self.assertEqual(status, 200)
        result = data["result"]
        return json.loads(result["content"][0]["text"]) if not result.get("isError") else {"error": result["content"][0]["text"]}

    def submit(self, **kw):
        args = dict(role="developer", goal="add x", repo=self.url, allowed_files=["src/*.py"], review=True)
        args.update(kw)
        return self.tool("submit_task", **args)

    def run_pool(self):
        async def go():
            await self.pool.dispatch_once()
            while self.pool.running:
                await asyncio.gather(*list(self.pool.running))
                await self.pool.dispatch_once()
        asyncio.run(go())

    def test_submit_with_review_creates_a_pipeline_and_flags_conflicts(self):
        first = self.submit(allowed_files=["src/**"])
        self.assertEqual((first["status"], first["pipeline"]), ("queued", True))
        self.assertNotIn("warnings", first)
        second = self.submit(allowed_files=["src/a.py"])
        self.assertTrue(any(first["task_id"] in w for w in second["warnings"]))

    def test_tasks_without_review_behave_as_before(self):
        out = self.submit(review=False)
        self.assertFalse(out["pipeline"])
        status = self.tool("task_status", task_id=out["task_id"])
        self.assertNotIn("stage", status)

    def test_status_follows_the_pipeline_and_the_result_is_the_final_report(self):
        self.agent.script = [writes("src/x.py", "v1\n"), changes("x.py:1 wrong"), writes("src/x.py", "v2\n"), approve("All fine.")]
        tid = self.submit()["task_id"]
        before = self.tool("task_status", task_id=tid)
        self.assertEqual((before["status"], before["stage"], before["round"]), ("running", "develop", 1))
        self.run_pool()
        status = self.tool("task_status", task_id=tid)
        self.assertEqual(status["status"], "done")
        result = self.tool("task_result", task_id=tid)["report"]
        self.assertEqual((result["verdict"], result["rounds"], result["changed_files"]), ("APPROVE", 2, ["src/x.py"]))
        self.assertEqual(result["review_summary"], "All fine.")
        self.assertTrue(result["branch"].startswith("pool/"))
        self.assertEqual([s["stage"] for s in result["pipeline"]["steps"]], ["develop", "review", "rework", "review"])

    def test_bad_options_come_back_as_an_error_not_a_crash(self):
        self.assertIn("max_rounds", self.submit(max_rounds=9)["error"])
        self.assertIn("reviewer task", self.submit(role="reviewer")["error"])

    def test_list_models_carries_hints_and_history(self):
        self.agent.script = [writes("src/x.py", "v1\n"), approve()]
        self.submit(model="gemini-3.8-flash-low", review_model="gemini-3.1-pro-high")
        self.run_pool()
        guide = {m["id"]: m for m in self.tool("list_models")}
        self.assertEqual(guide["gemini-3.8-flash-low"]["family"], "flash")
        self.assertEqual(guide["gemini-3.8-flash-low"]["stats"]["tasks"], 1)
        self.assertEqual(guide["gemini-3.1-pro-high"]["effort"], "high")

    def test_dashboard_payload_shows_pipelines_and_counts_each_one_once(self):
        self.agent.script = [writes("src/x.py", "v1\n"), approve()]
        tid = self.submit()["task_id"]
        self.run_pool()
        status, data = self.http("GET", "/api/overview")
        self.assertEqual(status, 200)
        pipe = data["pipelines"][0]
        self.assertEqual((pipe["id"], pipe["status"], pipe["stage"]), (tid, "approved", "approved"))
        self.assertEqual([s["stage"] for s in pipe["steps"]], ["develop", "review"])
        self.assertEqual(data["counts"]["done"], 1)  # one pipeline, not one per stage
        root = [t for t in data["tasks"] if t["id"] == tid][0]
        self.assertEqual(root["status"], "done")  # the pipeline's first task stands for the whole pipeline
        stages = sorted(t["stage"] for t in data["tasks"])
        self.assertEqual(stages, ["develop", "review"])

    def test_the_task_endpoint_returns_the_effective_view(self):
        self.agent.script = [writes("src/x.py", "v1\n"), approve()]
        tid = self.submit()["task_id"]
        self.run_pool()
        status, task = self.http("GET", f"/api/tasks/{tid}")
        self.assertEqual((status, task["status"], task["report"]["verdict"]), (200, "done", "APPROVE"))
        self.assertEqual(task["pipeline"]["stage"], "approved")


if __name__ == "__main__":
    unittest.main()
