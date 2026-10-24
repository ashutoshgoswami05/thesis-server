"""
Measures how far the harness and warp agree on the ordering of the backends
(thesis Section 7.3.4).

Usage: python analysis/instrument_agreement.py
Reads bench/results/benchmark_summary.csv and bench/results/warp_summary.csv
and writes bench/results/instrument_agreement.csv.

The harness figures are the medians of the 16MB concurrent workload, the
closest of its workloads to warp's four concurrent streams of 16MiB objects.
The agreement is Spearman's rank correlation: 1 means the same ordering, 0
no relation, -1 the reverse. With six backends there are only 720 possible
orderings, so the p-value is exact rather than approximated -- the share of
all orderings that correlate at least as strongly, in either direction.
"""

import csv
import itertools
import logging
from pathlib import Path

logger = logging.getLogger("thesis")

REPO_ROOT = Path(__file__).parent.parent
RESULTS_DIR = REPO_ROOT / "bench" / "results"
OUTPUT_CSV = RESULTS_DIR / "instrument_agreement.csv"

HEADLINE_SIZE = 16 * 1024 * 1024


def ranks(values):
    """Rank 1 for the smallest value. The data has no ties, which is checked
    by the caller, so no tie handling is needed."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0] * len(values)
    for rank, index in enumerate(order, start=1):
        result[index] = rank
    return result


def spearman(first_ranks, second_ranks):
    n = len(first_ranks)
    squared = sum((a - b) ** 2 for a, b in zip(first_ranks, second_ranks))
    return 1 - 6 * squared / (n * (n * n - 1))


def exact_p_value(first_ranks, rho):
    """Two-sided: how many of all possible orderings of the second list give
    a correlation at least as far from zero as the observed one."""
    n = len(first_ranks)
    at_least_as_extreme = 0
    total = 0
    for ordering in itertools.permutations(range(1, n + 1)):
        total += 1
        if abs(spearman(first_ranks, ordering)) >= abs(rho) - 1e-12:
            at_least_as_extreme += 1
    return at_least_as_extreme / total


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    harness = {}
    with open(RESULTS_DIR / "benchmark_summary.csv", newline="") as f:
        for row in csv.DictReader(f):
            if int(row["size_bytes"]) == HEADLINE_SIZE and row["parallel"] == "True":
                harness[row["backend"]] = {
                    "PUT": float(row["upload_median_MBps"]),
                    "GET": float(row["download_median_MBps"]),
                }

    warp = {}
    with open(RESULTS_DIR / "warp_summary.csv", newline="") as f:
        for row in csv.DictReader(f):
            warp.setdefault(row["backend"], {})[row["operation"]] = float(row["median_MiBps"])

    backends = sorted(b for b in harness if b in warp)
    rows = []
    for operation, label in (("PUT", "write"), ("GET", "read")):
        harness_values = [harness[b][operation] for b in backends]
        warp_values = [warp[b][operation] for b in backends]
        if len(set(harness_values)) < len(backends) or len(set(warp_values)) < len(backends):
            raise SystemExit(f"tied values for {operation}; this script assumes none")

        harness_ranks = ranks(harness_values)
        warp_ranks = ranks(warp_values)
        rho = spearman(harness_ranks, warp_ranks)
        p_value = exact_p_value(harness_ranks, rho)
        rows.append({
            "direction": label,
            "backends": len(backends),
            "spearman_rho": round(rho, 3),
            "exact_p_value": round(p_value, 3),
        })
        order = lambda ranking: " > ".join(b for _, b in sorted(zip(ranking, backends), reverse=True))
        logger.info(f"{label}: harness {order(harness_ranks)}")
        logger.info(f"{label}: warp    {order(warp_ranks)}")
        logger.info(f"{label}: rho = {rho:.3f}, exact two-sided p = {p_value:.3f}")

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Wrote {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
