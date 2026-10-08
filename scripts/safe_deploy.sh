#!/bin/bash
# Deploy without interrupting running agent tasks: pause intake, wait until nothing runs, deploy, verify.
# The restart switches the pause off by itself, so the queue resumes with the new version.
set -euo pipefail

URL="${PUBLIC_URL:-https://gemini-pool-production.up.railway.app}"
TOKEN_FILE="${POOL_TOKEN_FILE:-$HOME/projects/.gemini-pool-mcp-token}"
WAIT_S="${DRAIN_WAIT_SECONDS:-2100}"   # a task may run up to 30 minutes
RAILWAY="${RAILWAY_BIN:-$(command -v railway || echo "$HOME/.railway/bin/railway")}"
TOKEN="$(cat "$TOKEN_FILE")"

api() { curl -fsS -m 30 -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; }
resume() { api -X POST -d '{"drain": false}' "$URL/api/drain" >/dev/null 2>&1 || true; }

echo "Pausing intake of new tasks..."
api -X POST -d '{"drain": true}' "$URL/api/drain" >/dev/null
trap resume ERR INT TERM   # if anything fails before the restart, let the pool work again

deadline=$((SECONDS + WAIT_S))
while true; do
  running=$(api "$URL/api/overview" | python3 -c 'import json,sys; print(json.load(sys.stdin)["running"])')
  [ "$running" = "0" ] && break
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "Still $running task(s) running after ${WAIT_S}s; not deploying. Intake resumed."
    exit 1
  fi
  echo "$(date +%H:%M:%S) waiting for $running running task(s)..."
  sleep 15
done

echo "Nothing is running. Deploying..."
cd "$(dirname "$0")/.."
"$RAILWAY" up --service gemini-pool --ci | tail -1
trap - ERR INT TERM

for _ in $(seq 1 12); do
  sleep 5
  if [ "$(curl -s -m 10 "$URL/health" || true)" = "ok" ]; then
    state=$(api "$URL/api/overview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("intake paused" if d["draining"] else "intake open")')
    echo "Deployed. Health ok, $state."
    exit 0
  fi
done
echo "Deployed, but /health did not answer in time; check the Railway logs." >&2
exit 1
