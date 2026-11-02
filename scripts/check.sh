#!/usr/bin/env bash
# engram integrity + freshness guard — the maintenance-loop check (ingest → compile → lint).
# Regenerates every derived artifact, runs the typed linter, and FAILS if the derived views
# had drifted stale (regeneration changed tracked files) or lint found errors. Run before a
# commit, or wire as a pre-commit / CI hook.
#
#   bash scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "--- lint (typed) — fail fast on malformed notes BEFORE regenerating over them ---"
python3 scripts/lint_store.py   # exits non-zero on errors (set -e propagates)

# Corrections ledgers (_manifest/corrections/*.jsonl) — committed, hand-editable, merge-able, and
# read at distiller-LAUNCH time. A line that doesn't parse used to be silently skipped by both
# readers, so a bad merge cost the next sync its adversarial reminders AND its dedup key (a dropped
# id makes the next audit append a duplicate). Same fail-fast placement as the store linter: a
# broken ledger must stop a commit here, not a distiller mid-wave.
python3 scripts/corrections_lint.py   # exits non-zero on errors (set -e propagates)

# Pre-existing note-body MODIFICATIONS (tracked notes already dirty BEFORE we regenerate). Our
# generators only ADD backlinks to note frontmatter; a note whose body was ALREADY modified before
# this run is NOT our doing — it's the tell for an accidental in-place history re-distill (guard-audit
# finding 1/7). A deliberate backfill re-distill (granola-redistill) is the benign cause: investigate,
# confirm intent, then commit.
#
# check.sh does NOT compute the path list any more (2026-08-04) — enumeration, routing and the field
# gate are one call into verify_frontmatter_preserved.py, and no path ever crosses the shell boundary,
# because it cannot do so safely. This used to be
#     pre_mod_notes=$(git status --porcelain -- '*.md' | grep -E '^(.M|M.)' | grep -vE ...)
#     classified=$(... --classify $pre_mod_paths)          # unquoted, "store paths have no spaces"
# and both halves were wrong on the store's own filenames: plain porcelain QUOTES a path with a space
# or a non-ASCII byte (core.quotePath) and an unquoted `$var` word-splits, so a modified
# `research/José Ejémplo Muestra.md` arrived as THREE arguments naming no file. Hundreds of
# tracked notes carry a space, the people registry's canonical spellings carry accents, and `move_note` renames
# person notes TO those spellings — so the gate's precise per-field branch never saw the notes it
# exists for (they all fell to the blunt SUSPICIOUS halt below, which prints filename fragments).
# Fail-closed, but precision-blind. Detail + the exclusion rules: `modified_paths`/`_is_domain_note`.
#
# Captured PRE-regen, and now CHECKED against that same pre-regen snapshot: gen_backlinks.py may add
# `related:` to a note and `related` is FROZEN under the redistill profile, so a generator's own edit
# read as a violation (and, the other way, could restore a frozen field a redistill had blanked). A
# note the generators rewrote is caught by the STALE check below either way.
#
# Exit code is a bitmask: 4 = a frozen-field violation, 8 = pre-modified notes that are not from a
# distilled source. Any OTHER non-zero means the gate itself crashed — reported as such, never
# swallowed (`set -e` cannot see it through the `$(...) || rc=$?`).
mod_rc=0
mod_report=$(python3 scripts/verify_frontmatter_preserved.py --modified notes 2>&1) || mod_rc=$?

# MOVED/RENAMED notes — the frozen-field gate's blind spot until 2026-08-04 (review finding H4).
# The modified-note gate above matches `^(.M|M.)` ONLY, but a move is ` D <old>` + `?? <new>` (or `R` once
# staged), and `verify_frontmatter_preserved.check_path` resolves `git show HEAD:<path>` — which
# finds nothing at the DESTINATION. So the gate returned CLEAN for exactly the domain/tags/title
# re-stamp it hard-refuses on the identical note edited in place: `plan_move_note(..., new_domain=X,
# drop_tags=...)` re-stamped two frozen fields and nothing anywhere looked. (STALE below did fire,
# but it says "commit the regenerated output" — which is precisely what an operator then does.)
# `--moved` pairs the move itself (git's own pairing when staged, slug/provenance.ref/body otherwise)
# and diffs the destination against the ORIGINAL at HEAD. Routing mirrors the in-place gate exactly:
# distilled-source notes get the redistill field gate, everything else nothing (a rename is not a
# body modification, and move_note is the sanctioned tool for the obsidian person-note renames).
# Captured PRE-regen for the same reason `--modified notes` is: gen_backlinks.py may add `related:` to
# the moved note below, and `related` is FROZEN, so a generator's own edit would read as a violation.
moved_rc=0
moved_report=$(python3 scripts/verify_frontmatter_preserved.py --moved 2>&1) || moved_rc=$?

