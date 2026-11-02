#!/usr/bin/env python3
"""Pull the Outlook delta straight from Microsoft Graph — replaces the manual "Save as .eml" step.

Writes each message as raw RFC822 `.eml` into `_sources/outlook/` in exactly the shape
`outlook_extract.py` already expects (filenames are opaque to it; only content matters). Nothing
downstream of this script changes — it's a drop-in replacement for the manual export, not a new
pipeline stage.

Auth: `outlook_graph_auth.get_access_token()` (device-code flow, cached, auto-refreshing). Run
`python3 scripts/outlook_graph_auth.py login` once interactively first.

Delta tracking: a local, gitignored state file (`_sources/outlook-graph-export-state.json`) records
the max `receivedDateTime` this script has successfully written. `--since` overrides it (falls back
to `_manifest/sync-state.json`'s `outlook.watermark_date` on a first run, minus a 2-day overlap
margin — cheap and safe, since the pipeline dedups by stable thrid downstream regardless).

Same PHI posture as the manual export: this step only extracts to gitignored `_sources/`; nothing
here is committed or distilled. The existing outlook_select/phi_scan/worklist/distiller gates are
unchanged and still run after this. Data path is the employer's own Graph API tenant directly to this
machine — the same trust boundary the Outlook desktop client itself uses, not a new surface.

Usage:
    python3 scripts/outlook_graph_export.py [--since 2026-07-06] [--dest _sources/outlook]
                                             [--folders inbox,sentitems] [--dry-run]
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from outlook_graph_auth import get_access_token  # noqa: E402

GRAPH = "https://graph.microsoft.com/v1.0"
STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "_sources", "outlook-graph-export-state.json")
SYNC_STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "_manifest", "sync-state.json")
PAGE_SIZE = 50
MAX_RETRIES = 5
HTTP_TIMEOUT_S = 30  # $value bodies can be large; list pages are small


def _graph_get(url, token, accept="application/json"):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept)
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return resp.read(), resp.status
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES - 1:
                wait = int(e.headers.get("Retry-After", "5"))
                print(f"  429 throttled, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Graph GET {url} -> {e.code}: {body[:500]}") from e
    # unreachable: every loop iteration above either returns or raises, including on the final
    # attempt (the 429-continue guard is `attempt < MAX_RETRIES - 1`, false on the last one)


def _load_since(cli_since):
    if cli_since:
        return cli_since
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            st = json.load(f)
            if st.get("last_exported"):
                return st["last_exported"]
    if os.path.exists(SYNC_STATE_PATH):
        with open(SYNC_STATE_PATH) as f:
            wm = json.load(f).get("outlook", {}).get("watermark_date")
            if wm:
                # 2-day overlap margin: cheap re-fetch of a couple already-processed days, safe
                # because the pipeline dedups by stable thrid downstream regardless.
                d = datetime.date.fromisoformat(wm) - datetime.timedelta(days=2)
                return f"{d.isoformat()}T00:00:00Z"
    raise SystemExit("no --since given, no export state, and no outlook watermark_date in "
                      "sync-state.json — pass --since explicitly for a first run")


def _save_since(max_received):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump({"last_exported": max_received}, f)


def _safe_name(s):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def export(since, dest, folders, dry_run):
    token = get_access_token()
    os.makedirs(dest, exist_ok=True)

    max_received = since
    total = 0
    for folder in folders:
        query = urllib.parse.urlencode({
            "$filter": f"receivedDateTime ge {since}",
            "$select": "id,receivedDateTime,subject",
            "$orderby": "receivedDateTime asc",
            "$top": str(PAGE_SIZE),
        })
        url = f"{GRAPH}/me/mailFolders/{folder}/messages?{query}"
        print(f"[{folder}] listing messages since {since} ...")
        page = 0
        while url:
            body, status = _graph_get(url, token)
            data = json.loads(body)
            msgs = data.get("value", [])
            page += 1
            print(f"[{folder}] page {page}: {len(msgs)} messages")
            for m in msgs:
                mid = m["id"]
                received = m.get("receivedDateTime", "")
                if received > max_received:
                    max_received = received
                subj = (m.get("subject") or "no-subject")[:60]
                fname = f"{received[:10]}_{_safe_name(mid)[-40:]}.eml"
                fpath = os.path.join(dest, fname)
                if os.path.exists(fpath):
                    continue
                if dry_run:
                    print(f"  would fetch: {fname}  ({subj})")
                    total += 1
                    continue
                raw, _ = _graph_get(f"{GRAPH}/me/messages/{mid}/$value", token,
                                     accept="text/plain")
                with open(fpath, "wb") as f:
                    f.write(raw)
                total += 1
            url = data.get("@odata.nextLink")

    print(f"exported {total} messages to {dest} (dry_run={dry_run})")
    # Gate on whether the watermark actually advanced, not on `total` — on a resume where every
    # listed message already exists on disk (the exists-skip above), total stays 0 even though
    # max_received legitimately moved past `since`; gating on total would leave the watermark
    # stuck, re-listing the same growing date range forever.
    if not dry_run and max_received > since:
        _save_since(max_received)
        print(f"export state advanced to {max_received}")
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="ISO8601 datetime; overrides saved export state / watermark")
    ap.add_argument("--dest", default=os.path.join(os.path.dirname(__file__), "..", "_sources", "outlook"))
    ap.add_argument("--folders", default="inbox,sentitems",
                     help="comma-separated Graph well-known folder names")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    since = _load_since(args.since)
    folders = [f.strip() for f in args.folders.split(",") if f.strip()]
    export(since, args.dest, folders, args.dry_run)


if __name__ == "__main__":
    main()
