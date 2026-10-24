"""
Generates the LaTeX result tables of Chapter 7 and the appendix from the
result files, so that no figure in the thesis is transcribed by hand.

Usage: python analysis/make_result_tables.py
Reads:
  tests/results/compatibility_matrix.csv
  bench/results/benchmark_summary.csv
  bench/results/resource_usage.csv
  bench/results/comparisons.csv
  bench/results/warp_summary.csv   (optional)
Writes latex/tables/*.tex, which Chapter 7 and the appendix include.
"""

import csv
import logging
import statistics
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("thesis")

REPO_ROOT = Path(__file__).parent.parent
RESULTS_DIR = REPO_ROOT / "bench" / "results"
MATRIX_CSV = REPO_ROOT / "tests" / "results" / "compatibility_matrix.csv"
TABLES_DIR = REPO_ROOT / "latex" / "tables"

BACKENDS = ["rustfs", "garage", "seaweedfs", "juicefs", "ceph", "minio"]
LABELS = {
    "rustfs": "RustFS",
    "garage": "Garage",
    "seaweedfs": "SeaweedFS",
    "juicefs": "JuiceFS",
    "ceph": "Ceph",
    "minio": "MinIO",
}

# The seven capabilities that are compared. Server-side encryption is
# measured but excluded, because the recorded responses reflect how a
# deployment was configured (thesis Section 7.1).
COMPARABLE = [
    ("bucket_versioning", "Bucket versioning"),
    ("object_tagging", "Object tagging"),
    ("bucket_policy", "Bucket policy"),
    ("object_lock", "Object Lock"),
    ("cors_configuration", r"\ac{CORS}"),
    ("presigned_url", r"Presigned \ac{URL}"),
    ("legacy_sigv2_auth", r"Legacy \ac{SigV2}"),
]

SIZES = [(1024, "1\\,KB"), (102400, "100\\,KB"), (1048576, "1\\,MB"), (16777216, "16\\,MB")]


def write(name, lines):
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    path = TABLES_DIR / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"wrote {path.relative_to(REPO_ROOT)}")


def load_matrix():
    matrix = defaultdict(dict)
    with open(MATRIX_CSV, newline="") as f:
        for row in csv.DictReader(f):
            matrix[row["feature"]][row["backend"]] = row
    return matrix


def load_summary():
    summary = {}
    with open(RESULTS_DIR / "benchmark_summary.csv", newline="") as f:
        for row in csv.DictReader(f):
            summary[(row["backend"], int(row["size_bytes"]), row["parallel"] == "True")] = row
    return summary


def capability_table(matrix):
    """Table 7.1: which optional capabilities each backend supports."""
    lines = [
        r"\begin{tabular}{lcccccc}",
        r"\toprule",
        r"\textbf{Capability} & " + " & ".join(rf"\textbf{{{LABELS[b]}}}" for b in BACKENDS) + r" \\",
        r"\midrule",
    ]
    totals = {b: 0 for b in BACKENDS}
    for feature, label in COMPARABLE:
        cells = []
        for backend in BACKENDS:
            supported = matrix[feature].get(backend, {}).get("supported") == "True"
            totals[backend] += supported
            cells.append(r"\checkmark" if supported else "---")
        lines.append(f"{label} & " + " & ".join(cells) + r" \\")
    lines += [
        r"\midrule",
        rf"\textbf{{Total (of {len(COMPARABLE)})}} & "
        + " & ".join(rf"\textbf{{{totals[b]}}}" for b in BACKENDS)
        + r" \\",
        r"\bottomrule",
        r"\end{tabular}",
    ]
    write("capabilities.tex", lines)
    return totals


