"""Tests with a fake agent and a real local git repository (no Gemini quota is spent)."""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import pool as poolmod  # noqa: E402
from pool import Pool, RunResult  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin"})


class FakeAgent:
    """Scripted agent: each call pops the next behaviour from the queue."""

    def __init__(self):
        self.script = []
        self.calls = []

    async def __call__(self, *, prompt, workdir, env, timeout, model=None):
        self.calls.append({"workdir": Path(workdir), "env": env, "model": model, "prompt": prompt})
        action = self.script.pop(0) if self.script else (lambda wd: RunResult(0, json.dumps({"response": "ok"}), ""))
        return action(Path(workdir))


def write_file(rel, text="x\n"):
    def act(wd):
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return RunResult(0, json.dumps({"response": f"edited {rel}"}), "")
    return act


def commit_itself(rel, text="x\n"):
    """An agent that commits its own work instead of leaving it uncommitted."""
    def act(wd):
        write_file(rel, text)(wd)
        env = {"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
               "GIT_COMMITTER_EMAIL": "a@a", "PATH": "/usr/bin:/bin:/usr/local/bin"}
        subprocess.run(["git", "add", "-A"], cwd=wd, check=True, capture_output=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "agent commit"], cwd=wd, check=True, capture_output=True, env=env)
        return RunResult(0, json.dumps({"response": "committed"}), "")
    return act


