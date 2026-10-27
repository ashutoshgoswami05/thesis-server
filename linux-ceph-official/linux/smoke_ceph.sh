#!/usr/bin/env bash
# Checks that the official-image Ceph stack really works before a campaign is
# started against it.
#
# Usage:  bash linux/smoke_ceph.sh
#
# Worth running on its own first. The all-in-one image this replaces brought
# itself up from one entrypoint; here the bring-up lives in
# infra/ceph/entrypoint.sh, so a mistake in it shows as a backend that starts
# and then fails every run -- which in a CSV looks like a measurement.
#
# Exits non-zero on the first failure, and leaves the stack running so the
# logs can be read.

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
PY="$REPO/.venv/bin/python"

fail () {
  echo
  echo "FAILED: $*"
  echo
  echo "--- ceph-mon ---"; docker logs --tail 25 ceph-mon 2>&1
  echo "--- ceph-osd ---"; docker logs --tail 25 ceph-osd 2>&1
  echo "--- ceph-rgw ---"; docker logs --tail 25 ceph-rgw 2>&1
  echo
  echo "The stack is still up. Bring it down with:"
  echo "    cd $REPO/infra/ceph && docker compose down -v"
  exit 1
}

echo "=== 1. starting the stack"
cd "$REPO/infra/ceph" || exit 1
docker compose up -d 2>&1 | tail -5
cd "$REPO" || exit 1

echo
echo "=== 2. waiting for the rgw healthcheck (up to 8 minutes)"
healthy=no
for i in $(seq 1 48); do
  status=$(docker inspect --format '{{.State.Health.Status}}' ceph-rgw 2>&1)
  if [ "$status" = "healthy" ]; then
    healthy=yes
    echo "    healthy after ${i}0s"
    break
  fi
  if [ $((i % 6)) -eq 0 ]; then
    echo "    ${i}0s: $status"
  fi
  sleep 10
done
[ "$healthy" = "yes" ] || fail "ceph-rgw never became healthy"

echo
echo "=== 3. cluster state"
docker exec ceph-rgw ceph -s 2>&1 | sed 's/^/    /' || fail "ceph -s"
echo
docker exec ceph-rgw ceph versions 2>&1 | sed 's/^/    /'

echo
echo "=== 4. the version actually running"
running=$(docker exec ceph-rgw ceph --version 2>&1 | awk '{print $3}')
echo "    $running"
case "$running" in
  19.2.5) echo "    matches the version Chapter 7 measured" ;;
  *)      echo "    NOTE: not 19.2.5 -- record this, Ceph's numbers are not"
          echo "          comparable with Chapter 7 without it" ;;
esac

echo
echo "=== 5. all four daemons present to cAdvisor"
missing=
for c in ceph-mon ceph-mgr ceph-osd ceph-rgw; do
  if [ "$(docker inspect --format '{{.State.Running}}' "$c" 2>&1)" = "true" ]; then
    echo "    $c running"
  else
    echo "    $c NOT running"
    missing="$missing $c"
  fi
done
[ -z "$missing" ] || fail "not running:$missing"

echo
echo "=== 6. S3 user"
if docker exec ceph-rgw radosgw-admin user info --uid=thesisuser >/dev/null 2>&1; then
  echo "    thesisuser already present"
else
  docker exec ceph-rgw radosgw-admin user create --uid=thesisuser \
    --display-name="Thesis Test User" \
    --access-key=thesiscephaccess --secret-key=thesiscephsecret123 \
    >/dev/null 2>&1 || fail "could not create thesisuser"
  echo "    thesisuser created"
fi

echo
echo "=== 7. S3 round-trip through boto3, the client the harness uses"
"$PY" - <<'ENDPY' || fail "the S3 round-trip did not complete"
import sys

import boto3
from botocore.client import Config

client = boto3.client(
    "s3",
    endpoint_url="http://127.0.0.1:8000",
    aws_access_key_id="thesiscephaccess",
    aws_secret_access_key="thesiscephsecret123",
    region_name="us-east-1",
    config=Config(signature_version="s3v4"),
)

bucket = "smoke-test-bucket"
body = b"x" * 1024

client.create_bucket(Bucket=bucket)
print("    created bucket")
client.put_object(Bucket=bucket, Key="probe", Body=body)
print("    uploaded 1 KB")
got = client.get_object(Bucket=bucket, Key="probe")["Body"].read()
if got != body:
    print("    downloaded object does not match what was uploaded")
    sys.exit(1)
print("    downloaded and verified")
client.delete_object(Bucket=bucket, Key="probe")
client.delete_bucket(Bucket=bucket)
print("    deleted object and bucket")
ENDPY

echo
echo "=== 8. capability suite for ceph only"
"$PY" -m pytest tests/test_api_fidelity.py -q -k ceph 2>&1 | tail -5

echo
echo "=== 9. osd usage"
docker exec ceph-rgw ceph df 2>&1 | sed 's/^/    /'

echo
echo "=== smoke test passed"
echo "the stack is still up; bring it down before a campaign with:"
echo "    cd $REPO/infra/ceph && docker compose down"
