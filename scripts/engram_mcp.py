#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2"]
# ///
"""engram MCP server — read-only recall of the engram store from any MCP client (Claude Desktop).

Exposes the index-then-drill + grep recall recipes (see _global-skills/engram/SKILL.md) as MCP
tools so a plain Desktop chat — which has no filesystem — can answer "who is X", "what's the status
of Y", "what's open", "what changed since D" against the local store.

READ-ONLY + SANDBOXED. This is the ONE hard rule: the local working tree contains PLAINTEXT secrets
(`_vault/`, git-crypt-encrypted in git but decrypted on disk) and raw connector pulls (`_sources/`:
email / iMessage / PHI-holding). Every read path is gated so those never reach a chat:
  - only git-TRACKED files are served  → `_sources/` (gitignored) is excluded automatically;
  - `_vault/` (tracked) is excluded explicitly;
  - paths are realpath-resolved and contained to the repo root (no `..`/symlink escape).
There are NO write tools. Nothing here mutates the store.

The `mcp` SDK is imported lazily inside main() so this module (and its pure, security-critical
helpers) imports cleanly under the repo's Python 3.9 pytest suite without the SDK installed.

Run (Claude Desktop launches it this way via claude_desktop_config.json):
    uv run --python 3.12 --with mcp <repo-root>/scripts/engram_mcp.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repo root (parent of scripts/)
GIT = shutil.which("git") or "/usr/bin/git"            # absolute-safe: Desktop launches with a bare PATH
DENY_TOP = {"_vault"}                                   # tracked but sensitive (plaintext secrets on disk)
READ_CAP = 500_000                                      # char cap for a single file read (all real notes fit)
GREP_CAP_DEFAULT = 40


# ─────────────────────────── pure, security-critical helpers (unit-tested) ───────────────────────────

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([GIT, "-C", str(ROOT), *args], capture_output=True, text=True)


def load_tracked() -> set[str]:
    """The set of git-tracked repo-relative POSIX paths. Membership is the read allowlist:
    gitignored `_sources/` is absent (denied); `_vault/` is present but denied separately."""
    cp = _git("ls-files")
    if cp.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {cp.stderr.strip()}")
    return set(cp.stdout.splitlines())


def normalize_rel(path: str) -> str:
    """Normalize a caller-supplied path to a repo-relative POSIX string, tolerating the skill's
    `$E/` prefix, `./`, and absolute paths inside the root. Does NOT decide safety — that's
    safe_resolve's job (this only canonicalizes so the gate sees a clean value)."""
    s = path.strip().replace("$ENGRAM/", "").replace("$E/", "")
    s = s.lstrip()
    p = Path(s)
    base = Path(os.path.normpath(str(p if p.is_absolute() else (ROOT / s))))
    return base.as_posix()


def safe_resolve(path: str, tracked: set[str]) -> Path:
    """Resolve a caller path to an absolute file inside the store, or raise ValueError. The gate:
    (1) realpath-contained to ROOT (blocks `..`/symlink escape); (2) top-level dir not in DENY_TOP
    (blocks `_vault/`); (3) path is git-tracked (blocks `_sources/` and any untracked/nonexistent)."""
    cand = Path(normalize_rel(path)).resolve()
    if cand != ROOT and ROOT not in cand.parents:
        raise ValueError("path is outside the engram store")
    rel = cand.relative_to(ROOT).as_posix()
    if not rel or rel == ".":
        raise ValueError("refusing to read the store root")
    if rel.split("/", 1)[0] in DENY_TOP:
        raise ValueError("refused: _vault/ holds secrets and is never served")
    if rel not in tracked:
        raise ValueError(f"refused: '{rel}' is not a tracked store note "
                         "(gitignored/_sources/, untracked, or nonexistent)")
    if not cand.is_file():
        raise ValueError(f"not a file: {rel}")
    return cand


def filter_records(records, *, domain="", subpath="", tag="", participant="", since="", until="",
                   title_contains="", limit=40):
    """Filter an iterable of index.jsonl records by the given criteria; newest-first, capped."""
    out = []
    part_lc = participant.lower()
    title_lc = title_contains.lower()
    for r in records:
        if domain and r.get("domain") != domain:
            continue
        if subpath and (r.get("subpath") or "") != subpath:
            continue
        if tag and tag not in (r.get("tags") or []):
            continue
        if part_lc and not any(part_lc in (p or "").lower() for p in (r.get("participants") or [])):
            continue
        d = r.get("date") or ""
        if since and d < since:
            continue
        if until and d > until:
            continue
        if title_lc and title_lc not in (r.get("title") or "").lower():
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out[: max(1, limit)]


# ─────────────────────────────────── tool implementations ───────────────────────────────────

def engram_index() -> str:
    """Return the store's top-level domain map (INDEX.md) plus a short how-to. START HERE: pick the
    domain, then use engram_frontmatter/engram_grep to find notes and engram_read to open them.
    engram is a private, single-owner knowledge store — research, work, ventures, finance, personal
    life, the people involved and the decisions behind them, each note carrying its source provenance."""
    idx = (ROOT / "INDEX.md").read_text(encoding="utf-8", errors="replace")
    how = (
        "\n\n---\nRECALL RECIPES (this MCP server, read-only):\n"
        "• engram_people(name)           → who is X (canonical role/aliases from the registry)\n"
        "• engram_grep(query)            → full-text search across all notes (the engine)\n"
        "• engram_frontmatter(domain=…, subpath=…, tag=…, since=…) → structured scan (newest-first)\n"
        "• engram_read(path)             → open a note/dossier/_index by repo-relative path\n"
        "• engram_brief()                → deadlines & maintenance (open loops due soon, drift)\n"
        "Cross-cutting one-pagers live in dossiers/ (e.g. dossiers/example-project.md); curated\n"
        "commitments in digests/open-loops.md. Facts carry provenance; cite the note path.\n"
    )
    return idx + how


