#!/usr/bin/env python3
"""Local review-queue UI server.

A stdlib-only HTTP server that puts a real interface in front of the two human review queues that
otherwise live as raw JSONL in a terminal:
  1. the `/clarify` queue (`_manifest/clarify-queue.jsonl`) — answers routed through the deterministic
     write-back engine in `clarify_writeback.py` (imported, NEVER reimplemented here);
  2. the sensitive iMessage/WhatsApp hold queues (`_sources/*-sensitive/review.jsonl`) — drafted notes
     held local-only pending the owner's release / keep / exclude decision.

    python3 scripts/review_server.py [--root <store>] [--port 8765]

DELIBERATE SECURITY DECISIONS (not oversights — stated so a future editor doesn't "helpfully" relax them):
  * Binds 127.0.0.1 ONLY. There is intentionally NO `--lan`/`--host` flag and no code path that can
    bind to any other interface. This tool surfaces sensitive personal content (relationship, health,
    legal) drafted from the owner's messages; there is no legitimate reason to ever expose it off-loopback.
  * NO authentication. Single-user store + loopback-only threat model, matching engram's own stance
    ("engram is single-user, so there is no access-control notion of confidential"). A password on a
    localhost single-user tool buys nothing.
  * NO TLS (pointless for loopback).
  * Host-header allow-list (localhost / 127.0.0.1 only) as cheap DNS-rebinding defense-in-depth: a
    malicious web page that resolves its own hostname to 127.0.0.1 still can't drive this API because
    the browser sends the attacker's Host header, which we reject.

CONCURRENCY: ThreadingHTTPServer runs each request in its own thread. A single process-wide
`threading.RLock()` serializes every read->validate(old-hash)->write->log sequence so two requests
can't lost-update the queue or the sensitive ledgers. The clarify write path delegates to
`clarify_writeback.apply()`, which already has its own transaction/rollback discipline; the only
files this server writes directly (a sensitive review.jsonl line removal, an excluded-refs.jsonl
append, a held-note move) go through atomic temp-file+os.replace and an explicit rollback stack.

ACCEPTED RISK (stated, not silently swallowed): there is no cross-PROCESS lock against a concurrent
interactive `/clarify` session hand-editing people.md at the same moment. Operating convention:
don't run the UI and a conversational /clarify at once. The per-item content hash turns any
between-load-and-submit change into a safe 409, not a clobber.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)

# The write-back engine is imported and reused wholesale — this server is a thin HTTP wrapper over
# clarify_writeback.apply(); it duplicates none of its recipe/transaction logic.
import clarify_writeback as cw                                          # noqa: E402
from clarify_writeback import Store, load_queue, find_item, apply as apply_answer, WritebackError  # noqa: E402
from _lib.notes import split_frontmatter, get_field                    # noqa: E402  (single owner: fm parse)
from _lib.provenance import parse_ref                                  # noqa: E402  (single owner: ref parse)

UI_DIR = os.path.join(_SCRIPTS, "review_ui")
LOG_REL = "_manifest/clarify-log.jsonl"
CANONICAL_ROOT = os.path.dirname(_SCRIPTS)      # the real repo this script lives in

# One coarse process-wide re-entrant lock (see module docstring). Held only around in-memory
# read+validate+atomic-replace sequences, never around a subprocess launch.
_LOCK = threading.RLock()

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class _BadRequest(Exception):
    """Malformed request body (bad Content-Length / invalid JSON) -> 400, not a 500."""


# ---------------------------------------------------------------------------- helpers
def _item_hash(item):
    """Stable content hash of a queue item for optimistic locking. Computed from the canonical
    sort-keys JSON of the item AS LOADED FROM DISK (never including a previously-attached `_hash`),
    so the GET that shows it and the POST that validates it agree byte-for-byte."""
    core = {k: v for k, v in item.items() if k != "_hash"}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _atomic_write_text(path, text):
    """Write text atomically: unique temp in the SAME dir (mkstemp) then os.replace. Preserves the
    destination's mode on overwrite; cleans the temp up on any error."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".swap-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = 0o644
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_jsonl(path):
    """Parse a .jsonl file into a list of (line_text, obj). Blank lines dropped; a malformed line is
    kept as (line_text, None) so a rewrite can preserve it byte-for-byte."""
    out = []
    if not os.path.exists(path):
        return out
    for line in _read_text(path).splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            out.append((line, json.loads(s)))
        except json.JSONDecodeError:
            out.append((line, None))
    return out


