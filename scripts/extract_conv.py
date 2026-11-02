#!/usr/bin/env python3
"""Extract the USER/ASSISTANT conversational text from a Claude Code session transcript,
stripping tool_use / tool_result / thinking / images / system-reminders.

A real transcript is ~2% conversational text (the rest is tool I/O + embedded PDFs/images),
so this reduces a multi-MB `.jsonl` to a distillable ~15-50k-token conversation. It is the
front end of the `/engram-distill-session` skill (extract -> distill -> verify -> capture).

Usage:
  python3 extract_conv.py <transcript.jsonl> [<out.txt>]   # extract one transcript
  python3 extract_conv.py --latest <project-dir> [<out.txt>]  # newest REAL transcript in a dir
  python3 extract_conv.py --list <project-dir>             # list transcripts (newest first) w/ turn counts

`<project-dir>` is a `~/.claude/projects/<slug>/` directory (slug = cwd path with '/'->'-').
--latest skips `bridge-session` stubs (146-byte pointers) and picks the newest file that
actually contains user/assistant turns. Always prints stats (turns, chars, ~tokens).
Deterministic; reads only, writes only the out file.
"""
import glob
import json
import os
import re
import sys


def _text_of(content):
    """Concatenate only the 'text' blocks (drop tool_use/tool_result/thinking/image)."""
    if isinstance(content, str):
        return content
    return '\n'.join(b.get('text', '') for b in (content or []) if b.get('type') == 'text')


def extract(path):
    """Return (rendered_text, n_user, n_assistant). Empty text for a bridge-session stub."""
    parts, nu, na = [], 0, 0
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t = o.get('type')
            if t not in ('user', 'assistant'):
                continue
            s = _text_of((o.get('message') or {}).get('content')).strip()
            if not s:
                continue
            # drop pure system-reminder / local-command-stub turns, and strip inline reminders
            if s.startswith('<system-reminder>') or s.startswith('<local-command'):
                continue
            s = re.sub(r'<system-reminder>.*?</system-reminder>', '', s, flags=re.S).strip()
            if not s:
                continue
            if t == 'user':
                nu += 1
                parts.append('\n### USER:\n' + s)
            else:
                na += 1
                parts.append('\n### ASSISTANT:\n' + s)
    return '\n'.join(parts), nu, na


def _transcripts_newest_first(project_dir):
    files = glob.glob(os.path.join(project_dir, '*.jsonl'))
    return sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)


def _stats(path, text, nu, na, out):
    print(f"{os.path.basename(path)}: {nu} user + {na} asst turns "
          f"-> {out} ({len(text):,} chars, ~{len(text)//4:,} tok)")


def main(argv):
    if not argv:
        sys.exit(__doc__)
    if argv[0] == '--list':
        pdir = argv[1]
        for p in _transcripts_newest_first(pdir):
            _, nu, na = extract(p)
            tag = 'STUB' if (nu + na) == 0 else f'{nu}u/{na}a'
            print(f"{os.path.getmtime(p):.0f}  {os.path.getsize(p):>10,}B  {tag:>10}  {os.path.basename(p)}")
        return
    if argv[0] == '--latest':
        pdir = argv[1]
        out = argv[2] if len(argv) > 2 else os.path.join(pdir, '_conv.txt')
        for p in _transcripts_newest_first(pdir):
            text, nu, na = extract(p)
            if nu + na > 0:                       # skip bridge-session stubs
                open(out, 'w', encoding='utf-8').write(text)
                print(f"latest real transcript: {p}")
                _stats(p, text, nu, na, out)
                return
        sys.exit(f"no non-stub transcript found in {pdir}")
    # positional: <in.jsonl> [<out.txt>]
    path = argv[0]
    out = argv[1] if len(argv) > 1 else os.path.splitext(path)[0] + '_conv.txt'
    text, nu, na = extract(path)
    open(out, 'w', encoding='utf-8').write(text)
    _stats(path, text, nu, na, out)


if __name__ == '__main__':
    main(sys.argv[1:])
