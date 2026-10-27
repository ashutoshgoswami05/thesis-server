# Beyond MinIO — comparative evaluation of lightweight S3-compatible object stores

Supporting code and data for a master's thesis comparing five S3-compatible
object storage systems in a resource-constrained environment, across three
axes: **API fidelity**, **throughput**, and **CPU and memory consumption**.

Systems compared: **SeaweedFS**, **Garage**, **RustFS**, **JuiceFS** and
**Ceph** (standing in for MicroCeph — see the note at the end). **MinIO** is
deployed alongside as the reference baseline, not as a sixth candidate.

Everything in this repository regenerates the numbers in the thesis from the
raw result files. No figure in the thesis is typed in by hand:
`analysis/make_result_tables.py` writes the LaTeX tables and
`analysis/build_comparison.py` writes the figures.

## Measurement host

| | |
|---|---|
| CPU | Intel Core i3-1115G4, 2 physical cores, 4 hardware threads |
| Host memory | 8 GB (7.9 GB addressable) |
| Memory reaching containers | 3.7 GB, the WSL 2 default share |
| Container storage | Docker named volumes on the WSL 2 virtual disk |
| OS | Windows 11 Home Single Language |
| Container runtime | Docker 28.3.3, Compose v2.39.2, Docker Desktop on WSL 2 |
| Python | 3.13.14 |
| S3 client | boto3 1.43.51 / botocore 1.43.51 |
| Test framework | pytest 9.1.1 |
| Metrics | cAdvisor v0.55.1, Prometheus v3.13.1 |
| Independent cross-check | minio/warp 1.3.1, build `143bf4f` |

Backends are run **one at a time**. Several together do not fit in 3.7 GB,
and they would compete for the same four hardware threads.

## Image digests

Three backends are published under a tag that moves, so the tag alone does
not identify what was measured. The exact builds behind every number in the
thesis are recorded in `bench/results/image_digests.csv`:

| Backend | Image | Tag | Digest |
|---|---|---|---|
| RustFS | `rustfs/rustfs` | `latest` | `sha256:60f4f2f41ce9…` |
| Garage | `dxflrs/garage` | `v2.3.0` | `sha256:866bd13ed203…` |
| SeaweedFS | `chrislusf/seaweedfs` | `latest` | `sha256:52194fba4fec…` |
| JuiceFS | `juicedata/mount` | `ce-v1.1.2` | `sha256:7401035624a1…` |
| Ceph | `quay.io/benjamin_holmes/ceph-rgw` | `v19` (Squid) | `sha256:876d38c8eaf6…` |
| MinIO | `minio/minio` | `latest` | `sha256:14cea493d9a3…` |

To reproduce exactly, pull by digest rather than by tag:

```bash
docker pull rustfs/rustfs@sha256:60f4f2f41ce95216f8cac676e69f9d90c0bfec458a3bc7fd7fb9b7c2452ac57a
```

## Layout

```
infra/<backend>/     one docker-compose.yml per backend
monitoring/          Prometheus + Grafana + cAdvisor
tests/               API fidelity suite (pytest), 23 tests
bench/               throughput harness and campaign drivers
analysis/            metrics extraction, statistics, tables and charts
latex/               the thesis itself
```

Credentials in this repository are throwaway values for containers reachable
only from the measurement host. `latex/sync_listings.sh` replaces them with
`<REDACTED>` before any source file is reproduced in the thesis.

## Setup

