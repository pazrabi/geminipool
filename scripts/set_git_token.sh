#!/bin/bash
# Put a new GitHub token on the pool server and restart the pool safely.
# The token is typed hidden, checked against GitHub from this Mac, sent to the server over ssh stdin
# (never as a command-line argument) and written into /etc/gemini-pool.env. It is never printed.
set -euo pipefail

HOST="${POOL_SSH_HOST:-root@193.41.86.92}"
KEY="${POOL_SSH_KEY:-$HOME/.ssh/aeza_pool}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes)

printf 'Paste the GitHub token (nothing will be shown), then press Enter: '
read -rs TOKEN
echo
[ -n "$TOKEN" ] || { echo "No token entered."; exit 1; }

echo "Checking the token with GitHub..."
RESP="$(curl -sS -m 30 -H "Authorization: Bearer $TOKEN" -H 'Accept: application/vnd.github+json' \
  'https://api.github.com/user/repos?per_page=100&affiliation=owner,collaborator,organization_member' || true)"
SUMMARY="$(printf '%s' "$RESP" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("ERROR no valid answer from GitHub"); sys.exit()
if isinstance(d, dict):
    print("ERROR " + str(d.get("message")))
else:
    priv = sum(1 for r in d if r.get("private"))
    push = sum(1 for r in d if r.get("permissions", {}).get("push"))
    print("OK %d repositories reachable (%d private), push allowed on %d" % (len(d), priv, push))
')"
echo "$SUMMARY"
case "$SUMMARY" in OK*) ;; *) echo "The token was NOT saved."; exit 1 ;; esac
if [ "${CONFIRM:-ask}" != "yes" ]; then
  printf 'Save this token on the server and restart the pool? [y/N] '
  read -r ANS
  [ "$ANS" = "y" ] || [ "$ANS" = "Y" ] || { echo "Cancelled, nothing changed."; exit 1; }
fi

# The server keeps a dated copy of the old env file, then replaces (or adds) the GIT_PUSH_TOKEN line.
printf '%s\n' "$TOKEN" | "${SSH[@]}" "$HOST" '
  set -e
  IFS= read -r NEW
  cp /etc/gemini-pool.env "/etc/gemini-pool.env.bak-$(date +%Y%m%d-%H%M%S)"
  grep -v "^GIT_PUSH_TOKEN=" /etc/gemini-pool.env > /etc/gemini-pool.env.new || true
  printf "GIT_PUSH_TOKEN=%s\n" "$NEW" >> /etc/gemini-pool.env.new
  chmod 600 /etc/gemini-pool.env.new
  mv /etc/gemini-pool.env.new /etc/gemini-pool.env
'
unset TOKEN RESP
echo "Token saved on the server. Restarting the pool when nothing is running..."
exec "$HERE/safe_deploy.sh"
