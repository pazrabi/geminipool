"""Adding, enabling and removing agents, the login requirement and the concurrency cap."""

import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import server  # noqa: E402
from pool import Pool  # noqa: E402

TOKEN = "a" * 40


async def _never(**kw):
    raise RuntimeError("not used")


def queue_task(pool, task_id, role="developer"):
    now = time.time()
    conn = sqlite3.connect(pool.db_path)
    conn.execute(
        "INSERT INTO tasks (id, role, goal, repo, base_branch, allowed_files, status, attempts, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (task_id, role, "g", "https://github.com/o/r.git", "main", '["a"]', "queued", 0, now, now))
    conn.commit()
    conn.close()


def log_in(pool, account_id):
    token = pool.profiles / account_id / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("x")


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.make_pool()

    def make_pool(self, **kw):
        self.pool = Pool(self.tmp / "data", runner=_never, accounts=[{"id": "acc1", "role": "developer"}], **kw)

    def accounts(self):
        return {a["id"]: a for a in self.pool.list_accounts()}

    def test_add_account_registers_it_everywhere(self):
        acc = self.pool.add_account("acc2", "reviewer")
        self.assertEqual((acc["id"], acc["role"], acc["enabled"], acc["logged_in"]), ("acc2", "reviewer", 1, False))
        self.assertTrue((self.pool.profiles / "acc2").is_dir())
        saved = json.loads((self.tmp / "data" / "accounts.json").read_text())
        self.assertEqual({a["id"]: a["role"] for a in saved}, {"acc1": "developer", "acc2": "reviewer"})

    def test_add_account_validates(self):
        for bad in ("", "Acc2", "2acc", "a b", "a" * 17, "../x", "acc.2"):
            with self.assertRaises(ValueError, msg=bad):
                self.pool.add_account(bad)
        with self.assertRaises(ValueError):
            self.pool.add_account("acc2", "boss")
        self.pool.add_account("acc2")
        with self.assertRaises(ValueError):
            self.pool.add_account("acc2")

    def test_without_login_requirement_every_account_takes_tasks(self):
        self.pool.add_account("acc2")
        queue_task(self.pool, "aaaaaaaaaaa1")
        self.assertIsNotNone(self.pool._claim_next())

    def test_a_new_agent_gets_no_tasks_until_it_is_logged_in(self):
        self.make_pool(require_login=True)
        queue_task(self.pool, "aaaaaaaaaaa1")
        self.assertIsNone(self.pool._claim_next())  # acc1 has no login either
        log_in(self.pool, "acc1")
        task, account = self.pool._claim_next()
        self.assertEqual(account, "acc1")
        self.pool.add_account("acc2")
        queue_task(self.pool, "aaaaaaaaaaa2")
        self.assertIsNone(self.pool._claim_next())  # acc1 is busy, acc2 has not logged in
        log_in(self.pool, "acc2")
        self.assertEqual(self.pool._claim_next()[1], "acc2")

    def test_disabled_agent_is_skipped_and_the_choice_survives_a_restart(self):
        self.pool.add_account("acc2")
        self.pool.set_account_enabled("acc1", False)
        queue_task(self.pool, "aaaaaaaaaaa1")
        self.assertEqual(self.pool._claim_next()[1], "acc2")
        saved = json.loads((self.tmp / "data" / "accounts.json").read_text())
        restarted = Pool(self.tmp / "data", runner=_never, accounts=saved)
        self.assertEqual({a: v["enabled"] for a, v in {x["id"]: x for x in restarted.list_accounts()}.items()},
                         {"acc1": 0, "acc2": 1})

    def test_remove_account_deletes_its_profile_and_records(self):
        self.pool.add_account("acc2")
        log_in(self.pool, "acc2")
        self.pool.remove_account("acc2")
        self.assertNotIn("acc2", self.accounts())
        self.assertFalse((self.pool.profiles / "acc2").exists())
        saved = json.loads((self.tmp / "data" / "accounts.json").read_text())
        self.assertEqual([a["id"] for a in saved], ["acc1"])

    def test_a_working_agent_cannot_be_removed(self):
        queue_task(self.pool, "aaaaaaaaaaa1")
        self.pool._claim_next()
        with self.assertRaises(ValueError):
            self.pool.remove_account("acc1")
        self.assertIn("acc1", self.accounts())

    def test_unknown_accounts_are_rejected(self):
        with self.assertRaises(ValueError):
            self.pool.remove_account("nope")
        with self.assertRaises(ValueError):
            self.pool.set_account_enabled("nope", True)

    def test_concurrency_cap_limits_running_tasks(self):
        self.make_pool(max_concurrent=1)
        self.pool.add_account("acc2")
        queue_task(self.pool, "aaaaaaaaaaa1")
        queue_task(self.pool, "aaaaaaaaaaa2")
        self.assertIsNotNone(self.pool._claim_next())
        self.assertIsNone(self.pool._claim_next())  # a free agent exists, but the cap is reached

    def test_no_cap_by_default(self):
        self.pool.add_account("acc2")
        queue_task(self.pool, "aaaaaaaaaaa1")
        queue_task(self.pool, "aaaaaaaaaaa2")
        self.assertIsNotNone(self.pool._claim_next())
        self.assertIsNotNone(self.pool._claim_next())


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.pool = Pool(tmp / "data", runner=_never, accounts=[{"id": "acc1", "role": "developer"}])
        server.configure(self.pool, TOKEN, None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def call(self, method, path, obj=None, token=TOKEN):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, json.dumps(obj) if obj is not None else None, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def test_every_account_endpoint_needs_the_token(self):
        for method, path, body in (("POST", "/api/accounts", {"id": "acc2"}),
                                   ("POST", "/api/accounts/acc1/enabled", {"enabled": False}),
                                   ("DELETE", "/api/accounts/acc1", None)):
            self.assertEqual(self.call(method, path, body, token=None)[0], 401, path)
        self.assertIn("acc1", [a["id"] for a in self.pool.list_accounts()])

    def test_add_toggle_and_remove_over_http(self):
        status, acc = self.call("POST", "/api/accounts", {"id": "acc2", "role": "reviewer"})
        self.assertEqual((status, acc["id"], acc["role"], acc["logged_in"]), (201, "acc2", "reviewer", False))
        self.assertEqual(self.call("POST", "/api/accounts", {"id": "acc2"})[0], 400)  # duplicate
        self.assertEqual(self.call("POST", "/api/accounts", {"id": "Bad Id"})[0], 400)
        status, acc = self.call("POST", "/api/accounts/acc2/enabled", {"enabled": False})
        self.assertEqual((status, acc["enabled"]), (200, 0))
        self.assertEqual(self.call("POST", "/api/accounts/acc2/enabled", {"enabled": "no"})[0], 400)
        self.assertEqual(self.call("DELETE", "/api/accounts/acc2")[0], 200)
        self.assertEqual(self.call("DELETE", "/api/accounts/acc2")[0], 400)

    def test_overview_tells_whether_each_agent_is_logged_in(self):
        status, data = self.call("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertIn("logged_in", data["accounts"][0])


if __name__ == "__main__":
    unittest.main()
