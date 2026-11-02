#!/usr/bin/env python3
"""Deterministic sampler for the engram FIDELITY audit (`/engram-audit`).

The fidelity audit re-fetches a note's ORIGINAL source (Gmail thread / Granola transcript / local
iMessage chunk) and adversarially checks the note against it — is anything fabricated or distorted?
This script only draws the SAMPLE (deterministic, reproducible); the fan-out + grading is driven by
the `/engram-audit` skill. It is the audit twin of eval_report.py's question bank.

    python3 scripts/audit_sample.py --n 100 --scope high-stakes           # print the sample
    python3 scripts/audit_sample.py --n 30 --scope all --out sample.jsonl # write it

Which sources are eligible is decided by ONE table, SOURCE_RULES below — the single owner of that
question. Do not re-hardcode a source list anywhere else in this file (or in a caller): add a row.
Distilled sources reach their original either through a CONNECTOR (gmail, granola) or through a
LOCAL file under `_sources/` that the `fetch` field is RESOLVED HERE into (outlook thread text,
iMessage/WhatsApp chunks) — the local resolution is what makes those notes auditable by a subagent
at all, rather than only by hand. Chat-DB sources (iMessage/WhatsApp) stay opt-in behind
--include-chatdb (`--include-imessage` is a deprecated alias) because their sources are local-only
and sit next to the sensitive hold queue.

Sampling is a salted sha256 over the note path — same (--n, --scope, --seed) ⇒ same sample, and no
Math.random/Date (reproducible, resumable). Gmail refs are canonicalized to the connector's hex
thread id via the single-owner _lib.provenance.gmail_thread_key (handles the decimal/hex mismatch).
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.provenance import gmail_thread_key  # noqa: E402

INDEX = os.path.join("_manifest", "index.jsonl")
OUTLOOK_THREADS = os.path.join("_sources", "outlook-threads")

# High-stakes = where a wrong fact has real consequences. EDIT THESE for your store: name the
# domains whose notes carry consequential facts, and the tags that mark a consequential note
# anywhere. `--scope high-stakes` samples only what these two sets select.
HS_DOMAINS = {"finance", "health"}
HS_TAGS = {"decision", "commitment", "tax", "legal", "contract"}


def load_index():
    with open(INDEX, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def is_high_stakes(r):
    return r.get("domain") in HS_DOMAINS or bool(set(r.get("tags") or []) & HS_TAGS)


# ---- Source eligibility: the SINGLE OWNER of "can this note be audited?" ---------------------
# One row per `source` value that appears in _manifest/index.jsonl. A new connector source MUST get
# a row here (an unknown source is ineligible and is named in the all-filtered WARN, so it surfaces
# loudly rather than silently shrinking the pool).
#   auditable   — False => never sampled; `why` records the reason and is printed by the WARN.
#   gate        — None => eligible by default; "chatdb" => only with --include-chatdb.
#   skip_domains— domains of this source whose original is unreachable from here.
#   why         — how the original is reached, or why there is none.
SourceRule = collections.namedtuple("SourceRule", "auditable gate skip_domains why")

SOURCE_RULES = {
    # Distilled, re-fetched through a connector by the auditing subagent.
    # skip_domains: domains whose gmail notes came from a mailbox the connector cannot reach (a
    # second account synced separately, an export-only archive) — graded blind otherwise.
    "gmail":       SourceRule(True, None, frozenset(),
                              "gmail connector re-fetch"),
    "granola":     SourceRule(True, None, frozenset(), "granola connector re-fetch (transcript)"),
    # Distilled, resolved to a LOCAL source file by fetch_id — no connector needed.
    "outlook":     SourceRule(True, None, frozenset(),
                              "local rendered thread `_sources/outlook-threads/<thrid>.txt`"),
    "imessage":    SourceRule(True, "chatdb", frozenset(),
                              "local chunk `_sources/imessage/<chat>__c<nnn>.txt` (opt-in: "
                              "local-only source, adjacent to the sensitive hold queue)"),
    "whatsapp":    SourceRule(True, "chatdb", frozenset(),
                              "local chunk `_sources/whatsapp/...` (opt-in, same as imessage)"),
    # Not sampled — no distillation-fidelity question to ask.
    "obsidian":    SourceRule(False, None, frozenset(),
                              "verbatim ingest: content untouched, so there is nothing to distort"),
    "gdrive":      SourceRule(False, None, frozenset(),
                              "catalog links only — the file itself is never ingested"),
    "manual":      SourceRule(False, None, frozenset(),
                              "hand-authored in-store; no upstream original to check against"),
    "claude-chat": SourceRule(False, None, frozenset(),
                              "session transcript is not retained under _sources/, so it cannot be re-fetched"),
}


def eligible(r, include_chatdb=False):
    """Is this index record auditable? Table-driven — see SOURCE_RULES."""
    rule = SOURCE_RULES.get(r.get("source"))
    if rule is None or not rule.auditable:
        return False
    if r.get("domain") in rule.skip_domains:
        return False
    return include_chatdb if rule.gate == "chatdb" else True


def exclusion_reason(src, include_chatdb):
    """Why a source's notes were filtered out — for the all-filtered WARN."""
    rule = SOURCE_RULES.get(src)
    if rule is None:
        return "unknown source: no SOURCE_RULES row (add one)"
    if not rule.auditable:
        return rule.why
    if rule.gate == "chatdb" and not include_chatdb:
        return "gated: pass --include-chatdb (%s)" % rule.why
    return "domain not reachable: %s" % rule.why


