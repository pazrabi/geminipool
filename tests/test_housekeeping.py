"""The volume must not fill up: strip rebuildable bulk, retire old working copies, stop when space is low."""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import housekeeping  # noqa: E402
from pool import Pool  # noqa: E402

DAY = 86400


async def _never(**kw):
    raise RuntimeError("not used")


class HousekeepingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pool = Pool(self.tmp / "data", runner=_never, accounts=[{"id": "acc1", "role": "developer"}])

    def worktree(self, task_id):
        wt = self.pool.worktrees / task_id
        (wt / "src" / "__pycache__").mkdir(parents=True)
        (wt / "src" / "a.py").write_text("print(1)\n")
        (wt / "src" / "__pycache__" / "a.pyc").write_bytes(b"x" * 100)
        (wt / ".venv" / "lib").mkdir(parents=True)
        (wt / ".venv" / "lib" / "big.bin").write_bytes(b"x" * 10_000)
        (wt / "node_modules" / "p").mkdir(parents=True)
        (wt / "node_modules" / "p" / "i.js").write_text("1")
        (wt / ".git").mkdir()
        (wt / ".git" / "keep").write_text("git data")
        return wt

    def task(self, task_id, status, age, report=None):
        conn = sqlite3.connect(self.pool.db_path)
        now = time.time()
        conn.execute(
            "INSERT INTO tasks (id, role, goal, repo, base_branch, allowed_files, status, attempts, account_id, branch,"
            " report, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "developer", "g", "https://github.com/o/r.git", "main", '["a"]', status, 1, "acc1",
             "pool/" + task_id, json.dumps(report) if report else None, now - age - 60, now - age))
        conn.commit()
        conn.close()

    def test_strip_removes_rebuildable_bulk_and_keeps_work_and_git(self):
        wt = self.worktree("aaaaaaaaaaa1")
        self.assertEqual(housekeeping.strip_heavy(wt), 3)
        self.assertFalse((wt / ".venv").exists())
        self.assertFalse((wt / "node_modules").exists())
        self.assertFalse((wt / "src" / "__pycache__").exists())
        self.assertEqual((wt / "src" / "a.py").read_text(), "print(1)\n")
        self.assertEqual((wt / ".git" / "keep").read_text(), "git data")

    def test_strip_all_works_without_a_database(self):
        a, b = self.worktree("aaaaaaaaaaa1"), self.worktree("aaaaaaaaaaa2")
        self.assertEqual(housekeeping.strip_all(self.pool.worktrees), 6)
        self.assertFalse((a / ".venv").exists() or (b / ".venv").exists())
        self.assertEqual(housekeeping.strip_all(self.tmp / "missing"), 0)

    def test_a_running_task_keeps_its_environment(self):
        wt = self.worktree("aaaaaaaaaaa3")
        self.task("aaaaaaaaaaa3", "running", 10)
        self.pool.housekeep()
        self.assertTrue((wt / ".venv" / "lib" / "big.bin").exists())

    def test_recently_finished_pushed_task_is_stripped_but_kept(self):
        wt = self.worktree("aaaaaaaaaaa4")
        self.task("aaaaaaaaaaa4", "done", 3600, {"pushed": True})
        self.pool.housekeep()
        self.assertTrue((wt / "src" / "a.py").exists())
        self.assertFalse((wt / ".venv").exists())

    def test_old_pushed_task_working_copy_is_retired(self):
        wt = self.worktree("aaaaaaaaaaa5")
        self.task("aaaaaaaaaaa5", "done", 2 * DAY, {"pushed": True})
        self.assertEqual(self.pool.housekeep()["retired"], 1)
        self.assertFalse(wt.exists())

    def test_unpushed_work_is_kept_much_longer(self):
        wt = self.worktree("aaaaaaaaaaa6")
        self.task("aaaaaaaaaaa6", "done", 5 * DAY, {"pushed": False})
        self.pool.housekeep()
        self.assertTrue((wt / "src" / "a.py").exists())  # the only copy of the work
        self.task("aaaaaaaaaaa7", "done", 15 * DAY, {"pushed": False})
        old = self.worktree("aaaaaaaaaaa7")
        self.pool.housekeep()
        self.assertFalse(old.exists())

    def test_internal_failures_stay_recoverable(self):
        wt = self.worktree("aaaaaaaaaaa8")
        self.task("aaaaaaaaaaa8", "failed", 5 * DAY, {"error": "internal: boom", "pushed": False})
        self.pool.housekeep()
        self.assertTrue((wt / "src" / "a.py").exists())

    def test_aggressive_mode_retires_pushed_work_regardless_of_age(self):
        wt = self.worktree("aaaaaaaaaaa9")
        self.task("aaaaaaaaaaa9", "done", 60, {"pushed": True})
        self.assertEqual(self.pool.housekeep(aggressive=True)["retired"], 1)
        self.assertFalse(wt.exists())

    def test_orphan_directories_go_after_a_day(self):
        fresh, stale = self.worktree("bbbbbbbbbbb1"), self.worktree("bbbbbbbbbbb2")
        old = time.time() - 2 * DAY
        import os
        os.utime(stale, (old, old))
        self.pool.housekeep()
        self.assertTrue(fresh.exists())
        self.assertFalse(stale.exists())

    def test_low_disk_stops_new_tasks_and_logs_it_once(self):
        self.task("ccccccccccc1", "queued", 5)
        with mock.patch.object(housekeeping, "free_bytes", return_value=1024):
            self.assertIsNone(self.pool._claim_next())
            self.assertIsNone(self.pool._claim_next())
        self.assertTrue(self.pool.disk_low)
        self.assertEqual([e["kind"] for e in self.pool.list_events()], ["disk_low"])
        self.assertEqual(self.pool.get_task("ccccccccccc1")["status"], "queued")  # it waits, it is not failed

    def test_tasks_resume_once_space_returns(self):
        self.task("ccccccccccc2", "queued", 5)
        with mock.patch.object(housekeeping, "free_bytes", return_value=1024):
            self.assertIsNone(self.pool._claim_next())
        self.assertTrue(self.pool.disk_low)
        self.assertIsNotNone(self.pool._claim_next())  # real free space is plentiful here
        self.assertFalse(self.pool.disk_low)

    def test_cleanup_can_rescue_a_nearly_full_disk(self):
        self.worktree("ddddddddddd1")
        self.task("ddddddddddd1", "done", 60, {"pushed": True})
        self.task("ddddddddddd2", "queued", 5)
        space = iter([1024, 1024, 10 ** 9])  # low at the check, low again before cleaning, plenty after it
        with mock.patch.object(housekeeping, "free_bytes", side_effect=lambda p: next(space)):
            self.assertIsNotNone(self.pool._claim_next())
        self.assertFalse((self.pool.worktrees / "ddddddddddd1").exists())

    def test_overview_reports_disk(self):
        disk = self.pool.maintenance_status()["disk"]
        self.assertEqual(set(disk), {"total", "free", "low"})
        self.assertGreater(disk["total"], 0)


if __name__ == "__main__":
    unittest.main()
