"""Tests with a fake agent and a real local git repository (no Gemini quota is spent)."""

import asyncio
import json
import subprocess
import sys
import tempfile
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

    async def __call__(self, *, prompt, workdir, env, timeout):
        self.calls.append({"workdir": Path(workdir), "env": env})
        action = self.script.pop(0) if self.script else (lambda wd: RunResult(0, json.dumps({"response": "ok"}), ""))
        return action(Path(workdir))


def write_file(rel, text="x\n"):
    def act(wd):
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return RunResult(0, json.dumps({"response": f"edited {rel}"}), "")
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

    def test_restart_requeues_running_tasks(self):
        tid = self.submit()
        self.pool._claim_next()  # simulate a crash mid-run
        self.assertEqual(self.pool.get_task(tid)["status"], "running")
        self.pool._recover()
        self.assertEqual(self.pool.get_task(tid)["status"], "queued")


if __name__ == "__main__":
    unittest.main()
