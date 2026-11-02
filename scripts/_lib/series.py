"""Single owner for norm(title): the recurring-series title normalizer shared by
regen.py (MOC series grouping) and gen_backlinks.py (series-chain backlinks). Body is
byte-for-byte the prior inline copies; it reshapes every MOC "series", so keep it identical.
"""
import re


def norm(title):
    t = title
    t = re.sub(r'^\[[^\]]*\]\s*', '', t)
    t = re.sub(r'^(zoom|teams)\s*-\s*', '', t, flags=re.I)
    t = re.sub(r'^\d{4}-\d{2}-\d{2}\s+', '', t)
    t = re.sub(r'\([^)]*\)', '', t)
    t = re.sub(r'\d{1,2}/\d{1,2}/\d{2,4}', '', t)
    t = re.sub(r'\b(updated|weekly|bi-weekly|biweekly|monthly|meeting|meet|mtg|project|standing|call|session|huddle|touchbase|touchpoint)\b', '', t, flags=re.I)
    t = re.sub(r'\s*<>\s*', ' <> ', t.replace('<>', ' <> '))
    t = re.sub(r'\s+', ' ', t).strip(' -–·:,')
    return t or title.strip()
