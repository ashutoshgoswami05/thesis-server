"""
Extracts the throughput figures from warp's text reports into a CSV, so the
cross-validation results can sit alongside the harness's own numbers
(thesis Ch. 7.3).

Usage: python analysis/parse_warp.py
Reads bench/results/warp/<backend>.txt and writes
bench/results/warp_summary.csv.

warp reports throughput split into one-second buckets and prints the
fastest, median and slowest of those buckets per operation type. Those
three figures are what matter here: the spread between fastest and slowest
within a single uninterrupted run is an independent measure of how stable
this host is, measured by a tool that shares no code with
bench/benchmark.py.
"""

import csv
import logging
import re
from pathlib import Path

logger = logging.getLogger("thesis")

BOX_RULE = "\u2500" * 10  # warp draws its table separators with box-drawing characters

REPO_ROOT = Path(__file__).parent.parent
WARP_DIR = REPO_ROOT / "bench" / "results" / "warp"
OUTPUT_CSV = REPO_ROOT / "bench" / "results" / "warp_summary.csv"

CSV_FIELDS = [
    "backend",
    "operation",
    "median_MiBps",
    "fastest_MiBps",
    "slowest_MiBps",
    "spread_ratio",
]

# Matches e.g. " * 50% Median: 29.7MiB/s, 1.86 obj/s" and also the KiB/s
# form warp switches to for slow seconds (" * Slowest: 1617.3KiB/s, ...").
# A line that reports only "obj/s" carries no throughput figure at all,
# which happens when a whole second passed with nothing completing; that
# case is handled separately as zero rather than being dropped.
THROUGHPUT_LINE = re.compile(
    r"\*\s+(Fastest|50% Median|Slowest):\s+([\d.]+)(MiB|KiB|GiB)/s"
)
NO_THROUGHPUT_LINE = re.compile(r"\*\s+(Fastest|50% Median|Slowest):\s+0\.00 obj/s")

TO_MIB = {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0}


def parse_report(text, operation):
    """Returns (median, fastest, slowest) in MiB/s for one operation's
    report block, or None if that operation is absent or reported no
    throughput (warp omits MiB/s for metadata-only operations such as
    STAT and DELETE)."""
    marker = f"Report: {operation}."
    start = text.find(marker)
    if start == -1:
        return None

    # The block runs until the next report separator.
    end = text.find(BOX_RULE, start)
    block = text[start:end] if end != -1 else text[start:]

    values = {}
    for label, number, unit in THROUGHPUT_LINE.findall(block):
        values[label] = round(float(number) * TO_MIB[unit], 2)

    # A second in which nothing completed is zero throughput, not missing
    # data, and it matters: it is the clearest evidence of a stall.
    for label in NO_THROUGHPUT_LINE.findall(block):
        values.setdefault(label, 0.0)

    if "50% Median" not in values:
        return None
    return values.get("50% Median"), values.get("Fastest"), values.get("Slowest")


def summarize_backend(path):
    backend = path.stem
    text = path.read_text(encoding="utf-8", errors="replace")

    if "Error preparing server" in text or "<ERROR>" in text and "Report:" not in text:
        logger.warning(f"{backend}: warp did not complete a run -- see {path.name}")
        return []

    rows = []
    for operation in ("PUT", "GET"):
        parsed = parse_report(text, operation)
        if parsed is None:
            continue
        median, fastest, slowest = parsed
        # How far the best second of the run was from the worst. A ratio
        # near 1 means steady; a large ratio means the host delivered very
        # different throughput from one second to the next. Left empty when
        # the slowest second was a complete stall, since the ratio would be
        # unbounded rather than merely large.
        spread = round(fastest / slowest, 1) if slowest else None
        rows.append(
            {
                "backend": backend,
                "operation": operation,
                "median_MiBps": median,
                "fastest_MiBps": fastest,
                "slowest_MiBps": slowest,
                "spread_ratio": spread,
            }
        )
    return rows


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    rows = []
    for path in sorted(WARP_DIR.glob("*.txt")):
        rows.extend(summarize_backend(path))

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"Wrote {len(rows)} rows to {OUTPUT_CSV}")
    for row in rows:
        # warp does not always report every figure for every operation, so
        # format defensively rather than assuming all three are present.
        low = row["slowest_MiBps"]
        high = row["fastest_MiBps"]
        span = f"{low}-{high}" if low is not None and high is not None else "n/a"
        logger.info(
            f"  {row['backend']:10s} {row['operation']:4s} "
            f"median={row['median_MiBps']:7.1f} MiB/s  "
            f"range {span:>15s}  spread x{row['spread_ratio']}"
        )


if __name__ == "__main__":
    main()