def engram_people(query: str) -> str:
    """Look up a person/entity in the canonical registry (_manifest/people.md): name, email, role,
    domain, aliases. The fastest answer to 'who is X'. Case-insensitive substring match."""
    q = query.strip()
    if not q:
        return "provide a name to look up."
    cp = _git("grep", "-n", "-i", "-F", "-e", q, "--", "_manifest/people.md")
    if cp.returncode == 1:
        return f"no registry row matches '{q}'. Try engram_grep for a broader search."
    if cp.returncode not in (0, 1):
        return f"lookup error: {cp.stderr.strip()}"
    lines = cp.stdout.splitlines()
    return "\n".join(lines[:60]) or f"no match for '{q}'."


def engram_grep(query: str, include_archive: bool = False, max_results: int = GREP_CAP_DEFAULT) -> str:
    """Full-text search the store (git grep, case-insensitive, literal string). Returns
    file:line: snippet rows. This is the retrieval engine — use it to find which note(s) hold a
    fact, then engram_read the best hit. _vault/ is never searched; _archive/ (superseded notes) is
    excluded unless include_archive=True."""
    q = query.strip()
    if not q:
        return "provide a search string."
    pathspec = ["--", ":(exclude)_vault"]
    if not include_archive:
        pathspec.append(":(exclude)_archive")
    cp = _git("grep", "-n", "-i", "-F", "-e", q, *pathspec)
    if cp.returncode == 1:
        return f"no matches for '{q}'."
    if cp.returncode not in (0, 1):
        return f"search error: {cp.stderr.strip()}"
    lines = cp.stdout.splitlines()
    cap = max(1, min(max_results, 200))
    body = "\n".join(lines[:cap])
    if len(lines) > cap:
        body += f"\n… {len(lines) - cap} more matches (narrow the query or raise max_results)."
    return body


def engram_read(path: str) -> str:
    """Open a single store file (note, dossier, _index, glossary, people.md, a _manifest view) by
    repo-relative path, e.g. 'dossiers/example-project.md'. READ-ONLY and sandboxed: refuses
    _vault/, _sources/, and anything outside the tracked store. Returns the raw file incl.
    frontmatter/provenance."""
    try:
        p = safe_resolve(path, load_tracked())
    except ValueError as e:
        return f"refused: {e}"
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > READ_CAP:
        text = text[:READ_CAP] + f"\n\n… [truncated at {READ_CAP} chars; use engram_frontmatter for index.jsonl]"
    return text


def engram_frontmatter(domain: str = "", subpath: str = "", tag: str = "", participant: str = "",
                       since: str = "", until: str = "", title_contains: str = "", limit: int = 40) -> str:
    """Structured scan over every note's frontmatter (_manifest/index.jsonl) — filter by domain,
    SUBPATH (the primary within-domain narrowing axis — the sub-clusters a large domain is split
    into by scripts/assign_subpaths.py: e.g. domain='research' has subpath in
    {papers, grants, lab-ops}), tag, participant, date range (since/until, YYYY-MM-DD), or title
    substring. Returns newest-first 'date [domain/subpath] title · path' rows. Use for 'what changed
    since D', 'all <domain> notes about <subpath>', 'notes with person P'. Then engram_read a path."""
    jl = ROOT / "_manifest" / "index.jsonl"
    records = []
    with jl.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    hits = filter_records(records, domain=domain, subpath=subpath, tag=tag, participant=participant,
                          since=since, until=until, title_contains=title_contains, limit=limit)
    if not hits:
        return "no notes match those filters."

    def _dl(r):
        d, sp = r.get("domain", "?"), r.get("subpath")
        return f"{d}/{sp}" if sp else d

    rows = [f"{r.get('date','?')} [{_dl(r)}] {r.get('title','')}  ·  {r.get('path','')}" for r in hits]
    return "\n".join(rows)


def engram_brief() -> str:
    """Run the deterministic deadline & maintenance brief (scripts/engram_brief.py): open loops due
    soon / overdue with days-remaining and owner, drifted dossiers, sync-watermark staleness, and a
    hygiene tail. Read-only, no writes. The 'what needs attention' view."""
    script = ROOT / "scripts" / "engram_brief.py"
    if not script.is_file():
        return "engram_brief.py not found."
    import sys
    cp = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, cwd=str(ROOT))
    if cp.returncode != 0:
        return f"brief error: {cp.stderr.strip() or cp.stdout.strip()}"
    return cp.stdout.rstrip()


def main() -> None:
    from mcp.server.fastmcp import FastMCP  # lazy: keeps this module importable without the SDK

    app = FastMCP("engram")
    for fn in (engram_index, engram_people, engram_grep, engram_read, engram_frontmatter, engram_brief):
        app.tool()(fn)
    app.run()


if __name__ == "__main__":
    main()
