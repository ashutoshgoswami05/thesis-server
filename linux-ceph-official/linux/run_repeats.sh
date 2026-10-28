#!/usr/bin/env bash
# Runs the 960-run campaign N times, each to its own CSV.
#
# Usage:  bash linux/run_repeats.sh [N]        # default 15
#   detached:
#     nohup setsid bash linux/run_repeats.sh 15 > ~/repeats.log 2>&1 < /dev/null &
#
# Separate output files are the point: a single campaign cannot distinguish a
# 2% lead from run-to-run noise, and several of the margins in the first VPS
# campaign were under 5%. Comparing campaign medians against each other gives
# the floor below which a difference means nothing on this host.
#
# The fidelity suite runs once at the start -- it measures capabilities, which
# do not vary between repeats -- and the instrumented pass once at the end,
# while the monitoring stack is up.

set -u
REPEATS=${1:-15}
# Wipe every backend's storage after each block of this many campaigns; 0
# disables it. See reset_volumes.sh for why three of the six need it.
RESET_EVERY=${2:-5}
LAST=$REPEATS
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
PY="$REPO/.venv/bin/python"

ceph_up () {
  cd "$REPO/infra/ceph" || return 1
  docker compose up -d >/dev/null 2>&1
  for _ in $(seq 1 48); do
    [ "$(docker inspect --format '{{.State.Health.Status}}' ceph-rgw 2>&1)" = "healthy" ] && return 0
    sleep 10
  done
  return 1
}

echo "=== $REPEATS campaigns, starting $(date -u +%H:%M:%S)"
{
  echo "# Images used for these runs, resolved at run time."
  grep -h "image:" infra/*/docker-compose.yml monitoring/docker-compose.yml |
    sed 's/^ *image: //' | sort -u
  echo
  # Recorded from the running daemons rather than asserted, because this is
  # the line a reader will trust about which Ceph produced these numbers.
  echo "ceph: $(docker exec ceph-rgw ceph --version 2>/dev/null | awk '{print $3}' || echo '?') from quay.io/ceph/ceph, the project's own image"
  echo "host: $(hostname), $(nproc) vCPU, $(free -g | awk '/^Mem:/{print $2}') GB RAM, $(uname -r)"
  echo "repeats: $REPEATS campaigns of 960 runs, one CSV each"
} > bench/results/image_digests.txt

echo
echo "=== ceph s3 user"
if ceph_up; then
  if docker exec ceph-rgw radosgw-admin user info --uid=thesisuser >/dev/null 2>&1; then
    echo "thesisuser already exists"
  else
    docker exec ceph-rgw radosgw-admin user create --uid=thesisuser \
      --display-name="Thesis Test User" \
      --access-key=thesiscephaccess --secret-key=thesiscephsecret123 >/dev/null 2>&1 &&
      echo "thesisuser created"
  fi
  docker exec ceph-rgw ceph health 2>&1 | tail -1
else
  echo "WARNING: ceph did not become healthy; its runs will fail"
fi
cd "$REPO/infra/ceph" && docker compose down >/dev/null 2>&1
cd "$REPO" || exit 1

echo
echo "=== fidelity suite (once)"
bash tests/run_fidelity_all.sh 2>&1 | grep -E "^===|passed|failed" | tail -20

for i in $(seq 1 "$REPEATS"); do
  n=$(printf '%02d' "$i")
  echo
  echo "=== campaign $n of $REPEATS   $(date -u +%H:%M:%S)"
  bash bench/run_all_backends.sh "campaign_$n.csv" 2>&1 | grep -E "^===|Results written" | tail -12

  rows=$(tail -n +2 "bench/results/campaign_$n.csv" 2>/dev/null | wc -l)
  bad=$(awk -F, 'NR>1 && $17!="True"' "bench/results/campaign_$n.csv" 2>/dev/null | wc -l)
  echo "campaign $n: $rows rows, $bad failed"

  # Ceph's RGW defers deletion, so 960 runs of churn leave garbage behind. Left
  # alone across repeats the 10 GiB OSD fills and RGW starts returning
  # InsufficientCapacity, which silently ruins every later Ceph measurement.
  if ceph_up; then
    docker exec ceph-rgw radosgw-admin gc process --include-all >/dev/null 2>&1
    used=$(docker exec ceph-rgw ceph df 2>/dev/null | awk '/^TOTAL/{print $NF}')
    echo "ceph osd after gc: ${used:-?}% used"
    cd "$REPO/infra/ceph" && docker compose down >/dev/null 2>&1
    cd "$REPO" || exit 1
    case "$used" in
      ''|*[!0-9.]*) : ;;
      *) if [ "${used%%.*}" -ge 85 ]; then
           echo "ABORTING: ceph osd at ${used}% -- later runs would be invalid"
           break
         fi ;;
    esac
  fi
  echo "disk: $(df -h "$REPO" | awk 'NR==2{print $4" free"}')"

  # Three backends degrade under sustained bucket churn -- SeaweedFS stops
  # accepting writes, Garage's bucket metadata grows, Ceph's OSD fills -- so
  # storage goes back to empty every RESET_EVERY runs. Each block either side
  # of a reset is its own series; reset_volumes.sh appends the boundary to
  # bench/results/NOTE_volume_resets.txt so the CSVs carry the explanation.
  if [ "$RESET_EVERY" -gt 0 ] && [ $((i % RESET_EVERY)) -eq 0 ] && [ "$i" -lt "$LAST" ]; then
    bash "$REPO/linux/reset_volumes.sh" "after campaign $n" || exit 1
  fi
done

echo
echo "=== monitoring stack"
cd "$REPO/monitoring" || exit 1
docker compose up -d 2>&1 | tail -2
cd "$REPO" || exit 1
sleep 30
echo -n "prometheus ready: "
curl -s http://localhost:9090/-/ready || echo "NOT READY"
echo

echo "=== instrumented pass (once)"
bash bench/run_monitored.sh 5 2>&1 | grep -E "^===|Results written" | tail -12
"$PY" analysis/pull_metrics.py 2>&1 | tail -3

echo
echo "=== row counts"
for f in bench/results/campaign_*.csv; do
  [ -f "$f" ] && echo "$(tail -n +2 "$f" | wc -l) rows  $(basename "$f")"
done
echo "$(tail -n +2 bench/results/monitoring_benchmark_results.csv 2>/dev/null | wc -l) rows  monitoring_benchmark_results.csv"

echo
echo "=== failures across all campaigns"
awk -F, 'FNR>1 && $17!="True" {print $2}' bench/results/campaign_*.csv |
  sort | uniq -c || true

echo
echo "=== done at $(date -u +%H:%M:%S)"
