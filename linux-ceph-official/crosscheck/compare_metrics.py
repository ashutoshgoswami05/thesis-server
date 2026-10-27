"""
Reads each recorded window twice -- once through cAdvisor, once through the
backend's own metrics endpoint -- and reports whether the two instruments agree.

Usage: python crosscheck/compare_metrics.py
Reads crosscheck/results/windows.csv, queries Prometheus, writes
crosscheck/results/agreement.csv.

CPU is compared directly: container_cpu_usage_seconds_total and
process_cpu_seconds_total are both cumulative counters of CPU-seconds, so
increase() over one window gives two readings of the same quantity.

Memory is not the same quantity and is reported as a gap rather than a ratio.
container_memory_usage_bytes is the cgroup's accounting and includes page cache;
process_resident_memory_bytes is the resident set of the process. The difference
separates what the backend holds resident from what the kernel has cached for it.

Not part of the evaluation framework: see crosscheck/README.md.
"""

import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger("crosscheck")

PROMETHEUS_URL = "http://localhost:9090"
HERE = Path(__file__).parent
WINDOWS_CSV = HERE / "results" / "windows.csv"
OUTPUT_CSV = HERE / "results" / "agreement.csv"
STEP_SECONDS = "5s"  # matches monitoring/prometheus.yml's scrape_interval

# For each backend: the containers cAdvisor tracks, the Prometheus job that
# scrapes its own endpoint, the subset of containers that job covers -- the
# comparison is only fair over processes both instruments can see -- and the
# names its endpoint uses for its own CPU and memory.
#
# The names are not uniform. SeaweedFS uses the Prometheus Go client's default
# registry, so its process collector is unprefixed. JuiceFS prefixes the same
# collector with its own namespace. Garage exports neither: its registry holds
# only its own API and storage counters, so it does not report what it costs to
# run at all, and no comparison is possible for it.
BACKENDS = {
    "seaweedfs": {
        "containers": ["seaweedfs-master", "seaweedfs-volume",
                       "seaweedfs-filer", "seaweedfs-s3"],
        "job": "seaweedfs",
        "covered": ["seaweedfs-master", "seaweedfs-volume",
                    "seaweedfs-filer", "seaweedfs-s3"],
        "cpu_metric": "process_cpu_seconds_total",
        "memory_metric": "process_resident_memory_bytes",
    },
    "garage": {
        "containers": ["garage"],
        "job": "garage",
        "covered": ["garage"],
        "cpu_metric": None,
        "memory_metric": None,
        "note": "exports no process CPU or memory metric",
    },
    "juicefs": {
        "containers": ["juicefs-redis", "juicefs-gateway"],
        "job": "juicefs",
        "covered": ["juicefs-gateway"],
        "cpu_metric": "juicefs_process_cpu_seconds_total",
        "memory_metric": "juicefs_process_resident_memory_bytes",
        "note": "gateway only; Redis is not instrumented",
    },
}


def query_range(query, start, end):
    response = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": STEP_SECONDS},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        logger.warning(f"  query failed: {query}")
        return []
    return payload["data"]["result"]


def instant(query, moment):
    result = query_range(query, moment, moment)
    if not result or not result[0]["values"]:
        return None
    return float(result[0]["values"][-1][1])


def parse_time(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def summed_series(query, start, end):
    """Adds every returned series point by point, so a peak is the peak of the
    total rather than the sum of individual peaks."""
    result = query_range(query, start, end)
    if not result:
        return []
    columns = [[float(value) for _, value in series["values"]] for series in result]
    length = min(len(column) for column in columns)
    return [sum(column[i] for column in columns) for i in range(length)]


def cpu_seconds(selector, start, end, window):
    """Total CPU-seconds over the window, summed across matching series."""
    query = f"sum(increase({selector}[{window}s]))"
    return instant(query, end)


def compare(backend, start, end):
    spec = BACKENDS[backend]
    window = int((parse_time(end) - parse_time(start)).total_seconds())
    covered = "|".join(spec["covered"])
    everything = "|".join(spec["containers"])
    job = spec["job"]

    cadvisor_cpu = cpu_seconds(
        f'container_cpu_usage_seconds_total{{name=~"{covered}"}}', start, end, window)
    full_cpu = cpu_seconds(
        f'container_cpu_usage_seconds_total{{name=~"{everything}"}}', start, end, window)
    cadvisor_memory = summed_series(
        f'container_memory_usage_bytes{{name=~"{covered}"}}', start, end)

    native_cpu, native_memory = None, []
    if spec["cpu_metric"]:
        native_cpu = cpu_seconds(
            f'{spec["cpu_metric"]}{{job="{job}"}}', start, end, window)
        native_memory = summed_series(
            f'{spec["memory_metric"]}{{job="{job}"}}', start, end)

    cadvisor_peak = max(cadvisor_memory) / 1e6 if cadvisor_memory else None
    native_peak = max(native_memory) / 1e6 if native_memory else None

    row = {
        "backend": backend,
        "window_seconds": window,
        "processes_compared": len(spec["covered"]),
        "cadvisor_cpu_seconds": round(cadvisor_cpu, 2) if cadvisor_cpu else "",
        "native_cpu_seconds": round(native_cpu, 2) if native_cpu else "",
        "cpu_ratio_native_over_cadvisor": "",
        "cadvisor_peak_memory_MB": round(cadvisor_peak, 1) if cadvisor_peak else "",
        "native_peak_memory_MB": round(native_peak, 1) if native_peak else "",
        "memory_gap_MB": "",
        "native_share_of_memory_percent": "",
        "cadvisor_cpu_seconds_whole_backend": round(full_cpu, 2) if full_cpu else "",
        "cadvisor_memory_samples": len(cadvisor_memory),
        "native_memory_samples": len(native_memory),
        "native_cpu_metric": spec["cpu_metric"] or "",
        "note": spec.get("note", ""),
    }

    if cadvisor_cpu and native_cpu:
        row["cpu_ratio_native_over_cadvisor"] = round(native_cpu / cadvisor_cpu, 3)
    if cadvisor_peak and native_peak:
        row["memory_gap_MB"] = round(cadvisor_peak - native_peak, 1)
        row["native_share_of_memory_percent"] = round(
            100 * native_peak / cadvisor_peak, 1)
    return row


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not WINDOWS_CSV.exists():
        sys.exit(f"no windows recorded: run crosscheck/run_crosscheck.sh first")

    rows = []
    with open(WINDOWS_CSV, newline="") as f:
        for window in csv.DictReader(f):
            backend = window["backend"]
            if backend not in BACKENDS:
                logger.info(f"{backend}: no native endpoint, skipped")
                continue
            logger.info(f"{backend}: {window['start_time']} -> {window['end_time']}")
            rows.append(compare(backend, window["start_time"], window["end_time"]))

    if not rows:
        sys.exit("nothing to compare")

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"\nWrote {len(rows)} comparison(s) to {OUTPUT_CSV}\n")

    print(f"{'backend':<11} {'CPU-s cAdv':>11} {'CPU-s own':>10} {'ratio':>7}"
          f" {'peak cAdv':>10} {'peak own':>9} {'own share':>10}")
    for row in rows:
        print(f"{row['backend']:<11} {row['cadvisor_cpu_seconds']:>11}"
              f" {row['native_cpu_seconds']:>10}"
              f" {row['cpu_ratio_native_over_cadvisor']:>7}"
              f" {row['cadvisor_peak_memory_MB']:>10}"
              f" {row['native_peak_memory_MB']:>9}"
              f" {str(row['native_share_of_memory_percent']) + '%':>10}")


if __name__ == "__main__":
    main()
