#!/usr/bin/env bash
# Runs the warp cross-validation against every backend, one at a time.
#
# Usage: bash bench/run_warp_all.sh
# Output: bench/results/warp/<backend>.txt

set -u
REPO=/d/Thesis

run_backend () {
  name=$1
  dir=$2
  echo "=== $name ==="
  cd "$REPO/infra/$dir" || return 1
  docker compose up -d >/dev/null 2>&1

  if [ "$name" = "ceph" ]; then
    for _ in $(seq 1 18); do
      status=$(docker inspect --format '{{.State.Health.Status}}' ceph-aio 2>&1)
      if [ "$status" = "healthy" ]; then break; fi
      sleep 10
    done
  else
    sleep 8
  fi

  cd "$REPO" || return 1
  bash bench/run_warp.sh "$name" >/dev/null 2>&1
  echo "  finished $name"
  cd "$REPO/infra/$dir" || return 1
  docker compose down >/dev/null 2>&1
}

run_backend rustfs rustfs
run_backend garage garage
run_backend seaweedfs seaweedfs
run_backend juicefs juicefs
run_backend minio minio
run_backend ceph ceph

echo "warp cross-validation finished for all backends"
