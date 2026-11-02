#!/usr/bin/env python3
"""Regenerate the COMMITTED derived views: per-domain MOC maps (each <domain>/_index.md) and the
STATUS.md tallies block. Idempotent. Run from the repo root after an ingest.

The action-items / decisions digests are NO LONGER regenerated here — they moved to on-demand
generation in scripts/gen_digests.py (plan generic-bubbling-eagle.md, Feature D): their git history
isn't used as an audit trail, so they are not committed. regen.py now owns only the MOC tree + the
STATUS.md tallies.

    python3 scripts/regen.py
"""
import re, glob, os, collections, sys, shutil, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.constants import DOMAINS as TOPS
from _lib.series import norm
# Re-exported so `regen.section is _lib.notes.section` for downstream importers. The digest
# generator that used to consume it here moved to scripts/gen_digests.py; kept as the
# single-owner re-export.
from _lib.notes import section  # noqa: F401

# Single owner of the Tier-2 subpath annotations shown in the STATUS.md tally table (the counts
# themselves are generated). A domain absent here shows "—".
SUBPATH_LABELS = {
    "research": "papers · grants · lab-ops",
}
TALLY_START = "<!-- generated:tallies (regen.py owns this block — do NOT hand-edit) -->"
TALLY_END = "<!-- /generated:tallies -->"


def mdlink(text, path):
    """Render a valid markdown link. Escapes `[`/`]` in the display text (an unescaped
    `]` closes the link text early — e.g. titles like "[DRAFT] ...") and angle-bracket-wraps
    a destination containing `(`/`)`/spaces (an unescaped `)` closes the destination early —
    e.g. "Foo (2018).md"). Without this, such notes render broken and silently drop out of
    the MOC reachability graph that scripts/build_index.py measures."""
    disp = text.replace("[", "\\[").replace("]", "\\]")
    dest = f"<{path}>" if re.search(r'[()<>\s]', path) else path
    return f"[{disp}]({dest})"

def notes_in(dom):
    # sorted() so traversal order is deterministic across filesystems (glob order is FS-dependent;
    # without this, same-date digest rows ordered differently on Linux vs macOS — a stale-view churn)
    return sorted(f for f in glob.glob(f"{dom}/**/*.md", recursive=True) if not f.endswith("_index.md"))

def fm(fp):
    txt = open(fp, encoding="utf-8").read()
    if not txt.startswith("---") or "\n---" not in txt[3:]:
        raise SystemExit(f"regen: {fp} has no valid frontmatter (missing closing '---'); "
                         f"run scripts/lint_store.py first for an actionable list")
    end = txt.index("\n---", 3); head = txt[:end]; body = txt[end+4:]
    def g(k):
        m = re.search(rf'^{k}:\s*"?(.*?)"?\s*$', head, re.M); return (m.group(1).strip() if m else "")
    sub = g("subpath"); sub = "" if sub in ("", "null", "~") else sub
    return dict(title=g("title"), date=g("date"), subpath=sub, body=body)

# Keep every generated _index.md a single agent read. The real limit is TOKENS, not lines: these
# dense date+title+path link lines tokenize at ~1.7 bytes/token, so the ~25k-token single-read cap is
# ~45KB. A note-list leaf renders VERBOSE by default (one `**series**` header per group — the
# historical layout, so small MOCs stay byte-for-byte unchanged) and falls back to a COMPACT
# one-line-per-singleton layout if verbose exceeds the cap. If even the compact render is over cap the
# note set is SPLIT into child sub-MOCs by period (year -> quarter -> month -> day), recursing until
# every leaf fits — so there is no "grep-only" area any more; every note is in a single-read leaf.
# Level-1 stays the semantic `subpath`; levels 2+ are purely temporal, computed at regen time from the
# note `date`. Compaction/splitting preserve every note's markdown link, so build_index reachability
# is intact (it now scans **/_index.md at any depth as a hub).
BYTE_CAP = 40000   # ~23.5k tokens at ~1.7 B/tok — margin under the ~25k single-read cap
PERIODS = ["year", "quarter", "month", "day"]   # successive temporal split granularities
# Auto-generated period dirs (never contain note files — notes are NOT moved; they stay in {dom}/).
# Matched for the pre-regen stale-dir cleanup that keeps the tree idempotent.
GEN_DIR = re.compile(r'^(\d{4}|Q[1-4]|\d{2}|root|undated)$')

_OVERSIZE = []   # leaves still over cap even at day granularity — surfaced in the run summary

