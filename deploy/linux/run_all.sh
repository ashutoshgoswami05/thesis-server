#!/usr/bin/env bash
# The full VPS run: fidelity suite, plain campaign, instrumented pass.
#
# Run detached:
#   nohup setsid bash linux/run_all.sh > ~/run_all.log 2>&1 < /dev/null &
#
# Every compose file is pinned to an image digest. Five backends run the exact
# images the thesis measured; Ceph runs 19.2.7 because the 19.2.5 manifest the
# thesis used is no longer distributed. The digests actually used are recorded
# in bench/results/image_digests.txt so the two are never confused later.

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

echo "=== image digests in use  $(date -u +%H:%M:%S)"
{
  echo "# Images used for this run, resolved at run time."
  echo "# Recorded because four of the seven drifted between the thesis"
  echo "# measurements and a fresh pull; see the Digest column of the"
  echo "# thesis's Chapter 6 table for what it measured."
  echo
  grep -h "image:" infra/*/docker-compose.yml monitoring/docker-compose.yml |
    sed 's/^ *image: //' | sort -u
  echo
  echo "ceph: 19.2.7, NOT the thesis's 19.2.5 (manifest 876d38c8eaf6 withdrawn)"
  echo "host: $(nproc) vCPU, $(free -g | awk '/^Mem:/{print $2}') GB RAM, $(uname -r)"
} > bench/results/image_digests.txt
cat bench/results/image_digests.txt

# Ceph's S3 user lives in the cluster's own volumes, which survive a compose
# down, but the volume may be fresh. Create it only if it is missing.
echo
echo "=== ceph s3 user  $(date -u +%H:%M:%S)"
cd "$REPO/infra/ceph" || exit 1
docker compose up -d >/dev/null 2>&1
for _ in $(seq 1 24); do
  [ "$(docker inspect --format '{{.State.Health.Status}}' ceph-aio 2>&1)" = "healthy" ] && break
  sleep 10
done
if docker exec ceph-aio radosgw-admin user info --uid=thesisuser >/dev/null 2>&1; then
  echo "thesisuser already exists"
else
  docker exec ceph-aio radosgw-admin user create --uid=thesisuser \
    --display-name="Thesis Test User" \
    --access-key=thesiscephaccess --secret-key=thesiscephsecret123 >/dev/null 2>&1 &&
    echo "thesisuser created"
fi
docker exec ceph-aio ceph health 2>&1 | tail -1
docker exec ceph-aio radosgw-admin gc process --include-all >/dev/null 2>&1
docker compose down >/dev/null 2>&1
cd "$REPO" || exit 1

echo
echo "=== fidelity suite  $(date -u +%H:%M:%S)"
bash tests/run_fidelity_all.sh

echo
echo "=== plain campaign  $(date -u +%H:%M:%S)"
bash bench/run_all_backends.sh linux_campaign.csv

echo
echo "=== monitoring stack  $(date -u +%H:%M:%S)"
cd "$REPO/monitoring" || exit 1
docker compose up -d 2>&1 | tail -2
cd "$REPO" || exit 1
sleep 30
echo -n "prometheus ready: "
curl -s http://localhost:9090/-/ready || echo "NOT READY"
echo

echo "=== instrumented pass  $(date -u +%H:%M:%S)"
bash bench/run_monitored.sh 5

echo
echo "=== pull metrics  $(date -u +%H:%M:%S)"
.venv/bin/python analysis/pull_metrics.py 2>&1 | tail -12

echo
echo "=== row counts"
for f in bench/results/linux_campaign.csv \
         bench/results/monitoring_benchmark_results.csv \
         bench/results/resource_usage.csv \
         tests/results/compatibility_matrix1.csv; do
  [ -f "$f" ] && echo "$(tail -n +2 "$f" | wc -l) rows  $f"
done

echo
echo "=== failures in the campaign"
awk -F, 'NR>1 && $17!="True" {print $2}' bench/results/linux_campaign.csv |
  sort | uniq -c || echo "none"

echo
echo "=== done at $(date -u +%H:%M:%S)"
