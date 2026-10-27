#!/usr/bin/env bash
# Copies the Linux versions of the scripts over the Windows ones.
#
# Usage:  bash linux/install.sh [--dry-run]
#
# The originals are kept with a .windows suffix, so the same checkout can be
# moved back if needed. Only files carrying a Windows path are replaced; the
# compose files, tests/config.yaml and the monitoring stack are unchanged and
# are not touched.

set -eu
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
DRY=${1:-}

FILES="
bench/run_all_backends.sh
bench/run_monitored.sh
bench/run_post_campaign.sh
bench/run_warp.sh
bench/run_warp_all.sh
tests/run_fidelity_all.sh
crosscheck/run_crosscheck.sh
analysis/verify_thesis_numbers.py
analysis/audit_floats_and_citations.py
infra/ceph/docker-compose.yml
infra/ceph/entrypoint.sh
infra/ceph/ceph.env
infra/garage/docker-compose.yml
infra/juicefs/docker-compose.yml
infra/minio/docker-compose.yml
infra/rustfs/docker-compose.yml
infra/seaweedfs/docker-compose.yml
monitoring/docker-compose.yml
"

for relative in $FILES; do
  source="$HERE/$relative"
  target="$REPO/$relative"
  [ -f "$source" ] || { echo "missing in linux/: $relative"; continue; }

  if [ "$DRY" = "--dry-run" ]; then
    echo "would install $relative"
    continue
  fi

  if [ -f "$target" ] && [ ! -f "$target.windows" ]; then
    cp "$target" "$target.windows"
  fi
  cp "$source" "$target"
  case "$relative" in *.sh) chmod +x "$target" ;; esac
  echo "installed $relative"
done

[ "$DRY" = "--dry-run" ] || cat <<'DONE'

done. The originals are beside them as *.windows.

check nothing Windows-specific remains:
   grep -rn "/d/Thesis\|D:/Thesis\|Scripts/python" bench tests analysis crosscheck --include=*.sh --include=*.py
DONE
