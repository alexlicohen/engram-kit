#!/usr/bin/env python3
"""Deterministic, guarded WRITE-BACK helper — the core behind the `/engram-capture` skill
Lets ANY Claude session append ONE durable note (a decision, commitment,
person, or correction) to the engram store mid-work, then verifies + commits it LOCALLY (never
pushes). This is the DANGER-ZONE writer: it allocates provenance, enforces the PHI rule, and
mutates the store, so every step is fail-closed and reuses the store's single-owner primitives
(never re-implements them).

    python3 scripts/engram_capture.py --session <id> --domain <dom> --title "<t>" \
        --tags "a,b,c" --type event|evergreen --body-file <path> \
        [--participants "X,Y"] [--kind decision|commitment|person|correction] \
        [--supersedes <slug>] [--due <date>] [--owner <name>] [--date <YYYY-MM-DD>] \
        [--phi-reviewed] [--no-commit] [--flagged] [--root <path>]

Design:
  * Provenance ref = `claude-chat:<session-id>:c<n>`, n = max existing index for that session +1
    (0 if none). Deterministic, unique per capture, dup-ref-lint-safe.
  * PHI guard is FAIL-CLOSED: run scripts/phi_scan.py::scan (the single owner) on the drafted
    note; if it flags, REFUSE (exit non-zero, write nothing) unless --phi-reviewed is passed.
  * Commit is LOCAL only, message `capture: <title>` — NEVER push.

Single-owner reuse: DOMAINS/TAG_VOCAB from _lib.constants; provenance-ref parsing from
_lib.provenance/_lib.notes; subpath from assign_subpaths.classify; PHI from phi_scan.scan;
regenerate+lint via the store's own generators + lint_store.py.
"""
import argparse
import datetime
import glob
import hashlib
import os
import re
import subprocess
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)

from _lib.constants import DOMAINS, TAG_VOCAB          # noqa: E402  (single owner: vocab)
from _lib.provenance import parse_ref                  # noqa: E402  (single owner: ref parse)
from _lib.actions import OPENLOOPS_SECTIONS               # noqa: E402  (single owner: section map)
import phi_scan                                        # noqa: E402  (single owner: PHI detector)
import assign_subpaths                                 # noqa: E402  (single owner: subpath rules)
# NOTE: importing assign_subpaths runs its import-time os.chdir(real repo). Harmless here because
# every filesystem/git/subprocess op below uses an ABSOLUTE root + explicit cwd, never the ambient
# cwd. classify() itself is a pure function (no store access).

# Regeneration generators, in check.sh order. gen_backlinks is the ONLY one that edits note
# frontmatter (adds related: backlinks), so build_index must re-run after it -> convergence loop.
_GENERATORS = ["build_index.py", "build_cooccurrence.py", "regen.py",
               "gen_backlinks.py", "build_clarify_queue.py"]

# git pathspecs the capture commit is allowed to stage: knowledge dirs + manifest + digests.
# Deliberately EXCLUDES scripts/ and root docs so a capture commit can never sweep in the helper
# itself or an unrelated edit. `git add -A -- <these>` stages the new note, regenerated derived
# views, backlink edits to existing notes, and any new _index.md — nothing else.
_COMMIT_PATHSPECS = DOMAINS + ["_manifest", "digests"]

# domain -> the existing `## heading` in digests/open-loops.md a commitment is filed under.
# A domain absent here (or a heading not found in the file) gets a fresh `## <Domain>` section.
# Canonical map lives in _lib.actions (single owner; a task bridge routes seeds via its inverse).
_OPENLOOPS_SECTION = OPENLOOPS_SECTIONS


# --------------------------------------------------------------------------- helpers
def _die(msg, code=2):
    print(f"engram_capture: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _run(cmd, root):
    """Run a subprocess with cwd=root; return CompletedProcess (captured)."""
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True)


