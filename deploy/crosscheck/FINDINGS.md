# Cross-check results — 13 October 2026

A short instrumented pass against the three backends that Prometheus scrapes natively,
five repeats of the eight workloads each, each window read twice: once through cAdvisor
and once through the backend's own metrics endpoint. 120 runs, every one with
`integrity_ok` true, no errors.

**Not thesis figures.** A separate pass on a different day. The agreement between two
instruments is a property within one window, which is why this works at all, but no
absolute number here is comparable with Chapter 7.

| Backend | Window | cAdvisor CPU-s | Own CPU-s | Ratio | cAdvisor peak MB | Own peak MB | Own share |
|---|---|---|---|---|---|---|---|
| SeaweedFS | 68 s | 36.15 | 35.58 | **0.984** | 928.1 | 1063.0 | 114.5 % |
| JuiceFS (gateway) | 25 s | 24.78 | 24.75 | **0.998** | 1025.2 | 386.7 | 37.7 % |
| Garage | 27 s | 29.68 | — | — | 490.5 | — | — |

## 1. The CPU figures agree, within 2 %

The quantity RQ3 rests on is CPU time in core-seconds over the measurement window.
Two instruments sharing no code, reading the same window, differ by **1.6 % for
SeaweedFS and 0.2 % for the JuiceFS gateway**, the self-reported figure being the
slightly lower one in both cases.

That is the result worth having. The thesis measures resource consumption with cAdvisor
alone and argues for that choice on comparability grounds; it never shows that cAdvisor
is *right*. For the two systems where a second opinion exists, it is.

The residual is in the expected direction: a process's own counter starts when the
process does, while the cgroup is charged from container start, so the container figure
includes a little runtime setup the process does not count.

## 2. Coverage is two of six — and for a stronger reason than coverage

Only SeaweedFS and JuiceFS report the two quantities a resource comparison needs. The
other four do not, and each case was checked against both the project's documentation
and the deployed build.

| System | Native endpoint | Self-reports CPU time and resident memory | Established by |
|---|---|---|---|
| SeaweedFS | 4 components | **Yes** — `process_cpu_seconds_total` | measured |
| JuiceFS | gateway | **Yes** — `juicefs_process_cpu_seconds_total` | measured |
| Garage | admin API | **No** | 61 metrics measured; documentation lists none |
| Ceph | mgr module, `ceph-exporter` | **No** | 1,499 metrics measured; documentation lists none |
| RustFS | none at all | — | four candidate paths probed with signed requests |
| MinIO | JWT or public auth, never wired | — | — |

**Garage.** Its monitoring reference lists every metric it exposes, in six families:
system, cluster health, API endpoints, web, block manager, RPC and metadata tables.
None is a process metric. The deployed build agrees exactly: 61 Garage metrics during
the measured window — 9 `api_*`, 18 `block_*`, 11 `cluster_*`, 4 `garage_*`, 4 `rpc_*`,
15 `table_*` — plus Prometheus's own five. The only host-level figures are
`garage_local_disk_avail` and `garage_local_disk_total`; `block_ram_buffer_free_kb` is
an internal send buffer, not process memory.

**Ceph.** Two paths were checked, and neither carries the quantities needed. The mgr
`prometheus` module's documentation describes cluster-level metrics and counters
received from daemons, and names no per-daemon process CPU or resident memory; it also
states that daemon performance counters are no longer exported by that module by
default, having moved to `ceph-exporter`. That binary *is* present in the `ceph-aio`
image, though not started by its process supervisor. Started by hand against the
daemon admin sockets at `--prio-limit 0`, the most permissive setting, it served
**1,499 distinct metric names and not one CPU, `process_*` or resident-memory metric.**
The closest are 58 `ceph_mempool_*` gauges, which report Ceph's internal allocator
pools in bytes — a partial view of its own heap, not the process's resident set, and
not comparable with a cgroup total.

**This is a categorical difference, not a coverage gap.** Garage and Ceph are not
lightly instrumented: 61 and 1,499 metric names respectively. But all of it describes
*what the system did* — requests served, blocks written, queue depths, pool
occupancy — and none of it describes *what the system cost*. Section 4.3 currently
rejects native instrumentation because coverage "turned out uneven". The measured
position is stronger: for four of the six systems a native-metrics methodology was not
incomplete, it was impossible.

## 3. The metric names are not uniform

SeaweedFS uses the Prometheus Go client's default registry, so its process collector is
unprefixed: `process_cpu_seconds_total`. JuiceFS prefixes the same collector with its
own namespace: `juicefs_process_cpu_seconds_total`. Garage, written in Rust, has no
such collector.

Three systems, three conventions. A framework reading native instrumentation would need
per-backend knowledge of metric names — exactly the per-backend logic requirement R1
forbids.

## 4. Memory is not one quantity, and the direction depends on deployment shape

`container_memory_usage_bytes` is the cgroup's accounting, which includes page cache.
`process_resident_memory_bytes` is the process's resident set. The two diverge, and the
sign of the divergence is informative.

**JuiceFS, one instrumented process: the resident set is 37.7 % of the container
figure.** 386.7 MB resident against 1025.2 MB charged to the cgroup — a gap of
**638.5 MB** that is not the gateway's resident memory. For a system whose data path is
a local filesystem, that is overwhelmingly page cache from its own writes, charged to
the container that caused it.

This puts a number on something the thesis currently asserts without one: *"each
backend is measured shortly after starting, so the samples capture caches filling
rather than steady state."* In this window, for this backend, about three fifths of the
reported memory figure was cache rather than the process's own pages.

**SeaweedFS, four processes of one binary: the summed resident sets exceed the container
figure by 134.9 MB.** 1063.0 MB against 928.1 MB. The likely cause is that each of the
four `weed` processes counts the shared pages of the same binary in its own resident
set, while the cgroup counts each page once. The sum of per-process peaks is higher
still, at 1118.2 MB.

The practical consequence: **per-process resident memory cannot be summed into a backend
total.** For a multi-process deployment it overcounts, by an amount that depends on how
much the processes share. The cgroup figure does not have that problem, which is a
second reason to prefer it beyond the comparability argument.

## What this does not establish

- One pass, five repeats, one day. The CPU agreement is a tight result because it is a
  ratio within a window; the memory gaps are single observations and their size should
  not be quoted as a general figure.
- Nothing is established for RustFS, Ceph or MinIO, which is half the study.
- The JuiceFS comparison covers the gateway only. Redis is not instrumented, and the
  whole-backend cAdvisor CPU figure for that window was 26.07 core-seconds against the
  gateway's 24.78.

## Files

| | |
|---|---|
| `results/windows.csv` | the three measured windows, UTC |
| `results/runs.csv` | 120 harness runs from the pass |
| `results/agreement.csv` | the comparison, full columns |