echo "--- regenerate derived artifacts ---"
python3 scripts/build_index.py
python3 scripts/build_cooccurrence.py
python3 scripts/regen.py
python3 scripts/gen_backlinks.py
# 2nd index pass: gen_backlinks may have ADDED `related:` links to note frontmatter, which
# build_index reads into index.jsonl + the curated-inbound counts in backlinks.md. Re-index over
# the post-backlink notes so both converge in ONE check.sh run — otherwise a note that just gained a
# link leaves index.jsonl/backlinks.md stale until the NEXT run, so the gate never reaches a fixpoint
# on a clean tree. No-op (identical output) when gen_backlinks changed nothing. build_clarify_queue
# then reads the converged index.
python3 scripts/build_index.py
python3 scripts/build_clarify_queue.py

echo "--- PHI scan (store audit) — WARN-only on flags; a scan CRASH fails the gate ---"
# Bare invocation ON PURPOSE (Phase 4.1): flags → exit 0 → PHI-WARN printed, gate continues;
# an internal scan error → non-zero → `set -e` aborts check.sh (fail-closed — never green over
# a dead scan). Do NOT wrap in `|| true` / pipe through head — either would defeat fail-closed.
python3 scripts/phi_scan_store.py --summary

echo "--- dossier drift (informational — advisory only, never affects this gate's exit code) ---"
python3 scripts/dossier_drift.py | tail -1 || true   # advisory; `|| true` so a drift-tool crash never gates

# STATUS.md headline note-count vs reality (advisory WARN — STATUS.md is hand-maintained, so it drifts
# after a sync/reorg; surface it so tallies get refreshed, but never fail the gate on it).
status_n=$(grep -oE 'Total notes: [0-9,]+' _manifest/STATUS.md | head -1 | tr -dc 0-9)
index_n=$(wc -l < _manifest/index.jsonl | tr -d ' ')
if [ -n "$status_n" ] && [ "$status_n" != "$index_n" ]; then
  echo "STATUS-count WARN: STATUS.md headline says $status_n but index.jsonl has $index_n — refresh STATUS.md tallies."
fi

echo "--- freshness: regenerated views must match what's committed ---"
derived=(_manifest/index.jsonl _manifest/backlinks.md _manifest/co-occurrence.md
         _manifest/clarify-queue.jsonl
         _manifest/STATUS.md)   # STATUS tallies are regen-owned (v3.4) — a stale block now fails the gate
# digests/action-items.md + digests/decisions.md were REMOVED from this set (2026-07-12, plan
# generic-bubbling-eagle.md Feature D): they are no longer committed/regenerated by check.sh —
# generate on demand via `scripts/gen_digests.py`. The `digests/` prefix is already excluded from
# the pre_mod_notes / notes_changed greps above/below, so their working copies never trip the gate.
# every _index.md at ANY depth (period-split leaves nest arbitrarily deep); -print0 for safe paths
while IFS= read -r -d '' f; do derived+=("${f#./}"); done \
  < <(find . -name '_index.md' -not -path './.git/*' -print0)
stale=$(git status --porcelain -- "${derived[@]}" 2>/dev/null || true)
# notes themselves can gain backlinks; flag any note the generators just rewrote, too
notes_changed=$(git status --porcelain -- '*.md' | grep -vE '_manifest/|digests/|_index\.md|dossiers/|literature/|^.{3}[_.]|^.{3}[^/]+$' || true)

