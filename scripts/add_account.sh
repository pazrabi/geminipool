#!/bin/bash
# Add a Google account to the pool as an agent and log it in. Run it on your Mac:
#
#     scripts/add_account.sh acc2 [developer|reviewer|debugger]
#
# It registers the agent in the pool, starts agy's Google sign-in on the server, opens the sign-in page in your
# browser, asks you to paste the code Google shows, and checks that the login works.
# Safe to run again for an agent that is already registered (it just redoes the login; set FORCE=1 if it is
# already logged in). Environment: NO_OPEN=1 does not open the browser, LOGIN_WAIT=seconds is how long to wait.
set -euo pipefail

ID="${1:-}"
ROLE="${2:-developer}"
if ! [[ "$ID" =~ ^[a-z][a-z0-9_-]{0,15}$ ]]; then
  echo "usage: $0 <id> [role]   (id: lowercase letters, digits, - or _, starting with a letter, up to 16 characters)"
  exit 2
fi

URL="${PUBLIC_URL:-https://193-41-86-92.sslip.io}"
HOST="${POOL_SSH_HOST:-root@193.41.86.92}"
KEY="${POOL_SSH_KEY:-$HOME/.ssh/aeza_pool}"
TOKEN_FILE="${POOL_TOKEN_FILE:-$HOME/projects/.gemini-pool-mcp-token}"
WAIT_S="${LOGIN_WAIT:-90}"
DATA=/var/lib/gemini-pool
PROFILE="$DATA/profiles/$ID"
LOGIN_FILE="$PROFILE/.gemini/antigravity-cli/antigravity-oauth-token"
SESSION="login-$ID"
TOKEN="$(cat "$TOKEN_FILE")"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ServerAliveInterval=15 "$HOST")
# ssh reads stdin unless told not to, which would swallow what you paste; only the calls that need stdin use SSH.
SSHN=(ssh -n -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ServerAliveInterval=15 "$HOST")

api() { curl -sS -m 90 -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; }
remote() { "${SSH[@]}" bash -s -- "$ID" "$@"; }

# Stops the sign-in session and only this agent's agy (other agents may be working at the same time).
CLEANUP='ID=$1; PROFILE=/var/lib/gemini-pool/profiles/$ID
tmux kill-session -t "login-$ID" 2>/dev/null || true
for p in $(pgrep -u pool -x agy 2>/dev/null); do
  if tr "\0" "\n" < /proc/$p/environ 2>/dev/null | grep -qx "HOME=$PROFILE"; then kill "$p" 2>/dev/null || true; fi
done
rm -f "/run/login-code-$ID"'
cleanup() { remote <<<"$CLEANUP" >/dev/null 2>&1 || true; }

# 1. Register the agent in the pool.
resp=$(api -w $'\n%{http_code}' -X POST -d "{\"id\":\"$ID\",\"role\":\"$ROLE\"}" "$URL/api/accounts")
status=${resp##*$'\n'}; body=${resp%$'\n'*}
case "$status" in
  201) echo "Registered agent $ID ($ROLE) in the pool." ;;
  400) if grep -q "already exists" <<<"$body"; then echo "Agent $ID is already registered; continuing with its login."
       else echo "The pool refused: $body"; exit 1; fi ;;
  *)   echo "Unexpected answer from the pool ($status): $body"; exit 1 ;;
esac

# 2. Already logged in?
if "${SSHN[@]}" "test -s '$LOGIN_FILE'" && [ "${FORCE:-}" != "1" ]; then
  echo "$ID already has a Google login. Use FORCE=1 to log in again."
else
  trap cleanup EXIT

  # 3. Start agy's sign-in on the server (kept in tmux, so a dropped connection does not kill it).
  remote <<'REMOTE'
ID=$1; DATA=/var/lib/gemini-pool; PROFILE=$DATA/profiles/$ID; SESSION=login-$ID
tmux kill-session -t "$SESSION" 2>/dev/null || true
for p in $(pgrep -u pool -x agy 2>/dev/null); do
  if tr "\0" "\n" < /proc/$p/environ 2>/dev/null | grep -qx "HOME=$PROFILE"; then kill "$p" 2>/dev/null || true; fi