def _ledger_source(base):
    """Source a run-ledger's records belong to, from its filename. None => not a chat-DB ledger."""
    if base.startswith("whatsapp"):
        return "whatsapp"
    if base.startswith("imessage") or base.startswith("imsg"):
        return "imessage"
    return None


def _chunk_runs():
    """Lazy-load every chat-DB run ledger into two lookup maps. Single owner of ref -> chunk-file.

    The run ledgers (`_sources/{imessage,whatsapp}*run*.jsonl`, one record per extracted chunk:
    {chunk_id, chat_id, guid, fp, ...}) are the only durable record of which local `.txt` a chunk's
    text landed in, so they are the inverse of _lib.imsg_markers.chunk_ref. Both are needed because
    a note's ref carries the chunk in one of TWO historical forms — see resolve_chunk_fp.

    Both maps are keyed by (SOURCE, ...): iMessage and WhatsApp assign chat_ids from independent
    sequences, so chat 42 (and its chunk_id `42_c000`) exists in BOTH. Keying on chunk_id alone let
    a `whatsapp:...:42_c000` ref resolve to `_sources/imessage/42__c000.txt` (imessage sorts first
    in the glob) — a cross-source mis-resolve that check_ref_integrity.py surfaced. The ref carries
    its source prefix, so resolve_chunk_fp scopes every lookup to it.
    """
    if _chunk_runs.cache is None:
        by_chunk, by_guid_n = {}, {}
        for fn in sorted(glob.glob(os.path.join("_sources", "*run*.jsonl"))):
            src = _ledger_source(os.path.basename(fn))
            if src is None:
                continue
            with open(fn, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cid, fp, guid = rec.get("chunk_id"), rec.get("fp"), rec.get("guid")
                    if not (cid and fp):
                        continue
                    by_chunk.setdefault((src, cid), fp)
                    # The legacy `c<n>` map is BACKFILL-ONLY, keyed (source, guid, n). A guid's
                    # incremental chunks (`<chat>_incr<ts>_c000`) restart at c000 and would otherwise
                    # shadow the backfill `<chat>_c000` — and they sort FIRST in the glob, so a naive
                    # map made every legacy ref resolve to an incremental chunk (caught by two
                    # auditors fetching 2018 notes and getting 2026 texts). A legacy ref predates
                    # incremental sync by construction, so only a plain `<chat_id>_c<nnn>` satisfies it.
                    if guid and cid.startswith("%s_c" % rec.get("chat_id")):
                        n = cid.split("_c", 1)[1]
                        if n.isdigit():
                            by_guid_n.setdefault((src, guid, n.lstrip("0") or "0"), fp)
        _chunk_runs.cache = (by_chunk, by_guid_n)
    return _chunk_runs.cache


_chunk_runs.cache = None


def resolve_chunk_fp(ref):
    """`imessage:<guid>:<chunk>` / `whatsapp:<jid>:<chunk>` -> the local chunk .txt path, or None.

    Two ref shapes exist in the store and both must resolve:
      * FULL chunk_id  — 'imessage:any;-;+1555...:9001_incr20260101T000000Z_c000' (current; unique)
      * LEGACY short   — 'imessage:any;-;+1555...:c1'  (backfill-era; only unique WITHIN a guid,
                         which is exactly why chunk_ref was later widened — see _lib/imsg_markers)
      * LEGACY hybrid  — 'imessage:any;-;+1555...:c7_c000' — a full chunk_id carrying the short
                         form's stray 'c' prefix, from the transition between the two.
    A guid contains no ':', so an rsplit on the last ':' separates chunk from '<source>:<guid>'.
    Returns None (never a guess) if the ledger has no such chunk or the file is gone; the caller
    reports those rather than handing an auditor a path that isn't there.
    """
    if not ref or ":" not in ref:
        return None
    head, chunk = ref.rsplit(":", 1)
    src = head.split(":", 1)[0]                        # 'imessage' / 'whatsapp' — scopes every lookup
    by_chunk, by_guid_n = _chunk_runs()
    fp = by_chunk.get((src, chunk))
    if fp is None and chunk.startswith("c"):
        if chunk[1:].isdigit():                       # legacy short: 'c1' -> nth chunk of this guid
            guid = head.split(":", 1)[1] if ":" in head else head
            fp = by_guid_n.get((src, guid, chunk[1:].lstrip("0") or "0"))
        else:                                         # legacy hybrid: 'c7_c000' -> '7_c000'
            fp = by_chunk.get((src, chunk[1:]))
    return fp if fp and os.path.exists(fp) else None


def resolve_outlook_fp(ref):
    """`outlook:thread:<thrid>` -> `_sources/outlook-threads/<thrid>.txt`, or None if it is gone.

    Outlook's distiller reads a rendered thread file that stays on disk, so an outlook note is
    locally auditable exactly the way an iMessage chunk is — no connector, no work-mailbox access.
    Same contract as resolve_chunk_fp: None rather than a guess, so the caller reports the note
    instead of handing an auditor a path that isn't there.
    """
    if not ref or not ref.startswith("outlook:thread:"):
        return None
    thrid = ref.split("outlook:thread:", 1)[1].strip()
    if not thrid or "/" in thrid or thrid.startswith("."):
        return None
    fp = os.path.join(OUTLOOK_THREADS, thrid + ".txt")
    return fp if os.path.exists(fp) else None


def fetch_id(r):
    """What an auditor uses to re-fetch the source: hex thread id (gmail) / uuid (granola) /
    local file path (outlook thread; imessage & whatsapp chunks) — all resolved from the ref."""
    src = r.get("source")
    if src == "gmail":
        k = gmail_thread_key(r.get("ref", "") or "")
        return format(int(k), "x") if k else None
    if src == "granola":
        return (r.get("ref", "") or "").replace("granola:", "")
    if src == "outlook":
        return resolve_outlook_fp(r.get("ref"))
    return resolve_chunk_fp(r.get("ref"))             # imessage:<guid>:<chunk_id> -> _sources/...txt


def sample(records, n, scope, seed, exclude, include_chatdb, only=None, domains=None, sources=None):
    """-> (pool, chosen, filtered): `filtered` counts the notes the SELECTORS kept but eligibility
    dropped, per source. Selection and eligibility are separated precisely so an empty pool can be
    explained: `--only`/`--sources` naming real notes that are all ineligible is a caller error, not
    a clean audit, and main() turns a `filtered`-only outcome into a loud WARN + non-zero exit."""
    selected = [r for r in records
                if r.get("path") not in exclude
                and (only is None or r.get("path") in only)
                and (domains is None or r.get("domain") in domains)
                and (sources is None or r.get("source") in sources)
                and (scope != "high-stakes" or is_high_stakes(r))]
    pool = [r for r in selected if eligible(r, include_chatdb)]
    filtered = collections.Counter(r.get("source") for r in selected
                                   if not eligible(r, include_chatdb))
    salt = f"{seed}:".encode()
    pool.sort(key=lambda r: hashlib.sha256(salt + r["path"].encode()).hexdigest())
    return pool, pool[:n], filtered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="sample size")
    ap.add_argument("--scope", choices=["all", "high-stakes"], default="all")
    ap.add_argument("--seed", default="engram-audit", help="salt for the deterministic draw")
    ap.add_argument("--exclude", help="a JSONL of {path:...} already-audited notes to skip")
    ap.add_argument("--only", help="restrict the pool to the note paths in this file (one repo-relative "
                    "path per line) — used by /engram-sync to audit only the notes it just wrote")
    ap.add_argument("--domains", help="comma-separated domains to restrict to (e.g. research,projects)")
    ap.add_argument("--sources", help="comma-separated sources to restrict to (e.g. granola,imessage) — "
                    "for a per-distiller audit, since each distiller has its own FIDELITY RULES block")
    ap.add_argument("--include-chatdb", action="store_true",
                    help="include chat-DB notes (iMessage + WhatsApp; local-only sources)")
    ap.add_argument("--include-imessage", dest="include_chatdb", action="store_true",
                    help="deprecated alias for --include-chatdb (it always covered WhatsApp too)")
    ap.add_argument("--out", help="write sample JSONL here (default: stdout)")
    a = ap.parse_args()

    exclude = set()
    if a.exclude and os.path.exists(a.exclude):
        with open(a.exclude, encoding="utf-8") as fh:
            for l in fh:
                l = l.strip()
                if l:
                    try:
                        exclude.add(json.loads(l)["path"])
                    except (json.JSONDecodeError, KeyError):
                        pass

    only = None
    if a.only and os.path.exists(a.only):
        with open(a.only, encoding="utf-8") as fh:
            only = {l.strip() for l in fh if l.strip()}

    domains = set(a.domains.split(",")) if a.domains else None
    sources = set(a.sources.split(",")) if a.sources else None

    if "--include-imessage" in sys.argv:
        print("# NOTE: --include-imessage is deprecated; use --include-chatdb (same effect: "
              "iMessage + WhatsApp).", file=sys.stderr)

    pool, chosen, filtered = sample(load_index(), a.n, a.scope, a.seed, exclude, a.include_chatdb,
                                    only, domains, sources)
    recs = [{"path": r["path"], "source": r["source"],
             "domain": r.get("domain"), "fetch": fetch_id(r)} for r in chosen]
    # An unresolvable fetch is REPORTED, never silently emitted as an auditable row: a batch that
    # can't reach its source must not be graded (a missing source is not "0 defects" — skill step 3).
    unresolved = [x for x in recs if not x["fetch"]]
    recs = [x for x in recs if x["fetch"]]
    lines = [json.dumps(x) for x in recs]
    out = "\n".join(lines) + ("\n" if lines else "")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"scope={a.scope} pool={len(pool)} drew={len(chosen)} emitted={len(recs)} -> {a.out}",
              file=sys.stderr)
    else:
        sys.stdout.write(out)
        print(f"# scope={a.scope} pool={len(pool)} drew={len(chosen)} emitted={len(recs)}", file=sys.stderr)
    if unresolved:
        print(f"# WARN: {len(unresolved)} note(s) dropped — source unresolvable:", file=sys.stderr)
        for x in unresolved[:10]:
            print(f"#   {x['source']}\t{x['path']}", file=sys.stderr)

    # emitted=0 must never look like a clean audit. Two ways to get there, both non-zero:
    #  (a) the selectors (--only/--sources/--domains) matched real notes, but eligibility dropped
    #      every one of them — a caller error (wrong source, or a missing --include-chatdb);
    #  (b) notes were drawn but not one source could be resolved, so nothing is auditable.
    if not pool and filtered:
        print("!! WARN: nothing to audit — every selected note was filtered out by source "
              "eligibility. emitted=0 here means NOT AUDITED, not clean.", file=sys.stderr)
        for src, cnt in sorted(filtered.items(), key=lambda kv: (-kv[1], str(kv[0]))):
            print(f"!!   {src}: {cnt} note(s) excluded — {exclusion_reason(src, a.include_chatdb)}",
                  file=sys.stderr)
        sys.exit(3)
    if chosen and not recs:
        print(f"!! WARN: nothing to audit — all {len(chosen)} drawn note(s) had an unresolvable "
              "source. emitted=0 here means NOT AUDITED, not clean.", file=sys.stderr)
        sys.exit(4)


if __name__ == "__main__":
    main()
