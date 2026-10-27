"""
Checks how much the differences the thesis reports as supported depend on
two choices it made: running twenty repeats, and applying the Holm
correction across every comparison at once (thesis Sections 6.3 and 7.3).

Usage: python analysis/sensitivity.py
Reads bench/results/benchmark_results.csv and writes
bench/results/sensitivity.csv.

Repeats: the decision rule of compare_backends.py is re-applied to the first
5, 10 and 15 repeats of the campaign, which is exactly what a shorter
campaign would have produced, since every repeat runs all eight workloads.

Family: the same rule is applied with the correction split into two
families, the ten candidate-against-baseline comparisons and the 48
concurrent-against-sequential ones, to show which results would change.
"""

import csv
import logging
import math
from collections import defaultdict
from pathlib import Path

import compare_backends

logger = logging.getLogger("thesis")

REPO_ROOT = Path(__file__).parent.parent
RESULTS_DIR = REPO_ROOT / "bench" / "results"
OUTPUT_CSV = RESULTS_DIR / "sensitivity.csv"

REPEAT_COUNTS = (5, 10, 15, 20)
WATCHED = ("16MB concurrent upload: backend vs baseline", "rustfs")


def load_runs_with_repeats():
    """Returns {(backend, size, parallel): {direction: [(repeat, MB/s), ...]}}."""
    runs = defaultdict(lambda: defaultdict(list))
    with open(RESULTS_DIR / "benchmark_results.csv", newline="") as f:
        for row in csv.DictReader(f):
            if row["integrity_ok"] != "True":
                continue
            key = (row["backend"], int(row["size_bytes"]), row["parallel"] == "True")
            repeat = int(row["repeat_index"])
            runs[key]["upload"].append((repeat, float(row["bandwidth_upload_Bps"]) / 1_000_000))
            runs[key]["download"].append((repeat, float(row["bandwidth_download_Bps"]) / 1_000_000))
    return runs


def first_repeats(runs, count):
    """The runs a campaign of `count` repeats would have recorded."""
    result = defaultdict(lambda: defaultdict(list))
    for key, directions in runs.items():
        for direction, pairs in directions.items():
            result[key][direction] = [value for repeat, value in pairs if repeat < count]
    return result


def build_comparisons(runs):
    baseline, concurrency = [], []
    for direction in ("upload", "download"):
        baseline += compare_backends.backend_comparisons(runs, direction)
        concurrency += compare_backends.concurrency_comparisons(runs, direction)
    return baseline, concurrency


def supported(comparisons):
    """Applies the thesis's decision rule to one correction family."""
    compare_backends.holm_correction(comparisons)
    return {
        (c["comparison"], c["group_a"])
        for c in comparisons
        if c["p_holm"] < 0.05 and (c["ci_low"] > 0 or c["ci_high"] < 0)
    }


def smallest_p_value(n):
    """The smallest two-sided p-value the harness's Mann-Whitney test can
    return for two groups of n runs: every run of one group beats every run
    of the other."""
    z = (n * n / 2) / math.sqrt(n * n / 12 * (2 * n + 1))
    return math.erfc(z / math.sqrt(2))


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    all_runs = load_runs_with_repeats()
    rows = []

    previous = set()
    for count in REPEAT_COUNTS:
        baseline, concurrency = build_comparisons(first_repeats(all_runs, count))
        family = baseline + concurrency
        result = supported(family)
        watched = next(c for c in family if (c["comparison"], c["group_a"]) == WATCHED)
        smallest = smallest_p_value(count) * len(family)
        logger.info(
            f"{count:2d} repeats: {len(result):2d} of {len(family)} supported; "
            f"contains every one supported at fewer repeats: {previous <= result}; "
            f"smallest attainable corrected p {min(1.0, smallest):.3g}; "
            f"RustFS upload vs baseline corrected p {watched['p_holm']}"
        )
        rows.append({
            "analysis": "repeats",
            "setting": count,
            "comparisons": len(family),
            "supported": len(result),
            "nested_in_next": "",
            "smallest_corrected_p": f"{min(1.0, smallest):.3g}",
            "rustfs_upload_p_holm": watched["p_holm"],
        })
        if rows[-2:-1]:
            rows[-2]["nested_in_next"] = previous <= result
        previous = result

    baseline, concurrency = build_comparisons(first_repeats(all_runs, max(REPEAT_COUNTS)))
    one_family = supported([dict(c) for c in baseline + concurrency])
    split = supported([dict(c) for c in baseline]) | supported([dict(c) for c in concurrency])
    for label, result in (("one family of 58", one_family), ("families of 10 and 48", split)):
        rows.append({
            "analysis": "family",
            "setting": label,
            "comparisons": len(baseline) + len(concurrency),
            "supported": len(result),
            "nested_in_next": "",
            "smallest_corrected_p": "",
            "rustfs_upload_p_holm": "",
        })
    logger.info(f"One family: {len(one_family)} supported; split families: {len(split)} supported")
    for comparison, group in sorted(split - one_family):
        logger.info(f"  passes only when the family is split: {group}, {comparison}")

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Wrote {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
