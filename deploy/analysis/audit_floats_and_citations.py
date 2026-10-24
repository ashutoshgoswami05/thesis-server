"""Audits the document itself: every figure, table and listing referenced at
least once, every reference resolved, every citation present in the
bibliography, and the terminology and unit spellings counted.

Labels defined inside generated tables latex/tables/ are not read here, so a
reference to one of those is reported as unresolved; pdflatex is the
authority on that. Usage: python analysis/audit_floats_and_citations.py
"""
import re
from collections import Counter
from pathlib import Path

LATEX = Path("D:/Thesis/latex")
sources = sorted(LATEX.glob("chapters/*.tex")) + [LATEX / "appendix.tex", LATEX / "Thesis.tex"]
text = "\n".join(p.read_text(encoding="utf-8") for p in sources)

labels = set(re.findall(r"\\label\{([^}]+)\}", text))
refs = Counter(re.findall(r"\\(?:ref|autoref|pageref)\{([^}]+)\}", text))

print("=== floats never referenced ===")
for label in sorted(labels):
    if label.startswith(("fig:", "tab:", "lst:")) and label not in refs:
        print(" ", label)

print("=== references with no label ===")
for ref in sorted(refs):
    if ref not in labels:
        print(" ", ref)

bib = (LATEX / "bibliography.bib").read_text(encoding="utf-8")
keys = set(re.findall(r"@\w+\{([^,]+),", bib))
cited = set()
for group in re.findall(r"\\cite\w*\{([^}]+)\}", text):
    cited.update(k.strip() for k in group.split(","))

print("=== cited but not in bibliography ===")
for key in sorted(cited - keys):
    print(" ", key)
print("=== in bibliography but never cited ===")
for key in sorted(keys - cited):
    print(" ", key)

print("=== terminology counts (whole-word, prose) ===")
for word in ["backend", "backends", "system", "systems", "deployment",
             "deployments", "configuration", "candidate", "candidates",
             "implementation", "implementations"]:
    print(f"  {word:16s} {len(re.findall(rf'\b{word}\b', text, re.I))}")

print("=== unit spellings ===")
for unit in [r"MB/s", r"MiB/s", r"\\,MB", r"\\,MiB", r"\\,GB", r"core-seconds",
             r"\\,s\b", r"seconds"]:
    print(f"  {unit:16s} {len(re.findall(unit, text))}")
