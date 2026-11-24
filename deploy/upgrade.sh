#!/usr/bin/env bash
# Upgrade the server to an identified, tested SparkX-only commit.
#
#   sudo bash /tmp/jx-upgrade.sh --dry-run <commit> <expected-tree>   # read-only checks and plan
#   sudo bash /tmp/jx-upgrade.sh <commit> <expected-tree>             # backup, switch, start, verify
#
# Keeps .env, data/, credentials, the database and Docker volumes. Nginx is not
# touched: docker-compose.runtime.yml publishes the console on the old MCP port, so
# the existing vhost keeps working. Copy this file out of the target commit to /tmp
# first (see docs/DEPLOY.md); everything runs inside main(), so the checkout cannot
# change the script while it executes.
set -euo pipefail

REPO="${JX_REPO:-/opt/jx-alert-system}"
PROJECT="${JX_PROJECT:-jx-alert}"
DB_CONTAINER="${JX_DB_CONTAINER:-jx-alert-db-1}"
MCP_CONTAINER="${JX_MCP_CONTAINER:-jx-alert-mcp-1}"
NEW_PROFILES=(--profile scheduler --profile console --profile bot)

usage() {
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
}

say() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
fail() { say "STOP: $*" >&2; exit 1; }
repo_git() { git -c safe.directory="$REPO" -C "$REPO" "$@"; }
compose() {
  docker compose -p "$PROJECT" --project-directory "$REPO" -f "$REPO/docker-compose.yml" \
    -f "$REPO/docker-compose.runtime.yml" "${NEW_PROFILES[@]}" "$@"
}
sql() {  # Statement on stdin, executed in the DB container with its own root password.
  docker exec -i "$DB_CONTAINER" sh -c 'exec mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -N -B jxalert' <<<"$1"
}
env_value() { sed -n "s/^$1=//p" "$REPO/.env" | tail -n 1 | tr -d "\"'\r"; }
http_status() {  # http_status <port> <path> <host>
  curl -s -o /dev/null -w '%{http_code}' --max-time 8 -H "Host: $3" -H 'X-Forwarded-Proto: https' \
    "http://127.0.0.1:$1$2" || true
}
wait_for() {  # wait_for <seconds> <description> <command...>
  local deadline=$((SECONDS + $1)) what=$2
  shift 2
  until "$@"; do
    ((SECONDS < deadline)) || { say "FAILED: $what" >&2; return 1; }
    sleep 3
  done
  say "ok: $what"
}
status_is() { [[ "$(http_status "$1" "$2" "$3")" == "$4" ]]; }  # Re-evaluated on every retry.
health_is_sparkx() {
  local body
  body=$(curl -fsS --max-time 5 http://127.0.0.1:8000/health 2>/dev/null) && [[ $body == *'"data_source":"sparkx"'* ]]
}

main() {
  local dry_run=0 force=0 positional=()
  while (($#)); do
    case "$1" in
      --dry-run) dry_run=1 ;;
      --force) force=1 ;;
      -h|--help) usage; return 0 ;;
      *) positional+=("$1") ;;
    esac
    shift
  done
  ((${#positional[@]} == 2)) || { usage >&2; return 2; }
  local target=${positional[0]} tree=${positional[1]}

  # ---------------------------------------------------------------- read-only checks
  ((EUID == 0)) || fail "run with sudo"
  for command in git docker curl gzip; do
    command -v "$command" >/dev/null || fail "missing command: $command"
  done
  [[ -f "$REPO/.env" ]] || fail "$REPO/.env not found"
  repo_git cat-file -e "$target^{commit}" 2>/dev/null \
    || fail "commit $target is not in $REPO; fetch it first (git bundle or origin), see docs/DEPLOY.md"
  target=$(repo_git rev-parse "$target^{commit}")
  [[ "$(repo_git rev-parse "$target^{tree}")" == "$tree" ]] || fail "tree of $target is not the tested tree $tree"
  if ! repo_git cat-file -e "$target:src/jx_alert/sparkx/client.py" 2>/dev/null \
      || repo_git cat-file -e "$target:src/jx_alert/mcp_server.py" 2>/dev/null; then
    fail "$target is not a SparkX-only release"
  fi
  local previous
  previous=$(repo_git rev-parse HEAD)
  [[ "$previous" != "$target" ]] || fail "server is already at $target"
  if [[ -n "$(repo_git status --porcelain --untracked-files=no)" ]] && ((force == 0)); then
    fail "tracked files in $REPO are modified; inspect them (git status) or rerun with --force"
  fi
  [[ "$(docker inspect -f '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null)" == "true" ]] || fail "$DB_CONTAINER is not running"
  docker image inspect jx-alert-app jx-alert-bot >/dev/null 2>&1 \
    || fail "images jx-alert-app and jx-alert-bot must exist (this upgrade starts without building)"
  local check_dir
  check_dir=$(mktemp -d)
  repo_git show "$target:docker-compose.yml" >"$check_dir/docker-compose.yml"
  repo_git show "$target:docker-compose.runtime.yml" >"$check_dir/docker-compose.runtime.yml"
  docker compose -p "$PROJECT" --project-directory "$REPO" -f "$check_dir/docker-compose.yml" \
    -f "$check_dir/docker-compose.runtime.yml" "${NEW_PROFILES[@]}" config -q || fail "target compose files do not validate"
  rm -rf "$check_dir"
  local running
  running=$(sql "SELECT COUNT(*) FROM sync_jobs WHERE status='RUNNING' AND started_at > UTC_TIMESTAMP() - INTERVAL 6 HOUR")
  if ((running > 0)) && ((force == 0)); then
    fail "$running job(s) of the old release are RUNNING (e.g. the 18:30 collection); wait for them or use --force"
  fi
  local legacy_port
  legacy_port=$(env_value MCP_BIND_PORT)
  legacy_port=${legacy_port:-8001}
  say "current commit : $previous"
  say "target commit  : $target (tree $tree)"
  say "plan           : backup DB and data/*.json -> stop scheduler, bot and $MCP_CONTAINER -> checkout"
  say "                 -> start app, console (8002 and legacy $legacy_port), scheduler, bot -> verify"
  say "unchanged      : .env, data/secrets, Docker volumes, Nginx"
  if ((dry_run)); then
    say "dry run: nothing was changed"
    return 0
  fi

  # ---------------------------------------------------------------- backup
  local stamp backup
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup="$REPO/backups/pre-sparkx-$stamp"
  install -d -m 0700 "$backup"
  say "backup -> $backup"
  docker exec "$DB_CONTAINER" sh -c 'exec mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --single-transaction --quick --routines --triggers --databases jxalert' \
    | gzip >"$backup/jxalert.sql.gz"
  gzip -t "$backup/jxalert.sql.gz" && [[ -s "$backup/jxalert.sql.gz" ]] || fail "database dump is not readable"
  (cd "$REPO" && find data -maxdepth 2 -name '*.json' -not -path 'data/secrets/*' -exec cp -a --parents {} "$backup/" \;)
  printf '%s\n' "$previous" >"$backup/previous-commit"
  docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}' >"$backup/containers.txt"
  local table before after
  for table in notification_outbox notification_logs sync_jobs; do
    printf '%s %s\n' "$table" "$(sql "SELECT COUNT(*) FROM $table")" >>"$backup/counts-before.txt"
  done
  chmod -R go-rwx "$backup"
  say "ok: backup $(du -sh "$backup" | cut -f1)"

  # ---------------------------------------------------------------- switch
  say "stopping senders (scheduler, bot) and the retired MCP service"
  compose stop scheduler bot
  if [[ "$(docker inspect -f '{{.State.Running}}' "$MCP_CONTAINER" 2>/dev/null)" == "true" ]]; then
    docker stop "$MCP_CONTAINER" >/dev/null  # Kept (not removed) for rollback.
  fi
  repo_git checkout --quiet --detach "$target"
  [[ "$(repo_git rev-parse 'HEAD^{tree}')" == "$tree" ]] || fail "checked-out tree differs from $tree"
  say "ok: code at $target"
  compose up -d --no-build app console
  local rollback="sudo cp $REPO/deploy/rollback.sh /tmp/jx-rollback.sh && sudo bash /tmp/jx-rollback.sh $previous"
  wait_for 90 "app /health reports data_source=sparkx" health_is_sparkx || fail "app is not healthy; rollback: $rollback"
  local host
  host=$(compose exec -T app python -c 'from urllib.parse import urlsplit; from jx_alert.config import get_settings; print(urlsplit(get_settings().console_public_url).netloc)' | tr -d '\r')
  [[ -n "$host" ]] || fail "console public URL is not configured (CONSOLE_PUBLIC_URL / MCP_PUBLIC_URL); rollback: $rollback"
  wait_for 60 "console login page on 8002" status_is 8002 /console/login "$host" 200 \
    || fail "console is not serving; rollback: $rollback"
  wait_for 30 "existing vhost route via port $legacy_port" status_is "$legacy_port" /console/login "$host" 200 \
    || fail "legacy port $legacy_port does not reach the console; rollback: $rollback"
  [[ "$(http_status "$legacy_port" /mcp "$host")" == 410 ]] || fail "retired /mcp path does not answer 410; rollback: $rollback"
  # The official read-only SparkX Skills live in data/ (not in the repository); agents also retry later.
  if compose exec -T console python -m jx_alert.sparkx.skills install; then
    say "ok: SparkX Skills installed"
  else
    say "warning: SparkX Skills not installed now; install them in the console (SparkX MCP page)"
  fi
  compose up -d --no-build scheduler bot
  sleep 10
  local service services
  services=$(compose ps --status running --services)
  for service in app console scheduler bot; do
    grep -qx "$service" <<<"$services" || fail "$service is not running; rollback: $rollback"
  done
  while read -r table before; do
    after=$(sql "SELECT COUNT(*) FROM $table")
    ((after >= before)) || fail "$table shrank from $before to $after; rollback: $rollback"
    say "ok: $table $before -> $after"
  done <"$backup/counts-before.txt"
  local public
  public=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$host/console/login" || true)
  say "public https://$host/console/login -> $public (expected 200)"
  say "deployed $target (tree $tree); previous $previous; backup $backup"
  say "rollback: $rollback"
  say "next: open https://$host/console/. Lanes are ON by default and run once SparkX and DeepSeek are configured"
  say "      (hourly alerts within ~10 min inside the window, daily report and review at 08:30). To check content"
  say "      first, switch lanes off in 通知与报告, preview, then switch them back on. Map operators to get @-mentions."
}

main "$@"
