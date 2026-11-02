#!/usr/bin/env python3
"""Build the authoritative Gmail KEEP work-list for the chosen scope.

Scope: "everything I replied to" — every thread containing a message the owner sent (label
`Sent` or From == one of the owner's addresses: the `self` row of `_manifest/people.md`),
excluding Spam/Trash.

Emits one record per KEEP thread to gmail-keep.jsonl with: the message byte-offsets
(for extraction), a PROVISIONAL domain (finalized at distillation, which sees the
body), a phi_risk flag for the clinical/medical screen, and labels/participants.

Routing and the PHI-screen sender domains are configuration, not code:
`_manifest/constants/mail_routing.json` (see scripts/_lib/mail_routing.py).

Usage: python3 scripts/gmail_select.py <index.jsonl> <out-keep.jsonl>
"""
import sys, os, json, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.actions import self_emails      # noqa: E402  (single owner of the owner's addresses)
from _lib.mail_routing import ROUTING     # noqa: E402  (single owner of header routing)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SELF = self_emails(ROOT)
DROP_LABELS = {'Spam', 'Trash'}


def phi_screen(dom):
    """True iff a sender domain is one where clinical-patient PHI is plausible (configured)."""
    return ROUTING.phi_screen(dom)

def provisional_domain(labels, doms, subjects):
    """Best-effort domain from HEADERS ONLY (the configured domain_rules, first match wins).
    'unsorted' when nothing matches — the distiller (which reads the body) sets the
    authoritative domain."""
    domain, subpath = ROUTING.route(labels=labels, sender_domains=doms, subjects=subjects)
    return (domain, subpath) if domain else ('unsorted', None)


def main():
    idx_path, out_path = sys.argv[1], sys.argv[2]
    thr = collections.OrderedDict()
    for line in open(idx_path, encoding='utf-8'):
        r = json.loads(line)
        t = r.get('thrid') or f"_n{r['start']}"
        d = thr.get(t)
        if d is None:
            d = thr[t] = {'thrid': t, 'labels': set(), 'doms': set(), 'sent': False,
                          'subjects': [], 'parts': set(), 'dates': [], 'msgs': []}
        d['labels'].update(r.get('labels') or [])
        if 'Sent' in (r.get('labels') or []) or r.get('from_addr') in SELF:
            d['sent'] = True
        fa = r.get('from_addr')
        if fa:
            d['parts'].add(fa)
            if fa not in SELF:
                d['doms'].add(fa.split('@')[-1])
        d['parts'].update(r.get('to') or [])
        d['parts'].update(r.get('cc') or [])
        if r.get('subject'):
            d['subjects'].append(r['subject'])
        if r.get('date'):
            d['dates'].append(r['date'])
        d['msgs'].append([r['start'], r['end']])

    counts = collections.Counter()
    phi = 0
    with open(out_path, 'w', encoding='utf-8') as out:
        for t, d in thr.items():
            if not d['sent'] or (DROP_LABELS & d['labels']):
                continue
            domain, subpath = provisional_domain(d['labels'], d['doms'], d['subjects'])
            phi_risk = any(phi_screen(x) for x in d['doms'])
            dates = sorted(x for x in d['dates'] if x)
            flags = []
            if phi_risk:
                flags.append('phi-screen')
            if domain == 'unsorted':
                flags.append('needs-domain-confirm')
            rec = {
                'thrid': t,
                'domain': domain,
                'subpath': subpath,
                'phi_risk': phi_risk,
                'flags': flags,
                'subject': d['subjects'][0] if d['subjects'] else None,
                'n_msgs': len(d['msgs']),
                'date_start': dates[0] if dates else None,
                'date_end': dates[-1] if dates else None,
                'labels': sorted(d['labels']),
                'participants': sorted(p for p in d['parts'] if p),
                'offsets': d['msgs'],
            }
            out.write(json.dumps(rec, ensure_ascii=False) + '\n')
            counts[domain] += 1
            phi += phi_risk

    print(f"KEEP threads: {sum(counts.values()):,}   (phi-screen flagged: {phi:,})\n")
    for dom, n in counts.most_common():
        print(f"  {n:6,}  {dom}")


if __name__ == '__main__':
    main()