def throughput_table(summary, size, name):
    """Tables 7.2 and 7.3: median throughput with the coefficient of
    variation, for one object size under both access patterns."""
    lines = [
        r"\begin{tabular}{lrrrr}",
        r"\toprule",
        r"& \multicolumn{2}{c}{\textbf{Sequential}} & \multicolumn{2}{c}{\textbf{Concurrent}} \\",
        r"\cmidrule(lr){2-3}\cmidrule(lr){4-5}",
        r"\textbf{Backend} & \textbf{Upload} & \textbf{Download} & \textbf{Upload} &"
        r" \textbf{Download} \\",
        r"\midrule",
    ]
    for backend in BACKENDS:
        cells = []
        for parallel in (False, True):
            row = summary.get((backend, size, parallel))
            if not row:
                cells += ["---", "---"]
                continue
            for direction in ("upload", "download"):
                median = float(row[f"{direction}_median_MBps"])
                cv = float(row[f"{direction}_cv_percent"])
                cells.append(f"{median:.2f} ({cv:.0f}\\,\\%)")
        label = LABELS[backend]
        if backend == "minio":
            lines.append(r"\midrule")
            lines.append(
                r"\textit{MinIO (baseline)} & "
                + " & ".join(rf"\textit{{{c}}}" for c in cells)
                + r" \\"
            )
        else:
            lines.append(f"{label} & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    write(name, lines)


def resource_table():
    """Table 7.6: what each backend cost to run during the instrumented
    pass. CPU time is the figure to compare, because the windows differ in
    length."""
    with open(RESULTS_DIR / "resource_usage.csv", newline="") as f:
        usage = {row["backend"]: row for row in csv.DictReader(f)}
    lines = [
        r"\begin{tabular}{lrrrrrr}",
        r"\toprule",
        r"\textbf{Backend} & \textbf{Mean mem.} & \textbf{Peak mem.} & \textbf{Peak swap} &"
        r" \textbf{\ac{CPU} time} & \textbf{Window} & \textbf{Samples} \\",
        r"\midrule",
    ]
    for backend in BACKENDS:
        row = usage.get(backend)
        if not row or not row["mean_memory_MB"]:
            continue
        cells = [
            f"{float(row['mean_memory_MB']):.1f}\\,MB",
            f"{float(row['peak_memory_MB']):.1f}\\,MB",
            f"{float(row.get('peak_swap_MB') or 0):.1f}\\,MB",
            f"{float(row['cpu_seconds']):.1f}\\,s",
            f"{int(float(row['window_seconds']))}\\,s",
            row["memory_samples"],
        ]
        if backend == "minio":
            lines.append(r"\midrule")
            lines.append(
                r"\textit{MinIO (baseline)} & "
                + " & ".join(rf"\textit{{{c}}}" for c in cells)
                + r" \\"
            )
        else:
            lines.append(f"{LABELS[backend]} & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    write("resources.tex", lines)


def consolidated_table(summary, totals):
    """Table 7.7: the three axes side by side."""
    with open(RESULTS_DIR / "resource_usage.csv", newline="") as f:
        usage = {row["backend"]: row for row in csv.DictReader(f)}
    lines = [
        r"\begin{tabular}{lrrrrr}",
        r"\toprule",
        r"\textbf{Backend} & \textbf{Capabilities} & \textbf{Upload} & \textbf{Download} &"
        r" \textbf{Peak mem.} & \textbf{\ac{CPU} time} \\",
        r"\midrule",
    ]
    for backend in BACKENDS:
        row = summary.get((backend, 16777216, True))
        resource = usage.get(backend, {})
        cells = [
            f"{totals[backend]}/{len(COMPARABLE)}",
            f"{float(row['upload_median_MBps']):.1f}\\,MB/s" if row else "---",
            f"{float(row['download_median_MBps']):.1f}\\,MB/s" if row else "---",
            f"{float(resource['peak_memory_MB']):.0f}\\,MB" if resource.get("peak_memory_MB") else "---",
            f"{float(resource['cpu_seconds']):.0f}\\,s" if resource.get("cpu_seconds") else "---",
        ]
        if backend == "minio":
            lines.append(r"\midrule")
            lines.append(
                r"\textit{MinIO (baseline)} & "
                + " & ".join(rf"\textit{{{c}}}" for c in cells)
                + r" \\"
            )
        else:
            lines.append(f"{LABELS[backend]} & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    write("consolidated.tex", lines)


def comparison_table():
    """Table 7.4: the differences against the baseline that survive the
    decision rule of Section 6.3."""
    path = RESULTS_DIR / "comparisons.csv"
    if not path.exists():
        logger.warning("no comparisons.csv; skipping the significance table")
        return
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if "backend vs baseline" in r["comparison"]]
    lines = [
        r"\begin{tabular}{llrrrl}",
        r"\toprule",
        r"\textbf{Comparison} & \textbf{Backend} & \textbf{Median} & \textbf{Baseline} &"
        r" \textbf{95\,\% \acs{CI}} & \textbf{Supported} \\",
        r"\midrule",
    ]
    for row in rows:
        direction = "upload" if "upload" in row["comparison"] else "download"
        lines.append(
            f"16\\,MB {direction} & {LABELS[row['group_a']]} & "
            f"{float(row['median_a']):.1f} & {float(row['median_b']):.1f} & "
            f"[{float(row['ci_low']):.1f}, {float(row['ci_high']):.1f}] & "
            + ("yes" if row["supported"] == "True" else "no")
            + r" \\"
        )
    lines += [r"\bottomrule", r"\end{tabular}"]
    write("comparisons.tex", lines)


def appendix_workload_table(summary):
    """The complete per-workload statistics, which Chapter 7 summarises."""
    lines = [
        r"\begin{longtable}{@{}llrrrrrr@{}}",
        r"\caption{Every workload point in MB/s: median, mean and coefficient of variation"
        r" over 20 runs.}\label{tab:allworkloads}\\",
        r"\toprule",
        r"& & \multicolumn{3}{c}{\textbf{Upload}} &"
        r" \multicolumn{3}{c}{\textbf{Download}} \\",
        r"\cmidrule(lr){3-5}\cmidrule(lr){6-8}",
        r"\textbf{Backend} & \textbf{Workload} & \textbf{med.} &"
        r" \textbf{mean} & \textbf{\acs{CV}} & \textbf{med.} & \textbf{mean} &"
        r" \textbf{\acs{CV}} \\",
        r"\midrule",
        r"\endfirsthead",
        r"\toprule",
        r"& & \multicolumn{3}{c}{\textbf{Upload}} &"
        r" \multicolumn{3}{c}{\textbf{Download}} \\",
        r"\cmidrule(lr){3-5}\cmidrule(lr){6-8}",
        r"\textbf{Backend} & \textbf{Workload} & \textbf{med.} &"
        r" \textbf{mean} & \textbf{\acs{CV}} & \textbf{med.} & \textbf{mean} &"
        r" \textbf{\acs{CV}} \\",
        r"\midrule",
        r"\endhead",
        r"\bottomrule",
        r"\endlastfoot",
    ]
    for backend in BACKENDS:
        for size, size_label in SIZES:
            for parallel in (False, True):
                row = summary.get((backend, size, parallel))
                if not row:
                    continue
                pattern = "conc." if parallel else "seq."
                lines.append(
                    f"{LABELS[backend]} & {size_label} {pattern} & "
                    f"{float(row['upload_median_MBps']):.2f} & {float(row['upload_mean_MBps']):.2f} & "
                    f"{float(row['upload_cv_percent']):.0f}\\,\\% & "
                    f"{float(row['download_median_MBps']):.2f} & {float(row['download_mean_MBps']):.2f} & "
                    f"{float(row['download_cv_percent']):.0f}\\,\\% " + r"\\"
                )
    lines.append(r"\end{longtable}")
    write("all_workloads.tex", lines)


def appendix_capability_table(matrix):
    """The complete capability matrix, including what each backend
    answered, which the summary table in Chapter 7 leaves out."""
    lines = [
        r"\begin{longtable}{@{}p{3.0cm}p{1.8cm}p{1.4cm}p{7.0cm}@{}}",
        r"\caption{Every recorded capability result, with the response that decided"
        r" it.}\label{tab:allcapabilities}\\",
        r"\toprule",
        r"\textbf{Capability} & \textbf{Backend} & \textbf{Result} & \textbf{Recorded response} \\",
        r"\midrule",
        r"\endfirsthead",
        r"\toprule",
        r"\textbf{Capability} & \textbf{Backend} & \textbf{Result} & \textbf{Recorded response} \\",
        r"\midrule",
        r"\endhead",
        r"\bottomrule",
        r"\endlastfoot",
    ]

    def escape(text):
        for old, new in (("\\", ""), ("_", r"\_"), ("&", r"\&"), ("%", r"\%"),
                         ("#", r"\#"), ("{", r"\{"), ("}", r"\}"), ("$", r"\$")):
            text = text.replace(old, new)
        return text

    for feature in sorted(matrix):
        for backend in BACKENDS:
            row = matrix[feature].get(backend)
            if not row:
                continue
            detail = escape(row["detail"])[:150]
            lines.append(
                f"\\texttt{{{escape(feature)}}} & {LABELS[backend]} & "
                f"{'yes' if row['supported'] == 'True' else 'no'} & {detail} " + r"\\"
            )
    lines.append(r"\end{longtable}")
    write("all_capabilities.tex", lines)


def warp_table():
    """The cross-validation table: warp's per-second throughput buckets."""
    path = RESULTS_DIR / "warp_summary.csv"
    if not path.exists():
        logger.warning("no warp_summary.csv; skipping the cross-validation table")
        return
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    by_backend = defaultdict(dict)
    for row in rows:
        by_backend[row["backend"]][row["operation"]] = row

    lines = [
        r"\begin{tabular}{lrrrr}",
        r"\toprule",
        r"\textbf{Backend} & \textbf{PUT median} & \textbf{PUT range} &"
        r" \textbf{GET median} & \textbf{GET range} \\",
        r"\midrule",
    ]
    for backend in BACKENDS:
        entry = by_backend.get(backend)
        if not entry or "PUT" not in entry or "GET" not in entry:
            continue
        put, get = entry["PUT"], entry["GET"]
        lines.append(
            f"{LABELS[backend]} & {float(put['median_MiBps']):.1f} MiB/s & "
            f"{float(put['slowest_MiBps']):.1f}--{float(put['fastest_MiBps']):.1f} & "
            f"{float(get['median_MiBps']):.1f} MiB/s & "
            f"{float(get['slowest_MiBps']):.1f}--{float(get['fastest_MiBps']):.1f}"
            + r" \\"
        )
    lines += [r"\bottomrule", r"\end{tabular}"]
    write("warp.tex", lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    matrix = load_matrix()
    summary = load_summary()

    totals = capability_table(matrix)
    throughput_table(summary, 16777216, "perf16mb.tex")
    throughput_table(summary, 1024, "perf1kb.tex")
    resource_table()
    consolidated_table(summary, totals)
    comparison_table()
    warp_table()
    appendix_workload_table(summary)
    appendix_capability_table(matrix)
    logger.info(f"capability totals: {totals}")


if __name__ == "__main__":
    main()
