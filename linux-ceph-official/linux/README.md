# Running the framework on Linux

The measurement code is portable; only the driver scripts carry Windows
paths. This folder holds Linux versions of those nine files and two helpers
to set a machine up.

## What actually differs

| | Windows (this machine) | Linux |
|---|---|---|
| Repository root | `REPO=/d/Thesis`, hardcoded | derived from the script's own location |
| Interpreter | `.venv/Scripts/python.exe` | `.venv/bin/python` |
| Docker daemon | inside a WSL 2 VM | on the host |
| `/sys`, `/var/run` seen by cAdvisor | the VM's | the host's own |
| Harness → backend path | host → port proxy → WSL 2 → container | host → container |

The compose files need no change to *work* — relative bind mounts, named
volumes and the external network behave identically, and cAdvisor reads the
host's cgroups directly rather than through the WSL translation layer. They
do need one change to be *safe*: on this laptop `- "9000:9000"` binds
0.0.0.0 behind a home router, while on a VPS it offers the backend to the
internet with the throwaway keys that sit in `tests/config.yaml`. The copies
here bind all 23 published ports to `127.0.0.1`.

The harness reaches the backends over localhost and `warp` reaches them over
`thesis-net` by container name, so neither is affected. To open Grafana or
Prometheus from your laptop, tunnel instead of publishing:

```bash
ssh -L 3000:localhost:3000 -L 9090:localhost:9090 thesis
```

`tests/config.yaml` and every analysis script work unaltered.

The last two rows are worth noting for the results rather than the setup.
Removing the port proxy and the VM takes out two of the three candidate
explanations for the dispersion reported in Chapter 7, which is what makes a
Linux run interesting rather than merely convenient.

## Files

```
setup.sh      installs Docker from Docker's repository, creates the venv and thesis-net
install.sh    copies the nine converted files into place, keeping the originals as *.windows
bench/        five drivers
tests/        the fidelity driver
crosscheck/   the native-metrics cross-check driver
analysis/     the two scripts that hardcoded D:/Thesis
infra/        six compose files, every port bound to 127.0.0.1
monitoring/   the same, for cAdvisor, Prometheus and Grafana
upload.sh     sends the framework to the VPS        (run on Windows)
run_all.sh    the whole run: fidelity, campaign, monitoring, instrumented pass
run_repeats.sh    N campaigns, one CSV each, resetting volumes every 5
run_resource_repeats.sh  N instrumented passes, one resource CSV each
reset_volumes.sh  wipes every backend and recreates ceph s3 user
DEPLOY-VIA-GIT.md steps for a machine you cannot ssh into
fetch_results.sh  brings results into vps-results/  (run on Windows)
```

## Order

```bash
git clone <repo> thesis && cd thesis
bash linux/setup.sh          # docker, venv, network   (log out and in after this)
bash linux/install.sh        # put the Linux scripts in place
```

Then either run the whole thing with one driver:

```bash
nohup setsid bash linux/run_all.sh > ~/run_all.log 2>&1 < /dev/null &
```

which records the image digests, creates Ceph's S3 user if it is missing,
and runs the fidelity suite, the 960-run campaign, the monitoring stack and
the instrumented pass in that order -- about 17 minutes on 2 vCPU. Or run the
stages by hand:

```bash
bash tests/run_fidelity_all.sh          # 23 tests per backend
bash bench/run_all_backends.sh          # 960 runs
cd monitoring && docker compose up -d
bash bench/run_monitored.sh             # instrumented pass
python analysis/pull_metrics.py         # windows -> resource_usage.csv
```

## Images drift, and pinning them is not enough

Four of the seven images had moved between the thesis measurements and a
fresh pull: RustFS, SeaweedFS and Ceph were on `latest` (SeaweedFS had no tag
at all), and so had the Redis the JuiceFS stack uses. Only Garage and JuiceFS,
the two pinned to a real version, still resolved to what the thesis measured.
Every compose file is now pinned to `image@sha256:...`, taken from the Digest
column of the thesis's Chapter 6 table.

Two of those digests can no longer be fetched at all. `minio/minio` has been
withdrawn from Docker Hub entirely -- repository and all -- so MinIO has to be
carried over from a machine that still has it cached:

```bash
docker save minio/minio:latest minio/warp:latest | ssh thesis docker load
```

`docker load` preserves the registry digest, so the pinned reference resolves
afterwards. Ceph's 19.2.5 manifest is gone from quay.io and no surviving dated
tag holds it, so the copy here is pinned to 19.2.7 instead. Ceph is the one
backend where a difference against Chapter 7 cannot be blamed on the hardware.

**Wipe the volumes whenever you change an image.** A pinned image on a volume
an older or newer build already wrote is still a mixed system. Running the
thesis's RustFS over a volume a newer RustFS had initialised produced

```
pool metadata load failed: unknown version 3
```

and RustFS then stayed up, listened on 9000 and answered HTTP 503 to all 160
runs -- it does not migrate the format, warn, or exit. The campaign recorded
160 connection errors rather than anything about RustFS's speed. So:

```bash
for b in rustfs garage seaweedfs juicefs minio ceph; do
  (cd infra/$b && docker compose down -v)
done
docker volume prune -f
```

`run_all.sh` writes `bench/results/image_digests.txt` with the digests it
actually used, so a result set can always be matched back to the software
that produced it.

## Before the first campaign

**Create the Ceph S3 user once**, on a fresh volume:

```bash
docker exec ceph-rgw radosgw-admin user create --uid=thesisuser \
  --display-name="Thesis Test User" \
  --access-key=thesiscephaccess --secret-key=thesiscephsecret123
```

**Watch the disk.** Images and volumes reached 38.5 GB over one week here,
and Ceph's OSD filling to 97.8 % silently broke two measurement passes. If
RGW starts returning `InsufficientCapacity`, reclaim the space with
`docker exec ceph-rgw radosgw-admin gc process --include-all` rather than
deleting the volume.

**Credentials.** `tests/config.yaml`, `infra/*/*.env`, `infra/seaweedfs/s3.json`
and `monitoring/prometheus.yml` carry throwaway keys for containers that were
reachable only from the measurement host. On a VPS with a public address they
are reachable from the internet. Either bind the published ports to
`127.0.0.1` in the compose files, or put the whole machine behind a firewall
that allows only your own address.

## The results will not be comparable

Different CPU, different storage, no virtualised network. A Linux run starts
a new dataset rather than extending the one in `bench/results/`. Write it to
its own filenames:

```bash
bash bench/run_all_backends.sh linux_campaign.csv
```

`analysis/summarize_benchmarks.py` and `analysis/compare_backends.py` accept
an input filename, but write to fixed outputs — so they will overwrite
`benchmark_summary.csv` and `comparisons.csv`, which the thesis tables are
built from. Copy those two aside first, or run the analysis on a separate
checkout.
