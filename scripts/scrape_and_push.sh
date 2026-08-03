#!/bin/zsh
# Periodic scrape on this Mac: pull the shared state, run, push it back.
# Driven by launchd (com.car-alert.scrape); logs to /tmp/car-alert-scrape.log.
set -e
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:$PATH"

# one run at a time - launchd fires on schedule regardless of the previous run
LOCKDIR=/tmp/car-alert-scrape.lock
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "$(date '+%H:%M:%S') previous run still going, skipping"
  exit 0
fi
trap 'rmdir "$LOCKDIR"' EXIT

TOKEN=$(gh auth token 2>/dev/null || true)
REPO_URL="https://x-access-token:${TOKEN}@github.com/rostyslavjfg-oss/car-alert.git"

if [ -n "$TOKEN" ]; then
  # the repo db may have moved (manual Actions run) - take the newest state.
  # -X theirs resolves a binary listings.db conflict in favour of the remote.
  git -c credential.helper= pull --rebase -X theirs --autostash -q "$REPO_URL" main || {
    git rebase --abort 2>/dev/null || true
    echo "$(date '+%H:%M:%S') pull failed, running on the local copy"
  }
fi

"$HOME/.venvs/car-alert/bin/python" -u main.py   # no --drain: bot.py long-polls and would 409

if [ -n "$TOKEN" ]; then
  git add listings.db config/brands.json webapp/data 2>/dev/null || true
  if ! git diff --cached --quiet; then
    git -c user.email=rostyslav.chonka@triad.sk -c user.name="Rostyslav Chonka" \
        commit -qm "data: update listings [skip ci]"
    git -c credential.helper= push -q "$REPO_URL" main:main || \
      echo "$(date '+%H:%M:%S') push failed, will retry next cycle"
  fi
fi
