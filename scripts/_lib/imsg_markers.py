"""Single owner for iMessage resume-marker + crash-orphan reconciliation (finding-2 fix).

Crash-safe, idempotent iMessage chunk distillation rests on two invariants:

1. TRUSTWORTHY MARKER (written by CODE, not the LLM). The done-marker
   `_sources/imsg-done.d/<chunk_id>` is stamped only AFTER the distiller's declared note paths
   are verified to exist on disk — by `stamp_verified` here, driven POST-Workflow by
   scripts/stamp_markers.py off each chunk's RESULT file. (The Workflow runtime is pure JS with no
   fs, so the old in-workflow _lib/imsg_markers.mjs stamper was retired; this Python module is now
   the SOLE owner and it stamps after the run.) A marker therefore MEANS "this chunk's declared
   notes verifiably exist", never a bare LLM promise. (Previously the sub-agent wrote
   the marker itself via the Write tool AFTER writing notes; an interruption between the two left
   notes-without-marker, and the chunk was silently re-distilled on resume.)

2. IDEMPOTENT RE-DISTILL (reconcile before re-emit). A mid-agent crash still leaves notes on disk
   with no marker, so `imsg_worklist.py` re-emits that chunk. Before it is re-distilled, any store
   notes left by the crashed attempt are moved OUT of the live store (`reconcile_orphans`), so the
   re-distill can only REPLACE, never ACCUMULATE a second note sharing the chunk's ref. Orphans
   are found by the chunk's canonical provenance ref — the ONLY chunk-unique link that survives a
   mid-agent crash (the filename `<h>` hash is slug-dependent and not reconstructable from the
   chunk metadata).

The lint dup-ref EXEMPTION for `imessage:` refs is PERMANENT and correct (one chunk legitimately
yields several notes sharing its chunk-granular ref); it is NOT the dup backstop. This module is:
the dup-INGEST is prevented here, upstream, by reconcile — exactly as CLAUDE.md documents.
"""
import glob
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/ on path
from _lib.constants import DOMAINS
from _lib.provenance import parse_ref


def chunk_ref(guid, chunk_id, source="imessage"):
    """Canonical, code-computable, CHUNK-granular provenance ref for a chat-DB chunk.

    `<source>:<guid>:<chunk_id>` — every durable note distilled from one chunk SHARES this ref
    (multi-note-per-chunk: the lint exemption's intended shape), while the FULL chunk_id makes it
    chunk-UNIQUE. That uniqueness is what makes `reconcile_orphans` safe: the old ambiguous
    `:c<n>` suffix collided across backfill `<chat>_c<n>` and incremental `<chat>_incr_c<n>`
    chunks of the same chat, so keying cleanup on it could archive a DIFFERENT chunk's good notes.
    Must match, verbatim, the ref the distiller prompt tells the sub-agent to write (guid and
    chunk_id copied straight off the work-list line). `source` defaults to `imessage` for
    back-compat; the WhatsApp connector passes `source='whatsapp'` (same chunk-granular contract).
    """
    return "%s:%s:%s" % (source, guid, chunk_id)


def _guids_from_ledger(path):
    """Parse ONE excluded-refs ledger -> set of `any;±;<id>` chat guids. Missing file -> empty."""
    guids = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ref = ((obj.get("ref") if isinstance(obj, dict) else None) or "").strip()
                except ValueError:      # malformed JSON line -> skip
                    continue
                if not ref.startswith("imessage:"):
                    continue
                guid = ref[len("imessage:"):].rsplit(":", 1)[0]  # guid uses ';' not ':'; drop chunk
                if ";" in guid:                                   # sanity: a real chat guid, not junk
                    guids.add(guid)
    except FileNotFoundError:
        pass
    return guids


