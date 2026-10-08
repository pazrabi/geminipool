"""The develop -> review -> rework -> approved pipeline, driven with scripted agents and a real local git repo."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "app"))

import asyncio  # noqa: E402

import housekeeping  # noqa: E402
import pool as poolmod  # noqa: E402
from pool import Pool, RunResult, model_hint, patterns_overlap  # noqa: E402
from test_pool import FakeAgent, failing, git, write_file  # noqa: E402


def says(text):
    """A reviewer that answers with `text`."""
    return lambda wd: RunResult(0, json.dumps({"response": text}), "")


def approve(note="Looks good."):
    return says(f"{note}\n\nVERDICT: APPROVE")


def changes(note="src/x.py:1 has a bug, fix it."):
    return says(f"{note}\n\nVERDICT: CHANGES")


def writes(rel, text):
    return write_file(rel, text)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        self.remote.mkdir()
        git(self.remote, "init", "-q", "-b", "main")
        (self.remote / "README.md").write_text("hi\n")
        git(self.remote, "add", "-A")
        git(self.remote, "commit", "-q", "-m", "init")
        self.url = str(self.remote)
        self.agent = FakeAgent()
        self.make_pool([{"id": "acc1", "role": "developer"}, {"id": "acc2", "role": "developer"},
                        {"id": "rev1", "role": "reviewer"}])

    def make_pool(self, accounts, **kw):
        self.pool = Pool(self.tmp / "data", runner=self.agent, accounts=accounts, allow_local_repos=True, **kw)

    def run_pool(self):
        async def go():
            await self.pool.dispatch_once()
            while self.pool.running:
                await asyncio.gather(*list(self.pool.running))
                await self.pool.dispatch_once()
        asyncio.run(go())

    def run_one_wave(self):
        """Start whatever can start now and wait for it to finish, without starting the stages that follow."""
        async def go():
            await self.pool.dispatch_once()
            while self.pool.running:
                await asyncio.gather(*list(self.pool.running))
        asyncio.run(go())

    def submit(self, **kw):
        args = dict(role="developer", goal="add x", repo=self.url, allowed_files=["src/*.py"], review=True)
        args.update(kw)
        return self.pool.submit(**args)

    def view(self, task_id):
        return self.pool.task_view(task_id)

    def steps(self, task_id):
        return [(s["stage"], s["round"], s["status"]) for s in self.view(task_id)["pipeline"]["steps"]]

    # ---- the paths through the pipeline -------------------------------------------------------------------------

    def test_approved_in_one_round(self):
        seen = {}

        def reviewer(wd):
            seen["file"] = (wd / "src" / "x.py").read_text() if (wd / "src" / "x.py").exists() else None
            return approve("Clean change.")(wd)

        self.agent.script = [writes("src/x.py", "v1\n"), reviewer]
        tid = self.submit(test_cmd="test -f src/x.py")
        self.run_pool()
        self.assertEqual(seen["file"], "v1\n")  # the reviewer works on the developer's commit
        task = self.view(tid)
        self.assertEqual(task["status"], "done")
        r = task["report"]
        self.assertEqual((r["verdict"], r["rounds"], r["changed_files"], r["reason"]), ("APPROVE", 1, ["src/x.py"], None))
        self.assertEqual(r["review_summary"], "Clean change.")  # the verdict line is not part of the text
        self.assertTrue(r["tests"]["passed"])
        self.assertEqual(task["branch"], f"pool/{tid}")
        self.assertEqual(self.steps(tid), [("develop", 1, "done"), ("review", 1, "done")])
        self.assertEqual(self.pool.get_pipeline(tid)["status"], "approved")

    def test_reviewer_sends_it_back_and_the_second_round_is_approved(self):
        seen = {}

        def rework(wd):
            seen["before"] = (wd / "src" / "x.py").read_text()  # starts from the first attempt
            return write_file("src/x.py", "v2\n")(wd)

        self.agent.script = [writes("src/x.py", "v1\n"), changes("x.py:1 returns the wrong value."), rework, approve()]
        tid = self.submit(model=None)
        self.run_pool()
        self.assertEqual(seen["before"], "v1\n")
        task = self.view(tid)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["report"]["rounds"], 2)
        self.assertEqual(self.steps(tid), [("develop", 1, "done"), ("review", 1, "done"), ("rework", 2, "done"),
                                           ("review", 2, "done")])
        rework_prompt = self.agent.calls[2]["prompt"]
        self.assertIn("x.py:1 returns the wrong value.", rework_prompt)
        self.assertIn("Reviewer findings to fix", rework_prompt)
        self.assertNotIn("VERDICT", rework_prompt.split("Goal:")[1])  # the machine line is not passed on
        final_branch = task["report"]["branch"]
        self.assertNotEqual(final_branch, f"pool/{tid}")  # the approved code is the rework's branch
        log = subprocess.run(["git", "log", "--oneline", final_branch], cwd=self.pool.repos / next(self.pool.repos.iterdir()).name,
                             capture_output=True, text=True).stdout
        self.assertEqual(len(log.strip().splitlines()), 3)  # init + round 1 + round 2 on one line of history
        self.assertEqual(task["report"]["changed_files"], ["src/x.py"])  # cumulative against main

    def test_gives_up_after_max_rounds_and_hands_over_to_the_lead(self):
        self.agent.script = [writes("src/x.py", "v1\n"), changes("bad 1"), writes("src/x.py", "v2\n"), changes("bad 2")]
        tid = self.submit(max_rounds=2)
        self.run_pool()
        task = self.view(tid)
        self.assertEqual((task["status"], task["report"]["reason"]), ("needs_lead", "max_rounds"))
        self.assertIn("bad 2", task["report"]["review_summary"])
        self.assertEqual(len(self.agent.calls), 4)  # no third round

    def test_approval_with_failing_tests_is_not_trusted(self):
        # The reviewer says APPROVE, but the pool's own test run fails: that counts as CHANGES.
        self.agent.script = [writes("src/x.py", "bad\n"), approve("Fine by me."), writes("src/x.py", "good\n"), approve()]
        tid = self.submit(test_cmd="grep -q good src/x.py")
        self.run_pool()
        task = self.view(tid)
        self.assertEqual((task["status"], task["report"]["rounds"]), ("done", 2))
        self.assertIn("test command also failed", self.agent.calls[2]["prompt"])

    def test_missing_verdict_goes_to_the_lead(self):
        self.agent.script = [writes("src/x.py", "v1\n"), says("I looked at it. Seems fine?")]
        tid = self.submit()
        self.run_pool()
        task = self.view(tid)
        self.assertEqual((task["status"], task["report"]["reason"]), ("needs_lead", "review_no_verdict"))

    def test_a_reviewer_that_edits_files_hands_over_to_the_lead(self):
        def meddling(wd):
            (wd / "src" / "x.py").write_text("tampered\n")
            return approve()(wd)
        self.agent.script = [writes("src/x.py", "v1\n"), meddling]
        tid = self.submit()
        self.run_pool()
        task = self.view(tid)
        self.assertEqual((task["status"], task["report"]["reason"]), ("needs_lead", "review_failed"))

    def test_a_developer_failure_ends_the_pipeline_without_a_review(self):
        self.agent.script = [writes("secrets.env", "x\n")]
        tid = self.submit()
        self.run_pool()
        task = self.view(tid)
        self.assertEqual((task["status"], task["report"]["reason"]), ("failed", "violation"))
        self.assertEqual(self.steps(tid), [("develop", 1, "failed")])

    def test_a_developer_that_changes_nothing_goes_to_the_lead(self):
        self.agent.script = [says("Nothing to do.")]
        tid = self.submit()
        self.run_pool()
        self.assertEqual((self.view(tid)["status"], self.view(tid)["report"]["reason"]), ("needs_lead", "no_changes"))

    # ---- queueing between the stages ----------------------------------------------------------------------------

    def test_two_pipelines_share_one_reviewer_and_the_second_waits_for_review(self):
        self.agent.script = [writes("src/a.py", "a\n"), writes("src/b.py", "b\n")]
        a = self.submit(goal="task A", allowed_files=["src/a.py"])
        b = self.submit(goal="task B", allowed_files=["src/b.py"])
        self.run_one_wave()  # both developers work at the same time, one on each account
        self.assertEqual({self.pool.get_task(a)["account_id"], self.pool.get_task(b)["account_id"]}, {"acc1", "acc2"})
        # Both developers are done and both reviews are queued. Start one review only: the reviewer is then busy.
        first = self.pool._claim_next()
        self.assertEqual(first[1], "rev1")
        self.assertIsNone(self.pool._claim_next())
        stages = sorted((self.view(a)["pipeline"]["stage"], self.view(b)["pipeline"]["stage"]))
        self.assertEqual(stages, ["awaiting_review", "review"])
        waiting = [t for t in (a, b) if self.view(t)["pipeline"]["stage"] == "awaiting_review"][0]
        self.assertTrue(self.view(waiting)["pipeline"]["waiting"])
        self.assertEqual(self.view(waiting)["status"], "running")  # still in progress for the lead

    def test_without_a_reviewer_the_work_waits_and_the_lead_is_warned(self):
        self.make_pool([{"id": "acc1", "role": "developer"}])
        info = self.pool.submit_with_info("developer", "add x", self.url, ["src/*.py"], review=True)
        self.assertTrue(any("no reviewer" in w for w in info["warnings"]))
        self.agent.script = [writes("src/x.py", "v1\n")]
        self.run_pool()
        view = self.view(info["task_id"])
        self.assertEqual((view["status"], view["pipeline"]["stage"]), ("running", "awaiting_review"))
        self.assertIn("no_reviewer", [e["kind"] for e in self.pool.list_events()])

    def test_the_waiting_review_starts_when_a_reviewer_appears(self):
        self.make_pool([{"id": "acc1", "role": "developer"}])
        tid = self.submit()
        self.agent.script = [writes("src/x.py", "v1\n"), approve()]
        self.run_pool()
        self.assertEqual(self.view(tid)["pipeline"]["stage"], "awaiting_review")
        self.pool.add_account("rev1", "reviewer")
        self.run_pool()
        self.assertEqual(self.view(tid)["status"], "done")

    # ---- what the lead and the models see -----------------------------------------------------------------------

    def test_models_follow_the_stages(self):
        self.agent.script = [writes("src/x.py", "v1\n"), changes(), writes("src/x.py", "v2\n"), approve()]
        tid = self.submit(model=None)
        self.pool.models_lister = lambda env: (True, "gemini-a\tA\ngemini-b\tB")
        self.run_pool()
        # Dev and reviewer used the agents' defaults here; with explicit models they must be carried over:
        self.agent.calls.clear()
        self.agent.script = [writes("src/y.py", "y1\n"), changes(), writes("src/y.py", "y2\n"), approve()]
        t2 = self.submit(goal="y", allowed_files=["src/y.py"], model="gemini-a", review_model="gemini-b")
        self.run_pool()
        self.assertEqual([c["model"] for c in self.agent.calls], ["gemini-a", "gemini-b", "gemini-a", "gemini-b"])
        self.assertEqual(self.view(t2)["report"]["reviewer_model"], "gemini-b")

    def test_review_prompt_gives_the_reviewer_what_it_needs(self):
        self.agent.script = [writes("src/x.py", "v1\n"), approve()]
        self.submit(test_cmd="test -f src/x.py")
        self.run_pool()
        prompt = self.agent.calls[1]["prompt"]
        for needle in ("git diff origin/main...HEAD", "VERDICT: APPROVE", "Do NOT modify", "Tests that must pass: test -f src/x.py"):
            self.assertIn(needle, prompt)
        self.assertNotIn("Allowed file patterns", prompt)

    def test_an_active_pipeline_is_a_running_task_for_the_lead(self):
        tid = self.submit()
        view = self.view(tid)
        self.assertEqual((view["status"], view["pipeline"]["stage"]), ("running", "develop"))

    def test_an_old_task_cannot_steer_a_pipeline_that_moved_on(self):
        self.agent.script = [writes("src/x.py", "v1\n"), approve()]
        tid = self.submit()
        self.run_pool()
        self.assertEqual(self.view(tid)["status"], "done")
        self.pool._finish(tid, "failed", {"error": "internal: late failure", "account": "acc1"})  # stale event
        self.assertEqual(self.view(tid)["status"], "done")

    def test_working_copies_of_a_running_pipeline_survive_cleanup(self):
        self.agent.script = [writes("src/x.py", "v1\n")]
        tid = self.submit()
        self.run_one_wave()  # the developer finishes; the review is queued but not started
        report = self.pool.get_task(tid)["report"]
        report["pushed"] = True
        self.pool._exec("UPDATE tasks SET report = ? WHERE id = ?", (json.dumps(report), tid))
        self.pool.housekeep(aggressive=True)
        self.assertTrue((self.pool.worktrees / tid).exists())  # the review will start from this commit

    def test_two_developers_starting_together_in_one_repo_never_collide(self):
        # git cannot take two writers on one clone; the pool serializes them. Repeat to catch a timing problem.
        for i in range(6):
            self.agent.script = [writes(f"src/a{i}.py", "a\n"), writes(f"src/b{i}.py", "b\n")]
            a = self.pool.submit("developer", f"a{i}", self.url, [f"src/a{i}.py"])
            b = self.pool.submit("developer", f"b{i}", self.url, [f"src/b{i}.py"])
            self.run_one_wave()
            self.assertEqual((self.pool.get_task(a)["status"], self.pool.get_task(b)["status"]), ("done", "done"), i)

    # ---- submit-time checks, warnings and model guidance -----------------------------------------------------------

    def test_submit_validates_the_pipeline_options(self):
        with self.assertRaises(ValueError):
            self.submit(role="reviewer")
        for bad in (0, 6, "3", True):
            with self.assertRaises(ValueError, msg=bad):
                self.submit(max_rounds=bad)
        self.pool.models_lister = lambda env: (True, "gemini-a\tA")
        with self.assertRaises(ValueError):
            self.submit(review_model="gemini-zzz")

    def test_overlapping_work_in_the_same_repo_is_flagged(self):
        first = self.pool.submit_with_info("developer", "a", self.url, ["src/**"], review=True)
        self.assertEqual(first["warnings"], [])
        second = self.pool.submit_with_info("developer", "b", self.url, ["src/a.py"], review=True)
        self.assertTrue(any(first["task_id"] in w for w in second["warnings"]))
        third = self.pool.submit_with_info("developer", "c", self.url, ["docs/*.md"], review=True)
        self.assertFalse(any("may conflict" in w for w in third["warnings"]))

    def test_patterns_overlap(self):
        self.assertTrue(patterns_overlap(["bot/**"], ["bot/db.py"]))
        self.assertTrue(patterns_overlap(["*.py"], ["src/a.py"]))
        self.assertFalse(patterns_overlap(["docs/*.md"], ["bot/x.py"]))
        self.assertFalse(patterns_overlap(["bot/a/*"], ["bot/b/*"]))

    def test_model_hints_read_the_id(self):
        h = model_hint("gemini-3.8-flash-high")
        self.assertEqual((h["family"], h["generation"], h["effort"]), ("flash", 3.8, "high"))
        self.assertEqual(model_hint("gemini-3.1-pro-low")["family"], "pro")
        self.assertIsNone(model_hint("mystery")["family"])

    def test_model_guide_adds_measured_history(self):
        self.pool.models_lister = lambda env: (True, "gemini-3.8-flash-low\tFlash low\ngemini-3.1-pro-high\tPro high")
        self.agent.script = [writes("src/x.py", "v1\n"), approve()]
        self.submit(model="gemini-3.8-flash-low", review_model="gemini-3.1-pro-high")
        self.run_pool()
        guide = {g["id"]: g for g in self.pool.model_guide()}
        self.assertEqual(guide["gemini-3.8-flash-low"]["family"], "flash")
        self.assertEqual(guide["gemini-3.8-flash-low"]["stats"]["tasks"], 1)
        self.assertEqual(guide["gemini-3.1-pro-high"]["stats"]["done_share"], 1.0)
        self.assertIsNotNone(guide["gemini-3.1-pro-high"]["hint"])


if __name__ == "__main__":
    unittest.main()
