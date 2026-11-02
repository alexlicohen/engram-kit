#!/usr/bin/env python3
"""Post-Workflow iMessage done-marker stamper (relocated finding-2 guard).

    python3 scripts/stamp_markers.py [--root R] [--results DIR] [--done DIR]

The Workflow runtime is PURE JS (no filesystem access), so `imsg_distill.mjs` can no longer stamp
its own done-markers from code — the retired `_lib/imsg_markers.mjs::stampVerifiedMarkers` did that
INSIDE the workflow. The stamping moved out here, unchanged in substance:

  * each distill AGENT, after writing a chunk's notes, writes a per-chunk RESULT file
    `_sources/imsg-results.d/<chunk_id>.json` = {chunk_id, ref, notes[], sensitive[], durable_found}
    (one file per chunk → no write race; the agent does NOT write the done-marker);
  * the ORCHESTRATOR runs THIS script after the workflow. For each result file it re-verifies —
    via the single-owner `scripts/_lib/imsg_markers.py::stamp_verified` — that every declared
    note+sensitive path EXISTS on disk AND that len(notes)+len(sensitive) == durable_found, then
    stamps the code-owned marker `_sources/imsg-done.d/<chunk_id>`.

A missing path or a count mismatch → NO marker → the chunk stays PENDING and is reconciled +
re-distilled next run (crash-safety preserved, now in Python; `imsg_worklist.reconcile_orphans`
is unchanged). Re-running this script is idempotent: a marker is (re)written only when the declared
notes still verify, so a chunk whose orphans were later archived correctly drops back to pending.

Held SENSITIVE drafts (2026-09-27): a result may declare `_sources/<pfx>-sensitive/<name>.md`, which
review later moves — released into the store, or excluded. `resolve_held` finds where it went, so a
chunk whose drafts were all resolved is stamped instead of WITHHELD forever. Still withheld: a draft
pending review in `confirmed/`, or one LOST everywhere (reported on its own WITHHELD-LOST line).

Single owner: the verify+stamp logic is `_lib/imsg_markers.py` (also the twin used by the tests);
this script only iterates the result files.
"""
import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # scripts/ on path for _lib
from _lib.imsg_markers import stamp_verified, build_ref_index, _iter_store_notes  # noqa: E402
# (stamp_verified = single owner of verify + stamp; build_ref_index/_iter_store_notes = the store scan)

# A chat-DB source -> its `_sources/<prefix>-{results,done}.d` dir prefix. `--source` derives BOTH
# dirs from this so the pair can never be half-configured (the 2026-07-06 review foot-gun: a WhatsApp
# run that forgot --results/--done would re-stamp iMessage, stamp zero WhatsApp markers, and silently
# re-distill → duplicate notes). iMessage keeps its abbreviated `imsg-` prefix; whatsapp uses its own.
SOURCE_DIR_PREFIX = {"imessage": "imsg", "whatsapp": "whatsapp"}


# A held draft's filename ends `-<im|wa><chat>-<hash>.md`; the token survives a retitle on release.
_DRAFT_TOKEN_RE = re.compile(r"-((?:im|wa)\d+-[0-9a-f]{6})(?:-\d+)?\.md$")


def _sensitive_dir(path):
    """The `_sources/<pfx>-sensitive` dir a declared held-draft path lives in, or None."""
    d = os.path.dirname(path)
    if os.path.basename(d) in ("confirmed", "excluded"):
        d = os.path.dirname(d)
    return d if os.path.basename(d).endswith("-sensitive") else None


def _ledger_refs(path):
    """Exact refs recorded in one excluded-refs ledger (missing file / bad lines -> skipped)."""
    refs = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("ref"), str):
                    refs.add(obj["ref"].strip())
    except OSError:
        pass
    return refs


class _Store:
    """Lazy, per-run index of the live store (basename -> paths, ref -> paths)."""

    def __init__(self, root):
        self.root = root
        self._by_base = None
        self._by_ref = None

    def by_base(self, base):
        if self._by_base is None:
            self._by_base = {}
            for fp in _iter_store_notes(self.root):
                self._by_base.setdefault(os.path.basename(fp), []).append(fp)
        return self._by_base.get(base, [])

    def by_ref(self, ref):
        if self._by_ref is None:
            self._by_ref = build_ref_index(self.root)
        return self._by_ref.get(ref, [])


def resolve_held(path, chunk, root, store, claimed):
    """Where a declared held sensitive draft that is NOT at its declared path went.

    Returns (kind, where):
      ('pending-review', p) -- sits in `<sens>/confirmed/` awaiting the owner's review -> WITHHOLD
      ('released', p)       -- a live store note with the same basename AND the chunk's provenance
                               ref; or (retitled on release) same `<im|wa><n>-<hash>` token AND ref
      ('excluded', p)       -- relocated to `<sens>/excluded/`, or the chunk ref is recorded in the
                               live/vault `excluded-refs.jsonl` (p = that ledger)
      ('LOST', None)        -- nowhere: a real loss -> WITHHOLD, reported distinctly
    The ref must match for a release: a same-named DIFFERENT note (imsg_reclassify writes
    `<base>-2.md` on a clash, leaving the other file in place) is not this draft. `claimed` holds
    store paths already matched in this chunk, so one note can satisfy at most one draft and a
    chunk's own declared notes never double as a released draft.
    """
    ap = path if os.path.isabs(path) else os.path.join(root, path)
    sens = _sensitive_dir(ap)
    if sens is None:
        return "LOST", None
    base = os.path.basename(ap)
    pend = os.path.join(sens, "confirmed", base)
    if os.path.exists(pend):
        return "pending-review", pend                   # fail closed: pending wins over any copy
    ref = (chunk.get("ref") or "").strip()
    if ref:
        for fp in store.by_base(base):
            if os.path.normpath(fp) not in claimed and fp in store.by_ref(ref):
                return "released", fp
        m = _DRAFT_TOKEN_RE.search(base)
        if m:
            for fp in store.by_ref(ref):
                if os.path.normpath(fp) not in claimed and "-%s" % m.group(1) in os.path.basename(fp):
                    return "released", fp
    exc = os.path.join(sens, "excluded", base)
    if os.path.exists(exc):
        return "excluded", exc
    if ref:
        vault = os.path.join(root, "_vault", os.path.basename(sens), "excluded-refs.jsonl")
        for ledger in (os.path.join(sens, "excluded-refs.jsonl"), vault):
            if ref in _ledger_refs(ledger):
                return "excluded", ledger
    return "LOST", None


