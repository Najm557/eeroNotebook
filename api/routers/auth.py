"""Authentication discovery for the frontend.

`GET /api/auth/status` is unauthenticated and read on page load, before anyone has
signed in. Upstream's version answered a single question — is a password set? — and
that is no longer the question: members sign in with their own credentials against
an identity provider, and the shared password is the operator's account rather than
the way in.

So this reports *how* to authenticate. It deliberately exposes no secret and no
member data: only the provider's base URL, which the browser needs in order to
reach the sign-in endpoint, and whether the operator credential exists at all.
"""

import os

from fastapi import APIRouter

from open_notebook.identity import admin_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/status")
async def get_auth_status():
    """Describe how a caller should authenticate.

    `auth_enabled` is retained and always true: authentication is no longer
    optional, and a client that reads the old field still gets a truthful answer
    rather than a missing key.
    """
    auth_url = os.environ.get("EERONOTEBOOK_AUTH_URL", "")
    return {
        # Retained for compatibility with anything reading upstream's shape.
        "auth_enabled": True,
        "method": "member_token",
        # Where the browser exchanges a password for a token. Empty when identity
        # is not configured, which the frontend surfaces rather than guessing.
        "auth_url": auth_url,
        # Members are created by the operator; there is no self-service signup.
        "signup_enabled": False,
        # Whether the operator credential is configured. Not the credential itself,
        # and it grants nothing on its own — it resolves to an admin member and is
        # subject to the same access checks as any other.
        "admin_password_configured": bool(admin_password()),
        "message": "Sign in with your member credentials",
    }
