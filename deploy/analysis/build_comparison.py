"""
Merges the three result sets this thesis is built on (API fidelity,
performance, resource usage) into one comparison table, and draws the
charts used in the results chapter (thesis Section 5.5 and Chapter 7).

Usage: python analysis/build_comparison.py
Reads:
  tests/results/compatibility_matrix.csv
  bench/results/benchmark_results.csv
  bench/results/resource_usage.csv
Writes:
  bench/results/unified_comparison.csv
  bench/results/charts/api_fidelity.png
  bench/results/charts/performance.png
  bench/results/charts/resource_usage.png
  bench/results/charts/consolidated.png
  latex/images/performance.pdf, resource_usage.pdf, consolidated.pdf
    (vector copies of the three charts that appear in the thesis)
"""

import csv
import logging
import random
import statistics
from pathlib import Path

import matplotlib.pyplot as plt

logger = logging.getLogger("thesis")

REPO_ROOT = Path(__file__).parent.parent
COMPATIBILITY_CSV = REPO_ROOT / "tests" / "results" / "compatibility_matrix.csv"
BENCHMARK_CSV = REPO_ROOT / "bench" / "results" / "benchmark_results.csv"
RESOURCE_CSV = REPO_ROOT / "bench" / "results" / "resource_usage.csv"
UNIFIED_CSV = REPO_ROOT / "bench" / "results" / "unified_comparison.csv"
CHARTS_DIR = REPO_ROOT / "bench" / "results" / "charts"
THESIS_IMAGES_DIR = REPO_ROOT / "latex" / "images"

# Same order as the tables in the thesis, with the baseline (MinIO) last.
BACKENDS = ["rustfs", "garage", "seaweedfs", "juicefs", "ceph", "minio"]

# How each backend is named in the charts, matching the thesis tables.
BACKEND_LABELS = {
    "rustfs": "RustFS",
    "garage": "Garage",
    "seaweedfs": "SeaweedFS",
    "juicefs": "JuiceFS",
    "ceph": "Ceph",
    "minio": "MinIO (baseline)",
}

CANDIDATE_COLOUR = "steelblue"
BASELINE_COLOUR = "darkgray"  # MinIO is the reference point, not a candidate

# The "gray-area" S3 capabilities that aren't required for basic S3
# compliance -- these are what the thesis's fidelity comparison is about.
# (Core things like object_put_get_delete are expected to just work, so
# they're not useful for telling backends apart.)
#
# Server-side encryption (sse_s3) is tested but deliberately left out of
# this list: whether a backend accepted it mostly reflected how that
# deployment was configured (for example, whether an encryption key had
# been set up), not whether the feature exists, so counting it would
# compare configurations rather than systems (thesis Section 7.1).
OPTIONAL_FEATURES = [
    "bucket_versioning",
    "object_tagging",
    "bucket_policy",
    "object_lock",
    "cors_configuration",
    "presigned_url",
    "legacy_sigv2_auth",
]


def count_supported_capabilities(backend: str) -> tuple:
    """Returns (supported_count, tested_count) for one backend's optional
    capabilities. tested_count can be less than len(OPTIONAL_FEATURES) for
    JuiceFS, since most of its tests never got a chance to run (see
    the thesis)."""
    with open(COMPATIBILITY_CSV) as f:
        rows = [
            row
            for row in csv.DictReader(f)
            if row["backend"] == backend and row["feature"] in OPTIONAL_FEATURES
        ]
    supported = sum(1 for row in rows if row["supported"] == "True")
    return supported, len(rows)


def large_object_runs(backend: str) -> tuple:
    """Returns (upload_MBps_list, download_MBps_list): one value per run of
    the 16MB x 2 files, parallel workload that passed integrity checks."""
    with open(BENCHMARK_CSV) as f:
        rows = [
            row
            for row in csv.DictReader(f)
            if row["backend"] == backend
            and row["size_bytes"] == "16777216"
            and row["parallel"] == "True"
            and row["integrity_ok"] == "True"
        ]
    uploads = [float(r["bandwidth_upload_Bps"]) / 1_000_000 for r in rows]
    downloads = [float(r["bandwidth_download_Bps"]) / 1_000_000 for r in rows]
    return uploads, downloads


def large_object_bandwidth(backend: str) -> tuple:
    """Returns (median_upload_MBps, median_download_MBps) for the 16MB x 2
    files, parallel workload -- the headline throughput number of the
    thesis. Returns (None, None) if every repeat for this backend failed.

    The median is used rather than the mean because it isn't pulled around
    by a few unusually fast or slow runs. The runs on this workload vary a
    lot (coefficients of variation reach 85%), and for the most variable
    backends a handful of fast runs lift the mean well above what a typical
    run achieved. The full spread is reported separately by
    analysis/summarize_benchmarks.py, and it is the spread -- not this
    single figure -- that determines which differences between backends
    are supportable."""
    uploads, downloads = large_object_runs(backend)
    if not uploads:
        return None, None
    upload_mbps = statistics.median(uploads)
    download_mbps = statistics.median(downloads)
    return round(upload_mbps, 2), round(download_mbps, 2)


