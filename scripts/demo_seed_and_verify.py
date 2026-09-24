"""Seed two members' notebooks with disjoint content, then verify access live.

    EERONOTEBOOK_API_URL=http://10.17.8.52:5055 python3 scripts/demo_seed_and_verify.py

Two jobs in one pass, because they need the same fixture:

* It leaves the deployment in a state somebody can demo: two members, each with
  their own notebook and real embedded sources, and a Share between them.
* It checks the access rules against the live stack rather than against mocks -
  scoped lists, scoped search, the Share grant, a Viewer's refused writes, and
  revocation.

The content is deliberately fictional and specific. Each notebook carries a
made-up measurement that appears nowhere in any model's training data, so a
search result or an answer containing it can only have come from that notebook's
own sources. Real course material would prove less: a 14B model can produce
plausible facts about photosynthesis unaided, and then a leak and a correct
answer look identical.

Exits non-zero on the first failed check, naming what was expected.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("EERONOTEBOOK_API_URL", "").rstrip("/")
# No default, for the same reason demo_seed_members.py has none: a password with
# a fallback in the repository is a committed credential.
PASSWORD = os.environ.get("EERONOTEBOOK_DEMO_PASSWORD", "")

ADA = "ada@eeronotebook.invalid"
GRACE = "grace@eeronotebook.invalid"

# The two canaries. Neither string can be produced from general knowledge.
ADA_FACT = "the Vessey coefficient of mitochondrial transport is 0.847"
GRACE_FACT = "the Tarquin Ledger records 1,483 amphorae shipped from Ostia"

ADA_SOURCES = [
    (
        "Mitochondrial transport (lecture 4)",
        "Mitochondrial transport in the Renwick model. Under steady-state conditions "
        f"{ADA_FACT}, measured across 212 trials at the Kelford laboratory. The "
        "coefficient rises to 0.912 when the Ostrander buffer is substituted, which "
        "the lecture attributes to reduced membrane drag rather than to any change "
        "in carrier density. Students should note that the Renwick model assumes a "
        "fixed cristae surface area and therefore understates transport in tissue "
        "with high mitochondrial turnover.",
    ),
    (
        "Seminar notes: carrier density",
        "Carrier density measurements follow the Halloway protocol. The seminar "
        "recorded a mean density of 4,180 carriers per square micrometre in cardiac "
        "tissue and 1,270 in hepatic tissue. The ratio between them, close to 3.3, "
        "is the figure the problem set asks students to reproduce from the Renwick "
        "equations. Nothing in these notes concerns Roman trade or amphorae.",
    ),
]

GRACE_SOURCES = [
    (
        "Ostian trade records (week 2)",
        f"Ostia as a supply port in the second century BCE. {GRACE_FACT} in 143 BCE, "
        "of which 402 were consigned to military contractors and the remainder to "
        "private grain merchants. The ledger's own totals disagree with the harbour "
        "tally by 61 amphorae, a discrepancy the course uses to introduce the "
        "problem of reconciling administrative sources. Nothing here concerns "
        "mitochondria or carrier density.",
    ),
]


def call(method, path, token=None, payload=None, expect=None):
    url = f"{API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    try:
        parsed = json.loads(body or b"null")
    except json.JSONDecodeError:
        parsed = {"raw": body.decode(errors="replace")[:200]}
    if expect is not None and status != expect:
        sys.exit(f"FAIL {method} {path}: expected {expect}, got {status}: {parsed}")
    return status, parsed


def check(label, condition, detail=""):
    if condition:
        print(f"  PASS  {label}")
    else:
        sys.exit(f"  FAIL  {label}{': ' + detail if detail else ''}")


def sign_in(email):
    _, body = call("POST", "/api/auth/login", payload={"email": email, "password": PASSWORD}, expect=200)
    return body["access_token"]


def seed(token, notebook_name, description, sources):
    _, nb = call(
        "POST", "/api/notebooks", token, {"name": notebook_name, "description": description}, expect=200
    )
    nb_id = nb["id"]
    made = []
    for title, content in sources:
        # embed=True is what makes vector search and grounded answers possible;
        # embedding is a separate background job, which is why this polls below.
        # /api/sources takes multipart form data (it accepts uploads); the JSON
        # route is the separate one.
        _, src = call(
            "POST",
            "/api/sources/json",
            token,
            {"notebooks": [nb_id], "type": "text", "content": content, "title": title, "embed": True},
            expect=200,
        )
        made.append(src["id"])
    return nb_id, made


def wait_for_embedding(token, source_ids, budget=420):
    """The worker runs OPEN_NOTEBOOK_WORKER_MAX_TASKS=1, so every ingestion in
    the deployment is serialised behind one worker and these complete in turn."""
    deadline = time.time() + budget
    pending = list(source_ids)
    while pending and time.time() < deadline:
        still = []
        for sid in pending:
            status, body = call("GET", f"/api/sources/{sid}", token)
            if status != 200:
                still.append(sid)
                continue
            if body.get("embedded_chunks", 0) > 0:
                continue
            if body.get("status") == "failed":
                sys.exit(f"FAIL source {sid} failed: {body.get('error_message')}")
            still.append(sid)
        pending = still
        if pending:
            time.sleep(10)
    if pending:
        sys.exit(f"FAIL sources never embedded within {budget}s: {pending}")


def search(token, query):
    _, body = call("POST", "/api/search", token, {"query": query, "type": "text", "limit": 50}, expect=200)
    return body


def main():
    if not API:
        sys.exit("set EERONOTEBOOK_API_URL, e.g. http://10.17.8.52:5055")
    if not PASSWORD:
        sys.exit("set EERONOTEBOOK_DEMO_PASSWORD to the demo members' password")
    print(f"=== {API} ===\n")

    print("signing both members in")
    ada = sign_in(ADA)
    grace = sign_in(GRACE)
    print(f"  ada and grace hold tokens\n")

    print("seeding disjoint content")
    ada_nb, ada_srcs = seed(ada, "Cell Biology 210", "Ada's private notebook", ADA_SOURCES)
    grace_nb, grace_srcs = seed(grace, "Roman History 140", "Grace's private notebook", GRACE_SOURCES)
    print(f"  ada:   {ada_nb} with {len(ada_srcs)} sources")
    print(f"  grace: {grace_nb} with {len(grace_srcs)} sources")

    print("\nwaiting for embeddings (worker is serialised, one job at a time)")
    wait_for_embedding(ada, ada_srcs)
    wait_for_embedding(grace, grace_srcs)
    print("  all sources embedded")

    print("\n--- notebook list scoping ---")
    _, ada_list = call("GET", "/api/notebooks", ada, expect=200)
    _, grace_list = call("GET", "/api/notebooks", grace, expect=200)
    ada_ids = {n["id"] for n in ada_list}
    grace_ids = {n["id"] for n in grace_list}
    check("ada sees her own notebook", ada_nb in ada_ids)
    check("ada does NOT see grace's notebook", grace_nb not in ada_ids, f"leaked: {ada_ids}")
    check("grace sees her own notebook", grace_nb in grace_ids)
    check("grace does NOT see ada's notebook", ada_nb not in grace_ids, f"leaked: {grace_ids}")
    check(
        "ada's notebook reports role owner",
        next(n for n in ada_list if n["id"] == ada_nb).get("role") == "owner",
    )

    print("\n--- existence is not disclosed ---")
    call("GET", f"/api/notebooks/{grace_nb}", ada, expect=404)
    print("  PASS  ada opening grace's notebook gets 404, not 403")

    print("\n--- search scoping (the leak task 6.4 closed) ---")
    hit = search(ada, "Vessey coefficient")
    check("ada finds her own material", hit["total_count"] > 0, str(hit["total_count"]))
    blob = json.dumps(search(ada, "amphorae Ostia Tarquin"))
    check("ada's search cannot reach grace's content", "Tarquin Ledger" not in blob)
    check("ada's search returns none of grace's records", grace_srcs[0] not in blob)
    blob = json.dumps(search(grace, "Vessey coefficient mitochondrial"))
    check("grace's search cannot reach ada's content", "Vessey coefficient" not in blob)
    check("grace finds her own material", search(grace, "amphorae")["total_count"] > 0)

    print("\n--- sharing ---")
    call("GET", f"/api/notebooks/{ada_nb}/shares", grace, expect=404)
    print("  PASS  grace cannot list shares on a notebook she cannot see (404)")
    _, sh = call("POST", f"/api/notebooks/{ada_nb}/shares", ada, {"email": GRACE}, expect=200)
    print(f"  shared with {GRACE}")
    _, grace_list = call("GET", "/api/notebooks", grace, expect=200)
    shared = [n for n in grace_list if n["id"] == ada_nb]
    check("grace now sees ada's notebook", len(shared) == 1)
    check("and holds it as viewer, not owner", shared and shared[0].get("role") == "viewer",
          str(shared[0].get("role") if shared else None))
    call("GET", f"/api/notebooks/{ada_nb}", grace, expect=200)
    print("  PASS  grace can now open it")

    print("\n--- a viewer may read, and may not write ---")
    call("PUT", f"/api/notebooks/{ada_nb}", grace, {"name": "Grace was here"}, expect=403)
    print("  PASS  grace renaming ada's notebook is refused 403")
    call("DELETE", f"/api/notebooks/{ada_nb}", grace, expect=403)
    print("  PASS  grace deleting ada's notebook is refused 403")
    _, nb_now = call("GET", f"/api/notebooks/{ada_nb}", ada, expect=200)
    check("the notebook's name is untouched", nb_now["name"] == "Cell Biology 210", nb_now["name"])
    hit = search(grace, "Vessey coefficient")
    check("grace's search now reaches the shared material", hit["total_count"] > 0)

    print("\n--- revocation ---")
    grace_member = sh.get("member_id") or sh.get("member", {}).get("id") or sh.get("id")
    call("DELETE", f"/api/notebooks/{ada_nb}/shares/{grace_member}", ada, expect=200)
    _, grace_list = call("GET", "/api/notebooks", grace, expect=200)
    check("ada's notebook left grace's list", ada_nb not in {n["id"] for n in grace_list})
    call("GET", f"/api/notebooks/{ada_nb}", grace, expect=404)
    print("  PASS  and opening it is 404 again")
    _, still = call("GET", "/api/sources", ada, expect=200)
    surviving = {s["id"] for s in (still if isinstance(still, list) else still.get("sources", []))}
    check(
        "revocation destroyed none of ada's sources",
        all(s in surviving for s in ada_srcs),
        f"missing: {[s for s in ada_srcs if s not in surviving]}",
    )
    check("grace's search no longer reaches it", search(grace, "Vessey coefficient")["total_count"] == 0)

    print(f"\n=== all checks passed ===")
    print(f"\nLeft in place for the demo:")
    print(f"  {ADA}   owns 'Cell Biology 210'   ({ada_nb})")
    print(f"  {GRACE} owns 'Roman History 140'  ({grace_nb})")
    print("  both sign in with the password in EERONOTEBOOK_DEMO_PASSWORD")
    print("  the Share was created and then revoked, so it can be demonstrated live")


if __name__ == "__main__":
    main()