done
install -d -o pool -g pool "$PROFILE"
# An agent with its own outgoing address signs in from that address too, not from the server's shared one.
PROXY=$(python3 - "$ID" <<'PY'
import json, sys
try:
    for a in json.load(open("/var/lib/gemini-pool/accounts.json")):
        if a.get("id") == sys.argv[1]:
            print(a.get("proxy") or "")
except Exception:
    pass
PY
)
PROXY_ENV=""
[ -n "$PROXY" ] && PROXY_ENV="HTTPS_PROXY=$PROXY"
tmux new-session -d -s "$SESSION" -x 200 -y 50 "su pool -s /bin/bash -c 'cd $DATA && HOME=$PROFILE $PROXY_ENV /home/pool/.local/bin/agy'"
REMOTE

  pane() { "${SSHN[@]}" "tmux capture-pane -p -t '$SESSION' -S -200"; }

  # 4. Choose "Google OAuth" (first option) and 5. read the sign-in URL from the screen.
  signin_url=""
  chose=0
  for _ in $(seq 1 40); do
    screen=$(pane 2>/dev/null || true)
    if [ "$chose" = 0 ] && grep -q "Select login method" <<<"$screen"; then
      "${SSHN[@]}" "tmux send-keys -t '$SESSION' Enter"; chose=1; sleep 1; continue
    fi
    signin_url=$(python3 -c '
import re, sys
text = sys.stdin.read()
m = re.search(r"Open the URL below in your browser:\s*\n\s*[─-]+\s*\n(.*?)\n\s*[─-]{5,}", text, re.S)
if m:
    print("".join(line.strip() for line in m.group(1).splitlines()))
' <<<"$screen")
    [[ "$signin_url" == https://accounts.google.com/* ]] && break
    signin_url=""; sleep 1
  done
  if [ -z "$signin_url" ]; then
    echo "Could not get the sign-in link from agy. What the server shows:"; pane | grep -v '^[[:space:]]*$' | tail -15; exit 1
  fi

  echo
  echo "Open this link, sign in with the Google account for $ID and allow access:"
  echo
  echo "$signin_url"
  echo
  [ "${NO_OPEN:-}" = "1" ] || open "$signin_url" 2>/dev/null || true
  echo "Google will then show a code. Copy it and paste it here (nothing is displayed while you paste)."

  # 6. Take the code and type it into agy.
  read -r -s -p "Code: " CODE; echo
  CODE=$(tr -d '[:space:]' <<<"$CODE")
  if ! [[ "$CODE" =~ ^[A-Za-z0-9/_.~-]{20,250}$ ]]; then
    echo "That does not look like a Google code (expected something like 4/0A...). Run the script again."; exit 1
  fi
  printf '%s' "$CODE" | "${SSH[@]}" "umask 077; cat > '/run/login-code-$ID'"
  remote <<'REMOTE'
ID=$1; SESSION=login-$ID
tmux load-buffer -b "code-$ID" "/run/login-code-$ID"
tmux paste-buffer -d -b "code-$ID" -t "$SESSION"
sleep 0.5
tmux send-keys -t "$SESSION" Enter
rm -f "/run/login-code-$ID"
REMOTE

  # 7. Wait until agy has stored the login.
  printf 'Waiting for the login'
  for _ in $(seq 1 "$WAIT_S"); do
    if "${SSHN[@]}" "test -s '$LOGIN_FILE'"; then echo " done."; break; fi
    printf '.'; sleep 1
  done
  if ! "${SSHN[@]}" "test -s '$LOGIN_FILE'"; then
    echo; echo "The login did not complete. What agy shows now:"; pane | grep -v '^[[:space:]]*$' | tail -12
    echo "The code may have expired or been wrong; run the script again."; exit 1
  fi
  cleanup; trap - EXIT
fi

# 8. Check that the account really works.
echo "Checking the account..."
result=$(api -X POST "$URL/api/accounts/$ID/quota")
if python3 -c '
import json, sys
d = json.loads(sys.argv[1])
print("Limits:" if d.get("ok") else "The check failed:")
print(d.get("text") or d)
sys.exit(0 if d.get("ok") else 1)' "$result"; then
  echo "Agent $ID is ready: the pool will give it tasks automatically."
else
  echo
  echo "The login was saved, but the account is NOT usable yet, so the pool will not give it tasks."
  echo "If the text above has a link, open it and verify the account in Google, then run this again:"
  echo "    scripts/add_account.sh $ID"
  echo "(it skips the login and only repeats this check)."
  exit 1
fi
