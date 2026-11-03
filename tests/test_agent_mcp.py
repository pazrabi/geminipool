"""One list of MCP servers (the phone hub, for example) written into every agent's profile."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "app"))

import agent_mcp  # noqa: E402
from pool import Pool  # noqa: E402
from test_pool import FakeAgent  # noqa: E402

HUB = {"name": "droidhub", "url": "http://127.0.0.1:8090/mcp/",
       "headers": {"Authorization": "Bearer ${HUB_TOKEN}"}}


def cfg(home: Path) -> dict:
    return json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())


class AgentMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.home = self.tmp / "profiles" / "acc1"
        self.home.mkdir(parents=True)

    def write_list(self, servers):
        (self.data / "agent_mcp.json").write_text(json.dumps({"servers": servers}))

    def test_variables_come_from_the_environment_and_the_file_is_private(self):
        self.write_list([HUB])
        servers = agent_mcp.load(self.data, {"HUB_TOKEN": "s3cret"})
        agent_mcp.sync_profile(self.home, servers)
        entry = cfg(self.home)["mcpServers"]["droidhub"]
        self.assertEqual(entry["serverUrl"], "http://127.0.0.1:8090/mcp/")
        self.assertEqual(entry["headers"]["Authorization"], "Bearer s3cret")
        self.assertFalse(entry["disabled"])
        self.assertEqual((self.home / ".gemini" / "config" / "mcp_config.json").stat().st_mode & 0o077, 0)
        self.assertNotIn("s3cret", (self.data / "agent_mcp.json").read_text())

    def test_server_with_a_missing_variable_is_skipped_and_the_value_never_logged(self):
        self.write_list([HUB, {"name": "plain", "url": "https://example.org/mcp"}])
        with self.assertLogs("agent_mcp", "ERROR") as logs:
            servers = agent_mcp.load(self.data, {})
        self.assertEqual(list(servers), ["plain"])
        self.assertIn("HUB_TOKEN", logs.output[0])

    def test_bad_entries_and_bad_json_give_nothing(self):
        self.write_list([{"name": "bad name!", "url": "http://x"}, {"name": "ftp", "url": "ftp://x"}, {"url": "http://x"}])
        with self.assertLogs("agent_mcp", "ERROR"):
            self.assertEqual(agent_mcp.load(self.data, {}), {})
        (self.data / "agent_mcp.json").write_text("{not json")
        with self.assertLogs("agent_mcp", "ERROR"):
            self.assertEqual(agent_mcp.load(self.data, {}), {})
        (self.data / "agent_mcp.json").unlink()
        self.assertEqual(agent_mcp.load(self.data, {}), {})

    def test_hand_added_servers_survive_and_removed_ones_go(self):
        path = self.home / ".gemini" / "config" / "mcp_config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"mcpServers": {"mine": {"serverUrl": "http://a"}}, "other": 1}))
        self.write_list([HUB])
        agent_mcp.sync_profile(self.home, agent_mcp.load(self.data, {"HUB_TOKEN": "t"}))
        self.assertEqual(sorted(cfg(self.home)["mcpServers"]), ["droidhub", "mine"])
        self.assertEqual(cfg(self.home)["other"], 1)
        self.write_list([])
        agent_mcp.sync_profile(self.home, agent_mcp.load(self.data, {}))
        self.assertEqual(list(cfg(self.home)["mcpServers"]), ["mine"])

    def test_empty_config_file_and_repeat_runs(self):
        path = self.home / ".gemini" / "config" / "mcp_config.json"
        path.parent.mkdir(parents=True)
        path.write_text("")  # agy creates an empty file on first start
        self.write_list([HUB])
        servers = agent_mcp.load(self.data, {"HUB_TOKEN": "t"})
        self.assertEqual(agent_mcp.sync_profile(self.home, servers), ["droidhub"])
        before = path.read_text()
        self.assertEqual(agent_mcp.sync_profile(self.home, servers), ["droidhub"])
        self.assertEqual(path.read_text(), before)

    def test_unreadable_config_is_left_untouched(self):
        path = self.home / ".gemini" / "config" / "mcp_config.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        with self.assertRaises(ValueError):
            agent_mcp.sync_profile(self.home, {"a": {"serverUrl": "http://a"}})
        self.assertEqual(path.read_text(), "{broken")

    def test_sync_all_covers_every_profile_and_survives_a_broken_one(self):
        (self.tmp / "profiles" / "acc2" / ".gemini" / "config").mkdir(parents=True)
        (self.tmp / "profiles" / "acc2" / ".gemini" / "config" / "mcp_config.json").write_text("{broken")
        self.write_list([{"name": "plain", "url": "https://example.org/mcp"}])
        with self.assertLogs("agent_mcp", "ERROR"):
            done = agent_mcp.sync_all(self.data, self.tmp / "profiles", {})
        self.assertEqual(done, {"acc1": ["plain"]})

    def test_new_agent_gets_the_servers(self):
        self.write_list([{"name": "plain", "url": "https://example.org/mcp"}])
        pool = Pool(self.data, runner=FakeAgent(), allow_local_repos=True, accounts=[{"id": "acc1", "role": "developer"}])
        pool.add_account("acc9", "reviewer")
        self.assertEqual(list(cfg(pool.profiles / "acc9")["mcpServers"]), ["plain"])


if __name__ == "__main__":
    unittest.main()