def _preserved_head(idx):
    # The MOC head to keep verbatim: frontmatter + intro prose through the **Status:** marker.
    head = open(idx, encoding="utf-8").read()
    mst = re.search(r'\*\*Status:\*\*[^\n]*\n', head)
    if mst:
        return head[:mst.end()]
    # no Status marker: cut at a previously-generated Map block so re-runs don't stack it
    cut = re.search(r'\n##\s+Map\b', head)
    return head[:cut.start() + 1] if cut else head.rstrip() + "\n"

def _gen_head(dom, sub, label):
    # Minimal derived frontmatter + a **Status:** marker for an auto-generated period _index.md, so the
    # head-preservation + Status-marker logic works and the file is a valid (unlinted) reachability hub.
    return (f'---\ntitle: "{label} — period index"\ndomain: {dom}\n'
            f'subpath: {sub or "null"}\nstatus: merged\n---\n\n# {label}\n\n'
            f'_Auto-generated period split (by note date)._\n\n**Status:** merged (auto-split by date).\n')

def _pkey(date, level):
    # Temporal bucket key for a note `date` (YYYY-MM-DD) at split `level`. Non-ISO/empty -> 'undated'.
    if not date or len(date) < 10 or not date[:4].isdigit():
        return "undated"
    y, mo, d = date[:4], date[5:7], date[8:10]
    return [y, f"Q{(int(mo) - 1) // 3 + 1}", mo, d][level]

def _map_out(files, base, header, compact):
    # Build the note-list line list: `header` is the already-newline-wrapped section header. Verbose
    # mode (compact=False) reproduces the historical layout byte-for-byte. Compact mode drops the
    # redundant per-singleton `**series**` header (multi-note series stay grouped), keeping the note
    # link so build_index.py reachability is unchanged.
    groups = collections.defaultdict(list)
    for fp in files:
        m = fm(fp); groups[norm(m["title"])].append((m["date"], m["title"], fp, m["subpath"]))
    if compact:
        multi = sum(1 for v in groups.values() if len(v) > 1)
        summ = (f"_{len(files)} notes in {len(groups)} series ({multi} multi-note grouped; "
                f"{len(groups) - multi} singletons listed individually). "
                f"Auto-generated; drill into a note for detail._\n")
    else:
        summ = f"_{len(files)} notes in {len(groups)} series. Auto-generated; drill into a note for detail._\n"
    line = lambda d, t, fp, sub: f"- {d}{(' ·'+sub) if sub else ''} · {mdlink(t, os.path.relpath(fp, base))}"
    out = [header, summ]
    for k, v in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0].lower())):
        v.sort(reverse=True)
        if len(v) > 1 or not compact:
            out.append(f"\n**{k}**" + (f" ({len(v)})" if len(v) > 1 else ""))
            out += [line(*rec) for rec in v]
        else:
            out.append(line(*v[0]))
    return out

def _leaf_text(head, files, base):
    # A full note-list leaf: verbose, or compact if verbose would exceed BYTE_CAP. Deterministic.
    hdr = "\n## Map — by series / topic\n"
    text = head + "\n".join(_map_out(files, base, hdr, False)) + "\n"
    if len(text.encode("utf-8")) > BYTE_CAP:
        text = head + "\n".join(_map_out(files, base, hdr, True)) + "\n"
    return text

def _emit(idx_dir, head, files, dom, sub, level, label):
    # Write idx_dir/_index.md. LEAF (full note list) if it fits BYTE_CAP or temporal levels are
    # exhausted; else a PARENT 'By period' map + recurse into child period dirs. Idempotent: prior
    # period dirs are cleaned before regen, then recreated deterministically.
    os.makedirs(idx_dir, exist_ok=True)
    idx = os.path.join(idx_dir, "_index.md")
    leaf = _leaf_text(head, files, idx_dir)
    if len(leaf.encode("utf-8")) <= BYTE_CAP or level >= len(PERIODS):
        open(idx, "w", encoding="utf-8").write(leaf)
        if len(leaf.encode("utf-8")) > BYTE_CAP:
            _OVERSIZE.append((idx, len(leaf.encode("utf-8"))))
        return
    buckets = collections.defaultdict(list)
    for fp in files:
        buckets[_pkey(fm(fp)["date"], level)].append(fp)
    if len(buckets) <= 1:   # degenerate partition (all one period): advance granularity in place
        _emit(idx_dir, head, files, dom, sub, level + 1, label)
        return
    # parent map at idx_dir; recurse each bucket into idx_dir/<key>/
    parent_lines = [head.rstrip("\n"), "", "## Map", "", f"### By period ({len(files)})", ""]
    for key in sorted(buckets, reverse=True):
        parent_lines.append(f"**{key}** ({len(buckets[key])}) — {mdlink(key + '/_index.md', key + '/_index.md')}")
    open(idx, "w", encoding="utf-8").write("\n".join(parent_lines) + "\n")
    for key in sorted(buckets, reverse=True):
        _emit(os.path.join(idx_dir, key), _gen_head(dom, sub, f"{label}/{key}"),
              buckets[key], dom, sub, level + 1, f"{label}/{key}")

