# gemini-pool

A pool of Gemini agents (Antigravity CLI, `agy`) on an Aéza VPS (`deploy/SETUP.md`). Claude hands it narrow development tasks over MCP;
each task becomes a git branch `pool/<id>` plus a short report. Agents never merge or push to `main`.

- `app/` — the service (Python stdlib only): dispatcher, `agy` executor, MCP server, OAuth server, dashboard
- `tests/` — `python3 -m unittest discover -s tests` (fake agent, real local git, real HTTP; no quota is spent)
- `deploy/` — systemd unit, Caddyfile and setup notes for the server; `scripts/safe_deploy.sh` deploys
- `Dockerfile`, `railway.toml` — the previous Railway deployment (kept as a fallback, no longer used)

## Environment variables (`/etc/gemini-pool.env` on the server)

| Variable | Purpose |
|---|---|
| `POOL_MCP_TOKEN` | secret (≥32 chars): MCP access for Claude Code, the dashboard login, and the approval step of the connector |
| `PUBLIC_URL` | public https address of the service, used in OAuth discovery |
| `GIT_PUSH_TOKEN` | GitHub fine-grained token, Contents: read and write on the repositories the pool may push `pool/*` branches to |
| `AGY_MODEL` | optional default model id for all agents |

`POOL_DATA` (`/var/lib/gemini-pool`) holds the database, repositories, worktrees, agent profiles (the Google logins) and `accounts.json`.

## Agents

`accounts.json` in `POOL_DATA` (created from the dashboard when you pick a model) lists agents:
`[{"id": "acc1", "role": "developer", "model": "gemini-3.8-flash-high"}]`. Roles: `developer`, `reviewer`, `debugger`.
Log an agent in once on the server: see `deploy/SETUP.md` (Google OAuth, kept alive in tmux).

## Pipelines: develop, review, rework, approved

`submit_task` with `review=true` makes a pipeline. The developer's task is followed automatically by a review task for an
agent with role `reviewer` (it starts from the developer's commit, must not change files, runs the tests, and ends its answer
with `VERDICT: APPROVE` or `VERDICT: CHANGES`). On CHANGES (or failing tests, whatever the reviewer says) a rework task goes back to
a developer, starting from the same commit with the findings in its goal, up to `max_rounds` (default 3). On APPROVE the pipeline is
approved and the lead gets one report (`task_result` of the first task): final branch, cumulative changed files, both summaries,
tests. Otherwise the pipeline ends as `needs_lead` (reason `max_rounds`, `review_no_verdict`, `review_failed`, `no_changes`) or `failed`.

The first task's id stands for the whole pipeline: `task_status` shows `running` (with `stage`: develop, awaiting_review, review,
rework) until the end, then `done`, `needs_lead` or `failed`. A review waits in the queue while the only reviewer is busy
(`awaiting_review` on the dashboard). `list_models` returns hints read off the model ids plus the pool's measured history per
model. `submit_task` warns when new work may touch the same files as work in flight. Roles needed: at least one `developer`
and one `reviewer` agent, and `POOL_MAX_CONCURRENT` high enough for both (e.g. 3 with two developers and a reviewer).

## Adding agents (Google accounts)

On your Mac: `scripts/add_account.sh acc2 [developer|reviewer|debugger]`. It registers the agent in the pool, starts agy's
Google sign-in on the server, opens the sign-in page, asks you to paste the code Google shows, and checks the login.
The dashboard has the same pieces: **+ Добавить агента**, a card state «Нужен вход» with the command to run, and buttons to
turn an agent off or delete it. An agent takes no task until its login exists. `POOL_MAX_CONCURRENT` (server env, now 2) caps
how many tasks run at once on the small server. Per-agent `proxy` in `accounts.json` routes one agent through an HTTPS proxy.

## Connecting Claude

**Claude Code**: `claude mcp add --scope user --transport http gemini-pool <PUBLIC_URL>/mcp --header "Authorization: Bearer $(cat ~/projects/.gemini-pool-mcp-token)"`

**Claude (claude.ai, desktop, mobile)** as a custom connector: Settings → Connectors → Add custom connector →
URL `<PUBLIC_URL>/mcp`, leave the OAuth fields empty. Claude registers itself, opens the pool's approval page, and
you paste `POOL_MCP_TOKEN` once. Access tokens last an hour and refresh automatically.

## Automatic review in Claude Code

When a task finishes, Claude Code should pick up the branch for review without being asked. The pool's MCP
`instructions` (sent at connect, see `INSTRUCTIONS` in `app/server.py`) tell Claude to run, right after `submit_task`,
the Monitor tool on `scripts/pool_watch.py <task_id>...`. The script prints one line per finished task:

`POOL id=… status=done repo=… base=main branch=pool/… pushed=True tests=passed files=3 duration=95s model=…`

(`status=failed` carries `reason=quota|auth|network|timeout|agent_error|violation|internal`). That line wakes the
session, which then fetches the branch, reviews the diff, runs the project's tests and merges, fixes or re-queues.
The lines hold service fields only; the agent's own text never goes into them. Needs a new Claude Code session
after the instructions change (they are read when the connector connects).

## Safe deploys

`scripts/safe_deploy.sh` pauses intake, waits until nothing runs, deploys and checks health. The dashboard has the same
pause button.

## Dashboard

`<PUBLIC_URL>/` — agents, task history, errors, per-agent usage limits and model. Log in with `POOL_MCP_TOKEN`.
