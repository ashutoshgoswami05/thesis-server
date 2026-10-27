"""
Pulls CPU/memory metrics from Prometheus for each backend's recorded
benchmark window (thesis Ch. 5.3), and saves mean/peak usage per backend
to a CSV.

Usage: python analysis/pull_metrics.py
Reads bench/results/monitoring_windows.csv, which has one (backend, start,
end) row per instrumented benchmark run, and queries Prometheus's
HTTP API for cAdvisor's container_memory_usage_bytes and
container_cpu_usage_seconds_total over each window.
"""

import csv
import logging
import statistics
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger("thesis")

PROMETHEUS_URL = "http://localhost:9090"
REPO_ROOT = Path(__file__).parent.parent
WINDOWS_CSV = REPO_ROOT / "bench" / "results" / "monitoring_windows.csv"
OUTPUT_CSV = REPO_ROOT / "bench" / "results" / "resource_usage.csv"

STEP_SECONDS = "5s"  # matches monitoring/prometheus.yml's scrape_interval

# Which container(s) cAdvisor tracks for each backend. Some backends run
# more than one container (SeaweedFS, JuiceFS, Ceph) -- their usage is added
# together to get the backend's total footprint.
BACKEND_CONTAINERS = {
    "rustfs": ["rustfs"],
    "garage": ["garage"],
    "seaweedfs": ["seaweedfs-master", "seaweedfs-volume", "seaweedfs-filer", "seaweedfs-s3"],
    "juicefs": ["juicefs-redis", "juicefs-gateway"],
    "ceph": ["ceph-mon", "ceph-mgr", "ceph-osd", "ceph-rgw"],
    "minio": ["minio"],
}


def query_range(query: str, start: str, end: str) -> list:
    """Runs a Prometheus range query and returns its raw result list."""
    response = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": STEP_SECONDS},
    )
    response.raise_for_status()
    data = response.json()
    if data["status"] != "success":
        raise RuntimeError(f"Prometheus query failed: {data}")
    return data["data"]["result"]


def memory_values(container_name: str, start: str, end: str) -> list:
    query = f'container_memory_usage_bytes{{name="{container_name}"}}'
    result = query_range(query, start, end)
    if not result:
        return []
    return [float(value) for _, value in result[0]["values"]]


def swap_values(container_name: str, start: str, end: str) -> list:
    """Swap in use by the container, in bytes. R6 claims the workload fits
    in the memory available, and a backend that swapped would still appear
    to fit when only its resident memory is read."""
    query = f'container_memory_swap{{name="{container_name}"}}'
    result = query_range(query, start, end)
    if not result:
        return []
    return [float(value) for _, value in result[0]["values"]]


def cpu_seconds(container_name: str, start: str, end: str) -> float:
    """Returns the CPU time the container used during the window, in
    core-seconds.

    This is the figure RQ3 needs. A mean of the per-instant rates is not:
    the same workload spread over a longer window gives a lower mean, so a
    slower backend can look cheaper than a faster one. increase() handles
    counter resets, and the window length is taken from the recorded
    benchmark window."""
    window = int((parse_time(end) - parse_time(start)).total_seconds())
    query = (
        f'increase(container_cpu_usage_seconds_total{{name="{container_name}"}}[{window}s])'
    )
    result = query_range(query, end, end)
    if not result or not result[0]["values"]:
        return 0.0
    return float(result[0]["values"][-1][1])


def parse_time(timestamp: str):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def cpu_values(container_name: str, start: str, end: str) -> list:
    # container_cpu_usage_seconds_total is a counter (total CPU time used
    # since the container started), so rate() over a short window turns it
    # into "CPU cores in use", the same idea as `docker stats`' CPU %.
    # The 15s lookback needs to cover at least two scrapes (5s apart) for
    # rate() to have anything to work with.
    query = f'rate(container_cpu_usage_seconds_total{{name="{container_name}"}}[15s])'
    result = query_range(query, start, end)
    if not result:
        return []
    return [float(value) for _, value in result[0]["values"]]


def sum_series(series_list: list) -> list:
    """Adds multiple time series together point-by-point, for backends
    made of several containers. Series may have slightly different lengths
    since containers don't scrape on the exact same tick, so this only
    sums as many points as the shortest series has."""
    series_list = [s for s in series_list if s]
    if not series_list:
        return []
    shortest = min(len(s) for s in series_list)
    return [sum(s[i] for s in series_list) for i in range(shortest)]


def summarize_backend(backend: str, start: str, end: str) -> dict:
    logger.info(f"Querying Prometheus for {backend}, window {start} to {end}")
    containers = BACKEND_CONTAINERS[backend]

    memory_series = sum_series([memory_values(c, start, end) for c in containers])
    cpu_series = sum_series([cpu_values(c, start, end) for c in containers])
    total_cpu_seconds = sum(cpu_seconds(c, start, end) for c in containers)
    swap_series = sum_series([swap_values(c, start, end) for c in containers])
    window_seconds = int((parse_time(end) - parse_time(start)).total_seconds())

    if not memory_series or not cpu_series:
        logger.warning(f"  no data returned for {backend} -- was cAdvisor scraping it during this window?")
        return {
            "backend": backend,
            "mean_memory_MB": "",
            "peak_memory_MB": "",
            "mean_cpu_cores": "",
            "peak_cpu_cores": "",
            "cpu_seconds": "",
            "peak_swap_MB": "",
            "window_seconds": window_seconds,
            "memory_samples": 0,
            "cpu_samples": 0,
        }

    return {
        "backend": backend,
        "mean_memory_MB": round(statistics.mean(memory_series) / 1_000_000, 2),
        "peak_memory_MB": round(max(memory_series) / 1_000_000, 2),
        "mean_cpu_cores": round(statistics.mean(cpu_series), 4),
        "peak_cpu_cores": round(max(cpu_series), 4),
        # CPU time actually consumed for the workload, which is what makes
        # backends comparable when their runs take different lengths of time.
        "cpu_seconds": round(total_cpu_seconds, 2),
        "peak_swap_MB": round(max(swap_series) / 1_000_000, 2) if swap_series else 0.0,
        "window_seconds": window_seconds,
        # How many scrapes actually landed inside this backend's benchmark
        # window. Recorded because a short run at a coarse scrape interval
        # can yield only one or two points, and a "mean" over one sample
        # isn't a mean -- the thesis should say so rather than quietly
        # presenting it like the others.
        "memory_samples": len(memory_series),
        "cpu_samples": len(cpu_series),
    }


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    with open(WINDOWS_CSV) as f:
        windows = list(csv.DictReader(f))

    rows = [summarize_backend(w["backend"], w["start_time"], w["end_time"]) for w in windows]

    OUTPUT_CSV.parent.mkdir(exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="") as f:
        fieldnames = [
            "backend",
            "mean_memory_MB",
            "peak_memory_MB",
            "mean_cpu_cores",
            "peak_cpu_cores",
            "cpu_seconds",
            "peak_swap_MB",
            "window_seconds",
            "memory_samples",
            "cpu_samples",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"Saved resource usage summary to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
