#!/usr/bin/env bash
# Pull-based continuous deployment for the photo API.
#
#   bash deploy/autodeploy.sh           deploy origin/main if CI passed (the timer runs this)
#   bash deploy/autodeploy.sh install   one-time setup: swap, systemd units, start the timer
#
# passora-autodeploy.timer runs this every 2 minutes. When origin/main has a new
# commit whose CI "checks" job passed, it checks the commit out, installs
# requirements if they changed, refreshes the systemd units, restarts the API
# and waits for /health. If the API does not become healthy, it rolls back to
# the previous commit and never retries the failed one.
#
# Everything runs inside main() so bash has read the whole file before the
# checkout (which contains this file) changes.

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE="${SERVICE:-passora-backend}"
GITHUB_REPO="${GITHUB_REPO:-shreyakhot/passora_backend}"
BRANCH="${BRANCH:-main}"
CI_CHECK_NAME="${CI_CHECK_NAME:-checks}"  # job name in .github/workflows/ci.yml
STATE_DIR="${STATE_DIR:-$HOME/.passora-autodeploy}"
UNITS="passora-backend.service passora-autodeploy.service passora-autodeploy.timer"

# Prints "success", "pending" or the failed conclusion of the CI job for a commit.
ci_status() {
  curl -fsS -m 20 -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$GITHUB_REPO/commits/$1/check-runs?check_name=$CI_CHECK_NAME" |
    python3 -c '
import json, sys
runs = json.load(sys.stdin).get("check_runs", [])
if not runs:
    print("pending")
else:
    run = max(runs, key=lambda r: r.get("started_at") or "")
    print(run["conclusion"] if run["status"] == "completed" else "pending")
'
}

# Installs any unit in deploy/ that differs from the installed one.
sync_units() {
  local changed=0 unit rendered
  for unit in $UNITS; do
    rendered="$(sed -e "s|__USER__|$(id -un)|g" -e "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/$unit")"
    if [ "$rendered" != "$(cat "/etc/systemd/system/$unit" 2>/dev/null)" ]; then
      echo "  installing $unit"
      printf '%s\n' "$rendered" | sudo tee "/etc/systemd/system/$unit" >/dev/null
      changed=1
    fi
  done
  if [ "$changed" = 1 ]; then sudo systemctl daemon-reload; fi
}

wait_healthy() {
  local i
  for i in $(seq 1 60); do
    if curl -fs -m 5 http://127.0.0.1:8000/health; then echo; return 0; fi
    sleep 5
  done
  return 1
}

# apply <commit> <previous commit>: check out, install, restart, health check.
apply() {
  git -C "$APP_DIR" reset --hard --quiet "$1"
  if ! git -C "$APP_DIR" diff --quiet "$2" "$1" -- requirements.txt; then
    echo "  requirements.txt changed: installing"
    "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" || return 1
  fi
  sync_units
  echo "  restarting $SERVICE"
  sudo systemctl restart "$SERVICE"
  wait_healthy
}

deploy() {
  mkdir -p "$STATE_DIR"
  exec 9>"$STATE_DIR/lock"
  flock -n 9 || { echo "another deploy is running"; return 0; }

  cd "$APP_DIR"
  git fetch --quiet origin "$BRANCH"
  local current target short status
  current="$(git rev-parse HEAD)"
  target="$(git rev-parse "origin/$BRANCH")"
  [ "$current" = "$target" ] && return 0
  short="${target:0:7}"
  [ -f "$STATE_DIR/skip-$target" ] && return 0

  if ! status="$(ci_status "$target")"; then
    echo "$short: could not read the CI status from GitHub, will retry"
    return 0
  fi
  case "$status" in
    success) ;;
    pending)
      if [ ! -f "$STATE_DIR/waiting-$target" ]; then
        echo "$short: waiting for CI"
        touch "$STATE_DIR/waiting-$target"
      fi
      return 0 ;;
    *)
      echo "$short: CI $status, not deploying it"
      touch "$STATE_DIR/skip-$target"
      return 0 ;;
  esac

  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "$short: tracked files were changed on the server, not deploying:"
    git status --short --untracked-files=no
    return 1
  fi

  echo "$short: deploying (was ${current:0:7})"
  git log --oneline "$current..$target" | sed 's/^/  /'
  if apply "$target" "$current"; then
    echo "$short: deployed"
    rm -f "$STATE_DIR"/waiting-*
    return 0
  fi

  echo "$short: not healthy, rolling back to ${current:0:7}"
  sudo journalctl -u "$SERVICE" -n 30 --no-pager || true
  touch "$STATE_DIR/skip-$target"
  if apply "$current" "$target"; then
    echo "rolled back to ${current:0:7}"
  else
    echo "ROLLBACK FAILED: check 'sudo journalctl -u $SERVICE'"
  fi
  return 1
}

install() {
  # Swap: the API holds about 1.5 GB of models, so pip installs and restarts
  # need headroom on a 2 GB instance.
  if [ -z "$(swapon --show --noheadings)" ]; then
    echo "creating a 2 GB swap file"
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab >/dev/null
  fi
  sync_units
  sudo systemctl enable --now passora-autodeploy.timer
  systemctl list-timers passora-autodeploy.timer --no-pager
}

main() {
  set -euo pipefail
  case "${1:-deploy}" in
    deploy) deploy ;;
    install) install ;;
    *) echo "usage: $0 [deploy|install]" >&2; return 2 ;;
  esac
}

main "$@"
