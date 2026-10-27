# The official-image Ceph stack

This is a complete, self-contained copy of the measurement framework in which
the Ceph backend runs `quay.io/ceph/ceph` -- the build the Ceph project
publishes itself -- instead of the third-party all-in-one image the rest of
the study used. Everything else is unchanged, because a comparison only holds
if all six backends run in the same campaign on the same machine.

Deploy it on its own host. It is not an overlay on the existing checkout, and
the two should not share a machine.

## Why it exists

The study measured Ceph through `quay.io/benjamin_holmes/ceph-aio`, a personal
repository. Its 19.2.5 manifest, the digest recorded in the Chapter 6 image
table, was withdrawn from quay.io part-way through the work and cannot be
pulled on a new machine, which is why the Proxmox runs fell back to 19.2.7.
That is the one backend where a difference against Chapter 7 cannot be put
down to the hardware.

`quay.io/ceph/ceph` has the matching 19.2.5 build and dated tags that cannot
move, so it removes that risk. What it does not have is any way to start
itself.

## What the official image actually gives you

```
Entrypoint = None
Cmd        = ['/bin/bash']
```

No orchestration whatsoever. `docker compose up` on it starts a shell, which
exits. The image is the Ceph binaries on CentOS Stream 9, and `cephadm` is
expected to drive it.

`cephadm` was not used here for three reasons. It wants systemd on the host
and creates its own unit files, so the cluster would no longer be a Compose
stack brought up and torn down around each backend's turn. It deploys its own
Prometheus, Grafana, Alertmanager and node-exporter unless told not to, which
would compete with `monitoring/` for the same two cores and the same ports.
And it names containers `ceph-<fsid>-<daemon>-<host>`, where the fsid changes
on every bootstrap, so `analysis/pull_metrics.py` could not name the
containers it needs to sum.

So the bring-up lives in `infra/ceph/entrypoint.sh` instead: one script, with
the daemon's role as its argument.

| Service | Role | What it does on first start |
|---|---|---|
| `ceph-mon` | `mon` | writes `ceph.conf`, the admin and monitor keyrings and the monmap, runs `ceph-mon --mkfs`, then starts the monitor |
| `ceph-mgr` | `mgr` | registers `mgr.a`, starts the manager |
| `ceph-osd` | `osd` | `ceph osd new`, then `ceph-osd --mkfs` against a BlueStore file |
| `ceph-rgw` | `rgw` | registers `client.rgw.a`, starts `radosgw` on port 8000 |

`/etc/ceph` and `/var/lib/ceph` are shared volumes, so all four see the same
config and keyrings. On later starts only the monitor address is refreshed.

## What changed from the all-in-one stack

**Four containers, not one.** `analysis/pull_metrics.py` sums
`ceph-mon`, `ceph-mgr`, `ceph-osd` and `ceph-rgw`, the same way it already
sums SeaweedFS's four. Resource figures therefore cover the same processes as
before, just measured separately. A per-daemon breakdown is available in
Prometheus if it is wanted.

**`ceph-rgw` is the container the drivers talk to.** Every
`docker exec ceph-aio ...` became `docker exec ceph-rgw ...`, and the
readiness checks watch its healthcheck. That healthcheck requires the whole
cluster to be usable rather than just the process to be alive: monitor
reachable, at least one OSD up, and the S3 port answering.

**The readiness waits are longer.** 480 seconds instead of 180-300. The
all-in-one image came up in 70-80 seconds; this stack provisions a monitor
store and an OSD on first start after a wipe, and a wait that expires does
not abort the campaign, it records failures.

**One OSD, one replica.** `POOL_SIZE=1` in `ceph.env`. A single OSD cannot
hold Ceph's default three replicas, and a pool that cannot reach its replica
count stops accepting writes. The official `ceph/demo` image sets
`osd pool default size = 2` and defaults to one OSD, which leaves its pools
undersized; this is the honest single-OSD setting instead. **Write
throughput is therefore not directly comparable with a replicated
deployment**, and that is worth stating wherever these numbers appear.

**A 20 GB OSD, not 10 GB.** The old one filled inside a campaign, which is
why the drivers run `radosgw-admin gc process --include-all` between
campaigns and abort at 85%. Both are kept; the larger OSD just means the
abort is less likely to fire. 20 GB also fits beside the other five backends'
volumes on a 75 GB disk, which already peaked near 37 GB.

**No memory tuning.** Nothing sets `osd_memory_target`. The settings in
`ceph.conf` are only the ones without which a one-node cluster does not come
up at all, so what gets measured is Ceph's own defaults.

## Running it

```bash
bash linux/setup.sh            # docker, venv, thesis-net
bash linux/install.sh          # swap the Windows scripts for the Linux ones
```

MinIO's image cannot be pulled -- the repository was withdrawn from Docker Hub
-- so it has to be loaded from the archive:

```bash
gunzip -c /tmp/minio-images.tar.gz | docker load
docker image inspect minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e \
  --format 'minio ok' || echo 'MINIO DIGEST MISSING'
```

Then, before anything else, check that Ceph works:

```bash
mkdir -p bench/results tests/results crosscheck/results
bash linux/smoke_ceph.sh
```

That brings the stack up, prints `ceph -s`, confirms the running version,
checks all four daemons, creates the S3 user, and does a real
create/put/get/delete round-trip through boto3 -- the same client the harness
uses. **Do not start a campaign until it passes.** The bring-up is written
here rather than inherited from an image, and a backend that starts but does
not serve looks like a measurement rather than a broken setup.

Once it passes:

```bash
cd infra/ceph && docker compose down && cd ..
bash linux/reset_volumes.sh initial

nohup setsid bash linux/run_repeats.sh 15 5 > ~/repeats.log 2>&1 < /dev/null &
tail -f ~/repeats.log
```

Then, and only after the campaigns have finished:

```bash
nohup setsid bash linux/run_resource_repeats.sh 15 15 1 5 > ~/resources.log 2>&1 < /dev/null &
```

## Reading the results

Ceph's figures from this stack are not interchangeable with Chapter 7's. The
image is different, the version may be, the replica count is one, and the host
is different. What the stack supports is a comparison *within itself*: six
backends, same campaign, same machine.

If the version check in step 4 of the smoke test reports anything other than
19.2.5, record it. That is the single number which decides whether Ceph's
results here can be set beside the original campaign at all.
