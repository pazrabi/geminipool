"""MCP servers every agent of the pool can use (for example the phone hub), kept in one list.

The list lives in <data>/agent_mcp.json:
  {"servers": [{"name": "droidhub", "url": "http://127.0.0.1:8090/mcp/",
                "headers": {"Authorization": "Bearer ${DROIDHUB_POOL_TOKEN}"}}]}
`${NAME}` is replaced from the pool's environment (/etc/gemini-pool.env), so a secret never sits in the list itself.
A server whose variable is missing is skipped. The list is written into each account's agy profile
(<profile>/.gemini/config/mcp_config.json) at start and when an agent is added. Only the servers the pool put there
are ever changed or removed; servers someone added to a profile by hand are left alone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("agent_mcp")

_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
MANAGED_FILE = ".pool-mcp-managed.json"


def _expand(text: str, env: dict[str, str]) -> str:
    missing = [m for m in _VAR_RE.findall(text) if not env.get(m)]
    if missing:
        raise ValueError("environment variable not set: " + ", ".join(missing))
    return _VAR_RE.sub(lambda m: env[m.group(1)], text)


def load(data: Path, env: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """The servers to give to the agents, as agy's config entries, keyed by name. Bad entries are skipped and logged
    (never with their values)."""
    env = os.environ if env is None else env
    path = data / "agent_mcp.json"
    if not path.is_file():
        return {}
    try:
        items = json.loads(path.read_text()).get("servers", [])
    except (OSError, ValueError, AttributeError):
        log.error("agent_mcp.json is not valid JSON; no MCP servers given to agents")
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        try:
            name, url = str(item["name"]), str(item["url"])
            if not _NAME_RE.fullmatch(name):
                raise ValueError("name must be letters, digits, - or _")
            if not url.startswith(("http://", "https://")):
                raise ValueError("url must start with http:// or https://")
            headers = {str(k): _expand(str(v), env) for k, v in (item.get("headers") or {}).items()}
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            log.error("agent MCP server %r skipped: %s", item.get("name") if isinstance(item, dict) else "?", exc)
            continue
        entry: dict[str, Any] = {"disabled": False, "serverUrl": url}
        if headers:
            entry["headers"] = headers
        out[name] = entry
    return out


def _read_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
        return json.loads(text) if text.strip() else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ValueError(f"{path} is not valid JSON, left untouched")


def sync_profile(home: Path, servers: dict[str, dict[str, Any]]) -> list[str]:
    """Make `home`'s agy config contain `servers`: add or update ours, remove ones we added earlier that are no
    longer listed. Returns the names the pool now manages in this profile."""
    cfg_path = home / ".gemini" / "config" / "mcp_config.json"
    state_path = home / MANAGED_FILE
    try:
        previous = set(json.loads(state_path.read_text()))
    except (OSError, ValueError, TypeError):
        previous = set()
    cfg = _read_json(cfg_path)
    current = cfg.get("mcpServers")
    current = dict(current) if isinstance(current, dict) else {}
    for name in previous - set(servers):
        current.pop(name, None)
    current.update(servers)
    cfg["mcpServers"] = current
    if cfg == _read_json(cfg_path) and previous == set(servers):
        return sorted(servers)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    for path, payload in ((cfg_path, cfg), (state_path, sorted(servers))):
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.chmod(tmp, 0o600)  # the file can hold a token
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return sorted(servers)


def sync_all(data: Path, profiles: Path, env: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Apply the list to every existing profile. A profile that fails is logged and skipped."""
    servers = load(data, env)
    done: dict[str, list[str]] = {}
    if not profiles.is_dir():
        return done
    for home in sorted(p for p in profiles.iterdir() if p.is_dir()):
        try:
            done[home.name] = sync_profile(home, servers)
        except (OSError, ValueError) as exc:
            log.error("MCP servers for %s not written: %s", home.name, exc)
    return done
