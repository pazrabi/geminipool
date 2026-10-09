#!/usr/bin/env python3
"""Per-agent egress: one local proxy per agent, each sending its traffic out from its own IPv6 address.

An agent whose `proxy` in accounts.json is `http://127.0.0.1:<port>` (port 3001..3999) gets a listener on that port. The
listener only accepts HTTPS tunnelling (CONNECT) and opens the outgoing connection from an address of the server's
IPv6 /48 that belongs to this port alone: `<prefix>:<port-3000>::10`, for example port 3101 -> `<prefix>:101::10`. The
addresses are added to the network interface on demand. Destinations without an IPv6 address fall back to the server's
IPv4 (counted and logged, so it is visible). The service watches accounts.json, so adding an agent needs no restart.

Needs CAP_NET_ADMIN (to add the addresses); nothing else. Standard library only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import socket
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("egress")

BASE_PORT = 3000
PORT_RANGE = 999
ALLOWED_TARGET_PORTS = {80, 443}
PROXY_RE = re.compile(r"^http://127\.0\.0\.1:(\d{4,5})/?$")
HEAD_TIMEOUT = 10
CONNECT_TIMEOUT = 15
IDLE_TIMEOUT = 900
RELOAD_S = 15


def detect_network() -> tuple[str, str]:
    """(interface, /48 prefix) of the server's global IPv6 range, e.g. ('net0', '2a0b:4140:37a')."""
    out = subprocess.run(["ip", "-6", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        ip, plen = parts[3].split("/")
        if int(plen) <= 48:
            return parts[1], ":".join(ip.split(":")[:3])
    raise RuntimeError("this server has no global IPv6 range of /48 or larger, so agents cannot be given their own addresses")


class Egress:
    def __init__(self, accounts_path: Path, prefix: str, device: str | None, base_port: int = BASE_PORT,
                 manage_addresses: bool = True, bind_host: str = "127.0.0.1", source_override: dict[int, str] | None = None):
        self.accounts_path = Path(accounts_path)
        self.prefix, self.device = prefix, device
        self.base_port = base_port
        self.manage_addresses = manage_addresses
        self.bind_host = bind_host
        self.source_override = source_override or {}  # tests: use a loopback address instead of a real one
        self.servers: dict[int, asyncio.AbstractServer] = {}
        self.owners: dict[int, str] = {}
        self.stats: dict[int, dict[str, int]] = {}

    # ---- addresses and configuration ------------------------------------------------------------------------------

    def source_for(self, port: int) -> str:
        return self.source_override.get(port) or f"{self.prefix}:{port - self.base_port}::10"

    def wanted(self) -> dict[int, str]:
        """port -> agent id, for every agent configured to use a local egress port."""
        try:
            accounts = json.loads(self.accounts_path.read_text())
        except (OSError, ValueError):
            return dict(self.owners)  # an unreadable file must not tear down working listeners
        out = {}
        for acc in accounts:
            m = PROXY_RE.match(str(acc.get("proxy") or ""))
            if m and self.base_port < int(m.group(1)) <= self.base_port + PORT_RANGE:
                out[int(m.group(1))] = str(acc.get("id"))
        return out

    def _ensure_address(self, addr: str) -> None:
        if not self.manage_addresses:
            return
        proc = subprocess.run(["ip", "-6", "addr", "replace", f"{addr}/128", "dev", self.device, "nodad"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            log.error("could not add %s to %s: %s", addr, self.device, proc.stderr.strip())

    async def sync(self) -> None:
        wanted = self.wanted()
        for port in [p for p in self.servers if p not in wanted]:
            self.servers.pop(port).close()
            self.owners.pop(port, None)
            log.info("stopped listener on %d", port)
        for port, owner in wanted.items():
            self.owners[port] = owner
            if port in self.servers:
                continue
            self._ensure_address(self.source_for(port))
            self.stats.setdefault(port, {"v6": 0, "v4_fallback": 0, "refused": 0})
            self.servers[port] = await asyncio.start_server(
                lambda r, w, p=port: self.handle(p, r, w), self.bind_host, port)
            log.info("agent %s: listener %s:%d, outgoing address %s", owner, self.bind_host, port, self.source_for(port))

    async def run(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception("sync failed")
            await asyncio.sleep(RELOAD_S)

    # ---- one tunnel -------------------------------------------------------------------------------------------------

    async def handle(self, port: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stats = self.stats[port]
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            m = re.fullmatch(r"CONNECT (\[[0-9a-fA-F:]+\]|[^\s:]+):(\d{1,5}) HTTP/1\.[01]", line)
            if not m:
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                stats["refused"] += 1
                return
            host, target_port = m.group(1).strip("[]"), int(m.group(2))
            if target_port not in ALLOWED_TARGET_PORTS:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                stats["refused"] += 1
                return
            upstream = await self._open(port, host, target_port, stats)
            if upstream is None:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            up_reader, up_writer = upstream
            await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _open(self, port: int, host: str, target_port: int, stats: dict[str, int]):
        source = self.source_for(port)
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, target_port, family=socket.AF_INET6, type=socket.SOCK_STREAM)
        except socket.gaierror:
            infos = []
        for info in infos[:2]:
            try:
                conn = await asyncio.wait_for(
                    asyncio.open_connection(info[4][0], target_port, local_addr=(source, 0), family=socket.AF_INET6),
                    CONNECT_TIMEOUT)
                stats["v6"] += 1
                log.info("%s via %s -> %s:%d", self.owners.get(port), source, host, target_port)
                return conn
            except (OSError, asyncio.TimeoutError):
                continue
        try:  # no IPv6 route to this destination: use the server's own IPv4, and say so
            conn = await asyncio.wait_for(asyncio.open_connection(host, target_port), CONNECT_TIMEOUT)
            stats["v4_fallback"] += 1
            log.warning("%s: %s:%d has no IPv6, went out over the server's IPv4", self.owners.get(port), host, target_port)
            return conn
        except (OSError, asyncio.TimeoutError):
            return None

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await asyncio.wait_for(src.read(65536), IDLE_TIMEOUT)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--accounts", default="/var/lib/gemini-pool/accounts.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    device, prefix = detect_network()
    log.info("IPv6 range %s::/48 on %s", prefix, device)
    asyncio.run(Egress(Path(args.accounts), prefix, device).run())


if __name__ == "__main__":
    sys.exit(main())
