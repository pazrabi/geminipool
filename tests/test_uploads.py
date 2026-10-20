"""Owner uploads: storage rules, pinning, cleanup, handing files to agents, projects, and the HTTP API."""

import asyncio
import http.client
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "app"))

import server  # noqa: E402
import uploads as up  # noqa: E402
from pool import Pool, build_prompt  # noqa: E402
from test_pool import FakeAgent, git, write_file  # noqa: E402

TOKEN = "u" * 40


def put(store, upload_id, rel, data=b"x"):
    return store.add_file(upload_id, rel, io.BytesIO(data), len(data))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pool = Pool(self.tmp / "data", runner=FakeAgent(), accounts=[{"id": "acc1", "role": "developer"}])
        self.store = self.pool.uploads

    def test_files_and_folders_are_stored_with_sizes(self):
        u = self.store.create("photos")
        put(self.store, u["id"], "a/b/c.jpg", b"12345")
        put(self.store, u["id"], "note.txt", b"hello")
        got = self.store.get(u["id"])
        self.assertEqual((got["files"], got["size"]), (2, 10))
        self.assertEqual(sorted(self.store.file_list(u["id"])), ["a/b/c.jpg", "note.txt"])

    def test_dangerous_paths_are_refused(self):
        u = self.store.create("x")
        for bad in ("../evil", "a/../../b", ".git/config", "a/.GIT/x", "", "a/\x00b"):
            with self.assertRaises(up.UploadError, msg=bad):
                put(self.store, u["id"], bad)
        self.assertFalse((self.tmp / "evil").exists())
        put(self.store, u["id"], "/etc/passwd")  # a leading slash is dropped: it lands inside the upload folder
        self.assertTrue((self.store.root / u["id"] / "etc" / "passwd").exists())

    def test_size_limits(self):
        u = self.store.create("x")
        old = up.MAX_FILE_BYTES
        up.MAX_FILE_BYTES = 10
        self.addCleanup(setattr, up, "MAX_FILE_BYTES", old)
        with self.assertRaises(up.UploadError):
            put(self.store, u["id"], "big", b"x" * 11)
        old_total = up.MAX_TOTAL_BYTES
        up.MAX_TOTAL_BYTES = 15
        self.addCleanup(setattr, up, "MAX_TOTAL_BYTES", old_total)
        put(self.store, u["id"], "a", b"x" * 10)
        with self.assertRaises(up.UploadError):
            put(self.store, u["id"], "b", b"x" * 10)

    def test_truncated_upload_leaves_no_file(self):
        u = self.store.create("x")
        with self.assertRaises(up.UploadError):
            self.store.add_file(u["id"], "f", io.BytesIO(b"abc"), 10)
        self.assertEqual(self.store.file_list(u["id"]), [])
        self.assertEqual([p.name for p in (self.store.root / u["id"]).iterdir()], [])

    def test_pinned_survives_cleanup_and_unpinned_expires(self):
        keep = self.store.create("rules", pinned=True)
        drop = self.store.create("old")
        fresh = self.store.create("fresh")
        conn = self.pool._db()
        conn.execute("UPDATE uploads SET last_used = ?, created_at = ? WHERE id IN (?, ?)",
                     (time.time() - 30 * 86400, time.time() - 30 * 86400, keep["id"], drop["id"]))
        conn.close()
        for u in (keep, drop, fresh):
            put(self.store, u["id"], "f")
        self.assertEqual(self.store.cleanup(), 1)
        ids = {u["id"] for u in self.store.list()}
        self.assertEqual(ids, {keep["id"], fresh["id"]})
        self.assertFalse((self.store.root / drop["id"]).exists())

    def test_pin_can_be_switched_and_a_single_file_deleted(self):
        u = self.store.create("rules")
        put(self.store, u["id"], "a.md")
        put(self.store, u["id"], "sub/b.md")
        self.assertTrue(self.store.update(u["id"], pinned=True)["pinned"])
        self.assertFalse(self.store.update(u["id"], pinned=False)["pinned"])
        self.store.delete_file(u["id"], "sub/b.md")
        self.assertEqual(self.store.file_list(u["id"]), ["a.md"])
        for bad in ("nope.md", "../x", ".git"):
            with self.assertRaises(up.UploadError, msg=bad):
                self.store.delete_file(u["id"], bad)

    def test_default_total_limit_is_10_gb(self):
        self.assertEqual(up.MAX_TOTAL_BYTES, 10240 * up.MB)

    def test_upload_used_by_a_queued_task_is_not_deleted_or_expired(self):
        u = self.store.create("data")
        put(self.store, u["id"], "f")
        repo = self.make_repo()
        self.pool.allow_local_repos = True
        self.pool.submit("developer", "g", repo, ["*"], base_branch="main", uploads=[u["id"]])
        with self.assertRaises(up.UploadError):
            self.store.delete(u["id"])
        conn = self.pool._db()
        conn.execute("UPDATE uploads SET last_used = 0, created_at = 0")
        conn.close()
        self.assertEqual(self.store.cleanup(), 0)

    def make_repo(self):
        remote = self.tmp / "remote"
        remote.mkdir(exist_ok=True)
        git(remote, "init", "-q", "-b", "main")
        (remote / "README.md").write_text("hi\n")
        git(remote, "add", "-A")
        git(remote, "commit", "-q", "-m", "init")
        return str(remote)

    def test_unknown_upload_is_rejected_on_submit(self):
        self.pool.allow_local_repos = True
        with self.assertRaises(ValueError):
            self.pool.submit("developer", "g", self.make_repo(), ["*"], uploads=["0" * 12])


