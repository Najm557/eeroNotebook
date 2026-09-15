"""The local identity of an authenticated person.

A member is eeroNotebook's own record of someone who can sign in. It exists so
that application data — Notebook ownership, Shares, study progress — references a
local id and never an external provider's user id. Replacing the identity
provider then rewrites this table's `provider` and `subject` values and touches
nothing else (ADR 0003).

Deliberately provider-agnostic, in prose as well as in code: this module names no
provider, and a test asserts it. `provider` here is whichever system vouched for
the person, and all specifics live behind `open_notebook.identity`.
"""

from typing import Any, ClassVar, Dict, Optional

from loguru import logger

from open_notebook.database.repository import repo_query
from open_notebook.domain.base import ObjectModel


class Member(ObjectModel):
    """A person who can sign in to this instance."""

    table_name: ClassVar[str] = "member"
    nullable_fields: ClassVar[set[str]] = {"email"}

    # Which provider vouched for this person, and their id within it. Two fields
    # rather than one composite string so a provider migration stays a readable
    # UPDATE.
    provider: str
    subject: str
    email: Optional[str] = None

    @classmethod
    async def find_by_subject(
        cls, provider: str, subject: str
    ) -> Optional["Member"]:
        """Return the member for a provider identity, or None."""
        result = await repo_query(
            "SELECT * FROM member WHERE provider = $provider AND subject = $subject LIMIT 1",
            {"provider": provider, "subject": subject},
        )
        if not result:
            return None
        return cls(**result[0])

    @classmethod
    async def resolve(
        cls, provider: str, subject: str, email: Optional[str] = None
    ) -> "Member":
        """Return the member for a provider identity, creating one on first sign-in.

        The unique index on (provider, subject) is what makes this safe under
        concurrency: two simultaneous first requests cannot both insert. The
        loser's insert fails and it re-reads, rather than both succeeding and
        leaving one person with two identities and two sets of notebooks.
        """
        existing = await cls.find_by_subject(provider, subject)
        if existing is not None:
            if email is not None and existing.email != email:
                # The provider is authoritative for the address; keep the local
                # copy current so an operator recognises the account.
                existing.email = email
                await existing.save()
            return existing

        member = cls(provider=provider, subject=subject, email=email)
        try:
            await member.save()
        except Exception:
            # Most likely the unique index rejecting a concurrent insert. Re-read
            # before deciding it is a real failure — if the record now exists,
            # the other request won and its member is the correct one.
            racing = await cls.find_by_subject(provider, subject)
            if racing is not None:
                logger.debug(
                    "member insert lost a race for %s:%s; using the existing record",
                    provider,
                    subject,
                )
                return racing
            raise
        return member

    def _prepare_save_data(self) -> Dict[str, Any]:
        data = super()._prepare_save_data()
        # Never persist an empty subject: it would key every anonymous request to
        # the same member.
        if not data.get("subject"):
            raise ValueError("member.subject must not be empty")
        if not data.get("provider"):
            raise ValueError("member.provider must not be empty")
        return data