def excluded_chat_guids(root, path=None):
    """Chat guids to NEVER (re)ingest — the `any;±;<id>` guid-part of every ref in the excluded-refs
    ledger. The single owner of that decision; imsg_select imports this rather than re-parsing.

    Read as the UNION of two copies (v3.2 durability fix): the LIVE gitignored ledger
    `_sources/imsg-sensitive/excluded-refs.jsonl` AND the git-crypt-ENCRYPTED vault copy
    `_vault/imsg-sensitive/excluded-refs.jsonl`. The vault copy is the disaster-recovery path: the
    live ledger is local-only, so on a fresh clone / new machine only the vault (once `git-crypt
    unlock`ed) carries the exclusions — without it a re-sync would silently RE-INGEST the two
    human-confirmed PHI conversations (fail-open on the store's one irreversible rule). Union, not
    either-or, so a stale vault copy can never DROP a live exclusion. (An explicit `path=` reads only
    that file — for tests.)

    Human-confirmed PHI exclusions (e.g. a physician-patient conversation) are recorded at CHUNK
    granularity (`imessage:any;-;+1...:c99_c000`), but we exclude at CHAT granularity — the whole
    conversation, every chunk, past AND future. Deliberate + PHI-conservative: a re-sync re-chunks a
    chat's NEW messages under fresh chunk_ids a chunk-exact match would miss, and no part of a
    patient-care thread should ever enter the store. chat.db returns guids already service-normalized
    to `any;±;<id>` — exactly the guid-part of the ref — so `guid == ref-minus-prefix-and-chunk`.
    Missing/empty ledgers -> empty set; only `imessage:` refs are honored; malformed lines skip.
    """
    if path is not None:
        return _guids_from_ledger(path)
    live = os.path.join(root, "_sources", "imsg-sensitive", "excluded-refs.jsonl")
    vault = os.path.join(root, "_vault", "imsg-sensitive", "excluded-refs.jsonl")
    return _guids_from_ledger(live) | _guids_from_ledger(vault)


def _iter_store_notes(root):
    """Yield every LIVE store note (the DOMAINS dirs under `root`; `_archive/` excluded)."""
    for d in DOMAINS:
        for fp in glob.glob(os.path.join(root, d, "**", "*.md"), recursive=True):
            if not fp.endswith("_index.md"):
                yield fp


def build_ref_index(root, wanted=None):
    """Map provenance-ref -> [note paths] over the live store. If `wanted` (a set of refs) is
    given, collect only those (a fast scan when reconciling a few chunks). Reads the ref through
    the single-owner `parse_ref` (never a local regex); only the frontmatter head is read."""
    idx = {}
    for fp in _iter_store_notes(root):
        try:
            with open(fp, encoding="utf-8") as f:
                head = f.read(8192)          # frontmatter (hence the provenance ref) is at the top
        except OSError:
            continue
        ref = parse_ref(head)
        if not ref:
            continue
        ref = ref.strip()
        if wanted is not None and ref not in wanted:
            continue
        idx.setdefault(ref, []).append(fp)
    return idx


def _untracked_md(root):
    """Absolute, normalized paths of git-UNTRACKED (new, non-ignored) `.md` files under `root`.

    Real crash debris is an untracked new note the distiller just Wrote; a committed/staged/
    modified TRACKED note is NOT debris. Returns None when git can't answer (non-repo, git
    missing) so callers FAIL CLOSED — never archive what can't be proven untracked.

    Why marker-absence alone is NOT proof of debris: `_sources/imsg-done.d/` is GITIGNORED and
    re-derivable, so a fresh clone / new machine / rebuilt `_sources` / re-extract that reproduces
    the same deterministic chunk_ids reads EVERY committed iMessage chunk as 'pending'. Keying
    cleanup on 'pending' alone would archive committed notes (data loss). Untracked-ness is the
    durability gate that distinguishes debris from a committed original. The real store is a git repo.
    """
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    paths = set()
    for tok in out.stdout.split(b"\x00"):
        if not tok:
            continue
        rel = tok.decode("utf-8", "surrogateescape")
        if rel.endswith(".md"):
            paths.add(os.path.normpath(os.path.join(root, rel)))
    return paths


