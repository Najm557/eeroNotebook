"""The operator's credential, resolved as a member rather than as a bypass.

eeroNotebook keeps a shared admin password alongside per-member accounts. The
important design choice is that it does not skip member resolution: presenting it
resolves to one specific admin member, so every ownership and access check in task
6 applies to the operator exactly as it applies to anyone else.

The alternative — treating the shared password as sufficient on its own — is the
thing Requirement 4.5 and spec task 5.3 both warn against, because a credential
that bypasses resolution also bypasses every check built on top of it.
"""

import secrets
from typing import Optional

from open_notebook.identity.provider import MemberClaims
from open_notebook.utils.encryption import get_secret_from_env

# Recorded on the admin's member row. Stable, so the admin keeps the same identity
# — and therefore the same owned Notebooks — across restarts and password changes.
ADMIN_PROVIDER = "admin-password"
ADMIN_SUBJECT = "operator"


def admin_password() -> Optional[str]:
    """The configured admin password, or None when none is set.

    Supports the Docker secrets form upstream already honours.
    """
    return get_secret_from_env("OPEN_NOTEBOOK_PASSWORD")


def claims_for_admin_password(token: str) -> Optional[MemberClaims]:
    """Return admin claims when `token` is the admin password, else None.

    Compared in constant time: a shared secret checked with `==` leaks its length
    and prefix to anyone able to time the response, which upstream avoided and
    which there is no reason to give up.
    """
    configured = admin_password()
    if not configured:
        return None
    if not secrets.compare_digest(token.encode("utf-8"), configured.encode("utf-8")):
        return None
    return MemberClaims(provider=ADMIN_PROVIDER, subject=ADMIN_SUBJECT, email=None)
