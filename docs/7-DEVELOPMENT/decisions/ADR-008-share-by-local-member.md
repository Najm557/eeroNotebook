# ADR-008: A Share names a member of this instance, never an address the provider holds

- **Status**: Accepted
- **Date**: 2026-09
- **Related**: eeroNotebook spec task 6.3 (Requirements 6.1, 6.2, 6.6), task 5.4 (sign-in proxy), task 5.6 (empty `member` table), migration 25 (the `share` relation), [ADR 0003](../../adr/0003-pluggable-identity-boundary.md) (pluggable identity boundary)

## Context

A Notebook owner shares by typing somebody's email address, but a Share is a relation
`FROM member TO notebook`: it must point at a local `member` row. Those two do not line
up cleanly. Sign-up is disabled, members are created by the operator through the
identity provider's admin path, and `Member.resolve` only writes the local row on that
person's **first sign-in** — task 5.6 observed the table empty while the provider held
three accounts. So an address can name a real account with no local record.

There is a second force. Task 5.4 deliberately refused to distinguish "no such user"
from "wrong password" on the login path, to avoid handing out account enumeration. Any
endpoint that confirms whether an address exists weakens that.

## Decision

**An address is resolved against this instance's own `member` table, matched
case-insensitively, and an address with no local row is refused — with a message that
says the person must sign in once, not that the account does not exist.**

Requirement 6.6 is met by the shape of the request rather than by a rule: one request
grants one member access to one Notebook, the request model forbids unknown fields (so
`members`, `group` or `role` are rejected rather than ignored), and `role` is written
into the SurrealQL as the literal `viewer`.

## Alternatives considered

- **Ask the identity provider's admin API for the address.** The correct answer in
  principle: it works before a first sign-in and returns the provider's own subject, so
  the local row created now would be the row `Member.resolve` finds later. Rejected for
  this task — it puts a second provider-specific integration on the path that grants
  access to private material, and it could not be verified against a live provider from
  the development environment. An unverified call there is a worse trade than a
  documented "sign in once first". This is the alternative to revisit.
- **Create a placeholder member from the address alone.** The tempting wrong answer.
  `member` is keyed on (provider, subject) where subject is the provider's user id, never
  the address, so an invented row would never match the one created at first sign-in. The
  owner would be told "shared" and the Viewer would never get in.
- **A pending invitation bound at sign-in.** Needs new schema, and makes a typo a grant
  to whoever registers that address next — worse than the gap it closes.
- **A member directory the owner picks from.** Discloses the whole roster to every
  member, which is strictly more disclosure than letting an owner test one address.

## Consequences

- **An owner must wait for a first sign-in before sharing.** In classroom use the order
  becomes: operator creates accounts, learners sign in once, instructor shares.
- **Account enumeration is weakened, knowingly, and bounded.** A successful share
  confirms the address has signed in here at least once. The caller is already an
  authenticated member created by hand, not an anonymous visitor; a refusal is *not* an
  oracle for absence, because "no local row" covers both "no account" and "has one and
  has never signed in"; and no endpoint lists members, so an owner can only test an
  address they already hold.
- A member with no address — the admin/operator identity migration 25 creates — cannot be
  shared with by address. It can still hold a Share created another way, and the UI
  labels such a row so an owner can revoke it.
- Adding a provider lookup later is additive: it would change `member_for_email` and
  nothing else, since ownership and Shares reference `member` and never a provider id
  (ADR 0003).
