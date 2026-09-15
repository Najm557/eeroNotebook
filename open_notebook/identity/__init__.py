"""The identity boundary.

Everything provider-specific lives in this package. A route imports exactly one
name from it — `current_member` — and receives a `Member`; it never sees a token,
a claim, or a provider. Replacing GoTrue with another provider means implementing
`IdentityProvider` here and changing nothing outside (ADR 0003).

Two credentials resolve through the same seam: a member's access token, and the
operator's shared admin password. Both produce a `Member`, so the operator is
subject to the same access checks as anyone else — a credential that skipped
resolution would also skip every check built on it.

`tests/test_identity_boundary.py` asserts that no module outside this package
imports a JWT library or reads a GoTrue environment variable. The seam is only
real if something enforces it.

`set_provider` and `IdentityProvider` are exported for tests and for a future
provider migration. They are the seam's own controls, not application surface.
"""

from open_notebook.identity.admin import (
    ADMIN_PROVIDER,
    ADMIN_SUBJECT,
    admin_password,
    claims_for_admin_password,
)
from open_notebook.identity.dependency import (
    current_member,
    get_provider,
    set_provider,
)
from open_notebook.identity.middleware import (
    DEFAULT_EXCLUDED_PATHS,
    MemberAuthMiddleware,
)
from open_notebook.identity.provider import (
    GoTrueProvider,
    IdentityProvider,
    MemberClaims,
)

__all__ = [
    "current_member",
    "get_provider",
    "set_provider",
    "IdentityProvider",
    "MemberClaims",
    "GoTrueProvider",
    "MemberAuthMiddleware",
    "DEFAULT_EXCLUDED_PATHS",
    "ADMIN_PROVIDER",
    "ADMIN_SUBJECT",
    "admin_password",
    "claims_for_admin_password",
]
