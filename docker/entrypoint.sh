#!/usr/bin/env bash
# Checks what the container can see from the host before starting Relay, and says
# plainly what is missing instead of letting a task fail minutes later.
set -euo pipefail

say()  { printf '  %s\n' "$*"; }
warn() { printf '  warning: %s\n' "$*" >&2; }

echo
echo "  Relay (Docker) - checking what this container can use from the host"

check_login() {
  local label="$1" path="$2" hint="$3"
  if [ -e "$path" ]; then
    if [ -r "$path" ]; then
      say "$label: using host login at $path"
    else
      warn "$label: $path is mounted but not readable by uid $(id -u). Set RELAY_UID/RELAY_GID to your host user's ids and rebuild."
    fi
  else
    warn "$label: no login found at $path. $hint"
  fi
}

check_login "Codex " "$HOME/.codex/auth.json"          "Run 'codex' once on the host, or mount ~/.codex."
check_login "Claude" "$HOME/.claude/.credentials.json" "Run 'claude' then /login once on the host, or mount ~/.claude."
check_login "Gemini" "$HOME/.gemini"                   "Run 'gemini' once on the host, or mount ~/.gemini."

if [ -n "${GH_TOKEN:-}" ] || [ -e "$HOME/.config/gh/hosts.yml" ]; then
  say "GitHub: available"
else
  warn "GitHub: not signed in. Draft pull requests need GH_TOKEN or a mounted ~/.config/gh."
fi

# Codex's own sandbox relies on kernel features Docker normally blocks. The
# container already isolates the agents, so default Codex to running unsandboxed
# here unless the operator chose otherwise.
export RELAY_CFG_codex_sandbox="${RELAY_CFG_codex_sandbox:-danger-full-access}"

if [ -n "${RELAY_REPOS:-}" ]; then
  if [ -d "$RELAY_REPOS" ]; then
    say "Repositories: $RELAY_REPOS"
  else
    warn "RELAY_REPOS=$RELAY_REPOS is not mounted, so the task wizard will not find your repositories."
  fi
fi
say "Data: ${RELAY_DATA_DIR:-/data}"
say "UI:   http://127.0.0.1:${RELAY_PORT:-8767} on the host"
echo

exec "$@"