def resource_usage(backend: str) -> tuple:
    """Returns (peak_memory_MB, cpu_seconds) from the resource extraction.
    Returns (None, None) if that backend has no row (shouldn't normally
    happen, since all six were profiled).

    Peak memory and CPU time, not their means, are what the consolidated
    comparison reports: a mean rate falls as a window lengthens, and mean
    memory was not stable across passes."""
    with open(RESOURCE_CSV) as f:
        for row in csv.DictReader(f):
            if row["backend"] == backend and row["peak_memory_MB"]:
                return float(row["peak_memory_MB"]), float(row["cpu_seconds"])
    return None, None


def build_unified_table() -> list:
    rows = []
    for backend in BACKENDS:
        supported, tested = count_supported_capabilities(backend)
        upload_mbps, download_mbps = large_object_bandwidth(backend)
        peak_memory_mb, cpu_seconds = resource_usage(backend)
        rows.append(
            {
                "backend": backend,
                "capabilities_supported": supported,
                "capabilities_tested": tested,
                "upload_16MB_median_MBps": upload_mbps,
                "download_16MB_median_MBps": download_mbps,
                "peak_memory_MB": peak_memory_mb,
                "cpu_seconds": cpu_seconds,
            }
        )
        logger.info(f"{backend}: {rows[-1]}")
    return rows


def save_unified_csv(rows: list):
    UNIFIED_CSV.parent.mkdir(exist_ok=True)
    with open(UNIFIED_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Saved unified comparison table to {UNIFIED_CSV}")


def set_chart_style():
    """The thesis charts are drawn at the thesis's text width (420pt, about
    5.8 inches) so they are included without scaling, in a sans-serif font
    close to the one the thesis uses for captions."""
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
            "font.size": 8,
            "axes.titlesize": 8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "pdf.fonttype": 42,  # embed the font as TrueType, not Type 3
        }
    )


def save_chart(fig, name: str, in_thesis: bool):
    """Saves a PNG for quick viewing and, for charts that appear in the
    thesis, a PDF copy in latex/images -- vector, so it stays sharp when the
    thesis is zoomed or printed."""
    fig.savefig(CHARTS_DIR / f"{name}.png", dpi=200)
    if in_thesis:
        fig.savefig(THESIS_IMAGES_DIR / f"{name}.pdf")
    plt.close(fig)


def label_backend_axis(ax, backends: list):
    """Puts the backend names on the vertical axis, first backend at the
    top, as in the thesis tables."""
    ax.set_yticks(range(len(backends)))
    ax.set_yticklabels([BACKEND_LABELS[b] for b in backends])
    ax.invert_yaxis()


def draw_api_fidelity_chart(rows: list):
    fig, ax = plt.subplots(figsize=(7, 4))
    backends = [BACKEND_LABELS[r["backend"]] for r in rows]
    supported = [r["capabilities_supported"] for r in rows]
    tested = [r["capabilities_tested"] for r in rows]

    ax.bar(backends, tested, color="lightgray", label="capabilities tested")
    ax.bar(backends, supported, color="steelblue", label="capabilities supported")
    ax.set_ylabel(f"count (out of {len(OPTIONAL_FEATURES)} optional capabilities)")
    ax.set_title("API Fidelity: optional capabilities supported per backend")
    ax.legend()
    fig.tight_layout()
    save_chart(fig, "api_fidelity", in_thesis=False)


def draw_performance_chart(rows: list):
    """One dot per run of the 16MB x 2 files, parallel workload, with a bar
    at the median -- so the chart shows the spread behind each median in
    the thesis's large-object table, not only the median itself."""
    backends = [r["backend"] for r in rows]
    fig, (upload_ax, download_ax) = plt.subplots(1, 2, figsize=(5.8, 2.6), sharey=True)
    # Each dot is moved a little up or down at random so that runs with
    # similar values don't hide each other. The fixed seed makes the chart
    # come out the same every time the script is run.
    jitter = random.Random(0)

    for position, backend in enumerate(backends):
        colour = BASELINE_COLOUR if backend == "minio" else CANDIDATE_COLOUR
        uploads, downloads = large_object_runs(backend)
        for ax, values in ((upload_ax, uploads), (download_ax, downloads)):
            if not values:
                continue
            heights = [position + jitter.uniform(-0.2, 0.2) for _ in values]
            ax.scatter(values, heights, s=10, color=colour, alpha=0.7, linewidths=0)
            median = statistics.median(values)
            ax.plot([median, median], [position - 0.32, position + 0.32], color="black", linewidth=1.4)

    for ax, title in ((upload_ax, "Upload"), (download_ax, "Download")):
        ax.set_title(title)
        ax.set_xlabel("MB/s")
        ax.set_xlim(left=0)
        # A thin line separates the baseline (last row) from the candidates.
        ax.axhline(len(backends) - 1.5, color="lightgray", linewidth=0.8)
    label_backend_axis(upload_ax, backends)
    fig.tight_layout()
    save_chart(fig, "performance", in_thesis=True)


