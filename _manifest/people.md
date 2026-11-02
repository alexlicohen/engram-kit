# people / entity registry

Canonical names, aliases, relationship, and default domain for the people and organisations that
appear across notes. Distillers resolve participants through this file, so names stay consistent
and the right domain context is applied. Everyone below is FICTIONAL seed data — replace it.

**The `self` row is the owner.** Exactly one row carries the relationship `self`; every script
that needs to know who the store belongs to (owner-vs-waiting-on task classification, the
co-occurrence graph, the Granola glossary) reads it from here (`scripts/_lib/actions.self_aliases`).
Put your own name, spoken variants and e-mail addresses on that row; nothing else hard-codes them.

**Verification rule:** an entity not seeded here is added with `verify: true` and surfaced for
confirmation (`/clarify`) — never asserted as fact. Remove the flag once confirmed.

## Owner (domain `personal`)

| Canonical name | Aliases / emails | Relationship | Verify? |
|---|---|---|---|
| Avery Quillfeather | avery@example.com; averyq@example.com; avery.quillfeather@work.example; "Ave Quillfeather" | self | — |

## Family & friends (domain `personal`)

| Canonical name | Aliases / emails | Relationship | Verify? |
|---|---|---|---|
| Signe Tessaly | signe@example.org | partner | — |

## Research collaborators (domain `research`)

| Canonical name | Aliases / emails | Relationship/role | Domain | Verify? |
|---|---|---|---|---|
| Priscilla Vantreese | "Cilla Vantreese"; p.vantreese@univ.example | collaborator — co-investigator on the example study | research | — |
| Wren Ostrander | wren@univ.example | graduate student | research | true |

## Work (domain `work`)

| Canonical name | Aliases / emails | Relationship/role | Domain | Verify? |
|---|---|---|---|---|
| Oren Halvorsk | oren.halvorsk@work.example | manager | work | — |

## Ventures & advisors (domain `ventures`)

| Canonical name | Aliases / emails | Relationship/role | Domain | Verify? |
|---|---|---|---|---|
| Delphine Arkwright | delphine@venture.example | co-founder | ventures | — |

## Finance (domain `finance`)

| Canonical name | Org | Relationship/role | Verify? |
|---|---|---|---|
| Tobiah Fenn | Example Tax Partners | accountant (CPA) | — |

## Promoted via /clarify (2026-01-30) — owner-confirmed contacts

| Canonical name | Aliases / emails | Relationship/role | Domain | Verify? |
|---|---|---|---|---|
| Juno Marchetti-Vale | juno@example.org | neighbour | personal | — |
