#!/usr/bin/env bash
# Runs the two passes that follow a throughput campaign: the instrumented
# pass that produces the resource figures, and the cross-validation run.
# Both need the machine to themselves, so they run one after the other.
#
# Usage: bash bench/run_post_campaign.sh

set -u
REPO=/d/Thesis

echo "=== monitoring stack up ==="
cd "$REPO/monitoring" || exit 1
docker compose up -d >/dev/null 2>&1
sleep 20

echo "=== instrumented pass (5 repeats per workload) ==="
cd "$REPO" || exit 1
bash bench/run_monitored.sh 5

echo "=== extracting resource figures ==="
cd "$REPO" || exit 1
"$REPO/.venv/Scripts/python.exe" analysis/pull_metrics.py 2>&1 | tail -3

echo "=== monitoring stack down ==="
cd "$REPO/monitoring" || exit 1
docker compose down >/dev/null 2>&1

echo "=== cross-validation with warp ==="
cd "$REPO" || exit 1
bash bench/run_warp_all.sh

echo "post-campaign passes finished"
