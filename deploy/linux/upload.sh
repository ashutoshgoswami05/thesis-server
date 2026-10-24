#!/usr/bin/env bash
# Sends the framework to the VPS. Runs on THIS machine, in Git Bash.
#
# Usage:  bash linux/upload.sh user@host [remote_dir]
#   e.g.  bash linux/upload.sh root@203.0.113.10
#
# Sends only what is needed to run: the deployments, the suite, the harness,
# the analysis stage and the monitoring stack. Left behind are .venv (built
# on the VPS, and Windows binaries anyway), latex/, code/, thesis-baseline/
# and every existing result file -- the VPS starts with no results, so there
# is nothing there that can overwrite the thesis's data, and nothing comes
# back that was not measured there.

set -eu
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

[ $# -ge 1 ] || { echo "usage: bash linux/upload.sh user@host [remote_dir]" >&2; exit 1; }
TARGET=$1
REMOTE=${2:-thesis}
BUNDLE="$(mktemp -d)/thesis-framework.tar.gz"

cd "$REPO"
tar -czf "$BUNDLE" \
  --exclude='bench/results' \
  --exclude='tests/results' \
  --exclude='crosscheck/results' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.windows' \
  infra monitoring tests bench analysis crosscheck linux \
  requirements.txt pytest.ini README.md

echo "bundle: $(du -h "$BUNDLE" | cut -f1)"
scp "$BUNDLE" "$TARGET:/tmp/thesis-framework.tar.gz"
ssh "$TARGET" "mkdir -p '$REMOTE' && tar -xzf /tmp/thesis-framework.tar.gz -C '$REMOTE' && rm /tmp/thesis-framework.tar.gz && ls '$REMOTE'"
rm -rf "$(dirname "$BUNDLE")"

cat <<DONE

uploaded to $TARGET:~/$REMOTE

on the VPS:
   cd $REMOTE
   bash linux/setup.sh       # docker, venv, network  (then log out and back in)
   bash linux/install.sh     # swap in the Linux scripts
DONE