class AgentSeesUploadsTests(unittest.TestCase):
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
                         accounts=[{"id": "acc1", "role": "developer"}, {"id": "rev1", "role": "reviewer"}])
        self.store = self.pool.uploads

    def run_pool(self):
        async def go():
            await self.pool.dispatch_once()
            while self.pool.running:
                await asyncio.gather(*list(self.pool.running))
                await self.pool.dispatch_once()
        asyncio.run(go())

    def test_prompt_lists_only_the_attached_files(self):
        photo = self.store.create("screenshot")
        put(self.store, photo["id"], "shot.png", b"png")
        rules = self.store.create("rules of another project", pinned=True)
        put(self.store, rules["id"], "RULES.md", b"be nice")
        self.agent.script = [write_file("a.txt")]
        self.pool.submit("developer", "do it", self.url, ["*.txt"], uploads=[photo["id"]])
        self.run_pool()
        prompt = self.agent.calls[0]["prompt"]
        self.assertIn(str(self.store.root / photo["id"] / "shot.png"), prompt)
        self.assertNotIn("RULES.md", prompt)  # nothing is attached to every task: projects must not mix rules

    def test_prompt_is_unchanged_without_uploads(self):
        task = {"role": "developer", "stage": None, "base_branch": "main", "goal": "g", "allowed_files": ["*"],
                "test_cmd": None}
        self.assertEqual(build_prompt(task), build_prompt(task, ""))
        self.assertEqual(self.store.prompt_block([]), "")

    def test_review_and_rework_stages_inherit_attachments(self):
        photo = self.store.create("spec")
        put(self.store, photo["id"], "spec.pdf", b"pdf")
        self.agent.script = [write_file("a.txt")]
        tid = self.pool.submit("developer", "do it", self.url, ["*.txt"], review=True, uploads=[photo["id"]])
        self.run_pool()
        prompts = [c["prompt"] for c in self.agent.calls]
        self.assertEqual(len(prompts), 2)  # developer and reviewer
        self.assertTrue(all("spec.pdf" in p for p in prompts))
        self.assertEqual(self.pool.get_task(tid)["uploads"], [photo["id"]])

    def test_uploaded_project_becomes_a_repository_and_gets_the_branch(self):
        proj = self.store.create("my site")
        put(self.store, proj["id"], "index.html", b"<h1>hi</h1>")
        put(self.store, proj["id"], "css/site.css", b"body{}")
        self.agent.script = [write_file("css/site.css", "body{color:red}\n")]
        self.pool.push_token = "dummy"
        tid = self.pool.submit("developer", "make it red", f"upload:{proj['id']}", ["css/*"])
        self.run_pool()
        task = self.pool.get_task(tid)
        self.assertEqual(task["status"], "done", task["report"])
        self.assertTrue(task["report"]["pushed"], task["report"])
        project_dir = self.store.root / proj["id"]
        out = subprocess.check_output(["git", "show", f"pool/{tid}:css/site.css"], cwd=project_dir, text=True)
        self.assertIn("color:red", out)
        self.assertIn("body{}", (project_dir / "css/site.css").read_text())  # main is untouched
        self.assertTrue(self.store.get(proj["id"])["project"])
        self.store.delete(proj["id"])  # the task is over, so the project can go
        self.assertFalse(project_dir.exists())


class UploadApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pool = Pool(self.tmp / "data", runner=FakeAgent(), accounts=[{"id": "acc1", "role": "developer"}])
        server.configure(self.pool, TOKEN, None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def req(self, method, path, body=None, raw=False, token=TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Authorization": "Bearer " + token} if token else {}
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body, headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (data if raw else json.loads(data or b"{}"))

    def test_requires_the_token(self):
        for method, path in (("GET", "/api/uploads"), ("POST", "/api/uploads"), ("PUT", "/api/uploads/" + "a" * 12 + "/file?path=x"),
                             ("DELETE", "/api/uploads/" + "a" * 12), ("GET", "/api/uploads/" + "a" * 12 + "/download")):
            self.assertEqual(self.req(method, path, b"x" if method == "PUT" else None, token="")[0], 401, path)

    def test_full_cycle_create_upload_pin_download_delete(self):
        status, u = self.req("POST", "/api/uploads", {"name": "Фото отчёта", "note": "для багов"})
        self.assertEqual(status, 201)
        uid = u["id"]
        self.assertEqual(self.req("PUT", f"/api/uploads/{uid}/file?path=dir/a.txt", b"hello")[0], 200)
        self.assertEqual(self.req("PUT", f"/api/uploads/{uid}/file?path=../x", b"hello")[0], 400)
        status, listing = self.req("GET", "/api/uploads")
        self.assertEqual(status, 200)
        self.assertEqual(listing["uploads"][0]["files"], 1)
        self.assertGreater(listing["limits"]["max_file"], 0)
        status, got = self.req("POST", f"/api/uploads/{uid}", {"pinned": True})
        self.assertTrue(got["pinned"])
        self.assertNotIn("always", got)
        status, one = self.req("GET", f"/api/uploads/{uid}")
        self.assertEqual(one["file_names"], ["dir/a.txt"])
        self.assertEqual(self.req("DELETE", f"/api/uploads/{uid}/file?path=nope")[0], 400)
        self.assertEqual(self.req("PUT", f"/api/uploads/{uid}/file?path=dir/b.txt", b"b")[0], 200)
        self.assertEqual(self.req("DELETE", f"/api/uploads/{uid}/file?path=dir/b.txt")[0], 200)
        status, data = self.req("GET", f"/api/uploads/{uid}/download", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(zipfile.ZipFile(io.BytesIO(data)).read("dir/a.txt"), b"hello")
        self.assertEqual(self.req("DELETE", f"/api/uploads/{uid}")[0], 200)
        self.assertEqual(self.req("GET", f"/api/uploads/{uid}/download")[0], 404)

    def test_bad_flags_and_oversized_file_are_rejected(self):
        status, u = self.req("POST", "/api/uploads", {"name": "x"})
        self.assertEqual(self.req("POST", f"/api/uploads/{u['id']}", {"pinned": "yes"})[0], 400)
        old = up.MAX_FILE_BYTES
        up.MAX_FILE_BYTES = 4
        self.addCleanup(setattr, up, "MAX_FILE_BYTES", old)
        self.assertEqual(self.req("PUT", f"/api/uploads/{u['id']}/file?path=f", b"12345")[0], 400)

    def test_mcp_lists_uploads_and_submit_accepts_them(self):
        status, u = self.req("POST", "/api/uploads", {"name": "spec", "pinned": True})
        status, data = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                 "params": {"name": "list_uploads", "arguments": {}}})
        items = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual([i["id"] for i in items], [u["id"]])
        schema = next(t for t in server.TOOLS if t["name"] == "submit_task")["inputSchema"]["properties"]
        self.assertIn("uploads", schema)


if __name__ == "__main__":
    unittest.main()
