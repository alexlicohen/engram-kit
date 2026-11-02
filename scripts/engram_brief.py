#!/usr/bin/env python3
"""engram-brief (v3.5) — a pull-based, READ-ONLY deadline & maintenance brief. The second brain
starts *reminding*, not just recalling: it surfaces the time-decaying knowledge it alone holds
(due-dated commitments, dossier drift, watermark staleness) that nothing else volunteers.

Human-invoked only (never scheduled/daemon — see the skill). Deterministic, <1s, every line cited.
Sections: near-term open loops · drifted dossiers · sync-watermark staleness · hygiene tail.

    python3 scripts/engram_brief.py [--days N] [--today YYYY-MM-DD]

--today overrides "now" (for tests / what-if). All paths are cwd-relative so it is testable in a
temp repo; main() chdir's to the repo root first.
"""
import argparse
import collections
import datetime
import glob
import json
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _lib.actions import normalize_text                                       # noqa: E402


def _date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def open_loops(today, days, path="digests/open-loops.md"):
    """Unresolved open-loop lines with a real due date within `days` (overdue included). Robust to
    field order: keys off `due:`, `status:`, `[[note]]` anywhere on a `- ` bullet."""
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.rstrip()
        if not line.lstrip().startswith("- ") or "due:" not in line or "[[" not in line:
            continue
        mdue = re.search(r"due:\s*(\S+)", line)
        d = _date(mdue.group(1)) if mdue else None
        if not d:
            continue
        mst = re.search(r"status:\s*([^·\[]+)", line)
        st = (mst.group(1).strip().lower() if mst else "")
        if any(w in st for w in ("resolved", "done", "closed", "paid", "complete")):
            continue
        delta = (d - today).days
        if delta > days:
            continue
        text = re.split(r"\s+—\s+owner:|\s+·\s+", line.lstrip()[2:])[0].strip()
        owner = re.search(r"owner:\s*([^·]+)", line)
        note = re.search(r"\[\[([^\]]+)\]\]", line)
        out.append({"days": delta, "due": d.isoformat(), "text": text,
                    "owner": owner.group(1).strip() if owner else "?",
                    "note": note.group(1) if note else ""})
    out.sort(key=lambda r: r["days"])
    return out


def drifted_dossiers():
    """(slug, drifted_count, coverage_date) per drifted dossier — reuses dossier_drift (single owner)."""
    sys.path.insert(0, os.path.join(os.getcwd(), "scripts"))
    try:
        import dossier_drift as dd
    except Exception:
        return []
    notes = dd.load_index(dd.INDEX_PATH)
    res = [dd.analyze(p, notes)
           for p in sorted(glob.glob(os.path.join(dd.DOSSIER_DIR, "*.md")))
           if dd.slug_of(p) != "README"]
    return [(r["slug"], r["drifted_count"], r["coverage_date"])
            for r in sorted(res, key=lambda r: -r["drifted_count"]) if r["drifted_count"] > 0]


def watermark_staleness(today, path="_manifest/sync-state.json"):
    """(source, watermark_date, days_behind) per connector source, most-stale first."""
    out = []
    if not os.path.exists(path):
        return out
    for src, meta in json.load(open(path)).items():
        if not isinstance(meta, dict):
            continue
        d = _date(meta.get("watermark_date"))
        if d:
            out.append((src, meta["watermark_date"][:10], (today - d).days))
    out.sort(key=lambda x: -x[2])
    return out


def _load_jsonl_lenient(path):
    """All parseable JSON objects from a .jsonl file, malformed lines skipped (like hygiene()).
    Returns None when the file is ABSENT (distinguishes 'no ledger' from 'empty ledger')."""
    if not os.path.exists(path):
        return None
    rows = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except ValueError:
            continue
    return rows


def tasks_section(today, queue_path="_manifest/task-queue.jsonl",
                  sug_path="_manifest/task-suggestions.jsonl"):
    """The 🔔 TASKS lines for the brief (from the engram-tasks ledgers), or None when the task
    ledger is absent (⇒ section omitted entirely). Reflects the Reminders-bridge state the brain
    holds: unpushed backlog, live pushed load (mine vs waiting-on), held near-dups, pending
    completion-suggestions, and what resolved/was-dismissed."""
    q = _load_jsonl_lenient(queue_path)
    if q is None:
        return None
    sug = _load_jsonl_lenient(sug_path) or []

    def n(status):
        return sum(1 for r in q if r.get("status") == status)

    pushed = [r for r in q if r.get("status") == "pushed"]
    mine = sum(1 for r in pushed if r.get("owner_class") == "mine")
    waiting = sum(1 for r in pushed if r.get("owner_class") == "waiting_on")
    pending_sug = sum(1 for s in sug if s.get("status") == "open")
    week_ago = today - datetime.timedelta(days=7)
    done_week = sum(1 for r in q if r.get("status") == "done"
                    and (_date(r.get("resolved_at")) or datetime.date.min) >= week_ago)

    out = ["\n🔔 TASKS"]
    out.append("   open (unpushed) {} · pushed {} (mine {} / waiting-on {}) · needs_review {} · "
               "pending completion-suggestions {} · done this week {} · dismissed {}".format(
                   n("open"), len(pushed), mine, waiting, n("needs_review"),
                   pending_sug, done_week, n("dismissed")))

    for line in overdue_task_lines(pushed, today):
        out.append(line)
    return out