Requires Docker Desktop and Python 3.13.

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
docker network create thesis-net
```

`thesis-net` is a shared external network that every backend's compose file
attaches to; it only needs creating once.

## Running a backend

```bash
cd infra/rustfs && docker compose up -d
```

Swap `rustfs` for `garage`, `seaweedfs`, `juicefs`, `minio` or `ceph`.
Three need extra care:

- **ceph** takes 60–90 s to bootstrap. Wait for its healthcheck:
  ```bash
  docker inspect --format '{{.State.Health.Status}}' ceph-rgw
  ```
  On a fresh volume it also needs its S3 user created once:
  ```bash
  docker exec ceph-rgw radosgw-admin user create --uid=thesisuser \
    --display-name="Thesis Test User" \
    --access-key=thesiscephaccess --secret-key=thesiscephsecret123
  ```
  Address it by IP literal, not by hostname: RGW applies virtual-host bucket
  parsing to hostnames.
- **seaweedfs** must be started with `infra/seaweedfs/s3.json` mounted. Its
  S3 gateway verifies signatures only when identities are configured; with no
  identity file it accepts any credentials and every capability result taken
  from it would describe an unauthenticated service. The compose file passes
  `-config=/etc/seaweedfs/s3.json`.
- **juicefs** runs a one-shot `juicefs-format` init container before its
  gateway starts; `docker compose up -d` handles the ordering. The gateway
  runs with `--multi-buckets --keep-etag`; without the first it exposes
  exactly one bucket named after the filesystem.

Stop with `docker compose down` in the same directory. Data volumes persist;
only containers are removed.

Endpoints and credentials for all backends live in `tests/config.yaml`, the
single source of truth for both the test suite and the harness. A backend is
exercised only if it is `enabled: true` there.

## Reproducing the results

The four steps below produce every number in the thesis, in order. The full
sequence takes roughly three hours of machine time.

### 1. API fidelity

```bash
bash tests/run_fidelity_all.sh
```

Brings each backend up alone, runs the 23 tests against it, and stops it
again. To run one backend by hand:

```bash
./.venv/Scripts/python.exe -m pytest tests/ -k rustfs
```

Results are written to `tests/results/compatibility_matrix.csv`. That file
**merges** across runs keyed by `(backend, feature)`, so testing backends one
at a time builds the matrix up rather than overwriting it.

The suite distinguishes assertions from capability probes. An assertion
checks behaviour the S3 interface requires, so a failure is a defect in the
system. A probe records whether an optional capability is present, and either
answer is a valid result. Probes verify observable behaviour rather than the
acceptance of a call: versioning is checked by reading a superseded version
back by its id, Object Lock by attempting to delete a retained object, a
bucket policy by fetching the object it makes public without credentials.

`test_wrong_secret_key_is_rejected` is the negative control. It must pass for
every backend, because every other result depends on the server actually
verifying signatures.

### 2. Throughput campaign

```bash
bash bench/run_all_backends.sh
```

Brings each backend up alone, runs the full workload matrix against it, and
stops it again, so backends never compete for the machine. The matrix is
eight workloads (1 KB ×10, 100 KB ×10, 1 MB ×5, 16 MB ×2, each sequential and
concurrent) repeated 20 times, giving 960 runs, appended to
`bench/results/benchmark_results.csv`.

Three properties of the campaign matter for the figures:

- Each workload is run once, unrecorded, when a backend starts, so cold
  caches fall outside the reported runs.
- Each repeat runs all eight workloads once in an order drawn from
  `SHUFFLE_SEED = 20260912`, not in blocks. Running every repeat of one
  workload before the next charges drift over the campaign to whichever
  workload runs last, which is what produced an apparent concurrency penalty
  in an earlier version of this study.
- Checksums are compared after the timer stops, so download figures measure
  the transfer and not the client's hashing speed.

Single backend, or a custom output file:

```bash
./.venv/Scripts/python.exe bench/benchmark.py rustfs
./.venv/Scripts/python.exe bench/benchmark.py rustfs other.csv
```

### 3. Instrumented pass for CPU and memory

```bash
cd monitoring && docker compose up -d
bash bench/run_monitored.sh
```

- Prometheus: http://localhost:9090 (check http://localhost:9090/targets)
- Grafana: http://localhost:3000
- cAdvisor: http://localhost:8081

The driver records each backend's window into
`bench/results/monitoring_windows.csv` (`backend,start_time,end_time`, ISO-8601
UTC) and writes runs to `monitoring_benchmark_results.csv`. Instrumented runs
must never be mixed into the clean throughput file: the monitoring stack
competes for the same CPU.

Then extract the metrics:

```bash
./.venv/Scripts/python.exe analysis/pull_metrics.py
```

This queries Prometheus for each window and writes
`bench/results/resource_usage.csv`: mean and peak memory, peak swap, CPU time
in core-seconds, the window length, and the number of samples behind each
figure. **CPU time, not a mean rate, is the comparable figure** — a mean rate
falls as a window lengthens, so a slower backend can appear cheaper than a
faster one. The scrape interval is 5 s; at Prometheus's 15 s example value the
shortest windows here would hold two or three samples.

### 4. Statistics, tables and figures

```bash
./.venv/Scripts/python.exe analysis/summarize_benchmarks.py   # per-workload statistics
./.venv/Scripts/python.exe analysis/compare_backends.py       # significance and effect size
./.venv/Scripts/python.exe analysis/sensitivity.py            # dependence on repeat count and correction family
./.venv/Scripts/python.exe analysis/instrument_agreement.py   # rank correlation between harness and warp
./.venv/Scripts/python.exe analysis/parse_warp.py             # independent cross-check
./.venv/Scripts/python.exe analysis/build_comparison.py       # charts
./.venv/Scripts/python.exe analysis/make_result_tables.py     # LaTeX tables
```

`compare_backends.py` writes `bench/results/comparisons.csv`: a bootstrap 95 %
confidence interval for each difference of medians, a Mann-Whitney U p-value
corrected by the Holm method across all 58 comparisons, and Cliff's delta. A
difference is reported as supported only when the corrected p-value is below
0.05 **and** the interval excludes zero.

`sensitivity.py` re-applies that rule to the first 5, 10 and 15 repeats, and
with the correction split into families of 10 and 48. It shows that five
repeats cannot support anything, that the supported set is still growing at
twenty, and which three differences would pass under a narrower family.
`instrument_agreement.py` computes Spearman's rank correlation, with an exact
p-value, between the harness's and warp's orderings of the six backends.

The coefficient of variation reported alongside each throughput figure is a
descriptive measure of spread. It is not a significance test and says nothing
about whether two backends differ.

`make_result_tables.py` writes `latex/tables/*.tex`, which the thesis includes
directly, so re-running the campaign updates the document.

Two checks guard the prose, which tables cannot:

```bash
./.venv/Scripts/python.exe analysis/verify_thesis_numbers.py       # quoted figures vs the data
./.venv/Scripts/python.exe analysis/audit_floats_and_citations.py  # floats, references, citations
```

The first re-reads every figure the thesis quotes in running text and marks
`BAD` any that the result files no longer support. The second checks that
every figure, table and listing is referenced, that every reference resolves,
and that every citation is in the bibliography.

## Independent cross-check

```bash
docker run --rm --network thesis-net minio/warp:latest mixed \
  --host=<backend>:<port> --access-key=... --secret-key=... \
  --objects=20 --obj.size=16MiB --concurrent=4 --duration=30s
```

Output goes in `bench/results/warp/<backend>.txt` and is parsed by
`analysis/parse_warp.py`. warp shares no code with the harness. It reproduces
the run-to-run instability the harness shows, and it **orders the systems
differently**: Ceph is last under the harness and first under warp. The two
tools ask for different work — warp sustains four streams against one bucket
for thirty seconds, the harness creates a bucket, transfers a few objects and
tears it down. Neither stands in for the other.

## Building the thesis

```bash
bash latex/sync_listings.sh   # copy and redact the reproduced source extracts
cd latex && ./build.sh        # pdflatex ×3 + bibtex8
```

## Two things to know about the results

**Ceph is not MicroCeph.** MicroCeph ships as a snap and needs snapd with
systemd as PID 1, which an application container does not provide. A VM was
ruled out: this edition of Windows has no platform hypervisor, and the WSL 2
VM does not provide snap support by default. The `ceph-rgw` image runs the
same Ceph daemons under supervisord instead. Fidelity results should transfer,
since the same RGW component serves the S3 interface in both packagings;
resource figures describe this packaging and should not be attributed to
MicroCeph.

**Measurements on this host are noisy. Read the dispersion, not just the
median.**

- *Throughput*: coefficients of variation reach 111 %, two complete campaigns
  disagreed on the ordering of the middle of the field, and an independent
  tool shows order-of-magnitude variation within single runs. Repeats were
  raised from 5 to 20 in response. Run `analysis/summarize_benchmarks.py`
  before quoting any figure.
- *Memory*: each backend is measured shortly after starting, so samples
  capture caches filling rather than steady state. Garage's mean was recorded
  as 504, 30, 105.6 and 181.2 MB across four passes. Peak memory is the stable
  figure and the one the conclusions rest on.
