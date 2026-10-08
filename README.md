# gemini-pool

A pool of Gemini agents (Antigravity CLI, `agy`) on Railway. Claude hands it narrow development tasks over MCP;
each task becomes a git branch `pool/<id>` plus a short report. Agents never merge or push to `main`.

- `app/` — the service (Python stdlib only): dispatcher, `agy` executor, MCP server, OAuth server, dashboard
- `tests/` — `python3 -m unittest discover -s tests` (fake agent, real local git, real HTTP; no quota is spent)
- `Dockerfile`, `railway.toml` — deploy with `railway up`

## Railway variables

| Variable | Purpose |
|---|---|
| `POOL_MCP_TOKEN` | secret (≥32 chars): MCP access for Claude Code, the dashboard login, and the approval step of the connector |
| `PUBLIC_URL` | public https address of the service, used in OAuth discovery |
| `GIT_PUSH_TOKEN` | GitHub fine-grained token, Contents: read and write on the repositories the pool may push `pool/*` branches to |
| `AGY_MODEL` | optional default model id for all agents |

Volume `/data` holds the database, repositories, worktrees, agent profiles (the Google logins) and `accounts.json`.

## Agents

`/data/accounts.json` (created from the dashboard when you pick a model) lists agents:
`[{"id": "acc1", "role": "developer", "model": "gemini-3.8-flash-high"}]`. Roles: `developer`, `reviewer`, `debugger`.
Log an agent in once, in the container: `railway ssh --service gemini-pool -- env HOME=/data/profiles/acc1 agy`.

## Connecting Claude

**Claude Code**: `claude mcp add --scope user --transport http gemini-pool <PUBLIC_URL>/mcp --header "Authorization: Bearer $(cat ~/projects/.gemini-pool-mcp-token)"`

**Claude (claude.ai, desktop, mobile)** as a custom connector: Settings → Connectors → Add custom connector →
URL `<PUBLIC_URL>/mcp`, leave the OAuth fields empty. Claude registers itself, opens the pool's approval page, and
you paste `POOL_MCP_TOKEN` once. Access tokens last an hour and refresh automatically.

## Dashboard

`<PUBLIC_URL>/` — agents, task history, errors, per-agent usage limits and model. Log in with `POOL_MCP_TOKEN`.
