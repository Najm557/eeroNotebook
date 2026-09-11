# 0003: Identity is consumed through a replaceable boundary

**Status:** Accepted
**Date:** 2026-09-09

eeroNotebook authenticates users with GoTrue, which the team already operates. That choice is expected to change: a hosted identity provider such as Clerk is under consideration for a later edition.

The application therefore depends on an internal identity boundary that resolves a request to an authenticated user, rather than on GoTrue directly. Provider-specific token verification and user lookup live behind that boundary; authorization and ownership checks elsewhere depend only on the resolved user.

## Alternatives considered

- **Direct coupling to GoTrue:** Verify its tokens inline wherever a user is needed. Cheaper immediately, rejected because a provider change would then touch every authenticated route.

## Consequences

- One indirection exists that today has a single implementation, which may read as unnecessary without this record.
- Swapping providers should be confined to that boundary plus configuration.
