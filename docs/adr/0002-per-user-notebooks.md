# 0002: Notebooks are owned by individual users

**Status:** Accepted
**Date:** 2026-09-09

eeroNotebook serves a study team, including classroom use, where members keep their own material and share selected notebooks with named people. Upstream Open Notebook is deliberately a single-user product: one shared password gates the entire instance, and its own decision record `PDR-001` states that single-user is the current posture.

eeroNotebook departs from that posture. Every notebook has an owner, access is granted per person and is revocable, and only the owner may edit. This is a departure from upstream's recorded direction, not an extension of it.

## Alternatives considered

- **Shared instance password:** Upstream's model, requiring no application changes. Rejected because a study team cannot keep personal material separate, and per-person study progress has nowhere to live.
- **Team-wide shared library:** Every authenticated member sees every notebook. Rejected because classroom use requires selective sharing and later revocation.

## Consequences

- Owner scoping must reach every notebook, source, note, and study record; this is the largest single body of work in the project.
- Study progress is per person even on a shared notebook, which has no upstream equivalent and is built entirely here.
- Merges from upstream must be checked against single-tenancy assumptions. Upstream's `PDR-001` commits its contributors to not gratuitously precluding multi-user, which reduces but does not remove this risk.
