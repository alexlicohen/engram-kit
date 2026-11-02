#!/usr/bin/env python3
"""Bump a source's sync watermark FROM DISK — never from today().

    python3 scripts/bump_watermark.py --source <S> [--dry-run] [--root R] [--force] [--maxts P]

WATERMARK GRANULARITY — two regimes (see _manifest/SYNC.md "Watermark granularity"):
  * iMessage is TIMESTAMP-granular. chat.db gives an exact nanosecond `message.date`, and
    imsg_select/imsg_extract query `--since` with a STRICT `>`; so the watermark is the MAX message
    instant imsg_extract actually PROCESSED (read from the `_sources/imessage.maxts` sidecar,
    ceil-to-second), NOT a note date. That lets a same-day re-sync fetch only strictly-newer
    messages — no overlap, no miss. `--source imessage` uses this path.
  * Every OTHER source (gmail/granola) is DAY-granular. Their connectors query by DATE and dedup
    per item (Message-ID / granola:<id>), so re-querying the whole last day is correct and simpler.
    Their watermark is the MAX `date:` frontmatter of notes actually written (git-ADDED) this wave.

Either way the watermark reflects on-disk reality, not "today" (a distiller can run days after the
notes it covers, or partially — today() would silently skip anything after the run but before the
next watermark). Reuses scripts/sync_verify.py's wave-detection logic (SOURCES, git_changed_md,
note_meta, load_index, matches) for the day-granular sources rather than re-implementing it — the
"what notes belong to this wave" decision has ONE owner (sync_verify.py); the ns/timestamp math has
ONE owner (_lib/imsg_time.py).

Source -> sync-state.json key mapping (some sync_verify sources share a watermark, some have none):
    gmail              -> gmail_personal
    gmail-secondary    -> gmail_secondary
    granola-new        -> granola
    granola-redistill  -> granola
    imessage           -> imessage
    imsg-reclass       -> imessage
    whatsapp           -> whatsapp
    outlook            -> outlook
    drive              -> (no watermark key; error)
    gmail-remediate2   -> (no watermark key; error)
    gmail-update       -> (no watermark key; error — redistills grown threads IN PLACE, doesn't
                            advance the ingestion frontier)

Refuses to move a watermark BACKWARDS unless `--force`. Writes atomically (tmp + os.replace).
Gate: 0 matched git-ADDED notes -> watermark left unchanged, exit 0 (this is normal — a wave can
legitimately drop everything, or sync_verify can be re-run without new adds).
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.notes import split_frontmatter, get_field  # noqa: E402
from _lib.imsg_time import since_to_apple_ns  # noqa: E402  (single owner of the ns/timestamp math)
from sync_verify import SOURCES, git_changed_md, note_meta, load_index, matches  # noqa: E402
from wave_prepare import atomic_write  # noqa: E402  (single owner of the fsync'd tmp+replace write)

# sync_verify --source name -> sync-state.json key. None = no watermark tracked for this source
# (bumping it would be a mistake, not just a no-op — refuse loudly rather than silently skip).
# MUST cover every key in sync_verify.SOURCES — a source present there but missing here crashes
# with an unhandled KeyError instead of the clean "no tracked watermark" refusal below.
SOURCE_TO_WATERMARK_KEY = {
    "gmail": "gmail_personal",
    "gmail-secondary": "gmail_secondary",
    "granola-new": "granola",
    "granola-redistill": "granola",
    "imessage": "imessage",
    "imsg-reclass": "imessage",
    "whatsapp": "whatsapp",
    "outlook": "outlook",            # day-granular (email) — note-date watermark, like gmail/granola
    "drive": None,
    "gmail-remediate2": None,
    "gmail-update": None,            # redistills grown threads IN PLACE — not new-content ingestion,
                                      # doesn't advance any frontier, so there's nothing to bump
}
assert set(SOURCE_TO_WATERMARK_KEY) == set(SOURCES), (
    f"SOURCE_TO_WATERMARK_KEY is out of sync with sync_verify.SOURCES — missing: "
    f"{set(SOURCES) - set(SOURCE_TO_WATERMARK_KEY)}, extra: "
    f"{set(SOURCE_TO_WATERMARK_KEY) - set(SOURCES)}")

# Sources whose worklist has no date lower-bound (an arbitrary-order backlog, unlike gmail/
# granola's after:<watermark> connector query) — a computed watermark BEHIND the current one is
# EXPECTED there (an older backlog thread getting synced), not a bug signal, so it's a silent
# no-op instead of the hard refusal that protects the date-bounded sources from a genuine
# watermark-computation error.
BACKLOG_SOURCES = {"outlook"}


def note_date(root, path):
    """Read a note's `date:` frontmatter (ISO YYYY-MM-DD), or None if missing/unreadable."""
    abspath = os.path.join(root, path)
    if not os.path.exists(abspath):
        return None
    txt = open(abspath, encoding="utf-8").read()
    head, _ = split_frontmatter(txt)
    if head is None:
        return None
    d = get_field(head, "date")
    return d or None


