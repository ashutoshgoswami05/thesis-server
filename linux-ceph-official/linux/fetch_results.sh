#!/usr/bin/env bash
# Brings the VPS's results back under self-describing names. Runs on THIS
# machine, in Git Bash.
#
# Usage:  bash linux/fetch_results.sh user@host [label] [remote_dir]
#   e.g.  bash linux/fetch_results.sh thesis 15-campaigns
#
# Everything lands under vps-results/<date>_<host-class>_<label>/ and NOTHING
# is written into bench/results or tests/results. Those hold the measurements
# the thesis is built from; a run on different hardware is a separate dataset,
# not an extension of them, and merging the two would make every figure in
# Chapter 7 unreproducible.
#
# Files are renamed on arrival, because "campaign_01.csv" in a directory
# called "2026-10-19_1508" says nothing about what it holds. PROVENANCE.txt
# records the original name of each file, since the analysis scripts take
# their input filename as an argument and need the mapping.

set -eu
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

[ $# -ge 1 ] || { echo "usage: bash linux/fetch_results.sh user@host [label] [remote_dir]" >&2; exit 1; }
TARGET=$1
LABEL=${2:-}
REMOTE=${3:-thesis}

echo "fetching from $TARGET:~/$REMOTE"

# Host facts first -- they go into both the directory name and the provenance.
read -r HOSTNAME CORES MEM KERNEL <<<"$(ssh "$TARGET" \
  'echo "$(hostname) $(nproc) $(free -g | awk "/^Mem:/{print \$2}") $(uname -r)"')"
DOCKER=$(ssh "$TARGET" 'docker --version' 2>/dev/null || echo '?')

# "ubuntu-8gb-nbg1-1" -> "8gb"; falls back to the core/memory count.
CLASS=$(echo "$HOSTNAME" | grep -oE '[0-9]+gb' | head -1 || true)
[ -n "$CLASS" ] || CLASS="${CORES}vcpu-${MEM}gb"
[ -n "$LABEL" ] || LABEL="run"

STAMP=$(date +%Y-%m-%d)
DEST="$REPO/vps-results/${STAMP}_hetzner-${CLASS}_${LABEL}"
if [ -e "$DEST" ]; then
  DEST="${DEST}_$(date +%H%M)"
fi
STAGE="$(mktemp -d)"
mkdir -p "$DEST"

ssh "$TARGET" "cd '$REMOTE' && tar -czf - \
      \$( [ -d bench/results ]      && echo bench/results ) \
      \$( [ -d tests/results ]      && echo tests/results ) \
      \$( [ -d crosscheck/results ] && echo crosscheck/results ) 2>/dev/null" \
  | tar -xzf - -C "$STAGE"

# new name <- original name, recorded for PROVENANCE.txt
MAP=""
place () {
  source=$1
  target=$2
  [ -f "$STAGE/$source" ] || return 0
  cp "$STAGE/$source" "$DEST/$target"
  MAP="${MAP}   ${target}  <-  ${source}
"
}

# Repeated campaigns: campaign_07.csv -> campaign-07-of-15_960-runs.csv
total=$(find "$STAGE/bench/results" -name 'campaign_*.csv' 2>/dev/null | wc -l | tr -d ' ')
for path in "$STAGE"/bench/results/campaign_*.csv; do
  [ -f "$path" ] || continue
  base=$(basename "$path")
  index=$(echo "$base" | grep -oE '[0-9]+' | head -1)
  rows=$(( $(tail -n +2 "$path" | wc -l) ))
  place "bench/results/$base" "campaign-${index}-of-${total}_${rows}-runs.csv"
done

# A single unrepeated campaign keeps its own name.
if [ -f "$STAGE/bench/results/linux_campaign.csv" ]; then
  rows=$(( $(tail -n +2 "$STAGE/bench/results/linux_campaign.csv" | wc -l) ))
  place "bench/results/linux_campaign.csv" "campaign-single_${rows}-runs.csv"
fi

place "bench/results/monitoring_benchmark_results.csv" "instrumented-pass_cpu-and-memory.csv"

# Repeated resource passes: resource_usage_07.csv -> resource-pass-07_cadvisor.csv.
# Each pass keeps its own copy because run_monitored.sh replaces
# monitoring_windows.csv and pull_metrics.py replaces resource_usage.csv, so
# without these the whole series would collapse into the final pass.
passes=$(find "$STAGE/bench/results" -name 'resource_usage_*.csv' 2>/dev/null | wc -l | tr -d ' ')
for path in "$STAGE"/bench/results/resource_usage_*.csv; do
  [ -f "$path" ] || continue
  index=$(basename "$path" | grep -oE '[0-9]+' | head -1)
  place "bench/results/resource_usage_${index}.csv" \
        "resource-pass-${index}-of-${passes}_cadvisor.csv"
  place "bench/results/monitoring_windows_${index}.csv" \
        "resource-pass-${index}-of-${passes}_time-windows.csv"
done
place "bench/results/monitoring_windows.csv"            "instrumented-pass_time-windows.csv"
place "bench/results/resource_usage.csv"                "resource-usage_from-cadvisor.csv"
place "bench/results/image_digests.txt"                 "image-digests_as-run.txt"
place "tests/results/compatibility_matrix1.csv"         "fidelity_capability-matrix.csv"
place "tests/results/compatibility_matrix.csv"          "fidelity_capability-matrix.csv"
place "crosscheck/results/native_metrics.csv"           "crosscheck_native-metrics.csv"

# Any NOTE_* a run left behind records something about the data that the
# CSVs cannot show on their own -- a backend reset mid-run, for instance.
# These keep their own names so they stay findable.
for note in "$STAGE"/bench/results/NOTE_*.txt "$STAGE"/tests/results/NOTE_*.txt; do
  [ -f "$note" ] || continue
  place "${note#$STAGE/}" "$(basename "$note")"
done

{
  echo "source   : $TARGET:~/$REMOTE"
  echo "fetched  : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host     : $HOSTNAME, $CORES vCPU, $MEM GB RAM"
  echo "kernel   : $KERNEL"
  echo "docker   : $DOCKER"
  echo
  echo "Measured on different hardware from bench/results. Not comparable;"
  echo "keep separate."
  echo
  echo "Files were renamed on arrival. The analysis scripts take an input"
  echo "filename as an argument, so point them at the name on the left and"
  echo "remember it was produced as the name on the right:"
  echo
  printf '%s' "$MAP"
} > "$DEST/PROVENANCE.txt"

rm -rf "$STAGE"

echo
ls -1 "$DEST" | sed 's/^/   /'
echo
echo "landed in ${DEST#$REPO/}  (bench/results untouched)"
