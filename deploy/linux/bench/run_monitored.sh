#!/usr/bin/env bash
# Runs a short instrumented pass against every backend while the monitoring
# stack is up, recording the wall-clock window of each pass so that
# analysis/pull_metrics.py can attribute CPU and memory use to it.
#
# Usage:
#   docker compose -f monitoring/docker-compose.yml up -d
#   bash bench/run_monitored.sh [repeats]     # default 5
#
# Writes bench/results/monitoring_benchmark_results.csv (appended) and
# bench/results/monitoring_windows.csv (replaced; the previous file is kept
# with a .bak suffix, since stale windows would be attributed to this run).

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
REPEATS=${1:-5}
OUTPUT=monitoring_benchmark_results.csv
WINDOWS="$REPO/bench/results/monitoring_windows.csv"

if [ -f "$WINDOWS" ]; then
  mv "$WINDOWS" "$WINDOWS.bak"
fi
echo "backend,start_time,end_time" > "$WINDOWS"

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
    sleep 10
  fi

  start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  cd "$REPO" || return 1
  BENCH_REPEATS=$REPEATS "$PY" bench/benchmark.py "$name" "$OUTPUT" 2>&1 | tail -1
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "$name,$start,$end" >> "$WINDOWS"

  cd "$REPO/infra/$dir" || return 1
  docker compose down >/dev/null 2>&1
}

run_backend rustfs rustfs
run_backend garage garage
run_backend seaweedfs seaweedfs
run_backend juicefs juicefs
run_backend minio minio
run_backend ceph ceph

echo "instrumented pass finished; windows recorded in $WINDOWS"
