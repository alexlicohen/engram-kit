#!/usr/bin/env python3
"""Verify the store's one hard sensitivity rule: no clinical-patient PHI in the
WRITTEN notes (.md). Companion to phi_scan.py (which screens SOURCE threads at
ingest); this is the periodic store-side audit CLAUDE.md calls for.

Reuses phi_scan.scan() so the heuristic stays single-sourced. Over-flags by
design (recall > precision) — a flag means "human-review this note", not "leak".
Out-of-scope by rule (NOT a leak): a lab member's OWN date of birth in an
administrative note, and de-identification methodology discussion ("DOB jittering").

Phase 4: drive/ catalog files ARE scanned (the one historical PHI hit was a
patient-identifying *filename* in the Drive catalog), and every file's repo-
relative path is prepended to the scanned text so a PHI-bearing filename is
caught even when the body is clean. Output never includes matched PHI text —
path + level + counts + which detector fired only.

Usage:
  python3 scripts/phi_scan_store.py [--json out.jsonl] [--summary]

  --summary  counts + a PHI-WARN line only (for check.sh); the per-note list
             is printed by a bare run. Exit 0 on flags (over-flag by design);
             any internal error exits non-zero so check.sh fails closed.
"""
import os, sys, json, importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# single-source the heuristic from phi_scan.py
spec = importlib.util.spec_from_file_location(
    "phi_scan", os.path.join(ROOT, "scripts", "phi_scan.py"))
phi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phi)
scan = phi.scan

# infrastructure / non-knowledge dirs. drive/ is NOT skipped (Phase 4): its
# catalog .md files are scanned, filenames included. `_*-pending` staging dirs
# are skipped generically in _skip_dir().
SKIP = {"_archive", "_sources", "_vault", "_manifest",
        ".git", "scripts", "node_modules"}

def _skip_dir(d):
    return d in SKIP or d.startswith(".") or (d.startswith("_") and d.endswith("-pending"))

def main():
    out_path = None
    if "--json" in sys.argv:
        out_path = sys.argv[sys.argv.index("--json") + 1]
    summary = "--summary" in sys.argv

    rows = []
    counts = {"high": 0, "medium": 0, "low": 0, "none": 0, "flagged": 0, "scanned": 0}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, ROOT)
            if _skip_dir(rel.split(os.sep)[0]):
                continue
            # prepend the repo-relative path so PHI-bearing FILENAMES are caught
            # even with a clean body (the historical Drive-catalog incident)
            r = scan(rel + "\n" + open(fp, encoding="utf-8", errors="replace").read())
            counts["scanned"] += 1
            counts[r["level"]] += 1
            if r["phi"]:
                counts["flagged"] += 1
                rows.append({"path": rel, **r})

    rows.sort(key=lambda x: (x["level"] != "high", -x["ctx"]))
    print(f"scanned={counts['scanned']:,}  PHI-flagged={counts['flagged']:,}  "
          f"[high={counts['high']} medium={counts['medium']} "
          f"low={counts['low']} none={counts['none']}]")
    if counts["flagged"]:
        print(f"PHI-WARN: {counts['flagged']} notes flagged for human review "
              f"(over-flags by design — a flag routes to review, it is NOT a gate failure).")
    if summary:
        if counts["flagged"]:
            print("          Full per-note list: python3 scripts/phi_scan_store.py")
    else:
        print("\nFlagged notes (review — over-flags by design):")
        for r in rows:
            print(f"  [{r['level']:6s}] ctx={r['ctx']:2d} acad={r['acad']:2d} "
                  f"strong={r['strong']} | {r['path']}")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as o:
            for r in rows:
                o.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nreport -> {out_path}")

if __name__ == "__main__":
    main()
