# Server setup (Aéza VPS, Ubuntu 26.04 LTS, 1 vCPU / 2 GB / 30 GB)

What is on the server, so it can be rebuilt:

- User `pool` owns the data in `/var/lib/gemini-pool` (database, repos, worktrees, agent logins in `profiles/<id>`).
- Code in `/opt/gemini-pool` (this repository's `app/`, `scripts/`, `tests/`), run by `gemini-pool.service`
  (`deploy/gemini-pool.service`) with secrets in `/etc/gemini-pool.env` (root:pool, mode 640).
- `agy` installed for `pool` with Google's script: `curl -fsSL https://antigravity.google/cli/install.sh | bash`.
- Python 3.12 for the tests of tasks: `uv python install 3.12` into `/opt/pythons`, linked as `python3` in
  `/opt/pool-bin`, which comes first in the service's PATH (the server's own `python3` is 3.14).
- Caddy (`deploy/Caddyfile`) terminates HTTPS for `193-41-86-92.sslip.io` and proxies to `127.0.0.1:8080`.
- Firewall: only 22, 80, 443. SSH: keys only (`/etc/ssh/sshd_config.d/00-hardening.conf`). 2 GB swap file. fail2ban.

Log an agent in once (Google OAuth), keeping the session alive in tmux:

    ssh -t root@SERVER "tmux new-session -A -s login \"su pool -s /bin/bash -c 'cd /var/lib/gemini-pool && HOME=/var/lib/gemini-pool/profiles/acc1 /home/pool/.local/bin/agy'\""

Deploy with `scripts/safe_deploy.sh`. Logs: `journalctl -u gemini-pool -f`.

env variables (`/etc/gemini-pool.env`): POOL_DATA, POOL_BIND=127.0.0.1, PORT, PUBLIC_URL, AGY_BIN, POOL_MCP_TOKEN, GIT_PUSH_TOKEN.