def slugify(title, maxlen=55):
    """kebab-case slug of a title (store filename convention), length-capped at a hyphen
    boundary so filenames stay sane."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0] or s[:maxlen]
    return s.strip("-") or "note"


def next_capture_index(root, session):
    """n for `claude-chat:<session>:c<n>` = max existing index for this session +1 (0 if none).
    Scans every domain note's head via the single-owner parse_ref, so it sees exactly the refs
    the dup-ref linter would."""
    pat = re.compile(r"^claude-chat:" + re.escape(session) + r":c(\d+)$")
    hi = -1
    for d in DOMAINS:
        for fp in glob.glob(os.path.join(root, d, "**", "*.md"), recursive=True):
            if os.path.basename(fp) == "_index.md":
                continue
            try:
                head = open(fp, encoding="utf-8", errors="replace").read(1500)
            except OSError:
                continue
            m = pat.match((parse_ref(head) or "").strip())
            if m:
                hi = max(hi, int(m.group(1)))
    return hi + 1


def parse_list(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def build_note(title, date, tags, domain, subpath, ntype, ref, ingested,
               participants, supersedes, body):
    """Construct the schema-compliant note text (frontmatter + body). Single write; no post-edit."""
    tag_str = "[" + ", ".join(tags) + "]"
    part_str = "[" + ", ".join(participants) + "]"
    sup_str = "[" + ", ".join(supersedes) + "]"       # bare slugs per signed-off design (not wikilinks)
    sub_line = subpath if subpath else "null"
    head = (
        "---\n"
        f'title: "{title}"\n'
        f"date: {date}\n"
        "aliases: []\n"
        f"tags: {tag_str}\n"
        f"domain: {domain}\n"
        f"subpath: {sub_line}\n"
        f"type: {ntype}\n"
        "source: claude-chat\n"
        "provenance:\n"
        f'  ref: "{ref}"\n'
        f"  ingested: {ingested}\n"
        "  method: captured\n"
        f"participants: {part_str}\n"
        "status: done\n"
        "related: []\n"
        f"supersedes: {sup_str}\n"
        "superseded_by: []\n"
        "---\n"
    )
    body = body.rstrip("\n")
    return head + "\n" + body + "\n"


def append_open_loop(root, domain, title, owner, due, slug):
    """Append one commitment line to digests/open-loops.md under the domain's section (curated,
    regen-safe — the digest is never auto-regenerated). Creates the section (or the file) if
    absent. Format mirrors the existing lines exactly."""
    fp = os.path.join(root, "digests", "open-loops.md")
    line = (f"- {title} — owner: {owner or 'me'} · due: {due or '—'} "
            f"· status: open · [[{slug}]]")
    heading = _OPENLOOPS_SECTION.get(domain, domain.replace("-", " ").title())

    if not os.path.exists(fp):
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        content = (
            "---\n"
            'title: "Open loops & commitments — digest"\n'
            "domain: infra\n"
            "source: synthesized\n"
            f"generated: {datetime.date.today().isoformat()}\n"
            "type: evergreen\n"
            "---\n\n"
            f"## {heading}\n\n{line}\n"
        )
        open(fp, "w", encoding="utf-8").write(content)
        return fp

    lines = open(fp, encoding="utf-8").read().split("\n")
    # locate the heading line
    hi = next((i for i, ln in enumerate(lines) if ln.strip() == f"## {heading}"), None)
    if hi is None:
        # no such section — append a fresh one at EOF
        while lines and lines[-1].strip() == "":
            lines.pop()
        lines += ["", "---", "", f"## {heading}", "", line, ""]
        open(fp, "w", encoding="utf-8").write("\n".join(lines))
        return fp
    # section end = next `## ` heading or `---` rule, or EOF
    j = next((k for k in range(hi + 1, len(lines))
              if lines[k].startswith("## ") or lines[k].strip() == "---"), len(lines))
    # insert after the last non-blank line of the section
    k = j
    while k - 1 > hi and lines[k - 1].strip() == "":
        k -= 1
    lines.insert(k, line)
    open(fp, "w", encoding="utf-8").write("\n".join(lines))
    return fp


def regenerate_to_convergence(root):
    """Run the store's generators (check.sh order) until the tracked knowledge-dir tree stops
    changing. gen_backlinks edits notes -> build_index must re-run, so >1 pass is expected; the
    additive/idempotent backlinks converge in a few passes. Raises on a generator failure or if
    it fails to stabilize."""
    def snapshot():
        r = _run(["git", "status", "--porcelain", "--"] + _COMMIT_PATHSPECS, root)
        return r.stdout
    prev = None
    for _ in range(6):
        for g in _GENERATORS:
            r = _run([sys.executable, os.path.join(_SCRIPTS, g)], root)
            if r.returncode != 0:
                _die(f"generator {g} failed:\n{r.stdout}\n{r.stderr}", 4)
        cur = snapshot()
        if cur == prev:
            return
        prev = cur
    _die("regeneration did not converge after 6 passes", 4)


def run_lint(root):
    """Run lint_store.py against `root`; return (ok, output). ok == (0 errors)."""
    r = _run([sys.executable, os.path.join(_SCRIPTS, "lint_store.py")], root)
    return r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Guarded write-back of one note into engram.")
    ap.add_argument("--session", required=True)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--tags", required=True, help="comma-separated, from the controlled vocab")
    ap.add_argument("--type", dest="ntype", required=True, choices=["event", "evergreen"])
    ap.add_argument("--body-file", required=True)
    ap.add_argument("--participants", default="")
    ap.add_argument("--kind", choices=["decision", "commitment", "person", "correction"])
    ap.add_argument("--supersedes")
    ap.add_argument("--due")
    ap.add_argument("--owner")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--phi-reviewed", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--flagged", action="store_true", help="force routing to unsorted/ + flagged tag")
    ap.add_argument("--root", default=os.path.dirname(_SCRIPTS),
                    help="store root (defaults to the repo); tests point this at a temp store")
    a = ap.parse_args(argv)

    root = os.path.abspath(a.root)
    today = datetime.date.today().isoformat()

    # --- validate date ---
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", a.date):
        _die(f"--date '{a.date}' not ISO YYYY-MM-DD")

    # --- domain + routing ---
    domain = a.domain
    flagged = a.flagged
    if flagged and domain != "unsorted":
        print(f"engram_capture: --flagged overrides --domain {domain!r} -> routing to unsorted/")
        domain = "unsorted"
    if domain == "unsorted":
        flagged = True
    if domain not in DOMAINS:
        _die(f"domain '{a.domain}' not in canonical DOMAINS: {DOMAINS}")

    # --- tags (validate against controlled vocab) ---
    tags = parse_list(a.tags)
    if flagged and "flagged" not in tags:
        tags.append("flagged")
    if not tags:
        _die("at least one tag is required")
    bad = [t for t in tags if t not in TAG_VOCAB]
    if bad:
        _die(f"off-vocab tag(s) {bad}; allowed vocab lives in _manifest/constants/tag_vocab.json")

    # --- body ---
    if not os.path.isfile(a.body_file):
        _die(f"--body-file not found: {a.body_file}")
    body = open(a.body_file, encoding="utf-8").read()
    if not body.strip():
        _die("--body-file is empty")

    participants = parse_list(a.participants)

    # --- supersedes (correction) — bare slug, NEVER touches the superseded note ---
    supersedes = []
    if a.supersedes:
        supersedes = [a.supersedes.strip()]
        if a.kind and a.kind != "correction":
            print(f"engram_capture: note: --supersedes given with --kind {a.kind} "
                  f"(supersedes normally pairs with --kind correction)")

    # --- provenance ref (claude-chat:<session>:c<n>) ---
    n = next_capture_index(root, a.session)
    ref = f"claude-chat:{a.session}:c{n}"
    sha6 = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:6]

    # --- subpath (reuse the single-owner classifier; '' for non-big-domains / root) ---
    subpath = assign_subpaths.classify(domain, tags, a.title)

    # --- filename + path ---
    slug = slugify(a.title)
    fname = f"{a.date}-{slug}-{sha6}.md"
    note_path = os.path.join(root, domain, fname)
    if os.path.exists(note_path):
        _die(f"target already exists (refusing to overwrite): {note_path}", 5)

    # --- assemble note text IN MEMORY (nothing on disk yet) ---
    note_text = build_note(a.title, a.date, tags, domain, subpath, a.ntype, ref, today,
                           participants, supersedes, body)

    # --- PHI guard (FAIL-CLOSED) — scan BEFORE writing, so a refusal leaves NO note behind ---
    result = phi_scan.scan(note_text)
    if result["phi"]:
        reason = f"level={result['level']} signals={result['strong'] or 'dense-clinical-context'}"
        if not a.phi_reviewed:
            print(f"engram_capture: PHI GUARD REFUSED — drafted note flagged possible "
                  f"clinical-patient PHI ({reason}).")
            print("  Nothing was written. If this is NOT clinical-patient PHI (you are not the "
                  "person's treating physician), re-run with --phi-reviewed.")
            sys.exit(3)
        phi_note = f"FLAGGED ({reason}) — overridden by --phi-reviewed"
    else:
        phi_note = "clean"

    # --- write the note ---
    os.makedirs(os.path.dirname(note_path), exist_ok=True)
    open(note_path, "w", encoding="utf-8").write(note_text)
    rel_note = os.path.relpath(note_path, root)

    # --- commitment -> open-loops append (links to the note's real slug) ---
    ol_path = None
    if a.kind == "commitment":
        ol_path = append_open_loop(root, domain, a.title, a.owner, a.due,
                                   os.path.splitext(fname)[0])

    # --- verify: lint the store (catches bad frontmatter / dup ref / unresolved wikilinks) ---
    ok, lint_out = run_lint(root)
    if not ok:
        os.remove(note_path)                       # roll back the note so a refusal leaves nothing
        _die(f"lint_store rejected the drafted note (rolled back):\n{lint_out}", 6)

    # --- commit path ---
    commit_sha = None
    if a.no_commit:
        commit_state = "not committed (--no-commit): note written to the working tree, unstaged"
    else:
        regenerate_to_convergence(root)
        ok2, lint_out2 = run_lint(root)            # belt+suspenders after backlink edits
        if not ok2:
            _die(f"lint_store failed after regeneration:\n{lint_out2}", 6)
        add = _run(["git", "add", "-A", "--"] + _COMMIT_PATHSPECS, root)
        if add.returncode != 0:
            _die(f"git add failed:\n{add.stderr}", 7)
        commit = _run(["git", "commit", "-m", f"capture: {a.title}"], root)
        if commit.returncode != 0:
            _die(f"git commit failed:\n{commit.stdout}\n{commit.stderr}", 7)
        commit_sha = _run(["git", "rev-parse", "HEAD"], root).stdout.strip()
        # final gate: the store's own check must be green on the committed (clean) tree
        chk = _run(["bash", os.path.join(_SCRIPTS, "check.sh")], root)
        if chk.returncode != 0:
            _die(f"check.sh not green after commit {commit_sha}:\n{chk.stdout}\n{chk.stderr}", 8)
        commit_state = f"committed {commit_sha} (LOCAL only — not pushed)"

    # --- report ---
    print("engram_capture: OK")
    print(f"  note:   {rel_note}")
    print(f"  ref:    {ref}")
    print(f"  PHI:    {phi_note}")
    if ol_path:
        print(f"  open-loop appended: {os.path.relpath(ol_path, root)}")
    print(f"  commit: {commit_state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
