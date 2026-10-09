"""The per-agent egress proxy, tested on loopback (no real addresses are added)."""

import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import egress_proxy  # noqa: E402
from egress_proxy import Egress  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def echo_server(host: str):
    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(echo, host, 0)
    return server, server.sockets[0].getsockname()[1]


async def tunnel(proxy_port: int, target: str, target_port: int, payload: bytes = b"hello"):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    shown = f"[{target}]" if ":" in target else target  # an IPv6 literal goes in brackets
    writer.write(f"CONNECT {shown}:{target_port} HTTP/1.1\r\nHost: {shown}\r\n\r\n".encode())
    await writer.drain()
    status = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n")[0].decode()
    body = b""
    if " 200 " in status:
        writer.write(payload)
        await writer.drain()
        body = await asyncio.wait_for(reader.read(100), 5)
    writer.close()
    return status, body


class EgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.accounts = self.tmp / "accounts.json"
        self.base = free_port() - 7  # so that base + 7 is a free port we can use as an agent port
        self.port = self.base + 7

    def write_accounts(self, items):
        self.accounts.write_text(json.dumps(items))

    def egress(self, **kw):
        return Egress(self.accounts, prefix="2a0b:4140:37a", device="net0", base_port=self.base,
                      manage_addresses=False, source_override={self.port: "::1"}, **kw)

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_each_port_has_its_own_ipv6_address(self):
        e = Egress(self.accounts, "2a0b:4140:37a", "net0")
        self.assertEqual(e.source_for(3101), "2a0b:4140:37a:101::10")
        self.assertEqual(e.source_for(3102), "2a0b:4140:37a:102::10")
        self.assertNotEqual(e.source_for(3101), e.source_for(3102))

    def test_only_local_proxy_urls_in_range_are_served(self):
        self.write_accounts([
            {"id": "a", "role": "developer", "proxy": f"http://127.0.0.1:{self.port}"},
            {"id": "b", "role": "developer", "proxy": "http://user:pw@proxy.example.com:8080"},
            {"id": "c", "role": "developer", "proxy": f"http://127.0.0.1:{self.base + 5000}"},
            {"id": "d", "role": "developer", "proxy": "http://127.0.0.1:3000"},
            {"id": "e", "role": "developer"},
        ])
        self.assertEqual(self.egress().wanted(), {self.port: "a"})

    def test_tunnel_goes_out_from_the_agents_address(self):
        async def go():
            target, tport = await echo_server("::1")
            self.write_accounts([{"id": "a", "role": "developer", "proxy": f"http://127.0.0.1:{self.port}"}])
            e = self.egress()
            await e.sync()
            ALLOWED = egress_proxy.ALLOWED_TARGET_PORTS
            ALLOWED.add(tport)
            try:
                status, body = await tunnel(self.port, "::1", tport)
            finally:
                ALLOWED.discard(tport)
                target.close()
                for s in e.servers.values():
                    s.close()
            return status, body, e.stats[self.port]
        status, body, stats = self.run_async(go())
        self.assertIn("200", status)
        self.assertEqual(body, b"echo:hello")
        self.assertEqual((stats["v6"], stats["v4_fallback"]), (1, 0))

    def test_a_destination_without_ipv6_falls_back_to_ipv4_and_is_counted(self):
        async def go():
            target, tport = await echo_server("127.0.0.1")
            self.write_accounts([{"id": "a", "role": "developer", "proxy": f"http://127.0.0.1:{self.port}"}])
            e = self.egress()
            await e.sync()
            egress_proxy.ALLOWED_TARGET_PORTS.add(tport)
            try:
                status, body = await tunnel(self.port, "127.0.0.1", tport)
            finally:
                egress_proxy.ALLOWED_TARGET_PORTS.discard(tport)
                target.close()
                for s in e.servers.values():
                    s.close()
            return status, body, e.stats[self.port]
        status, body, stats = self.run_async(go())
        self.assertEqual(body, b"echo:hello")
        self.assertEqual((stats["v6"], stats["v4_fallback"]), (0, 1))

    def test_other_ports_and_plain_http_are_refused(self):
        async def go():
            self.write_accounts([{"id": "a", "role": "developer", "proxy": f"http://127.0.0.1:{self.port}"}])
            e = self.egress()
            await e.sync()
            try:
                status_port, _ = await tunnel(self.port, "example.com", 25)
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.write(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
                await writer.drain()
                plain = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n")[0].decode()
                writer.close()
            finally:
                for s in e.servers.values():
                    s.close()
            return status_port, plain, e.stats[self.port]["refused"]
        status_port, plain, refused = self.run_async(go())
        self.assertIn("403", status_port)
        self.assertIn("405", plain)
        self.assertEqual(refused, 2)

    def test_listeners_follow_the_accounts_file_without_a_restart(self):
        async def go():
            e = self.egress()
            self.write_accounts([])
            await e.sync()
            none = set(e.servers)
            self.write_accounts([{"id": "a", "role": "developer", "proxy": f"http://127.0.0.1:{self.port}"}])
            await e.sync()
            added = set(e.servers)
            self.accounts.write_text("{broken json")  # an unreadable file keeps what works
            await e.sync()
            kept = set(e.servers)
            self.write_accounts([{"id": "a", "role": "developer"}])
            await e.sync()
            removed = set(e.servers)
            for s in e.servers.values():
                s.close()
            return none, added, kept, removed
        none, added, kept, removed = self.run_async(go())
        self.assertEqual((none, added, kept, removed), (set(), {self.port}, {self.port}, set()))


if __name__ == "__main__":
    unittest.main()