# ---- sensitive-queue path handling ------------------------------------------------------
def _safe_sensitive_fp(root, fp):
    """Resolve a review.jsonl `fp` to an absolute path IFF it is a legitimate held-note location:
    `<root>/_sources/<x>-sensitive/confirmed/<name>.md`, no traversal outside `_sources/`.
    Returns the abspath or None (caller answers 400)."""
    if not isinstance(fp, str) or not fp or not fp.endswith(".md"):
        return None
    root_abs = os.path.abspath(root)
    sources_abs = os.path.join(root_abs, "_sources")
    abs_fp = os.path.normpath(os.path.join(root_abs, fp))
    try:
        if os.path.commonpath([abs_fp, sources_abs]) != sources_abs:
            return None
    except ValueError:
        return None
    conf = os.path.dirname(abs_fp)
    if os.path.basename(conf) != "confirmed":
        return None
    sens_root = os.path.dirname(conf)
    if not os.path.basename(sens_root).endswith("-sensitive"):
        return None
    return abs_fp


def _sensitive_paths(source):
    """Map a `source` (imsg|whatsapp) to its sensitive-area subpaths (all relative to root)."""
    dirname = {"imsg": "imsg-sensitive", "imessage": "imsg-sensitive",
               "whatsapp": "whatsapp-sensitive"}.get((source or "").lower())
    if not dirname:
        return None
    base = os.path.join("_sources", dirname)
    return {"root": base, "review": os.path.join(base, "review.jsonl"),
            "excluded_refs": os.path.join(base, "excluded-refs.jsonl"),
            "confirmed": os.path.join(base, "confirmed"), "excluded": os.path.join(base, "excluded")}


# ---------------------------------------------------------------------------- HTTP server
class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True                 # don't let a hung request thread block shutdown
    allow_reuse_address = True

    def __init__(self, addr, root):
        self.root = os.path.abspath(root)
        super().__init__(addr, Handler)


def make_server(root, port=0):
    """Construct the server bound to LOOPBACK ONLY. The bind address is the hardcoded literal
    "127.0.0.1"; there is no parameter, flag, or branch that can substitute another interface."""
    return ReviewServer(("127.0.0.1", port), root)   # 127.0.0.1 ONLY — see module docstring