def timestamp_watermark(root, source, maxts_path):
    """TIMESTAMP regime (chat-DB sources: imessage, whatsapp): the watermark is the MAX message
    instant the extractor processed, read from the `_sources/<source>.maxts` sidecar (ceil-to-second
    ISO-8601 UTC). Returns (new_val, evidence) or (None, reason) when there is no sidecar to read
    (fail-closed — never today())."""
    path = maxts_path or os.path.join(root, "_sources", f"{source}.maxts")
    if not os.path.exists(path):
        return None, (f"no max-processed-timestamp sidecar at {path} — run the {source} extractor "
                      f"(it writes the sidecar) before bumping the {source} watermark")
    val = open(path, encoding="utf-8").read().strip()
    if not val:
        return None, f"empty max-processed-timestamp sidecar {path}"
    return val, f"max processed message ts from {os.path.relpath(path, root)}"


def note_date_watermark(root, source):
    """DAY regime (gmail/granola): the watermark is the MAX `date:` of notes actually git-ADDED this
    wave. Reuses sync_verify's wave-detection. Returns (new_val, evidence) or (None, reason)."""
    cfg = SOURCES[source]
    index = load_index(root)
    changed = git_changed_md(root)
    dates = []
    for path, state in sorted(changed.items()):
        if state != "added":
            continue  # only notes actually WRITTEN this wave (git-added), matching sync_verify's
                       # incremental-mode contract — modified/deleted are not new watermark evidence.
        src, ref = note_meta(root, path, index)
        if not matches(cfg, src, ref, path):
            continue
        d = note_date(root, path)
        if d:
            dates.append(d)
    if not dates:
        return None, f"no new {source} notes on disk"
    return max(dates), f"{len(dates)} matched added note(s)"


def is_backwards(new_val, old):
    """True if new_val is EARLIER than old, comparing across the date/timestamp boundary (both are
    parsed to an exact instant via the single-owner _lib/imsg_time; a bare date is midnight UTC).
    This makes the date->timestamp transition for imessage compare correctly."""
    if not old:
        return False
    return since_to_apple_ns(new_val) < since_to_apple_ns(old)


def main():
    ap = argparse.ArgumentParser(description="Bump a source's sync watermark from on-disk reality "
                                              "(never today()); iMessage=timestamp, others=day.")
    ap.add_argument("--source", required=True, choices=sorted(SOURCES))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--force", action="store_true",
                    help="allow moving the watermark backwards (normally refused)")
    ap.add_argument("--maxts", default=None,
                    help="(imessage only) path to the max-processed-ts sidecar "
                         "(default _sources/imessage.maxts)")
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    key = SOURCE_TO_WATERMARK_KEY[args.source]
    if key is None:
        print(f"ERROR: source {args.source!r} has no tracked watermark in sync-state.json "
              f"(mapping is explicit in SOURCE_TO_WATERMARK_KEY) — refusing.")
        sys.exit(1)

    # Two regimes (see the module docstring + SYNC.md): chat-DB sources (imessage/whatsapp)=timestamp
    # sidecar, others (gmail/granola)=note date.
    if args.source in ("imessage", "whatsapp"):
        new_val, evidence = timestamp_watermark(root, args.source, args.maxts)
        if new_val is None:
            print(f"ERROR: {evidence}. Refusing (never today()).")
            sys.exit(1)
    else:
        new_val, evidence = note_date_watermark(root, args.source)
        if new_val is None:
            print(f"{evidence}; watermark unchanged")
            sys.exit(0)

    state_path = os.path.join(root, "_manifest", "sync-state.json")
    state = json.load(open(state_path, encoding="utf-8"))
    entry = state.get(key, {})
    old = entry.get("watermark_date")

    if is_backwards(new_val, old) and not args.force:
        if args.source in BACKLOG_SOURCES:
            print(f"{args.source}: computed watermark {new_val} is behind current {old} for "
                  f"{key!r} — this source's worklist has no date lower-bound (an arbitrary-order "
                  f"backlog), so an older batch getting synced is expected, not a bug. Watermark "
                  f"left at {old!r} (not moved backwards).")
            sys.exit(0)
        print(f"WARN: computed watermark {new_val} < current {old} for {key!r} — refusing to move "
              f"it backwards (pass --force to override). Watermark unchanged.")
        sys.exit(1)

    print(f"{args.source} -> {key}: {old!r} -> {new_val!r}  ({evidence})")

    if args.dry_run:
        print("(--dry-run: not written)")
        return

    if old is not None:
        entry["previous_watermark_date"] = old
    entry["watermark_date"] = new_val
    state[key] = entry

    atomic_write(state_path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {state_path}")


if __name__ == "__main__":
    main()
