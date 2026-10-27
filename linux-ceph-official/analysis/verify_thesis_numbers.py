"""Cross-checks the figures the thesis quotes against the result files, so
that a stale number cannot survive a re-run of the campaign unnoticed. Every
line marked BAD is a figure in the prose that the data no longer supports.

Usage: python analysis/verify_thesis_numbers.py
"""
import csv
import re
from pathlib import Path

ROOT = Path("D:/Thesis")
LATEX = ROOT / "latex"

summary = {}
with open(ROOT / "bench/results/benchmark_summary.csv", newline="") as f:
    for row in csv.DictReader(f):
        summary[(row["backend"], int(row["size_bytes"]), row["parallel"] == "True")] = row

resource = {}
with open(ROOT / "bench/results/resource_usage.csv", newline="") as f:
    for row in csv.DictReader(f):
        resource[row["backend"]] = row

warp = {}
with open(ROOT / "bench/results/warp_summary.csv", newline="") as f:
    for row in csv.DictReader(f):
        warp[(row["backend"], row["operation"])] = row

caps = {}
with open(ROOT / "tests/results/compatibility_matrix.csv", newline="") as f:
    for row in csv.DictReader(f):
        caps.setdefault(row["backend"], {})[row["feature"]] = row["supported"] == "True"

COMPARABLE = ["bucket_versioning", "object_tagging", "bucket_policy", "object_lock",
              "cors_configuration", "presigned_url", "legacy_sigv2_auth"]

sources = sorted(LATEX.glob("chapters/*.tex")) + [LATEX / "Thesis.tex", LATEX / "appendix.tex"]
text = "\n".join(p.read_text(encoding="utf-8") for p in sources)


def check(label, claimed, actual, tolerance=0.05):
    ok = abs(claimed - actual) <= tolerance * max(abs(actual), 1e-9)
    print(f"  [{'ok ' if ok else 'BAD'}] {label}: thesis {claimed}, data {actual:.4g}")
    return ok


print("=== 16 MB concurrent medians quoted in the prose ===")
for backend, upload, download in [("rustfs", 77.2, 189.1), ("minio", 35.5, 155.3)]:
    row = summary[(backend, 16777216, True)]
    check(f"{backend} upload", upload, float(row["upload_median_MBps"]))
    check(f"{backend} download", download, float(row["download_median_MBps"]))

print("=== resource figures quoted in the prose ===")
for backend, peak, cpu in [("rustfs", 312, 30.9), ("minio", 734, 40.1),
                           ("ceph", 1092, 55.5), ("juicefs", 1034, 53.8),
                           ("seaweedfs", 894, 79.4), ("garage", 349, 42.8)]:
    check(f"{backend} peak memory", peak, float(resource[backend]["peak_memory_MB"]))
    check(f"{backend} CPU time", cpu, float(resource[backend]["cpu_seconds"]))

print("=== stated spans ===")
peaks = [float(r["peak_memory_MB"]) for r in resource.values()]
cpus = [float(r["cpu_seconds"]) for r in resource.values()]
check("peak memory factor", 3.5, max(peaks) / min(peaks))
check("CPU time factor", 2.6, max(cpus) / min(cpus))
check("Ceph mean memory", 1027.4, float(resource["ceph"]["mean_memory_MB"]))
check("Garage mean memory", 181.2, float(resource["garage"]["mean_memory_MB"]))

print("=== capability totals ===")
for backend in ["rustfs", "garage", "seaweedfs", "juicefs", "ceph", "minio"]:
    total = sum(caps[backend].get(f, False) for f in COMPARABLE)
    print(f"  {backend:10s} {total}/7")

print("=== warp medians quoted in the prose ===")
check("ceph PUT", 92.1, float(warp[("ceph", "PUT")]["median_MiBps"]))
check("ceph GET", 248.6, float(warp[("ceph", "GET")]["median_MiBps"]))
check("minio GET slowest", 3.9, float(warp[("minio", "GET")]["slowest_MiBps"]))
check("minio GET fastest", 474.0, float(warp[("minio", "GET")]["fastest_MiBps"]))

print("=== dispersion ===")
cvs = []
for row in summary.values():
    cvs += [float(row["upload_cv_percent"]), float(row["download_cv_percent"])]
check("max CV", 111, max(cvs))
print(f"  distributions: {len(cvs)}, below 30%: {sum(c < 30 for c in cvs)},"
      f" above 60%: {sum(c > 60 for c in cvs)}")

print("=== Ceph download deficit ===")
with open(ROOT / "bench/results/comparisons.csv", newline="") as f:
    for row in csv.DictReader(f):
        if row["group_a"] == "ceph" and "download" in row["comparison"]:
            print("  ", {k: row[k] for k in
                         ("median_difference", "ci_low", "ci_high",
                          "cliffs_delta", "p_holm", "supported")})

print("=== instrument agreement (analysis/instrument_agreement.py) ===")
agreement = {}
with open(ROOT / "bench/results/instrument_agreement.csv", newline="") as f:
    for row in csv.DictReader(f):
        agreement[row["direction"]] = row
check("write rho", 0.14, float(agreement["write"]["spearman_rho"]))
check("read rho", -0.60, float(agreement["read"]["spearman_rho"]))
check("write exact p", 0.80, float(agreement["write"]["exact_p_value"]))
check("read exact p", 0.24, float(agreement["read"]["exact_p_value"]))

print("=== repeat count and correction family (analysis/sensitivity.py) ===")
repeats, families = {}, {}
with open(ROOT / "bench/results/sensitivity.csv", newline="") as f:
    for row in csv.DictReader(f):
        if row["analysis"] == "repeats":
            repeats[int(row["setting"])] = row
        else:
            families[row["setting"]] = row
check("family size", 58, float(repeats[20]["comparisons"]))
for count, claimed in ((5, 0), (10, 9), (15, 24), (20, 35)):
    print(f"  [{'ok ' if int(repeats[count]['supported']) == claimed else 'BAD'}] "
          f"supported at {count} repeats: thesis {claimed}, data {repeats[count]['supported']}")
check("smallest corrected p at 5 repeats", 0.52, float(repeats[5]["smallest_corrected_p"]))
check("RustFS corrected p at 15 repeats", 0.78, float(repeats[15]["rustfs_upload_p_holm"]))
check("RustFS corrected p at 20 repeats", 0.067, float(repeats[20]["rustfs_upload_p_holm"]))
split_gain = int(families["families of 10 and 48"]["supported"]) - int(families["one family of 58"]["supported"])
print(f"  [{'ok ' if split_gain == 3 else 'BAD'}] differences gained by splitting the family:"
      f" thesis 3, data {split_gain}")

print("=== run and test counts in the prose ===")
runs = sum(1 for _ in open(ROOT / "bench/results/benchmark_results.csv")) - 1
mon = sum(1 for _ in open(ROOT / "bench/results/monitoring_benchmark_results.csv")) - 1
print(f"  benchmark rows {runs}, instrumented rows {mon}")
print(f"  thesis says 960: {'960 benchmark runs' in text}")
print(f"  thesis says 23 tests: {len(re.findall(r'23 (fidelity )?tests', text))} mentions")
print(f"  '22 test' still present: {len(re.findall(r'22 (fidelity )?tests', text))}")