def reconcile_orphans(root, refs, archive_rel=os.path.join("_archive", "imsg-crash-orphans")):
    """Archive genuine crash-debris notes whose ref is in `refs`; NEVER touch a TRACKED note.

    Called by `imsg_worklist.py` for the refs of the chunks it is about to RE-EMIT (pending, i.e.
    un-marked). A store note bearing such a ref is a re-distill hazard, but ONLY genuine debris —
    a git-UNTRACKED new file the distiller Wrote — may be relocated. A TRACKED (committed / staged
    / modified) note that shares a pending chunk's ref is ANOMALOUS (marker absence != debris,
    because markers are gitignored and re-derivable — see `_untracked_md`): it is SKIPPED and
    surfaced, NEVER auto-archived. Archiving a committed note would drop curated frontmatter/edits
    AND move a file without fixing inbound `[[wikilinks]]`. FAILS CLOSED: if git can't prove a note
    untracked, it is skipped.

    Per "never delete a note — move to _archive/", debris is ARCHIVED, not unlinked; the subdir
    name flags it as crash debris. Returns {"moved": [(ref, src, dst)], "skipped": [(ref, src, why)]}.
    """
    refs = {r for r in refs if r}
    if not refs:
        return {"moved": [], "skipped": []}
    idx = build_ref_index(root, wanted=refs)
    if not idx:
        return {"moved": [], "skipped": []}
    untracked = _untracked_md(root)          # None => fail closed (prove-untracked-or-skip)
    archive_dir = os.path.join(root, archive_rel)
    moved, skipped = [], []
    for ref in sorted(refs):
        for src in sorted(idx.get(ref, [])):
            if untracked is None or os.path.normpath(src) not in untracked:
                skipped.append((ref, src, "git-unavailable" if untracked is None else "tracked"))
                continue
            os.makedirs(archive_dir, exist_ok=True)
            base = os.path.basename(src)
            dst = os.path.join(archive_dir, base)
            n = 2
            while os.path.exists(dst):
                stem, ext = os.path.splitext(base)
                dst = os.path.join(archive_dir, "%s-%d%s" % (stem, n, ext))
                n += 1
            shutil.move(src, dst)
            moved.append((ref, src, dst))
    return {"moved": moved, "skipped": skipped}


# --- done-marker helpers (the SINGLE owner of verify+stamp; scripts/stamp_markers.py drives these
#     after the Workflow — the retired _lib/imsg_markers.mjs did it in-workflow before pure-JS) ---

def marker_path(done_dir, chunk_id):
    return os.path.join(done_dir, chunk_id)


def verify_declared(paths, root):
    """(ok, missing): every declared note path (absolute or root-relative) must exist on disk."""
    missing = []
    for p in paths or []:
        ap = p if os.path.isabs(p) else os.path.join(root, p)
        if not os.path.exists(ap):
            missing.append(p)
    return (not missing), missing


def write_marker(done_dir, chunk_id, record):
    """Atomically write the code-owned done-marker (JSON audit record). Done-detection keys on
    marker EXISTENCE (imsg_worklist.py), so the content is free to be rich."""
    import json
    os.makedirs(done_dir, exist_ok=True)
    path = marker_path(done_dir, chunk_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def stamp_verified(done_dir, root, chunk):
    """Verify one chunk's declared notes+sensitive exist; stamp the marker IFF all do.
    Returns (stamped_bool, missing_list). The single owner of the verify+stamp loop —
    scripts/stamp_markers.py calls this once per RESULT file after the Workflow."""
    cid = chunk.get("chunk_id")
    if not cid:
        return False, ["<no chunk_id>"]
    notes = list(chunk.get("notes") or [])
    sensitive = list(chunk.get("sensitive") or [])
    # Completeness (finding 3): a chunk claiming N durable items must have RETURNED N paths, else
    # verify_declared([]) trivially passes and an under-reported chunk gets a marker and is skipped
    # forever (silent data loss). Withhold the marker on a count mismatch. (bool is an int subclass
    # — exclude it.)
    durable = chunk.get("durable_found")
    if isinstance(durable, int) and not isinstance(durable, bool) and durable > 0 \
            and len(notes) + len(sensitive) != durable:
        return False, ["incomplete: durable_found=%d but %d path(s) returned"
                       % (durable, len(notes) + len(sensitive))]
    declared = notes + sensitive
    ok, missing = verify_declared(declared, root)
    if not ok:
        return False, missing
    record = {
        "chunk_id": cid,
        "ref": chunk.get("ref"),
        "notes": notes,
        "sensitive": sensitive,
        "verified": True,
        # No wall-clock `stamped` field: the Node twin runs inside the Workflow sandbox (argless
        # new Date() throws there), so both twins omit it to stay in sync. Marker mtime records time.
    }
    write_marker(done_dir, cid, record)
    return True, []
