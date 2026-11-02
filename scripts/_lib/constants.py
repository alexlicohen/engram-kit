"""Single owner for the copy-pasted "decisions": the canonical top-level DOMAINS list, the
controlled TAG_VOCAB, and the known FULL-BACKFILL wave basenames (BACKFILL_WAVES). Loaded from
_manifest/constants/*.json (the single source of truth). Paths resolve to the repo root regardless
of cwd. (The Node loader _lib/constants.mjs was retired with the Workflow-runtime migration — the
pure-JS runtime has no fs, so nothing loads these via Node anymore; only Python reads them.)

    from _lib.constants import DOMAINS, TAG_VOCAB, BACKFILL_WAVES
"""
import json
import os

# scripts/_lib/constants.py -> repo root is three levels up (dirname x3).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONST = os.path.join(_ROOT, "_manifest", "constants")

with open(os.path.join(_CONST, "domains.json"), encoding="utf-8") as _f:
    DOMAINS = json.load(_f)["domains"]          # list, canonical order preserved

with open(os.path.join(_CONST, "tag_vocab.json"), encoding="utf-8") as _f:
    TAG_VOCAB = set(json.load(_f)["tags"])      # set, membership only

with open(os.path.join(_CONST, "backfill_waves.json"), encoding="utf-8") as _f:
    BACKFILL_WAVES = set(json.load(_f)["backfill_waves"])   # set, membership only
