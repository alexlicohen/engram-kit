#!/usr/bin/env python3
"""Post-wave verifier: prove a distiller produced the number of notes its contract promised —
and, critically, that it did NOT quietly touch history.

    python3 scripts/sync_verify.py --source SRC [--dropped N]

Reads `_sources/<source>.contract.consumed.json` — the contract resolve_wave.py atomically renamed
when the distiller launched (the contract is ONE-SHOT; see scripts/resolve_wave.py) — for
`expected_count` and `mode`, then computes the on-disk delta for that source: the new/modified
`.md` notes from `git status --porcelain`, cross-checked against their provenance refs (from
`_manifest/index.jsonl` when present, else read straight from the note frontmatter). Only notes
whose provenance ref matches the source's scheme are counted, so an unrelated edit elsewhere is
not mistaken for wave output. A still-live `<source>.contract.json` at verify time is itself a
FAIL: it means the distiller never consumed it (it did not run, or refused the contract).

The assertion is MODE-AWARE (guard-audit finding 1):
  - mode=incremental: every matched note MUST be git-ADDED. ANY matched MODIFIED note (including
    a staged-add overwritten in place, `AM` — evasion 2b) means the distiller re-distilled
    existing history in place, and ANY matched DELETED note (any `D` status — evasion 2a) means
    it destroyed history via delete-then-recreate -> FAIL, listing the paths. Then asserts
    `len(added) == expected_count - dropped`.
  - mode=backfill: in-place rewrites are the point (notes show as modified), so the count-only
    assertion `actual == expected_count - dropped` applies (matched deletes are still reported
    in the delta line).

Emits `SYNC OK: +N notes (expected N)` on success, or a clear FAIL + `sys.exit(1)`. `--dropped`
accounts for wave items that legitimately produced no note (thread/meeting/doc dropped as
non-durable, or a reclassify held rather than released).

Reuses scripts/_lib/provenance.py (parse_ref) and scripts/_lib/notes.py (split_frontmatter,
get_field). Run AFTER the distiller and after `build_index.py` (so index.jsonl reflects the new
notes); it also works pre-index by reading note frontmatter directly.
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.provenance import parse_ref          # noqa: E402
from _lib.notes import split_frontmatter, get_field  # noqa: E402
from _lib.hashing import sha256_file           # noqa: E402  (single owner of the hash, same as resolve_wave.py/wave_prepare.py)

# contract source name -> how to recognize a note produced by that wave.
#   ref_prefix : the provenance ref scheme the note must carry
#   src        : the frontmatter `source:` value
#   suffix     : (optional) filename regex the note must match — used only to separate the two
#                waves that SHARE the gmail:thread: scheme (a primary vs a secondary mailbox)
#   not_suffix : (optional) filename regex the note must NOT match (the primary excludes -sec…)
SOURCES = {
    # A second mailbox synced as its own source shares the gmail:thread: scheme + source:gmail with
    # the primary, so the -sec filename suffix is the ONLY reliable discriminator. Do NOT require a
    # positive suffix on the PRIMARY — LLM filename adherence is unreliable (a distiller once wrote
    # arbitrary suffixes instead of the documented -<thrid6> convention), and a verifier that depends
    # on it silently UNDERCOUNTS. Excluding the secondary's suffix is sufficient.
    "gmail":             dict(ref_prefix="gmail:thread:", src="gmail",
                              not_suffix=r"-sec[0-9a-f]{8}\.md$"),
    # gmail-update REDISTILLS grown gmail threads IN PLACE — same gmail:thread: scheme
    # + source:gmail + same -lv exclusion as gmail; it is a BACKFILL wave, so the matched notes show
    # as MODIFIED (count-only assertion, added==0 in a clean tree). Run it on a clean tree AFTER the
    # new-thread gmail sync is committed, else those staged-added gmail notes also match here.
    "gmail-update":      dict(ref_prefix="gmail:thread:", src="gmail",
                              not_suffix=r"-sec[0-9a-f]{8}\.md$"),
    # A SECOND gmail mailbox synced separately (e.g. a shared lab account). Same scheme/source as the
    # primary; told apart only by the -sec filename suffix. Drop this entry if you sync one mailbox.
    "gmail-secondary":   dict(ref_prefix="gmail:thread:", src="gmail",
                              suffix=r"-sec[0-9a-f]{8}\.md$"),
    "granola-new":       dict(ref_prefix="granola:", src="granola"),
    "granola-redistill": dict(ref_prefix="granola:", src="granola"),
    # Extension point: no Drive distiller ships with the kit; this key reserves the gdrive: source.
    "drive":             dict(ref_prefix="gdrive:", src="gdrive"),
    "imessage":          dict(ref_prefix="imessage:", src="imessage"),
    "whatsapp":          dict(ref_prefix="whatsapp:", src="whatsapp"),
    # Outlook (work email) — one note per thread, own scheme + source, like gmail but needing no
    # filename discriminator (distinct ref_prefix). Day-granular watermark (deduped per thread).
    "outlook":           dict(ref_prefix="outlook:thread:", src="outlook"),
    "gmail-remediate2":  dict(ref_prefix="gmail:thread:", src="gmail"),
    "imsg-reclass":      dict(ref_prefix="imessage:", src="imessage"),
}


def git_changed_md(root):
    """Return {path: state} for every changed .md under root, state in
    {added, modified, deleted}. Renames/copies are reported at their destination path.

    Classification order is load-bearing (guard-audit finding 1, evasions 2a/2b):
      1. ANY delete state (` D`, `D `, `AD`, `MD`, `RD`, ...) -> "deleted" — previously these
         fell through the chain and were INVISIBLE, so a re-distill that deleted a committed
         note and wrote a new-named one passed incremental as pure "added" (evasion 2a).
      2. A worktree-side `M` (`AM`, `MM`, `RM`, ` M`) -> "modified" — it must WIN over the add
         cases, else `AM` (a staged-added note overwritten in place) classifies as "added" and
         an in-place overwrite slips through incremental (evasion 2b).
      3. Then the add cases (`??`, `A `, `R `, `C `) and staged-only `M `."""
    out = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                         capture_output=True, text=True, check=True).stdout
    changed = {}
    for line in out.splitlines():
        if not line:
            continue
        xy, rest = line[:2], line[3:]
        if " -> " in rest:            # rename/copy: take the destination
            rest = rest.split(" -> ", 1)[1]
        path = rest.strip().strip('"')
        if not path.endswith(".md"):
            continue
        if "D" in xy:
            changed[path] = "deleted"   # evasion 2a: deletes must be visible, never fall through
        elif xy[1] == "M":
            changed[path] = "modified"  # evasion 2b: worktree-M (AM/MM/RM/ M) beats staged-add
        elif "?" in xy or "A" in xy:
            changed[path] = "added"
        elif "R" in xy or "C" in xy:
            changed[path] = "added"     # moved-into-store (e.g. imsg-reclass release)
        elif "M" in xy:
            changed[path] = "modified"  # staged-only modify (`M `)
    return changed


def load_index(root):
    """path -> {source, ref} from _manifest/index.jsonl, if it exists."""
    idx = {}
    p = os.path.join(root, "_manifest", "index.jsonl")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                idx[r["path"]] = {"source": r.get("source", ""), "ref": r.get("ref", "")}
    return idx


def note_meta(root, path, index):
    """(source, ref) for a note path — prefer the regenerated index, else read the file.
    A path missing from the worktree (a DELETED note, evasion 2a) is read from git HEAD so the
    delete can still be attributed to its source; ("", "") only if it never was committed."""
    if path in index:
        return index[path]["source"], index[path]["ref"]
    abspath = os.path.join(root, path)
    if os.path.exists(abspath):
        txt = open(abspath, encoding="utf-8").read()
    else:
        r = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=root,
                           capture_output=True, text=True)
        if r.returncode != 0:
            return "", ""
        txt = r.stdout
    head, _ = split_frontmatter(txt)
    if head is None:
        return "", ""
    return get_field(head, "source"), (parse_ref(head) or "")


def matches(cfg, source, ref, path):
    if not ref.startswith(cfg["ref_prefix"]):
        return False
    # A blank/missing source must NOT pass as a match — an unverified source is a mismatch, not a
    # free pass. Blindly accepting it lets a miscounted note (blank source, right ref_prefix)
    # plug a real gap left by a genuinely-missing note and print a false SYNC OK.
    if cfg.get("src") and source != cfg["src"]:
        return False
    if cfg.get("suffix") and not re.search(cfg["suffix"], path):
        return False
    if cfg.get("not_suffix") and re.search(cfg["not_suffix"], path):
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="Verify a distiller wave produced the expected notes.")
    ap.add_argument("--source", required=True, choices=sorted(SOURCES))
    ap.add_argument("--dropped", type=int, default=0,
                    help="wave items that legitimately produced no note (drops / held reclassifies)")
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    live_path = os.path.join(root, "_sources", f"{args.source}.contract.json")
    consumed_path = os.path.join(root, "_sources", f"{args.source}.contract.consumed.json")
    if os.path.exists(live_path):
        print(f"FAIL: live wave contract still present: {live_path} — the distiller never consumed "
              f"it (resolve_wave.py atomically renames the contract to *.contract.consumed.json when a "
              f"validated run starts). Either the distiller was not launched, or it refused the "
              f"contract — there is no completed run to verify.")
        sys.exit(1)
    if not os.path.exists(consumed_path):
        print(f"FAIL: consumed wave contract missing: {consumed_path} "
              f"(run scripts/wave_prepare.py, then the distiller — resolve_wave.py consumes the "
              f"contract at launch; this verifier reads the consumed copy)")
        sys.exit(1)
    contract = json.load(open(consumed_path, encoding="utf-8"))
    expected_count = contract["expected_count"]
    expected = expected_count - args.dropped
    mode = contract.get("mode")

    # A consumed contract is a snapshot of the wave AT THE MOMENT resolve_wave.py claimed it — it
    # is never deleted afterward, so a stale one from a PRIOR run can sit next to a wave file that
    # a later worklist run has since regenerated with different (larger) content. Trusting
    # expected_count blindly here would silently under-verify: a distiller launched straight
    # against that stale consumed contract (skipping a fresh wave_prepare.py+resolve_wave.py pass)
    # only processes the OLD slice, and this check would otherwise print a false SYNC OK. Re-hash
    # the wave file the contract itself names and refuse to trust a mismatch.
    wave_rel = contract.get("wave")
    if wave_rel:
        wave_path = wave_rel if os.path.isabs(wave_rel) else os.path.join(root, wave_rel)
        if not os.path.exists(wave_path):
            print(f"FAIL: consumed contract {consumed_path} names wave {wave_path}, which no "
                  f"longer exists — cannot confirm this verify run matches what was actually "
                  f"distilled.")
            sys.exit(1)
        current_sha = sha256_file(wave_path)
        if current_sha != contract.get("sha256"):
            print(f"FAIL: STALE contract — {consumed_path} recorded sha256 {contract.get('sha256')} "
                  f"(expected_count={expected_count}) for {wave_path}, but that file's CURRENT "
                  f"content hashes to {current_sha}. The wave was regenerated (e.g. a later "
                  f"worklist run) after this contract was consumed, so expected_count no longer "
                  f"reflects what's on disk today — this consumed contract cannot verify anything "
                  f"launched against the current wave. Re-run wave_prepare.py + resolve_wave.py "
                  f"for a FRESH contract before launching the distiller, then verify again.")
            sys.exit(1)

    cfg = SOURCES[args.source]
    index = load_index(root)
    changed = git_changed_md(root)

    # `deleted` is tracked SEPARATELY from matched/actual: it is not wave output (nothing was
    # produced), so it must not satisfy the count — it is pure evidence of harm (evasion 2a).
    matched, added, modified, deleted = [], [], [], []
    for path, state in sorted(changed.items()):
        source, ref = note_meta(root, path, index)
        if matches(cfg, source, ref, path):
            if state == "deleted":
                deleted.append(path)
                continue
            matched.append(path)
            (added if state == "added" else modified).append(path)

    actual = len(matched)
    in_index = sum(1 for p in matched if p in index)

    print(f"source={args.source} mode={mode} expected_count={expected_count} "
          f"dropped={args.dropped} -> expected {expected} notes")
    print(f"on-disk delta: {actual} notes ({len(added)} added, {len(modified)} modified"
          + (f", {len(deleted)} DELETED" if deleted else "")
          + f"); {in_index}/{actual} present in index.jsonl")

    def fail_count(n):
        print(f"SYNC FAIL: on-disk delta {n} != expected {expected} "
              f"(expected_count {expected_count} - dropped {args.dropped}).")
        surplus = [p for p in matched if p not in index]
        if surplus:
            print("  not-yet-indexed matched notes (run build_index.py, then re-verify):")
            for p in surplus[:20]:
                print(f"    {p}")
        sys.exit(1)

    if mode == "incremental":
        # An incremental wave may ONLY create notes. A matched MODIFIED note means the distiller
        # re-distilled existing history in place — the exact overwrite that wipes curated
        # frontmatter/backlinks (guard-audit finding 1); a matched DELETED note means it destroyed
        # history outright (delete-then-recreate, evasion 2a — and notes are NEVER deleted, only
        # archived). Fail loudly, name every casualty before exiting.
        harmed = False
        if deleted:
            print(f"SYNC FAIL: mode=incremental but {len(deleted)} matched note(s) were DELETED — "
                  f"an incremental wave must only CREATE notes; a delete during a create-only wave "
                  f"destroys committed history (evasion 2a; store rule: notes are archived, never "
                  f"deleted). Restore them (git checkout -- <path>) before anything is committed:")
            for p in deleted:
                print(f"    {p}")
            harmed = True
        if modified:
            print(f"SYNC FAIL: mode=incremental but {len(modified)} matched note(s) were MODIFIED "
                  f"in place — an incremental wave must only CREATE notes. The distiller "
                  f"re-distilled existing history over these notes (guard-audit finding 1); "
                  f"inspect/restore them (git diff / git checkout --) before anything is committed:")
            for p in modified:
                print(f"    {p}")
            harmed = True
        if harmed:
            sys.exit(1)
        if len(added) != expected:
            fail_count(len(added))
    elif mode == "backfill":
        # In-place re-distill is the point of a backfill (authorized via --allow-history at
        # contract time); modified notes are expected — count-only assertion.
        if actual != expected:
            fail_count(actual)
    else:
        print(f"SYNC FAIL: contract mode {mode!r} is not incremental|backfill — refusing to verify "
              f"(re-pin with scripts/wave_prepare.py).")
        sys.exit(1)

    print(f"SYNC OK: +{actual} notes (expected {expected})")


if __name__ == "__main__":
    main()
