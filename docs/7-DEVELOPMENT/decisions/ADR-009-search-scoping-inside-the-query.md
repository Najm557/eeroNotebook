# ADR-009: Notebook scoping lives inside the search functions, not around their results

- **Status**: Accepted
- **Date**: 2026-09
- **Related**: [ADR-006](ADR-006-migration-granularity.md), [ADR-008](ADR-008-share-by-local-member.md), spec task 6.4, Requirements 7.3 / 7.4 / 2.4

## Context

Upstream's `fn::text_search` and `fn::vector_search` span every row in the instance; eeroNotebook needs them confined to the Notebooks a caller owns or holds a Share on. The obvious low-cost option is to leave the two database functions alone and wrap their calls in a filtering `WHERE`, which needs no migration and no divergence from upstream schema.

That option is wrong, and measurably so. Both functions apply `LIMIT` to their ranked output, and `fn::vector_search` limits each of its three branches as well, so a filter applied afterwards narrows a top-N that was already chosen across every member's content. Measured on SurrealDB 2.6.5 with two members' material: with six of another member's notes matching the same term, a member asking for 3 results got back 1 of her own 3 matches. The other two were dropped with no error. A short result set is exactly what "no matches" looks like, so the fault has no symptom.

## Decision

The scoping predicate belongs inside each branch of each search function, before that branch's `LIMIT`. The set of reachable Notebooks is computed by `open_notebook/domain/access.py` and bound into the call as `$notebooks`; the function is told *which* Notebooks to read and never *how* that was decided, so `access.py` remains the only place that decides what a member may see.

Adding the parameter changes the functions' arity, which is deliberate: a caller still making upstream's unscoped call fails loudly rather than quietly searching the instance.

## Alternatives considered

- **Filter the returned rows in the calling query.** No migration, and it silently drops the caller's own results in proportion to other members' content. Rejected on measurement, not on principle.
- **Over-fetch inside, then filter and limit outside.** Correct only while the inflated inner limit exceeds the total match count, with a silent failure at the boundary. A tuning constant guarding a disclosure rule.
- **Reimplement the search SQL in application code, leaving the database functions untouched.** Keeps the schema pristine and forks search quality: upstream's own fixes to these functions, such as the embedding-dimension guard added in migration 9, would stop reaching us invisibly.
- **Filter in Python after the query returns.** Leaks the result count and the relevance ordering even when it hides the rows.

## Consequences

- Two upstream-owned functions are now redefined in an eeroNotebook migration. Upstream has redefined them in migrations 1, 3, 4 and 9, so a future merge touching them will conflict — visibly, in a file review, which is what Requirement 14.4 asks for.
- A future upstream redefinition that dropped the scoping would break the application on arity rather than widen search. `tests/test_search_scoping.py` additionally fails if the last definition of either function in the chain stops carrying the filter, and checks every branch individually — an aggregate count missed a single unscoped branch during development.
- Going down from the migration restores upstream's definitions, at which point search answers 500 on every call because the application still passes `$notebooks`. That is the fail-closed direction; a schema downgrade requires an application downgrade with it.
- `$notebooks` must be bound as record ids. String elements match nothing in SurrealDB, silently, which is the same defect ADR-008's work hit in `_emails_for`.
- `reference` and `artifact` carry no index, so each call performs two relation scans to build the accessible sets. Identical to the reads task 6.2's list routes already make, and the place to look first if search latency becomes a problem.
