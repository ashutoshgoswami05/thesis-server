"""
Summarises the raw benchmark runs into per-workload statistics for the
results chapter (thesis Ch. 7.2 and 7.3).

Usage: python analysis/summarize_benchmarks.py [input_csv_name]
Reads bench/results/benchmark_results.csv (or the file named as the
argument) and writes bench/results/benchmark_summary.csv.

Reports median and coefficient of variation alongside the mean, rather
than the mean alone. The measurements on this host turned out to be highly
dispersed -- on the largest workload one backend's repeats spanned roughly
an order of magnitude -- so a mean on its own would suggest a precision the
data does not have. The coefficient of variation makes that visible, and
is what Ch. 7.3 uses to decide which differences between backends the data
can actually support.
"""

import csv
import logging
import statistics
import sys
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("thesis")

REPO_ROOT = Path(__file__).parent.parent
RESULTS_DIR = REPO_ROOT / "bench" / "results"
OUTPUT_CSV = RESULTS_DIR / "benchmark_summary.csv"

CSV_FIELDS = [
    "backend",
    "num_files",
    "size_bytes",
    "parallel",
    "runs",
    "upload_mean_MBps",
    "upload_median_MBps",
    "upload_sd_MBps",
    "upload_cv_percent",
    "download_mean_MBps",
    "download_median_MBps",
    "download_sd_MBps",
    "download_cv_percent",
]


def load_successful_runs(input_path):
    """Returns only the runs that completed and passed integrity
    verification. A timing figure for a transfer that corrupted data would
    not be meaningful, so failed runs are excluded from the statistics
    rather than averaged in."""
    with open(input_path, newline="") as f:
        rows = [row for row in csv.DictReader(f) if row["integrity_ok"] == "True"]
    logger.info(f"Loaded {len(rows)} successful run(s) from {input_path}")
    return rows


def group_by_workload(rows):
    """Groups runs by (backend, num_files, size_bytes, parallel)."""
    groups = defaultdict(list)
    for row in rows:
        key = (row["backend"], row["num_files"], row["size_bytes"], row["parallel"])
        groups[key].append(row)
    return groups


def describe(values):
    """Returns (mean, median, standard deviation, coefficient of variation)
    in MB/s and percent. Standard deviation needs at least two values, and
    the coefficient of variation is undefined when the mean is zero."""
    mean = statistics.mean(values)
    median = statistics.median(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    cv = (100 * sd / mean) if mean > 0 else 0.0
    return round(mean, 2), round(median, 2), round(sd, 2), round(cv, 1)


def summarize_group(key, runs):
    backend, num_files, size_bytes, parallel = key
    uploads = [float(r["bandwidth_upload_Bps"]) / 1_000_000 for r in runs]
    downloads = [float(r["bandwidth_download_Bps"]) / 1_000_000 for r in runs]

    up_mean, up_median, up_sd, up_cv = describe(uploads)
    down_mean, down_median, down_sd, down_cv = describe(downloads)

    return {
        "backend": backend,
        "num_files": num_files,
        "size_bytes": size_bytes,
        "parallel": parallel,
        "runs": len(runs),
        "upload_mean_MBps": up_mean,
        "upload_median_MBps": up_median,
        "upload_sd_MBps": up_sd,
        "upload_cv_percent": up_cv,
        "download_mean_MBps": down_mean,
        "download_median_MBps": down_median,
        "download_sd_MBps": down_sd,
        "download_cv_percent": down_cv,
    }


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    input_name = sys.argv[1] if len(sys.argv) > 1 else "benchmark_results.csv"
    input_path = RESULTS_DIR / input_name

    rows = load_successful_runs(input_path)
    groups = group_by_workload(rows)

    summaries = [summarize_group(key, runs) for key, runs in sorted(groups.items())]

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(summaries)

    logger.info(f"Wrote {len(summaries)} workload summaries to {OUTPUT_CSV}")

    # A short console report, so the dispersion is visible without opening
    # the file. Sorted worst-first because the high-variance cases are the
    # ones that constrain what Ch. 7.3 may claim.
    worst = sorted(summaries, key=lambda s: s["upload_cv_percent"], reverse=True)[:5]
    logger.info("Highest upload variability (coefficient of variation):")
    for s in worst:
        logger.info(
            f"  {s['backend']:10s} {s['num_files']}x{s['size_bytes']}B "
            f"parallel={s['parallel']:5s} cv={s['upload_cv_percent']}% "
            f"(n={s['runs']})"
        )


if __name__ == "__main__":
    main()
