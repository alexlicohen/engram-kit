#!/usr/bin/env python3
"""Retrieval-eval harness — the deterministic half.

The eval itself is an LLM fan-out (one cold agent per question, run by the `/engram-eval` skill).
This script is the deterministic scaffolding around it: list the question bank, then take the
collected results and write a dated, comparable report + a run-over-run grade DIFF. It is an
INSTRUMENT, not a gate — never wire it into check.sh (LLM-cost, non-deterministic answers).

    python3 scripts/eval_report.py --list                       # print the question bank (for fan-out)
    python3 scripts/eval_report.py --write results.jsonl [--date YYYY-MM-DD]

`results.jsonl` = one JSON object per question the agents answered:
    {"id": "fin-taxdocs", "grade": "WELL|PARTIAL|THIN", "hops": 4,
     "notes": ["path/…md", …], "stale": ["<fact> -> <superseding note>", …], "answer": "one-liner"}

Writes `_manifest/eval-reports/<date>.{jsonl,md}` and prints the grade delta vs the prior report.
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QFILE = os.path.join(ROOT, "_manifest", "eval-questions.jsonl")
REPORT_DIR = os.path.join(ROOT, "_manifest", "eval-reports")
GRADES = ("WELL", "PARTIAL", "THIN")


def load_questions():
    return [json.loads(l) for l in open(QFILE, encoding="utf-8") if l.strip()]


def cmd_list(_):
    for q in load_questions():
        print(f"[{q['id']}] ({q['domain']}) {q['question']}")
    print(f"\n{len(load_questions())} questions -> fan out one cold agent each (see the /engram-eval skill).")


def _prev_report(date):
    """Most recent report jsonl strictly before `date` (for the run-over-run diff)."""
    others = sorted(p for p in glob.glob(os.path.join(REPORT_DIR, "*.jsonl"))
                    if os.path.basename(p)[:-6] < date)
    return others[-1] if others else None


def cmd_write(args):
    qs = {q["id"]: q for q in load_questions()}
    results = {}
    for l in open(args.results, encoding="utf-8"):
        if not l.strip():
            continue
        r = json.loads(l)
        if r["id"] not in qs:
            sys.exit(f"eval_report: result for unknown question id '{r['id']}'")
        if r.get("grade") not in GRADES:
            sys.exit(f"eval_report: '{r['id']}' has grade {r.get('grade')!r} (want one of {GRADES})")
        results[r["id"]] = r
    missing = [qid for qid in qs if qid not in results]
    if missing:
        sys.stderr.write(f"WARN: {len(missing)} question(s) have no result: {', '.join(missing)}\n")

    date = args.date or _today()
    os.makedirs(REPORT_DIR, exist_ok=True)
    raw = os.path.join(REPORT_DIR, f"{date}.jsonl")
    with open(raw, "w", encoding="utf-8") as f:
        for qid in qs:  # stable question order
            if qid in results:
                f.write(json.dumps(results[qid], ensure_ascii=False) + "\n")

    counts = Counter(r["grade"] for r in results.values())
    headline = " · ".join(f"{g} {counts.get(g, 0)}" for g in GRADES)

    # run-over-run diff
    prev_path = _prev_report(date)
    prev = {}
    if prev_path:
        prev = {json.loads(l)["id"]: json.loads(l)["grade"]
                for l in open(prev_path, encoding="utf-8") if l.strip()}
    rank = {"THIN": 0, "PARTIAL": 1, "WELL": 2}
    improved, regressed = [], []
    for qid, r in results.items():
        if qid in prev and prev[qid] != r["grade"]:
            (improved if rank[r["grade"]] > rank[prev[qid]] else regressed).append(
                f"{qid}: {prev[qid]} -> {r['grade']}")

    stale = [(qid, s) for qid, r in results.items() for s in (r.get("stale") or [])]

    lines = [f"# engram retrieval eval — {date}\n",
             f"**{len(results)}/{len(qs)} answered · {headline}**"
             + (f"  (vs {os.path.basename(prev_path)[:-6]})" if prev_path else "") + "\n"]
    if prev_path:
        lines.append("\n## Changes since last run")
        lines.append("- Regressed: " + ("; ".join(regressed) if regressed else "none"))
        lines.append("- Improved: " + ("; ".join(improved) if improved else "none") + "\n")
    if stale:
        lines.append("\n## Staleness / correctness flagged (act on these)")
        for qid, s in stale:
            lines.append(f"- **{qid}**: {s}")
        lines.append("")
    lines.append("\n## Results\n")
    lines.append("| id | domain | grade | hops | notes cited |")
    lines.append("|---|---|---|---|---|")
    for qid, q in qs.items():
        r = results.get(qid)
        if not r:
            lines.append(f"| {qid} | {q['domain']} | — (no result) | | |")
            continue
        lines.append(f"| {qid} | {q['domain']} | {r['grade']} | {r.get('hops', '')} "
                     f"| {len(r.get('notes') or [])} |")
    md = os.path.join(REPORT_DIR, f"{date}.md")
    open(md, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    print(f"eval report -> {os.path.relpath(md, ROOT)}   ({len(results)}/{len(qs)}: {headline})")
    if prev_path:
        print(f"  vs {os.path.basename(prev_path)[:-6]}: "
              f"regressed={len(regressed)} improved={len(improved)}")
        for r in regressed:
            print(f"    REGRESSED  {r}")
    if stale:
        print(f"  {len(stale)} staleness/correctness flag(s) — see the report.")


def _today():
    import datetime
    return datetime.date.today().isoformat()


def main():
    ap = argparse.ArgumentParser(description="engram retrieval-eval report helper (deterministic).")
    ap.add_argument("--list", action="store_true", help="print the question bank (for the fan-out)")
    ap.add_argument("--write", metavar="results.jsonl", help="write a dated report + diff from collected results")
    ap.add_argument("--date", help="report date YYYY-MM-DD (default today)")
    args = ap.parse_args()
    if args.list:
        cmd_list(args)
    elif args.write:
        args.results = args.write
        cmd_write(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
