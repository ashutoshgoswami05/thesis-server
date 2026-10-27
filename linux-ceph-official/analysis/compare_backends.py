"""
Decides which differences between backends the benchmark data actually
supports (thesis Section 6.3 and Chapter 7).

Usage: python analysis/compare_backends.py [input_csv_name]
Reads bench/results/benchmark_results.csv (or the file named as the
argument) and writes bench/results/comparisons.csv.

The runs are not normally distributed -- the distributions are skewed and
their spread differs between backends -- so this uses distribution-free
methods throughout:

  * a bootstrap 95% confidence interval for the difference of medians,
    which says how large the difference could plausibly be;
  * the Mann-Whitney U test, which asks whether one backend's runs tend to
    be faster than another's;
  * Cliff's delta, which says how consistently, on a scale from -1 to 1;
  * the Holm correction, because comparing six backends means many tests
    and some would look significant by chance.

The decision rule the thesis uses: a difference is reported as supported
only when the corrected p-value is below 0.05 AND the confidence interval
excludes zero.
"""

import csv
import logging
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

logger = logging.getLogger("thesis")

REPO_ROOT = Path(__file__).parent.parent
RESULTS_DIR = REPO_ROOT / "bench" / "results"
OUTPUT_CSV = RESULTS_DIR / "comparisons.csv"

BACKENDS = ["rustfs", "garage", "seaweedfs", "juicefs", "ceph", "minio"]
BOOTSTRAP_SAMPLES = 10000
BOOTSTRAP_SEED = 20260912

# The workload the thesis reports as its headline throughput figure.
HEADLINE_SIZE = 16 * 1024 * 1024


