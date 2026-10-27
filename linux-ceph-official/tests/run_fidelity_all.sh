#!/usr/bin/env bash
# Runs the API fidelity suite against every backend, one at a time, bringing
# each one up and back down around its own run so they never compete for the
# machine's CPU.
#
# Usage: bash tests/run_fidelity_all.sh
# Results merge into tests/results/compatibility_matrix.csv.

set -u
REPO=/d/Thesis
PY="$REPO/.venv/Scripts/python.exe"

run_backend () {
  name=$1
  dir=$2
  echo "=== $name ==="
  cd "$REPO/infra/$dir" || return 1
  docker compose up -d >/dev/null 2>&1

  # Ceph runs a full mon+mgr+osd+rgw bootstrap and is only usable once its
  # healthcheck passes; the others are ready after a few seconds.
  if [ "$name" = "ceph" ]; then
    for _ in $(seq 1 48); do
      status=$(docker inspect --format '{{.State.Health.Status}}' ceph-rgw 2>&1)
      if [ "$status" = "healthy" ]; then break; fi
      sleep 10
    done
  else
    sleep 10
  fi

  cd "$REPO" || return 1
  "$PY" -m pytest tests/ -k "$name" -q 2>&1 | tail -4
  cd "$REPO/infra/$dir" || return 1
  docker compose down >/dev/null 2>&1
}

run_backend rustfs rustfs
run_backend garage garage
run_backend seaweedfs seaweedfs
run_backend juicefs juicefs
run_backend minio minio
run_backend ceph ceph

echo "fidelity suite finished for all backends"
