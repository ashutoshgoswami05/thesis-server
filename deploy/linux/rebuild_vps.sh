#!/usr/bin/env bash
# Points the `thesis` alias at a freshly spun VPS and rebuilds it from scratch.
# Runs on THIS machine, in Git Bash.
#
# Usage:  bash linux/rebuild_vps.sh <new-ip>
#
# A new instance starts empty, so this does every step: rewrites the HostName
# in ~/.ssh/config, forgets the old host key, uploads the framework, installs
# Docker and the venv, swaps in the Linux scripts, and carries over the two
# images that can no longer be pulled from any registry.
#
# It deliberately does not pass StrictHostKeyChecking=accept-new. The first
# connection asks you to confirm the new machine's host key, which is a
# decision to make by looking, not one to automate away.

set -eu
[ $# -ge 1 ] || { echo "usage: bash linux/rebuild_vps.sh <new-ip>" >&2; exit 1; }
NEW=$1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CONFIG="$HOME/.ssh/config"

[ -f "$CONFIG" ] || { echo "no $CONFIG -- create the thesis host entry first" >&2; exit 1; }
OLD=$(ssh -G thesis 2>/dev/null | awk '/^hostname /{print $2}')
[ -n "$OLD" ] || { echo "the thesis alias has no HostName" >&2; exit 1; }

echo "=== ssh config"
if [ "$OLD" = "$NEW" ]; then
  echo "already $NEW"
else
  cp "$CONFIG" "$CONFIG.bak"
  sed -i "s/$OLD/$NEW/g" "$CONFIG"
  echo "$OLD -> $NEW  (previous file kept as config.bak)"
fi

echo "=== host keys"
ssh-keygen -R "$OLD" >/dev/null 2>&1 || true
echo "forgot the old host $OLD"
# The new host's entry is deliberately left alone. If one exists from a
# previous instance at the same address, ssh should say so loudly rather
# than have this script delete a key you may have already checked.
echo "the new machine's keys, to compare with the prompt on first connect:"
ssh-keyscan -T 10 "$NEW" 2>/dev/null > "$REPO/.hostkeys.tmp" || true
ssh-keygen -lf "$REPO/.hostkeys.tmp" 2>/dev/null | sed 's/^/   /' || echo "   (no answer on port 22 yet)"
rm -f "$REPO/.hostkeys.tmp"

echo
echo "=== connecting (confirm the fingerprint above when asked)"
ssh thesis 'echo "reached $(hostname): $(nproc) vCPU, $(free -g | awk "/^Mem:/{print \$2}") GB RAM"'

echo
echo "=== uploading the framework"
bash "$HERE/upload.sh" thesis

echo
echo "=== docker, venv, network"
ssh thesis 'cd thesis && bash linux/setup.sh' 2>&1 | tail -12

echo
echo "=== linux scripts into place"
ssh thesis 'cd thesis && bash linux/install.sh' 2>&1 | tail -4

echo
echo "=== images that cannot be pulled any more"
# minio/minio has been withdrawn from Docker Hub entirely, so the copy in this
# machine's cache is the only source. docker load preserves the registry
# digest, so the compose files' pinned reference still resolves afterwards.
if docker image inspect minio/minio:latest >/dev/null 2>&1; then
  echo "sending minio/minio + minio/warp (~285 MB, slow on a home uplink)"
  docker save minio/minio:latest minio/warp:latest | ssh thesis 'docker load' | tail -2
else
  echo "WARNING: minio/minio:latest is not in this machine's cache either."
  echo "         MinIO cannot run on the VPS without it."
fi

echo
echo "=== what the VPS now has"
ssh thesis 'docker image ls --format "   {{.Repository}} {{.ID}}" | sort'

cat <<DONE

rebuilt. Ceph's 19.2.5 image is not distributed any more, so infra/ceph is
pinned to 19.2.7 -- the VPS pulls that itself on first use.

next:
   ssh thesis
   cd thesis && nohup setsid bash linux/run_all.sh > ~/run_all.log 2>&1 < /dev/null &

then, back here:
   bash linux/fetch_results.sh thesis
DONE
