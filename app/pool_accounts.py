"""Pool mixin: accounts, egress ports and models."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from typing import Any

import agent_mcp
from pool_rules import ROLES

log = logging.getLogger("pool")


class AccountsMixin:
    def list_accounts(self) -> list[dict[str, Any]]:
        conn = self._db()
        rows = conn.execute(
            "SELECT a.*, q.checked_at AS quota_checked_at, q.ok AS quota_ok, q.output AS quota_text"
            " FROM accounts a LEFT JOIN quota q ON q.account_id = a.id ORDER BY a.id"
        ).fetchall()
        conn.close()
        accounts = [dict(r) for r in rows]
        for acc in accounts:
            acc["model"] = self._model_for(acc["id"])
            acc["logged_in"] = self.login_present(acc["id"])
            acc["egress"] = self._egress_label(acc["id"])
        return accounts

    # ---- account management ---------------------------------------------

    def _egress_label(self, account_id: str) -> dict[str, Any]:
        proxy = str(self.accounts_cfg.get(account_id, {}).get("proxy") or "")
        m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/?", proxy)
        if m:
            return {"mode": "own_ipv6", "port": int(m.group(1))}
        if proxy:
            return {"mode": "external_proxy", "host": proxy.split("@")[-1].split("//")[-1]}
        return {"mode": "server_ip"}

    def login_present(self, account_id: str) -> bool:
        """True once agy has stored a Google login in the account's profile."""
        return (self.profiles / account_id / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").is_file()

    def _save_accounts(self) -> None:
        path = self.data / "accounts.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self.accounts_cfg.values()), indent=2, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(path)

    def add_account(self, account_id: str, role: str = "developer") -> dict[str, Any]:
        """Register a new agent live. It takes no tasks until its Google login is done (see scripts/add_account.sh)."""
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", account_id or ""):
            raise ValueError("id must be 1-16 characters: lowercase letters, digits, - or _, starting with a letter")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        with self._cfg_lock:
            if account_id in self.accounts_cfg:
                raise ValueError(f"account {account_id} already exists")
            self.accounts_cfg[account_id] = {"id": account_id, "role": role}
            if self.egress:
                self.accounts_cfg[account_id]["proxy"] = f"http://127.0.0.1:{self._next_egress_port()}"
            (self.profiles / account_id).mkdir(parents=True, exist_ok=True)
            self._save_accounts()
            self._sync_accounts()
        try:
            agent_mcp.sync_profile(self.profiles / account_id, agent_mcp.load(self.data))
        except (OSError, ValueError) as exc:
            log.error("MCP servers for %s not written: %s", account_id, exc)
        return next(a for a in self.list_accounts() if a["id"] == account_id)

    def _next_egress_port(self) -> int:
        """A port above every one ever handed out, so a removed agent's address is never given to someone else."""
        state = self.data / "egress_ports.json"
        try:
            last = int(json.loads(state.read_text()).get("last", 3100))
        except (OSError, ValueError):
            last = 3100
        for cfg in self.accounts_cfg.values():
            m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/?", str(cfg.get("proxy") or ""))
            if m:
                last = max(last, int(m.group(1)))
        port = last + 1
        if port > 3999:
            raise ValueError("no free egress ports left")
        state.write_text(json.dumps({"last": port}))
        return port

    def set_account_enabled(self, account_id: str, enabled: bool) -> dict[str, Any]:
        with self._cfg_lock:
            if account_id not in self.accounts_cfg:
                raise ValueError("unknown account")
            self.accounts_cfg[account_id]["enabled"] = bool(enabled)
            self._save_accounts()
            self._sync_accounts()
        return next(a for a in self.list_accounts() if a["id"] == account_id)

    def remove_account(self, account_id: str) -> None:
        """Forget an agent and delete its profile (its Google login). Refused while it is working."""
        with self._cfg_lock:
            if account_id not in self.accounts_cfg:
                raise ValueError("unknown account")
            conn = self._db()
            try:
                row = conn.execute("SELECT busy_task FROM accounts WHERE id = ?", (account_id,)).fetchone()
                if row is not None and row["busy_task"]:
                    raise ValueError("the account is running a task; wait for it to finish")
                conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
                conn.execute("DELETE FROM quota WHERE account_id = ?", (account_id,))
            finally:
                conn.close()
            del self.accounts_cfg[account_id]
            self._save_accounts()
            shutil.rmtree(self.profiles / account_id, ignore_errors=True)

    def _model_for(self, account_id: str) -> str | None:
        """Model pinned for this account (accounts.json "model"), else the global AGY_MODEL, else None = agy default."""
        return self.accounts_cfg.get(account_id, {}).get("model") or os.environ.get("AGY_MODEL") or None

    def list_models(self, account_id: str) -> dict[str, Any]:
        """Models agy offers to this account. Blocking: call from a worker thread."""
        if account_id not in self.accounts_cfg:
            raise ValueError("unknown account")
        if self.models_lister is None:
            raise ValueError("model list is not available")
        ok, text = self.models_lister(self._agent_env(account_id))
        if ok:
            text = "\n".join(
                line for line in text.splitlines() if line.split("\t")[0].strip().startswith("gemini")
            )
        return {"account_id": account_id, "ok": ok, "text": text}

    def model_catalog(self) -> dict[str, str]:
        """Model id -> display name, as reported by `agy models` (cached 10 minutes). Empty if unavailable."""
        cached = self._models_cache
        if cached is not None and time.time() - cached[0] < 600:
            return cached[1]
        if self.models_lister is None or not self.accounts_cfg:
            return {}
        ok, text = self.models_lister(self._agent_env(next(iter(self.accounts_cfg))))
        catalog = {}
        if ok:
            for line in text.splitlines():
                parts = line.split("\t")
                # Only Gemini models are offered; agy also lists Claude and GPT models, which the pool ignores.
                if len(parts) == 2 and parts[0].strip().startswith("gemini"):
                    catalog[parts[0].strip()] = parts[1].strip()
        if catalog:
            self._models_cache = (time.time(), catalog)
        return catalog

    def _check_model(self, model: str) -> None:
        catalog = self.model_catalog()
        # If the list cannot be fetched, let agy decide rather than blocking every task.
        if catalog and model not in catalog:
            raise ValueError(f"unknown model '{model}'. Available: {', '.join(catalog)}")

    def set_model(self, account_id: str, model: str | None) -> dict[str, Any]:
        """Pin a model for one account (None = agy default) and persist it to accounts.json."""
        if account_id not in self.accounts_cfg:
            raise ValueError("unknown account")
        if model:
            if model not in self.model_catalog():
                raise ValueError("unknown model")
            self.accounts_cfg[account_id]["model"] = model
        else:
            self.accounts_cfg[account_id].pop("model", None)
        with self._cfg_lock:
            self._save_accounts()
        return {"account_id": account_id, "model": self._model_for(account_id)}
