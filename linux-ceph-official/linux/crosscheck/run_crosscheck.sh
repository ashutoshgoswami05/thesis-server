#!/usr/bin/env bash
# Runs a short instrumented pass against the three backends that expose native
# metrics, recording each window so compare_metrics.py can read the same window
# from both instruments.
#
# Usage: bash crosscheck/run_crosscheck.sh [repeats]      # default 5
#
# Deliberately does not reuse bench/run_monitored.sh: that script replaces
# bench/results/monitoring_windows.csv, which the thesis's resource table is
# computed from. Nothing here writes into the submitted result files.

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
REPEATS=${1:-5}
OUT="$REPO/crosscheck/results"
RUNS_NAME=crosscheck_runs.csv          # written under bench/results, moved at the end

mkdir -p "$OUT"
echo "backend,start_time,end_time" > "$OUT/windows.csv"

if ! docker ps --format '{{.Names}}' | grep -qx prometheus; then
  echo "prometheus is not running; start monitoring/ first" >&2
  exit 1
fi

# Host port each backend's S3 endpoint is published on, from tests/config.yaml.
port_of () {
  case "$1" in
    seaweedfs) echo 8333 ;;
    garage)    echo 3900 ;;
    juicefs)   echo 9004 ;;
  esac
}

run_backend () {
  name=$1
  echo "=== $name ==="
  cd "$REPO/infra/$name" && docker compose up -d >/dev/null 2>&1

  # Wait for the S3 endpoint to answer rather than guessing a sleep: JuiceFS
  # runs a one-shot format step before its gateway starts, and a run begun
  # too early would record failures inside the measured window.
  port=$(port_of "$name")
  for _ in $(seq 1 30); do
    if curl -s -o /dev/null --max-time 2 "http://localhost:$port"; then break; fi
    sleep 2
  done
  sleep 6   # two more scrape intervals, so the window opens on live samples

  start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  cd "$REPO" && BENCH_REPEATS=$REPEATS "$PY" bench/benchmark.py "$name" "$RUNS_NAME" 2>&1 | tail -1
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "$name,$start,$end" >> "$OUT/windows.csv"
  echo "  window $start -> $end"

  # Leave the containers up for one more scrape interval, so the last samples
  # of the window are collected before the containers disappear.
  sleep 8
  cd "$REPO/infra/$name" && docker compose down >/dev/null 2>&1
}

run_backend seaweedfs
run_backend garage
run_backend juicefs

if [ -f "$REPO/bench/results/$RUNS_NAME" ]; then
  mv "$REPO/bench/results/$RUNS_NAME" "$OUT/runs.csv"
  echo "runs moved to $OUT/runs.csv"
fi

echo "windows recorded in $OUT/windows.csv"
