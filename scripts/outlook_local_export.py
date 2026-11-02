#!/usr/bin/env python3
"""Read Outlook's own local offline-mail cache directly — a second (working) path for the
Outlook `.eml` export delta, independent of the Graph API path (which an employer's Conditional
Access policy can block — see `_manifest/SYNC.md` › Outlook).

Outlook for Mac's legacy "Outlook 15" profile caches synced mail LOCALLY as complete,
self-contained raw RFC822 files (`*.mime`, one per message, attachments fully embedded) under:

    ~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/<profile>/Files/S0/<accountID>/MimeFiles/**/*.mime

Verified: `file(1)` identifies these as "RFC 822 mail text ... CRLF line
terminators" — i.e. the exact same format `outlook_extract.py` already parses, just a different
extension, and verified self-contained (attachments carry real decoded payload bytes, not
out-of-band references). No SQL, no auth, no network, no Conditional Access — Outlook the app
already did the syncing to get these onto disk; this only reads what it wrote. (The
`Outlook.sqlite` `Mail`/`AccountsExchange` tables that looked like the obvious account->folder
map are declared as real tables but return 0 rows for this profile — likely because a "New
Outlook"-era build keeps its index elsewhere / server-side. This script doesn't depend on them at
all — it discovers the right account's MimeFiles dir by sniffing message headers instead.)

Read-only against the Outlook profile — never writes there. Copies each not-yet-exported `.mime`
verbatim into `_sources/outlook/` (gitignored) with a `local_` prefix and `.eml` extension, same
shape `outlook_extract.py` already reads. **Cross-source duplicates are NOT this script's job to
prevent** — a message copied here that's also already present from a manual export is a real
duplicate `.eml` file, deliberately left to `outlook_select.py`'s message-level Message-ID dedup
(the correct single owner of thread/message assembly) rather than solved at the export layer.
Idempotent via a local, per-account-dir export-state file recording already-copied source
basenames — cheap to fully re-scan anytime (no pagination/network cost like the Graph path had),
checkpointed periodically (not just at the end) so an interrupted run doesn't redo everything, and
written atomically so a crash mid-save can't corrupt the state file itself.

Usage:
    python3 scripts/outlook_local_export.py [--account-email you@work.example]
                                             [--dest _sources/outlook] [--dry-run]
"""
import argparse
import email
import glob
import hashlib
import itertools
import json
import os
import sys
import time

PROFILE_ROOT = os.path.expanduser(
    "~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles")
STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "_sources", "outlook-local-export-state.json")
# The work account whose cache to read: --account-email, else $ENGRAM_OUTLOOK_ACCOUNT (never committed).
DEFAULT_ACCOUNT_EMAIL = os.environ.get("ENGRAM_OUTLOOK_ACCOUNT", "")
SAMPLE_SIZE = 5          # messages sampled per candidate account dir when sniffing for a match
CHECKPOINT_EVERY = 200   # copies between state saves, so a mid-run crash loses at most this many
SETTLE_SECONDS = 30      # a .mime younger than this is skipped outright (not read, not marked
                          # seen) -- Outlook writes headers-first, body-streamed-after, so a file
                          # can pass the header-presence check in _looks_like_complete_message()
                          # while its body is still mid-write; there is no reliable universal
                          # "body is done" marker to parse for, so instead we just don't touch a
                          # file until it's had time to finish (a real bug found 2026-07-10: a
                          # header-complete/body-truncated file was copied and marked exported
                          # forever, since all four header checks passed on a truncated read)


def _iter_mime_files(mimefiles_dir):
    return glob.iglob(os.path.join(mimefiles_dir, "**", "*.mime"), recursive=True)


def find_account_mime_dirs(account_email, profile_root=PROFILE_ROOT):
    """Sniff every Files/S0/<id>/MimeFiles dir (across every local profile) by reading up to
    SAMPLE_SIZE sample messages' address headers, and return the ones addressed to/from
    account_email. Sampling several messages (and checking Delivered-To/X-Original-To, not just
    From/To/Cc) makes this robust to one unlucky Bcc-only sample wrongly excluding — or, worse,
    silently dropping mail from — a real match. Uses iglob so a non-matching dir's thousands of
    files are never fully materialized just to inspect the first few. Normally exactly one match."""
    matches = []
    for mimefiles_dir in glob.glob(os.path.join(profile_root, "*", "Files", "S0", "*", "MimeFiles")):
        for src in itertools.islice(_iter_mime_files(mimefiles_dir), SAMPLE_SIZE):
            with open(src, "rb") as f:
                msg = email.message_from_binary_file(f)
            headers = " ".join(str(msg.get(h, "")) for h in
                                ("From", "To", "Cc", "Delivered-To", "X-Original-To"))
            if account_email.lower() in headers.lower():
                matches.append(mimefiles_dir)
                break
    return matches


