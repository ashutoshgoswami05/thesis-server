# Metric cross-check — not part of the submission

A side investigation, kept out of `bench/`, `analysis/` and `tests/` so that nothing
here is mistaken for part of the evaluation framework the thesis describes. Nothing in
the submitted tree reads this directory, and no figure in the thesis comes from it.

## The question

The thesis measures resource consumption with cAdvisor alone. Section 4.3 justifies
that on comparability grounds — one system exposes no metrics endpoint, and on another
enabling the metrics module destabilised the service — but it never checks whether
cAdvisor's figures agree with what the systems report about themselves.

Three of the six are scraped natively as well as by cAdvisor, so for those three the
same window can be measured twice, by two instruments that share no code:

| Quantity | cAdvisor | Native endpoint |
|---|---|---|
| CPU | `container_cpu_usage_seconds_total` | `process_cpu_seconds_total` |
| Memory | `container_memory_usage_bytes` | `process_resident_memory_bytes` |

**CPU is a like-for-like comparison.** Both are cumulative counters of CPU-seconds, so
`increase()` over one window gives two independent readings of one quantity.

**Memory is not.** The cAdvisor figure is the cgroup's accounting, which includes page
cache; the native figure is the process's resident set. They are expected to differ,
and the difference is the point: it separates the pages the backend has resident from
the cache the kernel has attached to it.

## Coverage

| Backend | Native metrics | In this cross-check |
|---|---|---|
| SeaweedFS | 4 components, no auth | **Yes** — all four, `process_*` |
| JuiceFS | gateway only | **Yes** — gateway only, `juicefs_process_*`; Redis is not instrumented |
| Garage | admin API, bearer token | No — 61 metrics, none of them CPU or memory |
| Ceph | mgr module, `ceph-exporter` | No — 1,499 metrics, none of them CPU or memory |
| RustFS | none at all | No |
| MinIO | JWT / public auth, never wired | No |

Both negative cases were checked twice over, against the project's own documentation
and against the deployed build; `FINDINGS.md` records what each exposes instead.

Sources: [Garage monitoring reference](https://garagehq.deuxfleurs.fr/documentation/reference-manual/monitoring/) ·
[Ceph mgr prometheus module](https://docs.ceph.com/en/latest/mgr/prometheus/)

## Running it

Needs the monitoring stack up and `thesis-net` to exist.

```bash
bash crosscheck/run_crosscheck.sh
```

Brings each backend up alone, runs five repeats of the eight workloads, records the
window, brings it down. Writes `crosscheck/results/windows.csv` and moves the harness's
own output into `crosscheck/results/runs.csv`.

```bash
./.venv/Scripts/python.exe crosscheck/compare_metrics.py
```

Queries Prometheus twice per window and writes `crosscheck/results/agreement.csv`.

## What it cannot do

The figures here are not the thesis's figures and are not comparable with them: this is
a separate pass on a different day, and the thesis's own stability results say such
passes differ. Agreement between two instruments is a property *within* one window, so
it does not need to reproduce the reported campaign — but a number from this directory
must never be quoted as a resource result.