class Handler(BaseHTTPRequestHandler):
    server_version = "engram-review/1.0"

    @property
    def root(self):
        return self.server.root

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---- low-level io -------------------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            return {}
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            raise _BadRequest("bad Content-Length")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _BadRequest("invalid JSON")
        return obj if isinstance(obj, dict) else {}

    def _host_ok(self):
        """DNS-rebinding defense: accept only Host headers whose hostname is localhost/127.0.0.1
        (any port). An absent Host (rare, pre-HTTP/1.1) is allowed; anything else is rejected."""
        host = self.headers.get("Host")
        if not host:
            return True
        hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        hostname = hostname.strip("[]")
        return hostname in ("localhost", "127.0.0.1")

    # ---- routing ------------------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._send_json({"error": "forbidden host"}, 403)
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path in ("/", ""):
                return self._serve_static("index.html")
            if path.startswith("/review_ui/"):
                return self._serve_static(path[len("/review_ui/"):])
            if path == "/api/queue":
                return self._api_queue(query)
            if path == "/api/log":
                return self._api_log(query)
            if path == "/api/sensitive-queue":
                return self._api_sensitive_queue(query)
        except Exception as e:                       # never crash the server on one bad request
            return self._send_json({"error": str(e)}, 500)
        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._send_json({"error": "forbidden host"}, 403)
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            try:
                body = self._read_body()
            except _BadRequest as e:
                return self._send_json({"error": str(e)}, 400)
            if path == "/api/answer":
                dry = query.get("dry_run", ["0"])[0] in ("1", "true") or bool(body.get("dry_run"))
                return self._api_answer(body, dry_run=dry)
            if path == "/api/dry-run":
                return self._api_answer(body, dry_run=True)
            if path == "/api/refresh":
                return self._api_refresh(query)
            if path == "/api/sensitive-decision":
                return self._api_sensitive_decision(body)
        except Exception as e:
            return self._send_json({"error": str(e)}, 500)
        return self._send_json({"error": "not found"}, 404)

    # ---- static -------------------------------------------------------------------------
    def _serve_static(self, rel):
        """Serve a file from scripts/review_ui/ only, with a traversal guard (commonpath, not
        startswith) and a dot-component reject."""
        full = os.path.normpath(os.path.join(UI_DIR, rel))
        try:
            within = os.path.commonpath([full, UI_DIR]) == UI_DIR
        except ValueError:
            within = False
        if not within or any(p.startswith(".") for p in os.path.relpath(full, UI_DIR).split(os.sep) if p not in (".", "")):
            return self._send_json({"error": "not found"}, 404)
        if not os.path.isfile(full):
            return self._send_json({"error": "not found: %s" % rel}, 404)
        ctype = _CONTENT_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- clarify queue ------------------------------------------------------------------
    def _api_queue(self, query):
        status = query.get("status", ["open"])[0]
        store = Store(self.root)
        with _LOCK:
            items = load_queue(store)
        out = []
        for it in items:
            if status and status != "all" and it.get("status") != status:
                continue
            enriched = dict(it)
            enriched["_hash"] = _item_hash(it)
            out.append(enriched)
        return self._send_json({"items": out, "count": len(out), "status": status})

    def _api_answer(self, body, dry_run=False):
        item_id = body.get("id")
        answer = body.get("answer")
        old = body.get("old")
        if not item_id or answer is None:
            return self._send_json({"error": "id and answer are required"}, 400)
        store = Store(self.root)
        with _LOCK:
            # Re-fetch the CURRENT item under the lock so the hash we validate reflects disk NOW.
            try:
                item = find_item(store, item_id)
            except WritebackError:
                return self._send_json({"error": "item %r no longer in the queue" % item_id,
                                        "current": None}, 409)
            cur_hash = _item_hash(item)

            if dry_run:
                try:
                    res = apply_answer(self.root, item, answer, dry_run=True)
                except WritebackError as e:
                    return self._send_json({"error": str(e)}, 422)
                res["_hash"] = cur_hash          # carry the hash into the UI's confirm step
                return self._send_json(res)

            # Real apply: optimistic-lock on the caller's last-seen hash.
            if old != cur_hash:
                stale = dict(item)
                stale["_hash"] = cur_hash
                return self._send_json({"error": "stale — the item changed since you loaded it",
                                        "current": stale}, 409)
            try:
                res = apply_answer(self.root, item, answer)     # verify=True: lint + rollback inside
            except WritebackError as e:
                return self._send_json({"error": str(e)}, 422)
            return self._send_json(res)

    def _api_refresh(self, query):
        """Re-run build_clarify_queue.py, then return the refreshed queue.

        SAFETY: build_clarify_queue.py hardcodes its ROOT from __file__ and os.chdir()s to it, so it
        ALWAYS rebuilds the canonical repo's queue regardless of cwd. We therefore only invoke it when
        this server is actually serving that canonical repo; against any other --root (e.g. a hermetic
        test store) we SKIP the rebuild rather than silently mutate the real store, and just return the
        current queue."""
        status = query.get("status", ["open"])[0]
        with _LOCK:
            rebuilt, summary = False, "skipped: server root is not the canonical repo"
            if os.path.abspath(self.root) == os.path.abspath(CANONICAL_ROOT):
                proc = subprocess.run([sys.executable, os.path.join(_SCRIPTS, "build_clarify_queue.py"), "--summary"],
                                      cwd=self.root, capture_output=True, text=True)
                rebuilt = proc.returncode == 0
                summary = (proc.stdout + proc.stderr).strip()
            store = Store(self.root)
            items = load_queue(store)
        out = []
        for it in items:
            if status and status != "all" and it.get("status") != status:
                continue
            enriched = dict(it)
            enriched["_hash"] = _item_hash(it)
            out.append(enriched)
        return self._send_json({"rebuilt": rebuilt, "summary": summary,
                                "items": out, "count": len(out), "status": status})

    def _api_log(self, query):
        try:
            n = int(query.get("n", ["50"])[0])
        except ValueError:
            n = 50
        rows = [obj for _, obj in _read_jsonl(os.path.join(self.root, LOG_REL)) if obj is not None]
        return self._send_json({"entries": rows[-n:], "total": len(rows)})

    # ---- sensitive queue ----------------------------------------------------------------
    def _api_sensitive_queue(self, query):
        source = query.get("source", ["imsg"])[0]
        paths = _sensitive_paths(source)
        if not paths:
            return self._send_json({"error": "source must be 'imsg' or 'whatsapp'"}, 400)
        review_abs = os.path.join(self.root, paths["review"])
        items = []
        for _, obj in _read_jsonl(review_abs):
            if not isinstance(obj, dict) or "fp" not in obj:
                continue
            fp = obj["fp"]
            abs_fp = _safe_sensitive_fp(self.root, fp)
            content, present = None, False
            if abs_fp and os.path.isfile(abs_fp):
                content, present = _read_text(abs_fp), True
            items.append({"fp": fp, "title": obj.get("title", ""), "date": obj.get("date", ""),
                          "present": present, "content": content})
        return self._send_json({"source": source, "items": items, "count": len(items)})

    def _api_sensitive_decision(self, body):
        """release / keep / exclude a held sensitive note. Higher-stakes than the clarify path (it
        moves content between never-committed and committed-to-git states), so every mutating branch
        is atomic + rollback-safe: on any failure the held file is back in confirmed/ and both ledgers
        are byte-for-byte as they started."""
        fp = body.get("fp")
        decision = body.get("decision")
        if decision not in ("release", "exclude", "keep"):
            return self._send_json({"error": "decision must be release / exclude / keep"}, 400)
        abs_fp = _safe_sensitive_fp(self.root, fp)
        if not abs_fp:
            return self._send_json({"error": "fp is not a valid held-note path under _sources/*-sensitive/confirmed/"}, 400)

        conf = os.path.dirname(abs_fp)
        sens_root = os.path.dirname(conf)
        review_abs = os.path.join(sens_root, "review.jsonl")
        excluded_refs_abs = os.path.join(sens_root, "excluded-refs.jsonl")
        excluded_dir = os.path.join(sens_root, "excluded")
        basename = os.path.basename(abs_fp)

        with _LOCK:
            if decision == "keep":
                # Explicit no-op — leave the entry in review.jsonl for a later decision.
                return self._send_json({"ok": True, "decision": "keep", "fp": fp,
                                        "message": "left in the hold queue; no files changed"})

            if not os.path.isfile(abs_fp):
                return self._send_json({"error": "held note file not present locally: %s" % fp,
                                        "hint": "held notes are local-only; re-run the iMessage ingest to regenerate"}, 404)

            if decision == "release":
                return self._decide_release(abs_fp, fp, basename, review_abs)
            return self._decide_exclude(abs_fp, fp, basename, body.get("reason"),
                                        review_abs, excluded_refs_abs, excluded_dir)

    # -- release: confirmed/ -> real store at <domain>/<basename>, lint-gated, rollback on fail ----
    def _decide_release(self, abs_fp, fp, basename, review_abs):
        txt = _read_text(abs_fp)
        head, _ = split_frontmatter(txt)
        if head is None:
            return self._send_json({"error": "held note has no valid frontmatter; cannot place it"}, 422)
        domain = get_field(head, "domain")
        if not domain or domain not in cw.DOMAINS:
            return self._send_json({"error": "held note domain missing/invalid (%r); cannot pick a target dir" % domain}, 422)
        target_rel = os.path.join(domain, basename)
        target_abs = os.path.join(self.root, target_rel)
        if os.path.exists(target_abs):
            return self._send_json({"error": "target already exists: %s" % target_rel}, 409)

        # Plain filesystem move: the source is under gitignored _sources/, the target is a tracked
        # dir, so this crosses the git boundary — it is NOT a `git mv`. The note becomes a NEW,
        # UNTRACKED file the caller must `git add`. We report that explicitly.
        os.makedirs(os.path.dirname(target_abs), exist_ok=True)
        shutil.move(abs_fp, target_abs)
        try:
            ok, out = cw._run_lint(self.root)     # lint the WHOLE store with the note now in place
            if not ok:
                shutil.move(target_abs, abs_fp)   # rollback: put it back in confirmed/
                return self._send_json({"error": "lint_store rejected the released note (rolled back)",
                                        "detail": out}, 422)
            self._remove_review_entry(review_abs, fp)   # only after lint passes
        except Exception:
            if os.path.exists(target_abs) and not os.path.exists(abs_fp):
                shutil.move(target_abs, abs_fp)
            raise
        return self._send_json({"ok": True, "decision": "release", "fp": fp,
                                "moved_to": target_rel, "git_add_needed": target_rel,
                                "message": "released to %s — run `git add %s` to track it" % (target_rel, target_rel)})

    # -- exclude: append excluded-refs, relocate held file to excluded/ (never delete) -------------
    def _decide_exclude(self, abs_fp, fp, basename, reason, review_abs, excluded_refs_abs, excluded_dir):
        txt = _read_text(abs_fp)
        head, _ = split_frontmatter(txt)
        ref = parse_ref(head if head is not None else txt)
        if not ref:
            return self._send_json({"error": "cannot read a provenance ref from the held note; refusing to exclude"}, 422)
        title = self._review_title(review_abs, fp)
        entry = {"ref": ref, "reason": (reason or "excluded via review UI"),
                 "excluded": datetime.date.today().isoformat(), "title": title}
        dst = os.path.join(excluded_dir, basename)
        if os.path.exists(dst):
            return self._send_json({"error": "a file with this name already exists in excluded/: %s" % basename}, 409)

        # Capture originals for a byte-exact rollback, then mutate; restore all on any failure.
        review_orig = _read_text(review_abs) if os.path.exists(review_abs) else None
        refs_orig = _read_text(excluded_refs_abs) if os.path.exists(excluded_refs_abs) else None
        os.makedirs(excluded_dir, exist_ok=True)
        moved = False
        try:
            shutil.move(abs_fp, dst)                                   # 1. relocate the draft (not delete)
            moved = True
            prev = refs_orig or ""
            if prev and not prev.endswith("\n"):
                prev += "\n"
            _atomic_write_text(excluded_refs_abs, prev + json.dumps(entry, ensure_ascii=False) + "\n")  # 2. record exclusion
            self._remove_review_entry(review_abs, fp)                 # 3. drop from the hold queue
        except Exception:
            if moved and os.path.exists(dst) and not os.path.exists(abs_fp):
                shutil.move(dst, abs_fp)
            if refs_orig is None:
                if os.path.exists(excluded_refs_abs):
                    os.remove(excluded_refs_abs)
            else:
                _atomic_write_text(excluded_refs_abs, refs_orig)
            if review_orig is not None:
                _atomic_write_text(review_abs, review_orig)
            raise
        return self._send_json({"ok": True, "decision": "exclude", "fp": fp, "ref": ref,
                                "moved_to": os.path.relpath(dst, self.root),
                                "excluded_ref_appended": True,
                                "message": "excluded: ref recorded, draft relocated to excluded/ (not deleted)"})

    # -- shared ledger edits --------------------------------------------------------------
    def _remove_review_entry(self, review_abs, fp):
        """Rewrite review.jsonl WITHOUT the line whose parsed `fp` == fp, preserving every other
        line byte-for-byte (never leave a dangling stale queue entry)."""
        if not os.path.exists(review_abs):
            return
        kept = []
        for line, obj in _read_jsonl(review_abs):
            if isinstance(obj, dict) and obj.get("fp") == fp:
                continue
            kept.append(line)
        _atomic_write_text(review_abs, ("\n".join(kept) + "\n") if kept else "")

    def _review_title(self, review_abs, fp):
        for _, obj in _read_jsonl(review_abs):
            if isinstance(obj, dict) and obj.get("fp") == fp:
                return obj.get("title", "")
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="engram review-queue UI server (clarify + sensitive holds).")
    ap.add_argument("--root", default=CANONICAL_ROOT, help="store root (defaults to this repo)")
    ap.add_argument("--port", type=int, default=8765, help="localhost port (default 8765)")
    a = ap.parse_args(argv)
    httpd = make_server(a.root, a.port)
    host, port = httpd.server_address[0], httpd.server_address[1]
    print("\n  engram review UI at http://localhost:%d  (root: %s)" % (port, os.path.abspath(a.root)))
    print("  bound to %s only — local, unauthenticated, single-user. Ctrl-C to stop.\n" % host)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.\n")
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