def _task_key(row, due):
    """Identity of one action item for display: (source note, normalized text, effective due).

    NOT the slug — several distinct action items are routinely extracted from ONE note (a Granola
    meeting yields 5+), so keying/printing by slug collapsed them into byte-identical lines and
    made real work look like a duplicate-ledger bug (2026-09-06). normalize_text is imported from
    _lib.actions — the same normalization task_id() hashes over, so a genuine re-capture of the
    same item under a different id groups here."""
    return (row.get("note_path") or row.get("slug") or "",
            normalize_text(row.get("text") or ""),
            due.isoformat())


def overdue_task_lines(pushed, today, top=5, width=58):
    """OVERDUE lines for the pushed rows, oldest-first, DEDUPED by _task_key with a `×N` suffix
    when the same item sits in the ledger N times. Deduping is display-only — ledger rows are
    never dropped or rewritten by the brief (it is read-only)."""
    dated = sorted(((_date(r.get("due") or r.get("due_auto")), r) for r in pushed),
                   key=lambda x: (x[0] or datetime.date.max))
    groups = collections.OrderedDict()
    for d, r in dated:
        if not d or d >= today:
            continue
        g = groups.setdefault(_task_key(r, d), {"due": d, "row": r, "n": 0})
        g["n"] += 1
    lines = []
    for g in list(groups.values())[:top]:
        r = g["row"]
        owner = r.get("owner") or ("me" if r.get("owner_class") == "mine" else "?")
        text = " ".join((r.get("text") or "").split())
        if len(text) > width:
            text = text[:width - 1].rstrip() + "…"
        lines.append("   OVERDUE {}  {}{}  — {} ({})".format(
            g["due"].isoformat(), text or "?",
            "  ×{}".format(g["n"]) if g["n"] > 1 else "",
            r.get("slug") or "?", owner))
    if len(groups) > top:
        lines.append("   … +{} more overdue task(s) "
                     "(`python3 scripts/engram_tasks.py status`)".format(len(groups) - top))
    return lines


def hygiene():
    unsorted = len([f for f in glob.glob("unsorted/**/*.md", recursive=True)
                    if not f.endswith("_index.md")])
    deferred = 0
    if os.path.exists("_manifest/clarify-queue.jsonl"):
        for ln in open("_manifest/clarify-queue.jsonl"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                if json.loads(ln).get("status") == "deferred":
                    deferred += 1
            except ValueError:      # a hand-edited / malformed line must not crash the brief
                continue
    try:
        r = subprocess.run(["git", "rev-list", "--count", "origin/main..HEAD"],
                           capture_output=True, text=True)
        unpushed = int(r.stdout.strip() or 0) if r.returncode == 0 else None
    except (OSError, ValueError):
        unpushed = None
    return {"unsorted": unsorted, "deferred_clarify": deferred, "unpushed": unpushed}


def compose(today, days):
    L = [f"engram brief — {today.isoformat()}  (open items within {days} days; read-only)"]
    loops = open_loops(today, days)
    overdue = [r for r in loops if r["days"] < 0]
    upcoming = [r for r in loops if r["days"] >= 0]

    def line(r):
        when = f"OVERDUE {-r['days']}d" if r["days"] < 0 else f"in {r['days']}d"
        return (f"   {when:<12} {r['due']}  {r['text']} ({r['owner']})"
                + (f"  [[{r['note']}]]" if r["note"] else ""))

    # UPCOMING first (the reminders that matter most — not yet missed), then OVERDUE (crowds out
    # the future ones if merged, which is exactly why they are split).
    L.append("\n📅 UPCOMING" + (f" (next {days}d)" if upcoming else " — none in window"))
    for r in upcoming[:10]:
        L.append(line(r))
    L.append(f"\n⏰ OVERDUE ({len(overdue)})" if overdue else "\n⏰ OVERDUE — none")
    for r in overdue[:8]:
        L.append(line(r))
    if len(overdue) > 8:
        L.append(f"   … +{len(overdue) - 8} more overdue (open-loops.md may itself need reconciling)")
    ts = tasks_section(today)
    if ts is not None:
        L.extend(ts)
    dr = drifted_dossiers()
    L.append("\n📋 DRIFTED DOSSIERS (run `dossier_drift.py` for the notes)"
             + ("" if dr else " — none"))
    for slug, n, cov in dr:
        L.append(f"   {slug} — {n} unlinked note(s) since coverage {cov}")
    wm = watermark_staleness(today)
    L.append("\n🔄 SYNC WATERMARKS")
    for src, d, behind in wm:
        flag = "  ⚠" if behind >= 3 else "   "
        L.append(f"{flag}{src}: {behind}d behind (watermark {d})")
    h = hygiene()
    up = "?" if h["unpushed"] is None else h["unpushed"]
    L.append("\n🧹 HYGIENE")
    L.append(f"   unsorted: {h['unsorted']} · deferred clarify: {h['deferred_clarify']} "
             f"· unpushed commits: {up}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--today", default=None, help="override today (YYYY-MM-DD); default = system date")
    a = ap.parse_args()
    os.chdir(_ROOT)
    today = _date(a.today) or datetime.date.today()
    print(compose(today, a.days))


if __name__ == "__main__":
    main()