rc=0
# In-place-rewrite CONTENT-INTEGRITY gate (Feature B, plan generic-bubbling-eagle.md). Replaces the
# old blunt "any pre-existing body mod -> SUSPICIOUS fail" with a PRECISE, source-scoped preserve-
# field gate. `--modified notes` (run pre-regen, above) = domain notes already modified before this
# run — the generators only ADD backlinks, so a pre-existing BODY rewrite is not our doing.
#
# DESIGN CALL (documented for review — genuine false-positive/false-negative tradeoff): the
# DISTILLED connector sources are the ones with an in-place rewrite flow — gmail_update.mjs,
# granola_redistill.mjs, and the fidelity-audit fix passes that rewrite bodies in place (outlook
# added 2026-07-20; imessage/whatsapp added the same day, when the granola/iMessage audit produced 9
# confirmed iMessage body defects and there was NO gate-passing way to correct them: every iMessage
# body fix fell to the blunt halt below, i.e. the store could measure a defect it could not fix).
# Only notes that were gmail/granola/outlook/imessage/whatsapp AT HEAD get the `redistill` preserve-
# field gate: a CLEAN redistill (body + `updated:` changed, participants append-only) now PASSES;
# a frozen-field change (blanked related:, mutated provenance.ref, retitled, re-domained, or a
# distiller illegally setting `verified:`) is a HARD ERROR that names the field. We deliberately do
# NOT apply the redistill freeze to every modified note: that profile freezes nearly the whole
# frontmatter (tags/domain/subpath/related/...), so gating every note would false-positive on
# legitimate frontmatter MAINTENANCE — a tag fix, a subpath move, a /clarify role/domain correction
# — on notes that never went through a machine redistill. Every OTHER pre-modified note (obsidian =
# VERBATIM by convention, gdrive/manual/claude-chat) keeps the blunt SUSPICIOUS halt: no in-place
# rewrite path exists for those, so a pre-existing body rewrite is a hand-edit or an accidental
# history re-distill — worth an investigate-before-commit stop. Note the precise gate is not a
# loosening: a title/date/ref change is still a HARD failure for these sources, it just names the
# field instead of stopping on the whole file. Classify by the HEAD source (what the note WAS), so a
# redistill that corrupted `source:` itself still lands in the precise gate.
#
# The gmail/granola/outlook/imessage/whatsapp source list is NOT written here (2026-08-02): it was a
# third hardcoded copy alongside clarify_writeback.py's and this file's prose, and adding a sixth
# distilled source while missing one copy silently reopens the seam. `classify` (called by
# `--modified notes` above) is the single owner — it reads _lib/frontmatter_diff.REDISTILL_SOURCES
# and reads `source:` from the FRONTMATTER BLOCK (the old `grep -m1 '^source:'` here scanned the
# whole file, so a body line `source: gmail` could route a hand-edited note into the precise gate
# instead of the halt).

# SANCTIONED-OVERRIDE LEDGER REVIEW (mandatory, added 2026-08-02). Every uncommitted line in
# _manifest/frontmatter-overrides.jsonl is a LIVE excuse for a frozen-field change on a note — the
# one thing that can make the gate below go green over a changed title/provenance.ref/participants.
# Nothing used to show them: every modified-note query above excludes `_manifest/`, so a committed line
# could also be rewritten IN PLACE with no signal anywhere. This prints the pending lines and FAILS
# if a committed line was modified or removed (the ledger is append-only).
echo "--- sanctioned-override ledger (REVIEW every uncommitted line — each excuses a frozen field) ---"
if ! python3 scripts/verify_frontmatter_preserved.py --audit-ledger; then
  echo "OVERRIDE-LEDGER ERROR: _manifest/frontmatter-overrides.jsonl is APPEND-ONLY and a line that is"
  echo "already committed was modified or removed (report above). A committed entry is an inert audit"
  echo "record; rewriting one is either a bad merge/rebase or an attempt to retro-authorize a change."
  echo "Restore it from HEAD (git checkout HEAD -- _manifest/frontmatter-overrides.jsonl) and append a"
  echo "NEW entry via scripts/edit_note_frontmatter.py if a further change is genuinely sanctioned."
  rc=1
fi

