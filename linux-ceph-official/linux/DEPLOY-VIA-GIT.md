# Deploying to a machine you cannot SSH into

Written for a Proxmox guest reached through the web console. The framework
goes out over git, the results come back over git, and the one file git cannot
carry sensibly is handled separately.

## 1. `linux/` on its own is not enough

The driver scripts live in `linux/`, but what they *run* does not:

```
bench/benchmark.py              the harness itself
analysis/pull_metrics.py        prometheus -> resource_usage.csv
tests/test_api_fidelity.py      the 23 capability tests
tests/config.yaml               endpoints and credentials
infra/*/*.env                   per-backend credentials
infra/seaweedfs/s3.json         seaweedfs identities
monitoring/prometheus.yml        scrape config
requirements.txt                 boto3 etc.
```

Push these directories, which is what `upload.sh` sends:

```
infra  monitoring  tests  bench  analysis  crosscheck  linux
requirements.txt  pytest.ini  README.md
```

Leave out `.venv` (built on the target), `latex/`, `code/`, `thesis-baseline/`
and every existing result file. A `.gitignore` with these keeps it simple:

```
.venv/
latex/
code/
thesis-baseline/
vps-results/
bench/results/
tests/results/
crosscheck/results/
__pycache__/
*.pyc
*.windows
```

## 2. The credentials go with it

`tests/config.yaml`, `infra/garage/garage.env`, `infra/minio/minio.env`,
`infra/rustfs/rustfs.env` and `infra/seaweedfs/s3.json` contain working keys.
They are throwaway keys for containers that were only ever reachable from the
measurement host, but a public repository publishes them permanently and
GitHub's secret scanning will flag them.

**Use a private repository.** If it has to be public, rotate the keys first:
change them in those five files and in nothing else -- the harness reads them
all from there.

The compose files also publish every port on `127.0.0.1` only, so even with
the keys known the backends are not reachable from outside the guest.

## 3. MinIO's image cannot be pulled

`minio/minio` has been withdrawn from Docker Hub -- the whole repository, so
pull-by-digest fails too. The five other backends pull normally by the digests
pinned in the compose files. MinIO has to travel as a file.

On the machine that still has it cached:

```bash
docker save minio/minio:latest minio/warp:latest | gzip -1 > minio-images.tar.gz
```

That is **71 MB**. Attach it to a GitHub release rather than committing it, so
it stays out of the repository history:

```bash
gh release create images-v1 minio-images.tar.gz --notes "minio 14cea493d9a3, warp 72ae1b02216b"
```

`docker load` preserves the registry digest, so the compose file's pinned
`minio/minio@sha256:14cea493d9a3...` still resolves after loading.

If you would rather not move the file, the alternative is to repin
`infra/minio/docker-compose.yml` to a MinIO image that is still published
(quay.io has them). That is a **different build** from the one the thesis
measured, so MinIO's numbers would no longer be comparable with Chapter 7 or
with the earlier VPS runs. Note it explicitly if you do.

## 4. On the new server

```bash
git clone <your private repo> thesis && cd thesis

bash linux/setup.sh            # docker from docker's repo, venv, thesis-net
bash linux/install.sh          # swap the Windows scripts for the Linux ones

# MinIO's image, from the release asset
curl -L -o /tmp/minio-images.tar.gz <release asset url>
gunzip -c /tmp/minio-images.tar.gz | docker load
docker image inspect minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e \
  --format 'minio ok' || echo 'MINIO DIGEST MISSING'

mkdir -p bench/results tests/results crosscheck/results   # upload excludes these
bash linux/reset_volumes.sh initial                       # creates ceph's S3 user
```

`reset_volumes.sh` is what creates the Ceph S3 user. Skip it and every Ceph
run fails with `InvalidAccessKeyId`, which looks like a measurement rather
than a missing user.

Then the runs, detached so a dropped console does not kill them:

```bash
nohup setsid bash linux/run_repeats.sh 15 5 > ~/repeats.log 2>&1 < /dev/null &
tail -f ~/repeats.log
```

`15 5` is fifteen campaigns, resetting every five. For the resource passes:

```bash
nohup setsid bash linux/run_resource_repeats.sh 15 15 1 5 > ~/resources.log 2>&1 < /dev/null &
```

`15 15 1 5` is fifteen passes, fifteen repeats each, numbered from 1,
resetting every five. Run them one after the other, never together: the
campaigns measure wall-clock time on two cores and the monitoring stack is
three more containers competing for them.

## 5. Getting the results back

On the server, put them on their own branch:

```bash
cd ~/thesis
git checkout -b results-$(date +%Y-%m-%d)
git add -f bench/results tests/results crosscheck/results
git commit -m "Added results from the proxmox guest"
git push -u origin HEAD
```

`-f` is needed because `.gitignore` excludes those directories, which is what
you want for the framework push and not for this one.

Back here, **do not check that branch out.** `bench/results/` holds the
measurements the thesis is built from, and a checkout would overwrite them.
Extract instead:

```bash
git fetch origin results-2026-10-24
mkdir -p vps-results/2026-10-24_proxmox
git archive origin/results-2026-10-24 bench/results tests/results |
  tar -x -C vps-results/2026-10-24_proxmox
```

`git archive` writes the files without touching the working tree or the index.
Then confirm the thesis data is untouched:

```bash
python thesis-baseline/baseline.py verify
```

Results are small -- fifteen campaigns came to about 470 KB -- so git is a
reasonable transport for them.

## 6. Proxmox: check whether it is a VM or a container

This matters for the resource passes, not the campaigns.

**A KVM/QEMU VM** behaves like any Ubuntu host. Nothing to do.

**An LXC container** needs `nesting=1` to run Docker at all, and even then
cAdvisor's mounts (`/`, `/sys`, `/var/lib/docker`, `/dev/disk`) resolve against
a namespaced view of the host's cgroups. Memory and CPU figures may be wrong
or missing, and `container_memory_usage_bytes` may report the host's values
rather than the container's. The campaign timings are unaffected because they
are wall-clock measurements taken by the harness itself.

Check which you have:

```bash
systemd-detect-virt
```

`kvm` or `qemu` means a VM, `lxc` means a container. If it is `lxc`, verify
cAdvisor before trusting a four-hour run:

```bash
cd monitoring && docker compose up -d
sleep 30
curl -s 'http://localhost:9090/api/v1/query?query=container_memory_usage_bytes' |
  head -c 300
```

Empty or implausible values mean the resource passes will not produce usable
data on that guest, and the campaigns are still fine.

## 7. Disk

The campaigns peaked at 37 GB of images and volumes on a 75 GB disk. With
100 GB and a reset every five runs there is ample room. The reset also keeps
Ceph's 10 GiB OSD from filling, which is what broke the first attempt at the
resource passes.