def stamp_all(results_dir, done_dir, root, resolved_log=None):
    """Verify + stamp every per-chunk RESULT file in `results_dir`. Returns (stamped, withheld):
    stamped is a list of chunk_ids marked done; withheld is a list of (chunk_id, missing_list).

    A declared SENSITIVE draft missing from its declared path is first run through `resolve_held`:
    a released/excluded draft is substituted by the path that proves its resolution (so the
    single-owner `stamp_verified` still does every existence + count check); a pending-review or
    LOST draft is left as-is, and its entry in `missing_list` is prefixed `pending-review: ` /
    `LOST: ` so the two withheld kinds stay distinct. `resolved_log` (a list) collects
    (chunk_id, kind, declared, where) for each substitution."""
    stamped, withheld = [], []
    if not os.path.isdir(results_dir):
        return stamped, withheld
    store = _Store(root)
    for fp in sorted(glob.glob(os.path.join(results_dir, "*.json"))):
        try:
            with open(fp, encoding="utf-8") as f:
                chunk = json.load(f)
        except (OSError, ValueError) as e:
            withheld.append((os.path.basename(fp), ["unreadable result file: %s" % e]))
            continue
        cid = chunk.get("chunk_id")
        claimed = {os.path.normpath(p if os.path.isabs(p) else os.path.join(root, p))
                   for p in (chunk.get("notes") or [])}
        sensitive, labels, subs = [], {}, []
        for p in (chunk.get("sensitive") or []):
            ap = p if os.path.isabs(p) else os.path.join(root, p)
            if os.path.exists(ap):
                sensitive.append(p)
                continue
            kind, where = resolve_held(p, chunk, root, store, claimed)
            if kind in ("released", "excluded"):
                claimed.add(os.path.normpath(where))
                sensitive.append(where)
                subs.append((cid, kind, p, where))
            else:
                sensitive.append(p)
                labels[p] = "%s: %s" % (kind, p) if where is None else \
                    "%s: %s (now at %s)" % (kind, p, where)
        check = dict(chunk, sensitive=sensitive) if chunk.get("sensitive") else chunk
        ok, missing = stamp_verified(done_dir, root, check)
        if ok:
            stamped.append(cid)
            if resolved_log is not None:
                resolved_log.extend(subs)
        else:
            withheld.append((cid or os.path.basename(fp), [labels.get(m, m) for m in missing]))
    return stamped, withheld


def main():
    ap = argparse.ArgumentParser(
        description="Stamp chat-DB (iMessage/WhatsApp) done-markers from per-chunk RESULT files "
                    "(post-Workflow).")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--source", choices=sorted(SOURCE_DIR_PREFIX), default=None,
                    help="derive BOTH dirs from the source (imessage->_sources/imsg-*, "
                         "whatsapp->_sources/whatsapp-*) so the pair can't be half-configured. "
                         "--results/--done override it. Omitting both --source and the dirs = imessage.")
    ap.add_argument("--results", default=None, help="results dir (overrides --source)")
    ap.add_argument("--done", default=None, help="done-marker dir (overrides --source)")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    # default source = imessage (its abbreviated imsg-* dirs) for back-compat with bare invocations.
    pfx = SOURCE_DIR_PREFIX.get(args.source, "imsg")
    results_dir = args.results or os.path.join(root, "_sources", f"{pfx}-results.d")
    done_dir = args.done or os.path.join(root, "_sources", f"{pfx}-done.d")

    resolved = []
    stamped, withheld = stamp_all(results_dir, done_dir, root, resolved_log=resolved)
    lost = [(c, m) for c, m in withheld if any(x.startswith("LOST: ") for x in m)]
    print("stamped %d done-marker(s); withheld %d (stay pending → re-distilled next run)%s"
          % (len(stamped), len(withheld), "; %d with a LOST held draft" % len(lost) if lost else ""))
    for cid, kind, declared, where in resolved:
        print("  resolved %s: held draft %s -> %s (%s)"
              % (cid, os.path.basename(declared), os.path.relpath(where, root), kind))
    # Withheld chunks are NOT an error (they remain pending and are re-distilled) but surface loudly
    # so an under-reported / half-written chunk is never invisible. A LOST held draft (not in the
    # sensitive dir, confirmed/, excluded/, the excluded-refs ledgers, or any store domain) is a
    # real loss and gets its own line.
    for cid, missing in withheld:
        tag = "WITHHELD-LOST" if (cid, missing) in lost else "WITHHELD"
        print("  %s %s: %s" % (tag, cid, missing), file=sys.stderr)


if __name__ == "__main__":
    main()
