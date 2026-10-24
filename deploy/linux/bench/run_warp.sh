#!/usr/bin/env bash
# Cross-validates the throughput measurements from bench/benchmark.py with
# minio/warp, an independent, third-party S3 benchmarking tool.
#
# Purpose is not to produce a second set of headline numbers. It is to test
# whether the large run-to-run dispersion observed with the purpose-built
# harness is a property of this environment or an artefact of that harness.
# If warp shows comparable dispersion, the variance is environmental; if it
# does not, the harness is implicated.
#
# Usage: bash bench/run_warp.sh <backend>
#   e.g. bash bench/run_warp.sh rustfs
# Requires the named backend to already be running.
#
# Results are written to bench/results/warp/<backend>.txt.

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ $# -lt 1 ]; then
  echo "usage: bash bench/run_warp.sh <backend>" >&2
  exit 1
fi

BACKEND=$1
OUTDIR="$REPO/bench/results/warp"
mkdir -p "$OUTDIR"

# Endpoint (container-internal host:port, no scheme -- warp adds it) and
# credentials per backend. These mirror tests/config.yaml; warp cannot read
# that file, so they are repeated here.
case "$BACKEND" in
  rustfs)    HOST="rustfs:9000";            AK="thesisaccesskey";     SK="thesissecretkey1234";   REGION="us-east-1" ;;
  garage)    HOST="garage:3900";            AK="GKea1446f9810cbd0b8633f9a64361357d"; SK="4c1c9e2085c276979148580c92c4bd14a3fa554efc2ba7f72937d14ccbb4ed7c"; REGION="garage" ;;
  seaweedfs) HOST="seaweedfs-s3:8333";      AK="thesisseaweedaccess"; SK="thesisseaweedsecret123"; REGION="us-east-1" ;;
  juicefs)   HOST="juicefs-gateway:9090";   AK="thesisjuiceaccess";   SK="thesisjuicesecret123";  REGION="us-east-1" ;;
  minio)     HOST="minio:9000";             AK="thesisminioaccess";   SK="thesisminiosecret123";  REGION="us-east-1" ;;
  ceph)      HOST="ceph-aio:8000";          AK="thesiscephaccess";    SK="thesiscephsecret123";   REGION="us-east-1" ;;
  *) echo "unknown backend: $BACKEND" >&2; exit 1 ;;
esac

# Every backend now creates buckets on demand, so warp uses its own default
# bucket, which it creates and clears itself. The JuiceFS gateway needed a
# pre-existing bucket until it was configured with --multi-buckets.
EXTRA=""

# Ceph's RGW applies virtual-host bucket parsing to hostnames but treats a
# bare IP as path-style (the same quirk that made the AWS CLI fail during
# deployment). Addressed by hostname, warp's bucket operations fail with
# "the specified bucket does not exist", so resolve the container's address
# and hand warp the IP literal instead.
if [ "$BACKEND" = "ceph" ]; then
  CEPH_IP=$(docker inspect ceph-aio \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null)
  if [ -n "$CEPH_IP" ]; then
    HOST="$CEPH_IP:8000"
    echo "resolved ceph-aio to $HOST (IP literal required by RGW)"
  fi
fi

# Flag choices, all constrained by this host rather than copied from
# warp's defaults:
#
# --objects=20  warp's default is 2500. At 16MiB per object that is a ~40GB
#               preparation phase, which does not fit and which on the first
#               attempt wrote 2022 objects (~32GB) into the backend before
#               the run was aborted. 20 objects is ~320MB, enough to measure
#               against and small enough to finish.
# --concurrent=4  warp defaults to 20 concurrent streams. On two cores that
#               measures queueing rather than the backend.
# --obj.size=16MiB  matches the largest workload in bench/benchmark.py, so
#               the two tools are doing comparable work.
# --duration=30s  short, because the purpose is a variance cross-check
#               rather than a sustained-throughput figure.
#
# No --noclear: warp should remove its own objects afterwards. The first
# attempt used it and left 2022 objects behind that had to be deleted
# manually.
echo "=== warp mixed: $BACKEND ($HOST) ==="
# --region matters here: Ceph's RGW rejected warp's bucket creation
# outright without it, the same way it rejected the AWS CLI's default
# pseudo-region. The region also forms part of the SigV4 signature, so it
# has to match what each backend expects.
docker run --rm --network thesis-net minio/warp:latest mixed \
  --host="$HOST" \
  --access-key="$AK" \
  --secret-key="$SK" \
  --region="$REGION" \
  --objects=20 \
  --obj.size=16MiB \
  --concurrent=4 \
  --duration=30s \
  $EXTRA 2>&1 | tee "$OUTDIR/$BACKEND.txt"

echo "written to $OUTDIR/$BACKEND.txt"
