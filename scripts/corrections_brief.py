#!/usr/bin/env python3
"""corrections_brief.py — render the OPEN fidelity-audit corrections for one source into the
pre-launch brief each distiller's Setup agent reads (Feature C, live-injection step).

    python3 scripts/corrections_brief.py --source granola
    python3 scripts/corrections_brief.py --source gmail --older-than 2026-06-01   # prune, then render

Reads `_manifest/corrections/<S>.jsonl`, keeps `status == "open"` rows, renders each as a short
bullet, and writes `_sources/<S>.corrections-brief.txt`. The distiller reads that file in its Setup
phase and folds the bullets into `distillPrompt()` as extra adversarial reminders. Run by the
orchestrator at the SAME point SYNC.md runs `resolve_wave.py` — which now calls write_brief()
itself, in the same pre-launch step, and records the brief's sha256 into the consumed contract.

Robustness contract (the distiller depends on it):
  * zero open rows       -> an EMPTY file (the Setup agent handles "exists but empty" gracefully)
  * missing ledger       -> an EMPTY brief, never a crash
  * a MALFORMED ledger line -> RAISES (it used to be skipped; a silently-dropped line means the
    prompt under-injects and dedup mis-fires — `scripts/corrections_lint.py` gates this in check.sh)
  * only `open` rows inject; once a human hardens a correction into the FIDELITY RULES block and
    flips it to `applied` — or `--older-than` ages it to `stale` — it stops surfacing here.
  * at most MAX_BULLETS (25) bullets, NEWEST FIRST, plus a one-line "omitted" note when it truncates:
    the brief is interpolated verbatim into a distiller prompt, and an unbounded ledger would grow
    into (and eventually crowd out) the FIDELITY RULES it is meant to reinforce.
"""
import argparse
import os

from log_correction import iter_ledger, ledger_path, mark_stale_before, VALID_SOURCES, ROOT

# Cap the bullets folded into a distiller prompt. 25 open corrections is already a long adversarial
# reminder list; beyond that the oldest entries are the least useful (an audit finding that has been
# open for months is a candidate for hardening or --older-than, not for silently bloating the prompt).
MAX_BULLETS = 25


def brief_path(source, root=ROOT):
    return os.path.join(root, "_sources", "%s.corrections-brief.txt" % source)


def open_entries(source, root=ROOT):
    """OPEN entries for `source`, NEWEST FIRST (by `date`, ties broken by ledger position, i.e.
    append order). Raises ValueError on a malformed ledger line."""
    rows = [(str(e.get("date", "")), lineno, e)
            for lineno, e in iter_ledger(source, root) if e.get("status") == "open"]
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [e for _, _, e in rows]


def render_open(source, root=ROOT):
    """Return the rendered brief text (bullets for every OPEN correction, newest first, capped at
    MAX_BULLETS; "" if none/missing). Raises ValueError on a malformed ledger line."""
    entries = open_entries(source, root)
    lines = []
    for e in entries[:MAX_BULLETS]:
        kind = e.get("kind", "other")
        finding = (e.get("finding") or "").strip()
        note = (e.get("note_path") or "").strip()
        bullet = "- [%s] %s" % (kind, finding)
        if note:
            bullet += " (ref: %s)" % note
        lines.append(bullet)
    omitted = len(entries) - len(lines)
    if omitted > 0:
        lines.append("(note: %d older open correction%s omitted — showing the %d newest; harden the "
                     "old ones into the FIDELITY RULES block, or age them out with "
                     "`corrections_brief.py --source %s --older-than YYYY-MM-DD`)"
                     % (omitted, "" if omitted == 1 else "s", MAX_BULLETS, source))
    return "\n".join(lines)


def write_brief(source, root=ROOT):
    """Render + atomically write `_sources/<source>.corrections-brief.txt`. Returns (path, text)."""
    text = render_open(source, root)
    outp = brief_path(source, root)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    tmp = outp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, outp)
    return outp, text


def main():
    ap = argparse.ArgumentParser(
        description="Render OPEN corrections for a source into its pre-launch distiller brief "
                    "(newest first, at most %d bullets)." % MAX_BULLETS)
    ap.add_argument("--source", required=True, choices=VALID_SOURCES)
    ap.add_argument("--older-than", metavar="YYYY-MM-DD",
                    help="before rendering, flip every OPEN entry dated before this to status "
                         "`stale` so it stops injecting. Rewrites the ledger in place; no line is "
                         "ever deleted (log_correction.mark_stale_before is the writer).")
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    if args.older_than:
        flipped = mark_stale_before(args.source, args.older_than, root)
        print("pruned %d open correction%s dated before %s -> stale (%s)"
              % (len(flipped), "" if len(flipped) == 1 else "s", args.older_than,
                 ledger_path(args.source, root)))
        for eid in flipped:
            print("  STALE %s" % eid)
    total = len(open_entries(args.source, root))
    outp, _ = write_brief(args.source, root)
    extra = " (%d older omitted)" % (total - MAX_BULLETS) if total > MAX_BULLETS else ""
    print("wrote %s (%d open correction%s%s)" % (outp, total, "" if total == 1 else "s", extra))


if __name__ == "__main__":
    main()
