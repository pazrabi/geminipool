"""Entry point: HTTP/MCP server in a thread, dispatcher loop in asyncio."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import executor
import housekeeping
import server
from oauth import OAuth
from pool import Pool


def load_accounts(data: Path) -> list[dict]:
    cfg = data / "accounts.json"
    if cfg.exists():
        return json.loads(cfg.read_text())
    return [{"id": "acc1", "role": "developer"}]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = os.environ.get("POOL_MCP_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("POOL_MCP_TOKEN must be set (at least 32 characters)")
    data = Path(os.environ.get("POOL_DATA", "/data"))
    # Before anything touches the database: if the volume filled up, SQLite cannot even open. Nothing is
    # running at process start, so the rebuildable bulk in the working copies can go at once.
    housekeeping.strip_all(data / "worktrees")
    pool = Pool(
        data_dir=data,
        runner=executor.agy_runner,
        accounts=load_accounts(data),
        push_token=os.environ.get("GIT_PUSH_TOKEN") or None,
        quota_checker=executor.agy_quota,
        models_lister=executor.agy_models,
    )
    server.configure(pool, token, OAuth(pool.db_path, token))
    # Railway needs 0.0.0.0; on a server behind a reverse proxy set POOL_BIND=127.0.0.1.
    bind = os.environ.get("POOL_BIND", "0.0.0.0")
    httpd = ThreadingHTTPServer((bind, int(os.environ.get("PORT", "8080"))), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    logging.getLogger("main").info("gemini-pool started, accounts=%s", [a["id"] for a in pool.list_accounts()])
    await pool.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
