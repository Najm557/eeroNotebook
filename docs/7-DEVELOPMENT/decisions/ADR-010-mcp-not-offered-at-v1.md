# ADR-010: The MCP interface is not offered at v1, and access enforcement has exactly one point

- **Status**: Accepted
- **Date**: 2026-09
- **Related**: [ADR-008](ADR-008-share-by-local-member.md), [ADR-009](ADR-009-search-scoping-inside-the-query.md), spec task 6.6, Requirement 7.2

## Context

Requirement 7.2 asks that the MCP interface enforce the same access rules as the REST API. A reader taking that literally goes looking for a second enforcement point in this codebase. There is none, and there should not be: **no MCP server lives in this repository.** `open-notebook-mcp` is a separately published package ([PyPI](https://pypi.org/project/open-notebook-mcp), [Epochal-dev/open-notebook-mcp](https://github.com/Epochal-dev/open-notebook-mcp)) that holds no database credentials and reaches this API over HTTP like any other client.

Verified rather than assumed, against version 0.3.0 and a locally-run API at migration 26. It exposes 33 tools; 32 issue HTTP requests and one (`search_capabilities`) is a local catalogue. All 32 go through a single `make_request` helper, so they share one auth path, and none of their paths is in the middleware's exclusion list. 25 of the 32 land on routes that resolve `current_member` and therefore run task 6.2's and 6.4's checks; the remaining 7 are instance configuration (`/api/models`, `/api/settings`) plus one tool whose route does not exist here.

The obstacle is the credential. The package reads one environment variable, `OPEN_NOTEBOOK_PASSWORD`, and puts its value verbatim into `Authorization: Bearer`. Task 5.3 removed the shared-password gate, so that slot now behaves as a general bearer slot with two usable fillings, and both were measured:

- A member's GoTrue access token authenticates as that member and inherits all of 6.2/6.4's scoping. But `GOTRUE_JWT_EXP` is 3600 and the package has no refresh — it never calls `/api/auth/refresh`, and the value is fixed when the MCP client spawns the process. After an hour every tool answers `Token has expired` until a human pastes a new JWT and restarts the client.
- The operator's password authenticates as the admin member (`provider=admin-password`, `is_admin=true`). That is the only credential here that does not expire, and it is the one the existing documentation tells people to use. Migration 25 backfilled every previously-unowned Notebook to the admin, so that identity reaches the whole pre-existing library.

## Decision

**The MCP interface is not part of v1.** The operator does not configure it, and [the documentation page](../../5-CONFIGURATION/mcp-integration.md) says so at the top rather than merely omitting it — the configuration it describes still works, which is the reason a warning is needed instead of silence.

**Requirement 7.2 is satisfied structurally, not by a second implementation.** Access is enforced in one place: `open_notebook/domain/access.py`, consulted by the `require_*` functions the routes depend on, behind `MemberAuthMiddleware`. Any MCP client is on the far side of that, so it cannot enforce less than the REST API and cannot be made to. There is nothing to look for in this codebase because there is nothing to find.

Enabling it later requires a member credential that outlives one hour. That is a new decision with a new record, not a configuration change.

## Alternatives considered

- **Document the member-token path and ship it.** Mechanically it works, and it was measured working. It fails an hour later with no recovery short of editing the client's config file, which is exactly the half-configured interface this task existed to avoid.
- **Keep the documented operator-password configuration.** Puts the highest-privilege credential in a third-party client's plaintext config and gives every AI-assistant conversation through it unscoped read and write over the entire library. Rejected on consequence, not on whether it functions — it functions, which is the problem.
- **Issue long-lived member API tokens from this API.** The honest fix, and out of proportion to v1: a credential type, an issuance route, revocation, and a middleware path that accepts a bearer token GoTrue never signed and cannot expire. It also widens the blast radius of a leaked token from an hour to forever, which the operator should choose deliberately.
- **Teach the MCP server to sign in and refresh.** The right place for it, and a different repository. Member sign-in at `/api/auth/login` needs email *and* password, so the client config would hold the member's actual password — worse than holding a token.
- **Refuse the operator password as a bearer credential, so the documented configuration simply fails.** Task 5.4 made the admin credential *be* the bearer token, deliberately, so the operator can still sign in when the identity provider is down. Breaking that to discourage a misconfiguration trades a working recovery path for a warning a document can carry.

## Consequences

- Requirement 7.2 is met with no code in this repository. Anyone auditing it reads this record and the route scoping, not an MCP module.
- The single enforcement point is a property to preserve, not an accident. A future MCP server that talked to SurrealDB directly, or ran inside the API process with its own data access, would create the second enforcement point 7.2 is about — and would need to re-derive owner-or-Share for itself.
- Nothing stops an operator from pointing the package at a deployment anyway. It is a published package and this API is an ordinary HTTP API. Requirement 7.2 still holds if they do; what they lose is the choice of identity, not the enforcement.
- Five tools reach instance configuration that no task has scoped by member: `get_settings`, `update_settings`, `list_models`, `create_model` and `get_default_models` all succeed for a member with no Notebook access. Measured, and consistent with 6.2 — it scoped content routes, and instance configuration is shared by design. Worth a decision of its own if members ever outnumber people the operator trusts with the processing engine.
- Version 0.3.0 has already drifted from this API regardless of access: `get_model` calls a `GET /api/models/{id}` that does not exist (405), and `execute_chat` and `get_chat_context` omit fields this API's schemas require (422). A future version may drift further in either direction, which is one more reason the interface is not a v1 commitment.