def failing(stderr):
    return lambda wd: RunResult(1, "", stderr)


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        self.remote.mkdir()
        git(self.remote, "init", "-q", "-b", "main")
        (self.remote / "README.md").write_text("hi\n")
        git(self.remote, "add", "-A")
        git(self.remote, "commit", "-q", "-m", "init")
        self.agent = FakeAgent()
        self.accounts = [{"id": "acc1", "role": "developer"}, {"id": "acc2", "role": "developer"},
                         {"id": "rev1", "role": "reviewer"}]
        self.pool = Pool(self.tmp / "data", runner=self.agent, accounts=self.accounts, allow_local_repos=True)
        self.url = str(self.remote)  # local path works as a clone source

    def run_pool(self):
        async def go():
            await self.pool.dispatch_once()
            while self.pool.running:
                await asyncio.gather(*list(self.pool.running))
                await self.pool.dispatch_once()
        asyncio.run(go())

    def submit(self, **kw):
        args = dict(role="developer", goal="add x", repo=self.url, allowed_files=["src/*.py"])
        args.update(kw)
        return self.pool.submit(**args)

    def test_submit_validates_input(self):
        with self.assertRaises(ValueError):
            self.submit(role="boss")
        with self.assertRaises(ValueError):
            self.submit(allowed_files=[])
        with self.assertRaises(ValueError):
            self.submit(repo="git@github.com:a/b.git")
        with self.assertRaises(ValueError):
            self.submit(base_branch="nope")

    def test_developer_task_done_on_branch(self):
        self.agent.script = [write_file("src/x.py")]
        tid = self.submit(test_cmd="test -f src/x.py")
        self.run_pool()
        t = self.pool.get_task(tid)
        self.assertEqual(t["status"], "done", t["report"])
        self.assertEqual(t["report"]["changed_files"], ["src/x.py"])
        self.assertTrue(t["report"]["tests"]["passed"])
        self.assertEqual(t["branch"], f"pool/{tid}")
        self.assertFalse(t["report"]["pushed"])  # no push token configured

    def test_agent_that_commits_by_itself_still_finishes(self):
        self.agent.script = [commit_itself("src/x.py")]
        tid = self.submit()
        self.run_pool()
        t = self.pool.get_task(tid)
        self.assertEqual(t["status"], "done", t["report"])
        self.assertEqual(t["report"]["changed_files"], ["src/x.py"])
        wt = self.tmp / "data" / "worktrees" / tid
        log = subprocess.run(["git", "log", "--oneline"], cwd=wt, capture_output=True, text=True).stdout
        self.assertIn("agent commit", log)

    def test_change_outside_allowed_files_fails(self):
        self.agent.script = [write_file("secrets.env")]
        tid = self.submit()
        self.run_pool()
        t = self.pool.get_task(tid)
        self.assertEqual(t["status"], "failed")
        self.assertTrue(any("outside allowed" in v for v in t["report"]["violations"]))

    def test_reviewer_must_not_edit(self):
        self.agent.script = [write_file("src/x.py")]
        tid = self.submit(role="reviewer")
        self.run_pool()
        t = self.pool.get_task(tid)
        self.assertEqual(t["status"], "failed")
        self.assertTrue(any("reviewer changed" in v for v in t["report"]["violations"]))

    def test_failing_tests_are_reported_not_hidden(self):
        self.agent.script = [write_file("src/x.py")]
        tid = self.submit(test_cmd="false")
        self.run_pool()
        t = self.pool.get_task(tid)
        self.assertEqual(t["status"], "done")
        self.assertFalse(t["report"]["tests"]["passed"])

    def test_quota_pauses_account_and_moves_task(self):
        self.agent.script = [failing("Error: 429 RESOURCE_EXHAUSTED quota exceeded"), write_file("src/x.py")]
        tid = self.submit()
        self.run_pool()
        t = self.pool.get_task(tid)
        self.assertEqual(t["status"], "done", t["report"])
        self.assertEqual(t["attempts"], 2)
        self.assertEqual(t["account_id"], "acc2")  # the paused account was skipped
        states = {a["id"]: a for a in self.pool.list_accounts()}
        self.assertEqual(states["acc1"]["state"], "quota")
        self.assertGreater(states["acc1"]["paused_until"], 0)

    def test_auth_error_disables_account_for_scheduling(self):
        self.agent.script = [failing("401 UNAUTHENTICATED")]
        self.submit()
        self.run_pool()
        states = {a["id"]: a for a in self.pool.list_accounts()}
        self.assertEqual(states["acc1"]["state"], "auth_error")

    def test_account_google_refuses_is_set_aside_and_its_task_moves_on(self):
        refusal = "error: Eligibility check failed: Your current account is not eligible for Antigravity. Verify your account."
        self.agent.script = [failing(refusal), write_file("src/x.py")]
        tid = self.submit()
        self.run_pool()
        t = self.pool.get_task(tid)
        self.assertEqual((t["status"], t["account_id"]), ("done", "acc2"), t["report"])
        self.assertEqual({a["id"]: a["state"] for a in self.pool.list_accounts()}["acc1"], "auth_error")

    def test_failed_limit_check_sets_an_ineligible_account_aside_and_a_good_one_restores_it(self):
        self.pool.quota_checker = lambda env: (False, "error: Eligibility check failed: not eligible. Verify your account")
        self.pool.check_quota("acc1")
        self.assertEqual(self.pool.accounts_state("acc1"), "auth_error")
        tid = self.submit()
        self.run_pool()
        self.assertEqual(self.pool.get_task(tid)["account_id"], "acc2")  # acc1 got nothing
        self.pool.quota_checker = lambda env: (True, "Gemini Models\tWeekly Limit Remaining\t90%\t2026-10-15T00:00:00Z")
        self.pool.check_quota("acc1")
        self.assertEqual(self.pool.accounts_state("acc1"), "ok")

    def test_a_failed_check_for_another_reason_does_not_ban_the_account(self):
        self.pool.quota_checker = lambda env: (False, "agy did not answer within 90 seconds")
        self.pool.check_quota("acc1")
        self.assertEqual(self.pool.accounts_state("acc1"), "ok")

    def test_task_waits_when_every_matching_account_is_paused(self):
        # Two developer accounts, both hit the quota: the task stays queued until a pause ends.
        self.agent.script = [failing("429 quota")] * 2
        tid = self.submit()
        self.run_pool()
        t = self.pool.get_task(tid)
        self.assertEqual(t["status"], "queued")
        self.assertEqual(t["attempts"], 2)
        states = {a["id"]: a["state"] for a in self.pool.list_accounts()}
        self.assertEqual(states["acc1"], "quota")
        self.assertEqual(states["acc2"], "quota")

    def test_retries_stop_after_max_attempts(self):
        # Network errors are retried after a delay; the third failure is final.
        self.agent.script = [failing("ECONNRESET")] * poolmod.MAX_ATTEMPTS
        tid = self.submit()
        conn = self.pool._db()
        for _ in range(poolmod.MAX_ATTEMPTS):
            self.run_pool()
            conn.execute("UPDATE tasks SET not_before = 0 WHERE id = ?", (tid,))
        t = self.pool.get_task(tid)
        self.assertEqual(t["status"], "failed")
        self.assertEqual(t["attempts"], poolmod.MAX_ATTEMPTS)

    def test_agent_env_has_no_push_token(self):
        self.agent.script = [write_file("src/x.py")]
        self.submit()
        self.run_pool()
        env = self.agent.calls[0]["env"]
        self.assertFalse(any("PUSH" in k for k in env))
        self.assertEqual(env["HOME"], str(self.tmp / "data" / "profiles" / "acc1"))

    def test_agent_cannot_push(self):
        # The worktree hook directory refuses pushes from the agent's own git calls.
        self.agent.script = [write_file("src/x.py")]
        tid = self.submit()
        self.run_pool()
        wt = self.tmp / "data" / "worktrees" / tid
        proc = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/evil"], cwd=wt, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)

    def test_errors_are_logged_as_events(self):
        self.agent.script = [failing("Error: 429 RESOURCE_EXHAUSTED quota exceeded"), write_file("src/x.py")]
        tid = self.submit()
        self.run_pool()
        events = self.pool.list_events()
        self.assertEqual([e["kind"] for e in events], ["quota limit"])
        self.assertEqual(events[0]["account_id"], "acc1")
        self.assertEqual(events[0]["task_id"], tid)
        self.assertIn("RESOURCE_EXHAUSTED", events[0]["message"])

    def test_violation_is_logged(self):
        self.agent.script = [write_file("secrets.env")]
        self.submit()
        self.run_pool()
        self.assertEqual([e["kind"] for e in self.pool.list_events()], ["violation"])

    def test_check_quota_stores_result_per_account(self):
        calls = []
        self.pool.quota_checker = lambda env: (calls.append(env["HOME"]) or True, "Usage: 40% of daily limit")
        res = self.pool.check_quota("acc1")
        self.assertTrue(res["ok"])
        self.assertTrue(calls[0].endswith("profiles/acc1"))
        acc = {a["id"]: a for a in self.pool.list_accounts()}
        self.assertEqual(acc["acc1"]["quota_text"], "Usage: 40% of daily limit")
        self.assertIsNone(acc["acc2"]["quota_text"])

    def test_failed_quota_check_is_logged(self):
        self.pool.quota_checker = lambda env: (False, "not logged in")
        self.assertFalse(self.pool.check_quota("acc1")["ok"])
        self.assertEqual(self.pool.list_events()[0]["kind"], "quota_check_failed")

    def test_check_quota_rejects_unknown_account(self):
        self.pool.quota_checker = lambda env: (True, "")
        with self.assertRaises(ValueError):
            self.pool.check_quota("nope")

    def test_dashboard_overview_shape(self):
        import server
        self.agent.script = [write_file("src/x.py")]
        self.submit()
        self.run_pool()
        data = server.overview(self.pool)
        self.assertEqual(data["counts"]["done"], 1)
        self.assertEqual({a["id"] for a in data["accounts"]}, {"acc1", "acc2", "rev1"})
        task = data["tasks"][0]
        self.assertNotIn("report", task)  # the list stays light; the full report is fetched per task
        self.assertIn("goal", task)

    def test_account_model_is_passed_to_the_agent_and_reported(self):
        accounts = [{"id": "acc1", "role": "developer", "model": "gemini-x"}]
        pool = Pool(self.tmp / "data2", runner=self.agent, accounts=accounts, allow_local_repos=True)
        self.agent.script = [write_file("src/x.py")]
        tid = pool.submit("developer", "g", self.url, ["src/*.py"])

        async def go():
            await pool.dispatch_once()
            await asyncio.gather(*list(pool.running))
        asyncio.run(go())
        self.assertEqual(self.agent.calls[-1]["model"], "gemini-x")
        self.assertEqual(pool.get_task(tid)["report"]["model"], "gemini-x")
        self.assertEqual(pool.list_accounts()[0]["model"], "gemini-x")

    def test_account_without_model_uses_agy_default(self):
        self.assertIsNone({a["id"]: a for a in self.pool.list_accounts()}["acc1"]["model"])

    def test_list_models(self):
        self.pool.models_lister = lambda env: (True, "gemini-x\tGemini X\ngemini-z\tGemini Z")
        self.assertEqual(self.pool.list_models("acc1")["text"], "gemini-x\tGemini X\ngemini-z\tGemini Z")
        with self.assertRaises(ValueError):
            self.pool.list_models("nope")

    def test_task_model_overrides_account_model(self):
        accounts = [{"id": "acc1", "role": "developer", "model": "gemini-a"}]
        pool = Pool(self.tmp / "data3", runner=self.agent, accounts=accounts, allow_local_repos=True,
                    models_lister=lambda env: (True, "gemini-a\tGemini A\ngemini-b\tGemini B"))
        self.agent.script = [write_file("src/x.py")]
        tid = pool.submit("developer", "g", self.url, ["src/*.py"], model="gemini-b")

        async def go():
            await pool.dispatch_once()
            await asyncio.gather(*list(pool.running))
        asyncio.run(go())
        self.assertEqual(self.agent.calls[-1]["model"], "gemini-b")
        self.assertEqual(pool.get_task(tid)["report"]["model"], "gemini-b")

    def test_non_gemini_models_are_hidden_and_rejected(self):
        self.pool.models_lister = lambda env: (
            True, "gemini-a\tGemini A\nclaude-sonnet-4-6\tClaude Sonnet 4.6\ngpt-oss-120b-medium\tGPT-OSS 120B")
        self.assertEqual(list(self.pool.model_catalog()), ["gemini-a"])
        self.assertEqual(self.pool.list_models("acc1")["text"], "gemini-a\tGemini A")
        with self.assertRaises(ValueError):
            self.submit(model="claude-sonnet-4-6")
        with self.assertRaises(ValueError):
            self.pool.set_model("acc1", "gpt-oss-120b-medium")

    def test_unknown_task_model_is_rejected_with_the_available_list(self):
        self.pool.models_lister = lambda env: (True, "gemini-a\tGemini A\ngemini-b\tGemini B")
        with self.assertRaises(ValueError) as ctx:
            self.submit(model="gpt-9")
        self.assertIn("gemini-b", str(ctx.exception))

    def test_model_check_is_skipped_when_the_list_is_unavailable(self):
        self.pool.models_lister = lambda env: (False, "agy failed")
        tid = self.submit(model="anything")
        self.assertEqual(self.pool.get_task(tid)["model"], "anything")

    def test_old_database_gets_the_model_column(self):
        import sqlite3
        data = self.tmp / "old"
        data.mkdir()
        conn = sqlite3.connect(data / "pool.db")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, role TEXT NOT NULL, goal TEXT NOT NULL, repo TEXT NOT NULL,"
                     " base_branch TEXT NOT NULL, allowed_files TEXT NOT NULL, test_cmd TEXT, status TEXT NOT NULL,"
                     " attempts INTEGER NOT NULL DEFAULT 0, account_id TEXT, branch TEXT, report TEXT,"
                     " not_before REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        conn.commit()
        conn.close()
        pool = Pool(data, runner=self.agent, accounts=self.accounts, allow_local_repos=True)
        tid = pool.submit("developer", "g", self.url, ["src/*.py"], model=None)
        self.assertIsNone(pool.get_task(tid)["model"])

    def _break_task_with_internal_error(self):
        """A task whose agent work is on disk but whose finishing step crashed (as the old commit bug did)."""
        self.agent.script = [write_file("src/x.py")]
        tid = self.submit()
        self.run_pool()
        self.assertEqual(self.pool.get_task(tid)["status"], "done")
        self.pool._finish(tid, "failed", {"error": "internal: git -c failed: ", "account": "acc1"})
        return tid

    def test_recover_redoes_the_finishing_steps_without_running_the_agent(self):
        tid = self._break_task_with_internal_error()
        calls_before = len(self.agent.calls)
        task = self.pool.recover_task(tid)
        self.assertEqual(len(self.agent.calls), calls_before)  # no quota is spent
        task = self.pool.get_task(tid)
        self.assertEqual(task["status"], "done", task["report"])
        self.assertEqual(task["report"]["changed_files"], ["src/x.py"])
        self.assertIn("recovered", task["report"]["summary"])

    def test_recover_refuses_tasks_that_did_not_hit_an_internal_error(self):
        self.agent.script = [write_file("src/x.py")]
        tid = self.submit()
        self.run_pool()
        with self.assertRaises(ValueError):
            self.pool.recover_task(tid)  # status done
        with self.assertRaises(ValueError):
            self.pool.recover_task("nope00000000")

    def test_recover_needs_the_working_copy(self):
        tid = self._break_task_with_internal_error()
        import shutil
        shutil.rmtree(self.tmp / "data" / "worktrees" / tid)
        with self.assertRaises(ValueError):
            self.pool.recover_task(tid)

    def test_draining_stops_new_tasks_but_keeps_the_queue(self):
        tid = self.submit()
        self.pool.set_draining(True)
        self.assertIsNone(self.pool._claim_next())
        self.assertEqual(self.pool.get_task(tid)["status"], "queued")
        status = self.pool.maintenance_status()
        self.assertEqual((status["draining"], status["running"], status["safe_to_deploy"]), (True, 0, True))
        self.pool.set_draining(False)
        self.assertIsNotNone(self.pool._claim_next())

    def test_not_safe_to_deploy_while_a_task_runs(self):
        self.submit()
        self.pool._claim_next()
        status = self.pool.set_draining(True)
        self.assertEqual((status["running"], status["safe_to_deploy"]), (1, False))

    def test_a_restart_switches_maintenance_mode_off(self):
        self.pool.set_draining(True)
        restarted = Pool(self.tmp / "data", runner=self.agent, accounts=self.accounts, allow_local_repos=True)
        self.assertFalse(restarted.draining)

    def test_start_time_is_recorded_when_a_task_is_claimed(self):
        tid = self.submit()
        self.assertIsNone(self.pool.get_task(tid)["started_at"])  # waiting in the queue
        before = time.time()
        self.pool._claim_next()
        started = self.pool.get_task(tid)["started_at"]
        self.assertGreaterEqual(started, before)
        self.assertLessEqual(started, time.time())

    def test_set_model_validates_persists_and_resets(self):
        self.pool.models_lister = lambda env: (True, "gemini-a\tGemini A\ngemini-b\tGemini B")
        self.pool.set_model("acc1", "gemini-b")
        self.assertEqual({a["id"]: a for a in self.pool.list_accounts()}["acc1"]["model"], "gemini-b")
        saved = json.loads((self.tmp / "data" / "accounts.json").read_text())
        self.assertEqual({a["id"]: a.get("model") for a in saved}["acc1"], "gemini-b")
        with self.assertRaises(ValueError):
            self.pool.set_model("acc1", "not-a-model")
        self.pool.set_model("acc1", None)
        self.assertIsNone({a["id"]: a for a in self.pool.list_accounts()}["acc1"]["model"])

    def test_restart_requeues_running_tasks(self):
        tid = self.submit()
        self.pool._claim_next()  # simulate a crash mid-run
        self.assertEqual(self.pool.get_task(tid)["status"], "running")
        self.pool._recover()
        self.assertEqual(self.pool.get_task(tid)["status"], "queued")


if __name__ == "__main__":
    unittest.main()
