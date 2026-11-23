#!/usr/bin/env bash
# Return the server to the release that ran before deploy/upgrade.sh.
#
#   sudo cp /opt/jx-alert-system/deploy/rollback.sh /tmp/jx-rollback.sh
#   sudo bash /tmp/jx-rollback.sh <previous-commit>
#
# Restarts the previous services (including the retired MCP service) from their
# existing images. Nothing is deleted: the database, Docker volumes, data/ and the
# files created by the SparkX release stay as they are.
set -euo pipefail

REPO="${JX_REPO:-/opt/jx-alert-system}"
PROJECT="${JX_PROJECT:-jx-alert}"

say() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
fail() { say "STOP: $*" >&2; exit 1; }
repo_git() { git -c safe.directory="$REPO" -C "$REPO" "$@"; }
compose() {  # compose <profiles...> -- <arguments...>
  local profiles=()
  while [[ "$1" != "--" ]]; do profiles+=(--profile "$1"); shift; done
  shift
  docker compose -p "$PROJECT" --project-directory "$REPO" -f "$REPO/docker-compose.yml" \
    -f "$REPO/docker-compose.runtime.yml" "${profiles[@]}" "$@"
}
wait_for() {
  local deadline=$((SECONDS + $1)) what=$2
  shift 2
  until "$@"; do
    ((SECONDS < deadline)) || { say "FAILED: $what" >&2; return 1; }
    sleep 3
  done
  say "ok: $what"
}
healthy() { curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8000/health; }

main() {
  (($# == 1)) || { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2; return 2; }
  ((EUID == 0)) || fail "run with sudo"
  local previous
  repo_git cat-file -e "$1^{commit}" 2>/dev/null || fail "commit $1 is not in $REPO"
  previous=$(repo_git rev-parse "$1^{commit}")
  local current
  current=$(repo_git rev-parse HEAD)
  say "rolling back $current -> $previous"
  # Stop the new senders and the console first: the console holds the old MCP port.
  compose scheduler console bot -- stop scheduler bot console
  repo_git checkout --quiet --detach "$previous"
  local profiles=(scheduler console bot)
  if repo_git cat-file -e "$previous:src/jx_alert/mcp_server.py" 2>/dev/null; then
    profiles+=(mcp)
    compose "${profiles[@]}" -- up -d --no-build app mcp console
  else
    compose "${profiles[@]}" -- up -d --no-build app console
  fi
  wait_for 90 "app /health" healthy || fail "app did not become healthy at $previous"
  compose "${profiles[@]}" -- up -d --no-build scheduler bot
  sleep 10
  compose "${profiles[@]}" -- ps
  say "rolled back to $previous; database, volumes and data/ unchanged"
}

main "$@"
