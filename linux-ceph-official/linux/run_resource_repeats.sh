#!/usr/bin/env bash
# Repeats the instrumented pass N times, keeping each pass's resource figures.
#
# Usage:  bash linux/run_resource_repeats.sh [N] [repeats_per_pass]
#         defaults: 10 passes, 15 repeats per pass
#   detached:
#     nohup setsid bash linux/run_resource_repeats.sh > ~/resources.log 2>&1 < /dev/null &
#
# run_monitored.sh replaces monitoring_windows.csv and pull_metrics.py
# replaces resource_usage.csv, so a bare repeat would overwrite its own
# results. Each pass's two files are copied aside under its own index.
#
# 15 repeats per pass rather than the usual 5 is deliberate. Prometheus
# scrapes every 5 s, and the 5-repeat passes produced windows of 7-41 s --
# 2 to 9 samples. increase() over 2 samples cannot work, which is why
# cpu_seconds came back 0.0 for RustFS and JuiceFS. Tripling the repeats
# triples the window and gives increase() something to integrate over.

set -u
PASSES=${1:-10}
REPEATS=${2:-15}
# Third argument lets a few passes be redone without overwriting an existing
# series: `run_resource_repeats.sh 5 15 11` writes passes 11 to 15.
START=${3:-1}
# Wipe every backend's storage after each block of this many passes; 0
# disables it. See reset_volumes.sh for why three of the six need it.
RESET_EVERY=${4:-5}
LAST=$((START + PASSES - 1))
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
PY="$REPO/.venv/bin/python"

echo "=== $PASSES instrumented passes ($START to $((START + PASSES - 1))), $REPEATS repeats each  $(date -u +%H:%M:%S)"

echo "=== monitoring stack"
cd "$REPO/monitoring" || exit 1
docker compose up -d 2>&1 | tail -3
cd "$REPO" || exit 1

# Prometheus must already be scraping when the first window opens, or the
# first backend's series starts partway through its own measurement.
sleep 30
echo -n "prometheus ready: "
curl -s http://localhost:9090/-/ready || echo "NOT READY"
echo
echo -n "targets up: "
curl -s 'http://localhost:9090/api/v1/targets?state=active' |
  grep -o '"health":"up"' | wc -l

for i in $(seq "$START" $((START + PASSES - 1))); do
  n=$(printf '%02d' "$i")
  echo
  echo "=== resource pass $n of $PASSES   $(date -u +%H:%M:%S)"
  bash bench/run_monitored.sh "$REPEATS" 2>&1 | grep -E "^===|Results written" | tail -8

  "$PY" analysis/pull_metrics.py 2>&1 | tail -2
  cp bench/results/resource_usage.csv     "bench/results/resource_usage_$n.csv"
  cp bench/results/monitoring_windows.csv "bench/results/monitoring_windows_$n.csv"

  # Window length is the thing that decides whether the CPU column means
  # anything, so report it rather than discovering it afterwards.
  echo "windows (s) and samples this pass:"
  awk -F, 'NR>1 {print "   "$1}' bench/results/monitoring_windows_$n.csv |
    paste -sd' ' -
  "$PY" - <<'PYEOF'
import csv
rows = list(csv.DictReader(open("bench/results/resource_usage.csv", newline="")))
for r in rows:
    print(f"   {r['backend']:10s} window {r['window_seconds']:>4s}s  "
          f"cpu_samples {r['cpu_samples']:>3s}  cpu_seconds {r['cpu_seconds']:>7s}  "
          f"mem_mean {r['mean_memory_MB']:>8s} MB")
PYEOF
  echo "disk: $(df -h "$REPO" | awk 'NR==2{print $4" free"}')"

  # Ceph's RGW defers deletion, and 120 runs per pass of create/write/delete
  # leave the space held. Without this the 10 GiB OSD fills, the RGW exits
  # with status 1 and every later Ceph run fails on a closed connection --
  # which is what happened to passes 11 and 12 of the first attempt. The
  # campaign driver has always done this; this one did not.
  cd "$REPO/infra/ceph" || exit 1
  docker compose up -d >/dev/null 2>&1
  for _ in $(seq 1 48); do
    [ "$(docker inspect --format '{{.State.Health.Status}}' ceph-rgw 2>&1)" = "healthy" ] && break
    sleep 10
  done
  docker exec ceph-rgw radosgw-admin gc process --include-all >/dev/null 2>&1
  used=$(docker exec ceph-rgw ceph df 2>/dev/null | awk '/^TOTAL/{print $NF}')
  echo "ceph osd after gc: ${used:-?}% used"
  docker compose down >/dev/null 2>&1
  cd "$REPO" || exit 1
  case "$used" in
    ''|*[!0-9.]*) : ;;
    *) if [ "${used%%.*}" -ge 85 ]; then
         echo "ABORTING: ceph osd at ${used}% -- later passes would be invalid"
         break
       fi ;;
  esac

  # Three backends degrade under sustained bucket churn -- SeaweedFS stops
  # accepting writes, Garage's bucket metadata grows, Ceph's OSD fills -- so
  # storage goes back to empty every RESET_EVERY runs. Each block either side
  # of a reset is its own series; reset_volumes.sh appends the boundary to
  # bench/results/NOTE_volume_resets.txt so the CSVs carry the explanation.
  if [ "$RESET_EVERY" -gt 0 ] && [ $((i % RESET_EVERY)) -eq 0 ] && [ "$i" -lt "$LAST" ]; then
    bash "$REPO/linux/reset_volumes.sh" "after pass $n" || exit 1
  fi
done

echo
echo "=== files written"
ls -1 bench/results/resource_usage_*.csv bench/results/monitoring_windows_*.csv 2>/dev/null | sed 's/^/   /'

echo
echo "=== cpu_seconds == 0 anywhere (window too short to integrate)"
for f in bench/results/resource_usage_*.csv; do
  awk -F, -v f="$f" 'NR>1 && $6=="0.0" {print "   "f"  "$1}' "$f"
done
echo "(nothing listed above means every pass produced usable CPU figures)"

echo
echo "=== done at $(date -u +%H:%M:%S)"