def load_runs(input_path):
    """Returns {(backend, size_bytes, parallel): {direction: [MB/s, ...]}}."""
    runs = defaultdict(lambda: defaultdict(list))
    with open(input_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["integrity_ok"] != "True":
                continue
            key = (row["backend"], int(row["size_bytes"]), row["parallel"] == "True")
            runs[key]["upload"].append(float(row["bandwidth_upload_Bps"]) / 1_000_000)
            runs[key]["download"].append(float(row["bandwidth_download_Bps"]) / 1_000_000)
    logger.info(f"Loaded {len(runs)} workload points from {input_path}")
    return runs


def bootstrap_median_difference(first, second):
    """Returns the 95% confidence interval for median(first) - median(second),
    by resampling both groups with replacement."""
    rng = random.Random(BOOTSTRAP_SEED)
    differences = []
    for _ in range(BOOTSTRAP_SAMPLES):
        a = statistics.median(rng.choices(first, k=len(first)))
        b = statistics.median(rng.choices(second, k=len(second)))
        differences.append(a - b)
    differences.sort()
    low = differences[int(0.025 * BOOTSTRAP_SAMPLES)]
    high = differences[int(0.975 * BOOTSTRAP_SAMPLES) - 1]
    return low, high


def mann_whitney_p(first, second):
    """Two-sided p-value from the normal approximation, corrected for ties.
    With 20 runs per group the approximation is close enough."""
    combined = sorted([(value, 0) for value in first] + [(value, 1) for value in second])
    ranks = [0.0] * len(combined)
    index = 0
    while index < len(combined):
        last = index
        while last + 1 < len(combined) and combined[last + 1][0] == combined[index][0]:
            last += 1
        for position in range(index, last + 1):
            ranks[position] = (index + last) / 2 + 1
        index = last + 1

    n1, n2 = len(first), len(second)
    rank_sum = sum(rank for rank, (_, group) in zip(ranks, combined) if group == 0)
    u = rank_sum - n1 * (n1 + 1) / 2
    ties = Counter(value for value, _ in combined)
    n = n1 + n2
    tie_term = sum(count**3 - count for count in ties.values()) / (n * (n - 1))
    sigma = math.sqrt(n1 * n2 / 12 * ((n + 1) - tie_term))
    if sigma == 0:
        return 1.0
    z = (u - n1 * n2 / 2) / sigma
    return math.erfc(abs(z) / math.sqrt(2))


def cliffs_delta(first, second):
    """How often a run from the first group beats one from the second, minus
    how often it loses. 0 means the groups overlap completely, 1 means every
    run in the first group is faster."""
    greater = sum(1 for a in first for b in second if a > b)
    smaller = sum(1 for a in first for b in second if a < b)
    return (greater - smaller) / (len(first) * len(second))


def describe_delta(delta):
    """The usual thresholds for reading Cliff's delta."""
    size = abs(delta)
    if size < 0.147:
        return "negligible"
    if size < 0.33:
        return "small"
    if size < 0.474:
        return "medium"
    return "large"


def holm_correction(comparisons):
    """Adds a Holm-corrected p-value to each comparison, in place. Sorting by
    raw p-value and scaling by the number of tests still outstanding keeps
    the chance of any false positive across the whole family below 5%."""
    ordered = sorted(comparisons, key=lambda c: c["p_value"])
    total = len(ordered)
    running_max = 0.0
    for position, comparison in enumerate(ordered):
        corrected = min(1.0, comparison["p_value"] * (total - position))
        running_max = max(running_max, corrected)
        comparison["p_holm"] = round(running_max, 4)


def compare(label, first, second, first_name, second_name):
    low, high = bootstrap_median_difference(first, second)
    delta = cliffs_delta(first, second)
    return {
        "comparison": label,
        "group_a": first_name,
        "group_b": second_name,
        "median_a": round(statistics.median(first), 2),
        "median_b": round(statistics.median(second), 2),
        "median_difference": round(statistics.median(first) - statistics.median(second), 2),
        "ci_low": round(low, 2),
        "ci_high": round(high, 2),
        "p_value": round(mann_whitney_p(first, second), 4),
        "p_holm": None,
        "cliffs_delta": round(delta, 2),
        "effect": describe_delta(delta),
        "supported": None,
    }


def backend_comparisons(runs, direction):
    """Every candidate against the MinIO baseline, on the headline workload."""
    comparisons = []
    for backend in BACKENDS:
        if backend == "minio":
            continue
        candidate = runs[(backend, HEADLINE_SIZE, True)][direction]
        baseline = runs[("minio", HEADLINE_SIZE, True)][direction]
        if candidate and baseline:
            comparisons.append(
                compare(f"16MB concurrent {direction}: backend vs baseline",
                        candidate, baseline, backend, "minio")
            )
    return comparisons


def concurrency_comparisons(runs, direction):
    """Concurrent against sequential, for every backend and object size."""
    comparisons = []
    for backend in BACKENDS:
        for size in sorted({size for _, size, _ in runs}):
            concurrent = runs[(backend, size, True)][direction]
            sequential = runs[(backend, size, False)][direction]
            if concurrent and sequential:
                comparisons.append(
                    compare(f"{size}B {direction}: concurrent vs sequential",
                            concurrent, sequential, f"{backend} concurrent",
                            f"{backend} sequential")
                )
    return comparisons


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    input_name = sys.argv[1] if len(sys.argv) > 1 else "benchmark_results.csv"
    runs = load_runs(RESULTS_DIR / input_name)

    comparisons = []
    for direction in ("upload", "download"):
        comparisons += backend_comparisons(runs, direction)
        comparisons += concurrency_comparisons(runs, direction)

    holm_correction(comparisons)
    for comparison in comparisons:
        excludes_zero = comparison["ci_low"] > 0 or comparison["ci_high"] < 0
        comparison["supported"] = comparison["p_holm"] < 0.05 and excludes_zero

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(comparisons[0].keys()))
        writer.writeheader()
        writer.writerows(comparisons)
    logger.info(f"Wrote {len(comparisons)} comparisons to {OUTPUT_CSV}")

    # A short report, so the supported differences are visible without
    # opening the file.
    logger.info("Differences the data supports (Holm-corrected p < 0.05 and CI excluding zero):")
    for comparison in comparisons:
        if comparison["supported"]:
            logger.info(
                f"  {comparison['group_a']} vs {comparison['group_b']}: "
                f"{comparison['median_a']} vs {comparison['median_b']} MB/s, "
                f"difference {comparison['median_difference']} "
                f"[{comparison['ci_low']}, {comparison['ci_high']}], "
                f"p={comparison['p_holm']}, delta={comparison['cliffs_delta']} "
                f"({comparison['effect']})"
            )


if __name__ == "__main__":
    main()