if [ "$mod_rc" -ne 0 ]; then
  printf '%s\n' "$mod_report"
  rc=1
  if [ "$(( mod_rc & 4 ))" -ne 0 ]; then
    echo "FRONTMATTER-PRESERVE ERROR (redistill): a gmail/granola/outlook in-place redistill changed a FROZEN"
    echo "frontmatter field (per-field report above). An in-place redistill may change ONLY the body +"
    echo "'updated:' and may only APPEND participants; a changed title/date/tags/domain/subpath/related/"
    echo "supersedes/provenance.ref/verified is the tell for a redistill that dropped curated cross-links,"
    echo "re-anchored provenance, or re-titled the note. Restore the frozen fields from HEAD before"
    echo "committing. Do NOT blind-commit."
  fi
  if [ "$(( mod_rc & 8 ))" -ne 0 ]; then
    echo "SUSPICIOUS (listed above): these note bodies were already modified BEFORE regeneration and are"
    echo "NOT from a distilled connector source — the generators only add backlinks, so a pre-existing"
    echo "in-place note rewrite is the tell for an accidental history re-distill (guard-audit finding 1)"
    echo "or an unreviewed hand-edit. INVESTIGATE before committing; a deliberate hand-correction is the"
    echo "benign cause. Do NOT blind-commit."
  fi
  if [ "$(( mod_rc & 12 ))" -eq 0 ]; then      # 12 = RC_FIELD|RC_SUSPICIOUS: neither bit => not a finding
    echo "FRONTMATTER-GATE ERROR: the pre-modified-note gate exited $mod_rc — it CRASHED rather than"
    echo "reporting a finding (report above). Fix it before committing: a gate that cannot run gates"
    echo "nothing, and 'no findings' from a dead check is not a green tree."
  fi
fi

if [ "$moved_rc" -ne 0 ]; then
  printf '%s\n' "$moved_report"
  echo "FRONTMATTER-PRESERVE ERROR (move): a note was MOVED/RENAMED and the version at its NEW path"
  echo "changed a FROZEN frontmatter field relative to its ORIGINAL at HEAD (per-field report above)."
  echo "A move is not a licence to re-stamp identity — the same change made in place is hard-refused,"
  echo "and until 2026-08-04 the weaker operation was the one that passed. Restore the frozen fields,"
  echo "or make the change through scripts/edit_note_frontmatter.py (guarded, ledger-recorded) and"
  echo "commit the note together with its ledger line. Do NOT blind-commit."
  rc=1
fi

# Dossiers are NO LONGER invisible to modification detection (Feature B): a modified dossier gets the
# `dossier` preserve gate — title/domain/subpath/type/source/provenance.ref are FROZEN, while
# related/supersedes/superseded_by are MUTABLE (rewriting cross-links IS reconciliation's job). Until
# now dossier edits were entirely excluded from check.sh; this closes that gap. (Dossiers stay
# excluded from the STALE `notes_changed` check above — they are deliberately reconciled + committed,
# not regen output — so this preserve gate is their ONLY content-integrity check.)
# Same NUL-safe handoff as the note gate above (a dossier filename may carry a space or an accent
# just as a note's can): enumeration + gate in one call, no path list in shell.
dossier_rc=0
dossier_report=$(python3 scripts/verify_frontmatter_preserved.py --modified dossiers 2>&1) || dossier_rc=$?
if [ "$dossier_rc" -ne 0 ]; then
  printf '%s\n' "$dossier_report"
  rc=1
  if [ "$(( dossier_rc & 4 ))" -ne 0 ]; then
    echo "FRONTMATTER-PRESERVE ERROR (dossier): a modified dossier changed a FROZEN identity field"
    echo "(title/domain/subpath/type/source/provenance.ref — per-field report above). A reconcile may"
    echo "rewrite the body and the related/supersedes cross-links, but NEVER the note's identity. Use"
    echo "scripts/reconcile_dossier.py (validate->write->lint->rollback) or restore the frozen fields"
    echo "before committing."
  else
    echo "FRONTMATTER-GATE ERROR: the modified-dossier gate exited $dossier_rc — it CRASHED rather than"
    echo "reporting a finding (report above). Fix it before committing."
  fi
fi
if [ -n "$stale" ] || [ -n "$notes_changed" ]; then
  echo "STALE: regeneration changed tracked files — commit the regenerated output:"
  echo "$stale"
  echo "$notes_changed"
  rc=1
fi
if [ "$rc" -ne 0 ]; then exit 1; fi
echo "OK — derived views fresh, lint clean."
