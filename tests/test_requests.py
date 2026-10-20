"""Tasks typed into the dashboard, tasks without a repository, and files the agents hand back to the storage."""

import asyncio
import http.client
import io
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
import uploads as up  # noqa: E402
from pool import Pool, RunResult  # noqa: E402
from test_pool import FakeAgent, git, write_file  # noqa: E402

TOKEN = "r" * 40


class OutboxAgent(FakeAgent):
    """FakeAgent that also drops files into the output folder it finds in the prompt."""

    def __init__(self):
        super().__init__()
        self.outbox_files = {}

    async def __call__(self, *, prompt, workdir, env, timeout, model=None):
        for line in prompt.splitlines():
            if line.startswith("Output folder for files meant for the owner: "):
                box = Path(line.split(": ", 1)[1].strip())
                for rel, data in self.outbox_files.items():
                    (box / rel).parent.mkdir(parents=True, exist_ok=True)
                    (box / rel).write_bytes(data)
        return await super().__call__(prompt=prompt, workdir=workdir, env=env, timeout=timeout, model=model)


def put(store, upload_id, rel, data=b"x"):
    return store.add_file(upload_id, rel, io.BytesIO(data), len(data))


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.agent = OutboxAgent()
        self.pool = Pool(self.tmp / "data", runner=self.agent, allow_local_repos=True,
                         accounts=[{"id": "acc1", "role": "developer"}, {"id": "rev1", "role": "reviewer"}])
        self.store = self.pool.uploads
        server.configure(self.pool, TOKEN, None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def run_pool(self):
        async def go():
            await self.pool.dispatch_once()
            while self.pool.running:
                await asyncio.gather(*list(self.pool.running))
                await self.pool.dispatch_once()
        asyncio.run(go())

    def http(self, method, path, obj=None, token=TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Authorization": "Bearer " + token} if token else {}
        conn.request(method, path, json.dumps(obj) if obj is not None else None, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    # ---- scratch tasks -------------------------------------------------

    def test_scratch_task_answers_and_files_land_in_a_new_upload(self):
        src = self.store.create("source")
        put(self.store, src["id"], "data.csv", b"a,b\n1,2\n")
        self.agent.outbox_files = {"result.txt": b"done", "sub/more.txt": b"more"}
        tid = self.pool.submit("developer", "convert the csv", "scratch", [], uploads=[src["id"]])
        self.run_pool()
        task = self.pool.get_task(tid)
        self.assertEqual(task["status"], "done", task["report"])
        prompt = self.agent.calls[0]["prompt"]
        self.assertIn("scratch space", prompt)
        self.assertIn(str(self.store.root / src["id"] / "data.csv"), prompt)
        self.assertNotIn("Allowed file patterns", prompt)
        out = task["report"]["outputs"]
        self.assertEqual(out["count"], 2)
        created = self.store.get(out["upload_id"])
        self.assertFalse(created["pinned"])
        self.assertEqual(sorted(self.store.file_list(out["upload_id"])), ["result.txt", "sub/more.txt"])
        self.assertFalse(self.store.outbox(tid).exists())

    def test_scratch_without_files_creates_no_upload(self):
        tid = self.pool.submit("developer", "what is 2+2", "scratch", [])
        self.run_pool()
        task = self.pool.get_task(tid)
        self.assertEqual(task["status"], "done")
        self.assertIsNone(task["report"]["outputs"])
        self.assertEqual(self.store.list(), [])

    def test_save_to_adds_to_an_existing_upload_without_overwriting(self):
        target = self.store.create("notes", pinned=True)
        put(self.store, target["id"], "a.txt", b"mine")
        self.agent.outbox_files = {"a.txt": b"agent", "b.txt": b"new"}
        tid = self.pool.submit("developer", "add files", "scratch", [], save_to=target["id"])
        self.run_pool()
        out = self.pool.get_task(tid)["report"]["outputs"]
        self.assertEqual(out["upload_id"], target["id"])
        root = self.store.root / target["id"]
        self.assertEqual((root / "a.txt").read_bytes(), b"mine")  # the owner's file is untouched
        self.assertEqual((root / "b.txt").read_bytes(), b"new")
        self.assertTrue(any(f.startswith("a-") for f in self.store.file_list(target["id"])))
        self.assertEqual(len(self.store.list()), 1)

    def test_oversized_agent_file_is_skipped_not_stored(self):
        old = up.MAX_FILE_BYTES
        up.MAX_FILE_BYTES = 10
        self.addCleanup(setattr, up, "MAX_FILE_BYTES", old)
        self.agent.outbox_files = {"big.bin": b"x" * 50, "ok.txt": b"ok"}
        tid = self.pool.submit("developer", "make files", "scratch", [])
        self.run_pool()
        out = self.pool.get_task(tid)["report"]["outputs"]
        self.assertEqual(out["saved"], ["ok.txt"])
        self.assertEqual(out["skipped"], ["big.bin"])

    def test_scratch_cannot_be_reviewed_and_needs_a_valid_save_target(self):
        with self.assertRaises(ValueError):
            self.pool.submit("developer", "g", "scratch", [], review=True)
        with self.assertRaises(ValueError):
            self.pool.submit("developer", "g", "scratch", [], save_to="0" * 12)

    def test_scratch_task_leaves_no_state_behind(self):
        self.agent.outbox_files = {"x.txt": b"x"}
        tid = self.pool.submit("developer", "g", "scratch", [])
        self.run_pool()
        self.assertTrue(self.pool.get_task(tid)["report"]["outputs"]["count"] == 1)
        self.assertEqual(list((self.tmp / "data" / "outbox").iterdir()), [])

    # ---- repository tasks also get an output folder ----------------------

    def test_repository_task_can_hand_back_files_too(self):
        remote = self.tmp / "remote"
        remote.mkdir()
        git(remote, "init", "-q", "-b", "main")
        (remote / "README.md").write_text("hi\n")
        git(remote, "add", "-A")
        git(remote, "commit", "-q", "-m", "init")
        self.agent.outbox_files = {"report.md": b"# report"}
        self.agent.script = [write_file("a.txt")]
        tid = self.pool.submit("developer", "change and report", str(remote), ["*.txt"])
        self.run_pool()
        task = self.pool.get_task(tid)
        self.assertEqual(task["status"], "done", task["report"])
        self.assertEqual(task["report"]["changed_files"], ["a.txt"])  # the output folder is not part of the diff
        self.assertEqual(task["report"]["outputs"]["count"], 1)
        self.assertIn("Output folder", self.agent.calls[0]["prompt"])

    # ---- the dashboard endpoints ---------------------------------------

    def test_requests_need_the_token(self):
        self.assertEqual(self.http("GET", "/api/requests", token="")[0], 401)
        self.assertEqual(self.http("POST", "/api/requests", {"goal": "x"}, token="")[0], 401)

    def test_post_validates_input(self):
        for bad in ({}, {"goal": "  "}, {"goal": "x", "role": "reviewer"}, {"goal": "x", "uploads": "a"},
                    {"goal": "x", "uploads": ["0" * 12]}, {"goal": "x", "save_to": "0" * 12}, {"goal": "x", "model": 5}):
            self.assertEqual(self.http("POST", "/api/requests", bad)[0], 400, bad)

    def test_post_runs_a_task_and_get_shows_the_answer_and_files(self):
        src = self.store.create("input")
        put(self.store, src["id"], "in.txt")
        self.agent.outbox_files = {"out.txt": b"y"}
        status, data = self.http("POST", "/api/requests", {"goal": "do it", "uploads": [src["id"]]})
        self.assertEqual(status, 201)
        self.run_pool()
        status, listing = self.http("GET", "/api/requests")
        item = listing["requests"][0]
        self.assertEqual((item["id"], item["status"]), (data["task_id"], "done"))
        self.assertEqual(item["summary"], "ok")
        self.assertEqual(item["outputs"]["count"], 1)
        self.assertEqual(item["uploads"], [src["id"]])

    def test_requests_list_only_panel_tasks(self):
        self.pool.submit("developer", "from claude", "scratch", [])
        self.assertEqual(self.http("GET", "/api/requests")[1]["requests"], [])

    def test_project_mode_works_on_an_uploaded_folder(self):
        proj = self.store.create("site")
        put(self.store, proj["id"], "index.html", b"<h1>x</h1>")
        self.agent.script = [write_file("index.html", "<h1>y</h1>\n")]
        self.pool.push_token = "dummy"
        status, data = self.http("POST", "/api/requests", {"goal": "change heading", "project": proj["id"]})
        self.assertEqual(status, 201, data)
        self.run_pool()
        task = self.pool.get_task(data["task_id"])
        self.assertEqual(task["status"], "done", task["report"])
        self.assertEqual(task["repo"], str(self.store.root / proj["id"]))

    def test_models_endpoint_and_mcp_schema(self):
        status, data = self.http("GET", "/api/models")
        self.assertEqual(status, 200)
        self.assertIn("models", data)
        props = next(t for t in server.TOOLS if t["name"] == "submit_task")["inputSchema"]["properties"]
        self.assertIn("save_to", props)


if __name__ == "__main__":
    unittest.main()
