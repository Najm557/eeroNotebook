"""Create demo members through the operator's admin path, and sign each in once.

Run INSIDE the app container, which is the only place with both the JWT secret
and a route to the provider:

    docker exec -i eeronotebook-app python3 - < scripts/demo_seed_members.py

Two things this has to do, in this order, and the second is the one that is easy
to forget:

1. Create the account at the provider. Self-service signup is disabled
   (GOTRUE_DISABLE_SIGNUP), so this goes through the admin API with a
   service_role token minted from GOTRUE_JWT_SECRET - the same path an operator
   uses, not a back door around it.

2. Sign each member in once against this application. A Share names a member of
   *this instance* (ADR-008), and the local `member` row is only written on first
   sign-in by Member.resolve. An account that exists at the provider but has
   never signed in here cannot be shared with - the owner gets a refusal telling
   them to ask the person to sign in once. So a demo that skips this step fails
   at exactly the moment it is being shown.

Idempotent: an address that already exists is reused rather than treated as an
error, so this can be re-run while preparing a demo.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

AUTH = "http://eeronotebook-auth:9999"
API = "http://localhost:5055"

# Demo accounts only, on .invalid addresses that can never receive mail.
# The password is required from the environment and has no default on purpose:
# a credential committed to the repository is a credential, however short-lived
# the account it belongs to. GoTrue is configured for a 12-character minimum.
EMAILS = ["ada@eeronotebook.invalid", "grace@eeronotebook.invalid"]
PASSWORD = os.environ.get("EERONOTEBOOK_DEMO_PASSWORD", "")


def post(url, payload, token=None):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode(errors="replace")[:300]}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def service_role_token():
    """Mint the admin token. GOTRUE_JWT_ADMIN_ROLES is service_role and
    GOTRUE_JWT_AUD is authenticated, so both claims are required - a token
    missing either is refused 403 by the admin API rather than ignored.

    Signed with stdlib hmac rather than pyjwt: pyjwt is a dependency of the
    application's venv, not of the container's system python, and this script
    should not care which interpreter runs it.
    """
    secret = os.environ.get("GOTRUE_JWT_SECRET")
    if not secret:
        sys.exit("GOTRUE_JWT_SECRET absent - run this inside eeronotebook-app")
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = _b64(
        json.dumps(
            {"role": "service_role", "aud": "authenticated", "exp": int(time.time()) + 600}
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode()
    sig = _b64(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{claims}.{sig}"


def main():
    if len(PASSWORD) < 12:
        sys.exit(
            "set EERONOTEBOOK_DEMO_PASSWORD to at least 12 characters — "
            "the provider enforces a 12-character minimum and this script "
            "deliberately carries no default"
        )
    token = service_role_token()
    print("minted a service_role token")

    for email in EMAILS:
        status, body = post(
            f"{AUTH}/admin/users",
            {"email": email, "password": PASSWORD, "email_confirm": True},
            token,
        )
        if status in (200, 201):
            print(f"  created at provider: {email}")
        elif status in (409, 422) or "already" in json.dumps(body).lower():
            print(f"  already at provider:  {email}")
        else:
            sys.exit(f"  FAILED to create {email}: {status} {body}")

    print("\nsigning each in once, so a Share can name them (ADR-008):")
    for email in EMAILS:
        status, body = post(f"{API}/api/auth/login", {"email": email, "password": PASSWORD})
        if status != 200 or "access_token" not in body:
            sys.exit(f"  FAILED sign-in for {email}: {status} {body}")
        print(f"  {email}: local member row now exists, expires_in={body.get('expires_in')}")

    print("\ndone")


if __name__ == "__main__":
    main()
