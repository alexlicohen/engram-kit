"""Single owner of the mail selectors' header-only routing config,
`_manifest/constants/mail_routing.json`. gmail_select.py, gmail_secondary_select.py, mbox_triage.py
and outlook_select.py import from here; none of them hard-codes an institution, a person, a sender
domain or a label.

What the config decides (every key optional; a missing file = all empty):

  phi_screen_sender_domains  regexes over a correspondent's e-mail DOMAIN. A thread with any
                             participant matching one is flagged `phi_risk` (it must pass the PHI
                             screen) and is the population the deterministic PHI holds apply to
                             (outlook_select.phi_hold). Put the clinical institutions you work at
                             or correspond with here. EMPTY = nothing is PHI-screened by domain
                             (phi_scan.py still screens every thread's text).
  clinical_task_senders      exact lower-case addresses whose mail is a deterministic NON-PHI drop
                             (outlook_select.clinical_task_drop): e.g. clinic staff whose every
                             message is a task about an identifiable patient. A thread drops only
                             when EVERY non-owner sender is listed. Justify each entry from the
                             sender's own subject history, never from a display name.
  domain_rules               ordered rules; the first that matches gives the PROVISIONAL domain
                             (+ optional subpath). Each rule: {"domain", "subpath"?, and any of
                             "sender_domains" / "labels" / "subjects" (regexes, case-insensitive)
                             and "sender_addresses" (exact lower-case)}. A rule matches when ANY of
                             its patterns matches. No match = the selector's own default
                             (unsorted / review).

The routing is PROVISIONAL: the distiller reads the body and sets the authoritative domain, so a
wrong guess here costs nothing but a re-route. Every rule's domain must be in
`_manifest/constants/domains.json` (checked at load; a typo fails loudly).

    from _lib.mail_routing import ROUTING
    ROUTING.phi_screen("hospital.example")   -> bool
    ROUTING.route(labels=..., sender_domains=..., subjects=..., addresses=...) -> (domain, subpath)
"""
import json
import os
import re

from _lib.constants import DOMAINS

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(_ROOT, "_manifest", "constants", "mail_routing.json")

_PATTERN_KEYS = ("sender_domains", "labels", "subjects")


def _rx(patterns, where):
    pats = [p for p in (patterns or []) if p]
    if not pats:
        return None
    try:
        return re.compile("|".join(f"(?:{p})" for p in pats), re.I)
    except re.error as e:
        raise ValueError(f"mail_routing: bad regex in {where}: {e}") from None


class Routing:
    """A compiled routing config. Build one from a dict (tests do); ROUTING is the store's."""

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.phi_rx = _rx(cfg.get("phi_screen_sender_domains"), "phi_screen_sender_domains")
        self.clinical_task_senders = frozenset(
            a.strip().lower() for a in cfg.get("clinical_task_senders") or [] if a.strip())
        self.rules = []
        for i, r in enumerate(cfg.get("domain_rules") or []):
            dom = r.get("domain")
            if dom not in DOMAINS:
                raise ValueError(f"mail_routing: domain_rules[{i}] domain {dom!r} is not in "
                                 f"_manifest/constants/domains.json")
            self.rules.append({
                "domain": dom, "subpath": r.get("subpath"),
                "addresses": frozenset(a.lower() for a in r.get("sender_addresses") or []),
                **{k: _rx(r.get(k), f"domain_rules[{i}].{k}") for k in _PATTERN_KEYS}})

    def phi_screen(self, domain):
        """True iff an e-mail domain is one where clinical-patient data is plausible."""
        return bool(self.phi_rx and domain and self.phi_rx.search(domain))

    def route(self, labels=(), sender_domains=(), subjects=(), addresses=()):
        """(domain, subpath) of the first matching rule, else (None, None)."""
        labels = [x for x in labels if x]
        doms = [x for x in sender_domains if x]
        subjs = [x for x in subjects if x]
        addrs = {x.lower() for x in addresses if x}
        for r in self.rules:
            if ((r["addresses"] and addrs & r["addresses"])
                    or (r["sender_domains"] and any(r["sender_domains"].search(d) for d in doms))
                    or (r["labels"] and any(r["labels"].search(x) for x in labels))
                    or (r["subjects"] and any(r["subjects"].search(s) for s in subjs))):
                return r["domain"], r["subpath"]
        return None, None


def load(path=CONFIG_PATH):
    if not os.path.exists(path):
        return Routing({})
    with open(path, encoding="utf-8") as f:
        return Routing(json.load(f))


ROUTING = load()