def _safe_name(src_path):
    # basenames are Outlook's own opaque per-item numeric ids (e.g. "12345678.mime") — NOT
    # globally unique on their own (two different matched account dirs can share one), so hash
    # the full source path, not just the basename, or two distinct messages collide on the same
    # output filename and the second silently overwrites the first on disk (caught by
    # TestCrossAccountBasenameCollision — the first version of this fix hashed only the basename).
    h = hashlib.sha1(src_path.encode("utf-8")).hexdigest()[:10]
    stem = os.path.splitext(os.path.basename(src_path))[0]
    return f"local_{h}_{stem}.eml"


def _looks_like_complete_message(raw_bytes):
    """Second-layer, cheap sanity check (the primary defense is the SETTLE_SECONDS age gate in
    export(), below). A parse that yields NONE of the load-bearing headers is obviously not a real
    message — the caller must NOT mark it exported, so it's retried on a later run. This check
    CANNOT detect a file whose headers are fully flushed but whose body is still mid-write (there
    is no reliable universal "body is done" marker to parse for on a raw RFC822 byte stream) —
    that case is what the age gate exists to catch instead."""
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception:
        return False
    return bool(msg.get("Message-ID") or msg.get("Date") or msg.get("Subject") or msg.get("From"))


def _load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: {STATE_PATH} is corrupt/unreadable ({e}) — falling back to a full rescan "
              f"(harmless: already-exported messages just get re-copied over themselves)",
              file=sys.stderr)
        return {}
    return {mime_dir: set(basenames) for mime_dir, basenames in raw.get("exported_by_dir", {}).items()}


def _save_state(exported_by_dir):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    payload = {"exported_by_dir": {d: sorted(b) for d, b in exported_by_dir.items()}}
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
    os.replace(tmp_path, STATE_PATH)  # atomic on POSIX — a crash never leaves a truncated state file


def export(account_email, dest, dry_run, profile_root=None, settle_seconds=None):
    if profile_root is None:
        profile_root = PROFILE_ROOT  # re-read at call time, not bind time — keeps this patchable
    if settle_seconds is None:
        settle_seconds = SETTLE_SECONDS  # same reasoning — tests override this to 0
    mime_dirs = find_account_mime_dirs(account_email, profile_root=profile_root)
    if not mime_dirs:
        print(f"no MimeFiles dir found matching {account_email} — is Outlook signed in / synced?",
              file=sys.stderr)
        sys.exit(1)
    if len(mime_dirs) > 1:
        print(f"WARNING: {len(mime_dirs)} MimeFiles dirs matched {account_email} — de-duping "
              f"independently per dir, so a colliding basename ACROSS dirs is treated as two "
              f"distinct messages, not one duplicate: {mime_dirs}", file=sys.stderr)

    exported_by_dir = _load_state()
    os.makedirs(dest, exist_ok=True)
    new_count = 0
    since_checkpoint = 0
    for mime_dir in mime_dirs:
        seen = exported_by_dir.setdefault(mime_dir, set())
        for src in _iter_mime_files(mime_dir):
            base = os.path.basename(src)
            if base in seen:
                continue
            try:
                age = time.time() - os.path.getmtime(src)
            except OSError:
                continue  # vanished between listing and stat -- next run will just not see it
            if age < settle_seconds:
                # too fresh to trust as fully written (headers can be flushed before the body is
                # done) -- NOT marked seen, so it's picked up once it's settled on a later run
                continue
            fname = _safe_name(src)
            fpath = os.path.join(dest, fname)
            if dry_run:
                print(f"  would copy: {base} -> {fname}")
            else:
                with open(src, "rb") as fsrc:
                    raw = fsrc.read()
                if not _looks_like_complete_message(raw):
                    print(f"  SKIP (looks mid-write/truncated, will retry next run): {base}",
                          file=sys.stderr)
                    continue  # NOT marked seen — retried on the next invocation
                tmp_fpath = fpath + ".partial"
                with open(tmp_fpath, "wb") as fdst:
                    fdst.write(raw)
                os.replace(tmp_fpath, fpath)  # atomic — never leaves a half-written .eml in dest
            seen.add(base)
            new_count += 1
            since_checkpoint += 1
            if not dry_run and since_checkpoint >= CHECKPOINT_EVERY:
                _save_state(exported_by_dir)
                since_checkpoint = 0

    print(f"exported {new_count} new messages to {dest} (dry_run={dry_run})")
    if not dry_run and new_count:
        _save_state(exported_by_dir)
    return new_count


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-email", default=DEFAULT_ACCOUNT_EMAIL)
    ap.add_argument("--dest", default=os.path.join(os.path.dirname(__file__), "..", "_sources", "outlook"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.account_email:
        ap.error("pass --account-email or set ENGRAM_OUTLOOK_ACCOUNT")
    export(args.account_email, args.dest, args.dry_run)


if __name__ == "__main__":
    main()