def draw_resource_usage_chart(rows: list):
    """Memory (mean bar, peak tick) and CPU time per backend during the
    instrumented runs -- the same figures as the thesis's resource table.

    CPU is shown as time consumed over the window, not as a mean rate: a
    mean rate falls as a window lengthens, so it would make a slower backend
    look cheaper. CPU time is cumulative, so it has no peak to mark."""
    backends = [r["backend"] for r in rows]
    with open(RESOURCE_CSV) as f:
        usage = {row["backend"]: row for row in csv.DictReader(f)}
    colours = [BASELINE_COLOUR if b == "minio" else CANDIDATE_COLOUR for b in backends]
    positions = list(range(len(backends)))

    fig, (memory_ax, cpu_ax) = plt.subplots(1, 2, figsize=(5.8, 2.0), sharey=True)

    means = [float(usage[b]["mean_memory_MB"]) for b in backends]
    peaks = [float(usage[b]["peak_memory_MB"]) for b in backends]
    memory_ax.barh(positions, means, height=0.6, color=colours, label="mean")
    memory_ax.scatter(peaks, positions, marker="|", s=150, linewidths=1.6, color="black",
                      label="peak", zorder=3)
    memory_ax.set_xlabel("Memory (MB)")
    # Room on the right of the top rows, where the bars are shortest, so the
    # legend covers no data.
    memory_ax.set_xlim(0, max(peaks) * 1.3)
    memory_ax.legend(loc="upper right", frameon=False)

    cpu_seconds = [float(usage[b]["cpu_seconds"]) for b in backends]
    cpu_ax.barh(positions, cpu_seconds, height=0.6, color=colours)
    for position, value in zip(positions, cpu_seconds):
        cpu_ax.text(value, position, f" {value:.1f}", va="center")
    cpu_ax.set_xlabel("CPU time over the window (core-seconds)")
    cpu_ax.set_xlim(0, max(cpu_seconds) * 1.25)

    label_backend_axis(memory_ax, backends)
    fig.tight_layout()
    save_chart(fig, "resource_usage", in_thesis=True)


def draw_consolidated_chart(rows: list):
    """The three axes side by side, with the most favourable value on each
    one highlighted.

    The three panels have their own scales and their own units, and no
    composite score is computed from them. The figure shows where each
    backend stands on each axis so that the trade-offs between them are
    visible; it is not a ranking on one number."""
    backends = [r["backend"] for r in rows]
    positions = list(range(len(backends)))
    # (column, panel title, how the best value is picked, number format)
    panels = (
        ("capabilities_supported", f"Capabilities (of {len(OPTIONAL_FEATURES)})\nhigher is better", max, "{:.0f}"),
        ("upload_16MB_median_MBps", "Upload, 16 MB concurrent (MB/s)\nhigher is better", max, "{:.1f}"),
        ("peak_memory_MB", "Peak memory (MB)\nlower is better", min, "{:.0f}"),
    )

    fig, axes = plt.subplots(1, 3, figsize=(5.8, 1.75), sharey=True)
    for ax, (column, title, pick_best, number_format) in zip(axes, panels):
        values = [r[column] for r in rows]
        best = pick_best(values)
        colours = [CANDIDATE_COLOUR if v == best else "lightsteelblue" for v in values]
        ax.barh(positions, values, height=0.6, color=colours)
        upper = list(values)
        if column == "upload_16MB_median_MBps":
            # Whiskers span the middle half of the runs behind each median,
            # so the panel cannot be read as if the medians were exact.
            below, above = [], []
            for row, value in zip(rows, values):
                runs = sorted(large_object_runs(row["backend"])[0])
                half = len(runs) // 2
                below.append(value - statistics.median(runs[:half]))
                above.append(statistics.median(runs[half:]) - value)
            ax.errorbar(values, positions, xerr=[below, above], fmt="none",
                        ecolor="dimgray", elinewidth=1, capsize=2)
            upper = [v + a for v, a in zip(values, above)]
        # The values are printed at the end of each bar, so the x axis
        # itself is left out.
        for position, value, end in zip(positions, values, upper):
            ax.text(end, position, " " + number_format.format(value), va="center")
        ax.set_title(title)
        ax.set_xlim(0, max(upper) * 1.3)
        ax.set_xticks([])
        ax.spines["bottom"].set_visible(False)
    label_backend_axis(axes[0], backends)
    fig.tight_layout()
    save_chart(fig, "consolidated", in_thesis=True)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    rows = build_unified_table()
    save_unified_csv(rows)

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    THESIS_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    set_chart_style()
    draw_api_fidelity_chart(rows)
    draw_performance_chart(rows)
    draw_resource_usage_chart(rows)
    draw_consolidated_chart(rows)
    logger.info(f"Saved 4 charts to {CHARTS_DIR}, and the thesis figures to {THESIS_IMAGES_DIR}")


if __name__ == "__main__":
    main()
