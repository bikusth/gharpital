#!/usr/bin/env bash
# Safely save a workflow's results to the repo.
#
#   bash build/save_and_push.sh "Commit message" data/raw [more paths...]
#
# The paths given are this job's own source data (e.g. data/raw for the NMC scraper,
# data/sources for the hospital import). Everything under data/build and data/export is
# generated, so it is never merged: we take the latest repo, put our source data on top,
# rebuild the generated files fresh, and push. If someone else pushed in the meantime,
# we simply start again from their version. Nothing is ever lost to a merge conflict.
set -uo pipefail

msg="$1"; shift
paths=("$@")
stash="$(mktemp -d)"

for p in "${paths[@]}"; do
  if [ -e "$p" ]; then mkdir -p "$stash/$(dirname "$p")"; cp -a "$p" "$stash/$p"; fi
done

git config user.name "gharpital-bot"
git config user.email "gharpital-bot@users.noreply.github.com"

for attempt in 1 2 3 4 5 6; do
  git fetch --quiet origin main
  git reset --quiet --hard origin/main
  for p in "${paths[@]}"; do
    if [ -e "$stash/$p" ]; then rm -rf "$p"; mkdir -p "$(dirname "$p")"; cp -a "$stash/$p" "$p"; fi
  done
  python build/build_data.py
  git add -A "${paths[@]}" data/build data/export
  if git diff --cached --quiet; then
    echo "Nothing new to save."
    exit 0
  fi
  git commit --quiet -m "$msg [skip ci]"
  if git push --quiet origin HEAD:main; then
    echo "Saved: $msg"
    exit 0
  fi
  echo "Push rejected (the repo changed meanwhile); retrying from the latest version ($attempt/6)"
  sleep $((attempt * 5))
done

echo "Could not save after 6 attempts." >&2
exit 1