def _dir_has_note(d):
    for _, _, fs in os.walk(d):
        if any(f.endswith(".md") and f != "_index.md" for f in fs):
            return True
    return False

def _cleanup_generated(dom):
    # Delete auto-generated period-split dirs (year/quarter/month/day/root/undated) left by prior runs
    # so a bucket that shrinks under cap doesn't leave an orphaned _index.md. GUARD (belt+suspenders):
    # only a GEN_DIR-named dir that recursively contains NO note .md (only _index.md) is removed —
    # never a dir holding notes, never a semantic subpath dir (name won't match GEN_DIR).
    for dirpath, dirnames, _ in os.walk(dom):
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if GEN_DIR.match(d) and not _dir_has_note(full):
                shutil.rmtree(full)
                dirnames.remove(d)   # prune: don't descend into the removed subtree

def write_top_moc(idx, dom, by_sub):
    # Summarizing top-level domain MOC: an "### Areas" section (one line per subpath -> its sub-MOC) +
    # the subpath-less ("Root / unfiled") notes. If the root set fits inline it stays a leaf list; if
    # over cap it becomes "### Root — by period" -> {dom}/root/<year>/... (recursed). Subpathed notes
    # live only in their sub-MOC. Domains without any sub-cluster are one splittable note set.
    subs = sorted(s for s in by_sub if s)
    root_files = by_sub.get("", [])
    if not subs:
        _emit(dom, _preserved_head(idx), root_files, dom, "", 0, dom)
        return
    head = _preserved_head(idx)
    areas = ["\n## Map\n", "\n### Areas\n"]
    for s in subs:
        areas.append(f"**{s}** ({len(by_sub[s])}) — {mdlink(f'{s}/_index.md', f'{s}/_index.md')}")
    areas_str = "\n".join(areas)
    root_hdr = f"\n### Root / unfiled ({len(root_files)})\n"
    inline = head + areas_str + "\n" + "\n".join(_map_out(root_files, dom, root_hdr, False)) + "\n"
    if len(inline.encode("utf-8")) > BYTE_CAP:
        inline = head + areas_str + "\n" + "\n".join(_map_out(root_files, dom, root_hdr, True)) + "\n"
    if len(inline.encode("utf-8")) <= BYTE_CAP:
        open(idx, "w", encoding="utf-8").write(inline)
        return
    # root over cap: keep Areas as-is, replace the root leaf with a period-parent -> {dom}/root/<year>/
    root_buckets = collections.defaultdict(list)
    for fp in root_files:
        root_buckets[_pkey(fm(fp)["date"], 0)].append(fp)
    lines = [head.rstrip("\n"), "", "## Map", "", "### Areas", ""]
    for s in subs:
        lines.append(f"**{s}** ({len(by_sub[s])}) — {mdlink(f'{s}/_index.md', f'{s}/_index.md')}")
    lines += ["", f"### Root — by period ({len(root_files)})", ""]
    for key in sorted(root_buckets, reverse=True):
        lines.append(f"**{key}** ({len(root_buckets[key])}) — {mdlink(f'root/{key}/_index.md', f'root/{key}/_index.md')}")
    open(idx, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    for key in sorted(root_buckets, reverse=True):
        _emit(os.path.join(dom, "root", key), _gen_head(dom, "", f"{dom}/root/{key}"),
              root_buckets[key], dom, "", 1, f"{dom}/root/{key}")   # root already split by year -> children start at quarter

def regen_mocs():
    for dom in TOPS:
        _cleanup_generated(dom)   # remove prior auto period dirs BEFORE regenerating (idempotency)
    for dom in TOPS:
        files = notes_in(dom)
        # membership is by the note's `subpath` frontmatter — decoupled from physical location.
        by_sub = collections.defaultdict(list)
        for fp in files:
            by_sub[fm(fp)["subpath"]].append(fp)
        # Every subpath value present in the data must already have a committed scaffold. Without this,
        # write_top_moc() below links {sub}/_index.md into the domain's Areas map for a file that's
        # never written (dead link), AND the sub-cluster loop further down — which is glob-driven over
        # existing {dom}/*/_index.md dirs — never picks up that subpath's notes, so they silently
        # disappear from every MOC (not Areas, not Root). Fail loud instead: a new/renamed
        # assign_subpaths.py rule-table bucket needs a human/agent to author its scaffold (title + intro
        # prose), same as every existing {dom}/{sub}/_index.md was — mirrors the fail-loud group-guard /
        # frontmatter-preserve conventions elsewhere in this store rather than silently dropping notes.
        missing = sorted(s for s in by_sub if s and not os.path.exists(os.path.join(dom, s, "_index.md")))
        if missing:
            raise SystemExit(
                "regen: {} has notes classified into subpath(s) {} with no committed scaffold — "
                "author {} before regenerating (frontmatter: title/domain/subpath/status + a "
                "**Status:** marker — see any existing {}/*/_index.md for the pattern)".format(
                    dom, missing, ", ".join(f"{dom}/{s}/_index.md" for s in missing), dom))
        idx = f"{dom}/_index.md"
        if os.path.exists(idx):
            write_top_moc(idx, dom, by_sub)
        # sub-cluster MOCs — committed scaffolds at {dom}/{sub}/_index.md; each may itself period-split.
        for sidx in sorted(glob.glob(f"{dom}/*/_index.md")):
            sdir = os.path.dirname(sidx)
            sub = os.path.basename(sdir)
            _emit(sdir, _preserved_head(sidx), by_sub.get(sub, []), dom, sub, 0, sdir)

def regen_status_tallies():
    """Own STATUS.md's note tallies — the recurring hand-drift + STATUS-count-WARN source. Counts
    come from the fresh _manifest/index.jsonl (build_index runs before regen in check.sh), + a
    deterministic test count. Splices between the TALLY_START/TALLY_END markers; a hand-edit inside
    them is overwritten (so the count can no longer drift). No-op if STATUS.md/markers are absent."""
    path = os.path.join("_manifest", "STATUS.md")
    idx = os.path.join("_manifest", "index.jsonl")
    if not (os.path.exists(path) and os.path.exists(idx)):
        return
    recs = [json.loads(ln) for ln in open(idx, encoding="utf-8") if ln.strip()]
    by_src = collections.Counter(r.get("source", "?") for r in recs)
    by_dom = collections.Counter(r.get("domain", "?") for r in recs)
    # anchored to a real method def at line-start — NOT any "def test_" substring, else `def test_`
    # inside a string literal / comment (e.g. test_regen_status.py, which tests THIS counter) inflates it.
    ntests = sum(len(re.findall(r'^\s*def test_', open(f, encoding="utf-8").read(), re.M))
                 for f in glob.glob(os.path.join("scripts", "tests", "*.py")))
    src_line = " · ".join(f"{k} {v:,}" for k, v in by_src.most_common())
    lines = [TALLY_START,
             f"**Total notes: {len(recs):,}** — by source: {src_line}. · **{ntests} tests.**",
             "", "### By domain", "| Domain | Notes | Subpaths (Tier-2) |", "|---|---|---|"]
    for dom, n in by_dom.most_common():
        lines.append(f"| {dom} | {n:,} | {SUBPATH_LABELS.get(dom, '—')} |")
    lines.append(TALLY_END)
    text = open(path, encoding="utf-8").read()
    pat = re.compile(re.escape(TALLY_START) + r".*?" + re.escape(TALLY_END), re.S)
    if not pat.search(text):
        raise SystemExit("regen: STATUS.md is missing the generated:tallies markers — add them once")
    open(path, "w", encoding="utf-8").write(pat.sub(lambda _: "\n".join(lines), text))


if __name__ == "__main__":
    regen_mocs(); regen_status_tallies()
    if _OVERSIZE:
        print(f"WARN: {len(_OVERSIZE)} leaf MOC(s) still over BYTE_CAP={BYTE_CAP} at day granularity "
              f"(a single day exceeds the read cap — consider a finer key):")
        for idx, n in _OVERSIZE:
            print(f"  {n} bytes  {idx}")
    print("regenerated MOCs + STATUS tallies")
