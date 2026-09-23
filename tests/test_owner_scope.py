"""Owner scope on the data model: migration 25 and `Notebook.owner`.

Requirements 5.1, 5.2, 5.3, 7.5.

Migration 25 is the foundation every later access check stands on, and almost
none of it is reachable from application code — it is schema. So these tests read
the migration file itself and assert the specific properties that would be
expensive to lose:

- ownership points at `member`, not at a provider's user id (ADR 0003)
- `share` grants exactly one role, so there is nothing to widen to (Req 6.6)
- the up path has a matching down path for everything it defines
- the migration is registered, because migrations are hard-coded and a file on
  its own does nothing

The behavioural half of the migration was verified against a real SurrealDB 2.6.5
rather than here — schema semantics are the database's, not Python's, and a test
that mocked them would only assert this file's guesses. What is verified here is
the part that rots: the text of the migration, and the model field that reads it.
"""

import re
from pathlib import Path

import pytest
from surrealdb import RecordID

from open_notebook.database.async_migrate import AsyncMigrationManager
from open_notebook.database.repository import ensure_record_id
from open_notebook.domain.notebook import Notebook
from open_notebook.identity import ADMIN_PROVIDER, ADMIN_SUBJECT

MIGRATIONS = Path(__file__).parent.parent / "open_notebook" / "database" / "migrations"
UP = (MIGRATIONS / "25.surrealql").read_text(encoding="utf-8")
DOWN = (MIGRATIONS / "25_down.surrealql").read_text(encoding="utf-8")


def statements(migration: str) -> str:
    """The migration as AsyncMigration.from_file will actually execute it.

    Lines starting with `--` are dropped and the rest are joined with spaces, so
    this is the real input to the database - not the annotated file.
    """
    lines = [
        line.strip()
        for line in migration.split("\n")
        if line.strip() and not line.strip().startswith("--")
    ]
    return " ".join(lines)


UP_SQL = statements(UP)
DOWN_SQL = statements(DOWN)


class TestMigrationIsRegistered:
    """A migration file that nobody runs is not a migration."""

    def test_migration_25_is_registered_up_and_down(self):
        # `>=` rather than `== 25`: later tasks add migrations of their own (6.4
        # took 26), and this file is about migration 25. That migration being
        # registered *as number 25* is what matters, and
        # test_registered_migration_25_is_the_file_on_disk asserts it by index.
        # The newest migration's own test owns the exact count.
        manager = AsyncMigrationManager()
        assert len(manager.up_migrations) >= 25, (
            "migration 25 is not registered in AsyncMigrationManager; migrations "
            "are hard-coded rather than discovered, so the file alone does nothing"
        )
        assert len(manager.down_migrations) >= 25

    def test_every_up_migration_has_a_down_migration(self):
        manager = AsyncMigrationManager()
        assert len(manager.up_migrations) == len(manager.down_migrations), (
            "the runner indexes both lists by version, so a mismatch rolls back "
            "the wrong migration"
        )

    def test_registered_migration_25_is_the_file_on_disk(self):
        manager = AsyncMigrationManager()
        assert manager.up_migrations[24].sql == UP_SQL
        assert manager.down_migrations[24].sql == DOWN_SQL


class TestNoTrailingCommentSwallowsTheMigration:
    """`from_file` joins every line into one, so a trailing `--` comments out the
    rest of the migration and the statements after it silently never run.

    Checked across all migrations, not just 25: the hazard belongs to the loader.
    """

    def test_no_migration_has_sql_followed_by_a_comment_on_one_line(self):
        offenders = []
        for path in sorted(MIGRATIONS.glob("*.surrealql")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").split("\n"), start=1
            ):
                stripped = line.strip()
                if not stripped or stripped.startswith("--"):
                    continue
                # A `--` after SQL on the same line would comment out everything
                # that follows once the file is collapsed into a single line.
                if "--" in stripped:
                    offenders.append(f"{path.name}:{number}: {stripped}")
        assert not offenders, (
            "these lines put a comment after SQL, which will comment out every "
            f"statement that follows once the file is joined into one line: {offenders}"
        )


class TestOwnershipPointsAtTheLocalIdentity:
    """Requirement 5.1, and ADR 0003's boundary."""

    def test_owner_is_a_record_link_to_member(self):
        assert re.search(
            r"DEFINE FIELD IF NOT EXISTS owner ON TABLE notebook TYPE option<record<member>>",
            UP_SQL,
        ), "notebook.owner must be a record<member> link"

    def test_owner_names_no_identity_provider(self):
        """Ownership must never point at a provider's user id, or replacing the
        provider would rewrite every owned Notebook (Requirement 4.4)."""
        for provider_term in ("gotrue", "jwt", "sub ", "auth.users", "supabase"):
            assert provider_term not in UP_SQL.lower(), (
                f"migration 25 names {provider_term!r}; ownership must reference "
                "`member` and know nothing about who authenticated it"
            )

    def test_owner_is_indexed(self):
        """"Which Notebooks does this member own" runs on every list request."""
        assert "DEFINE INDEX IF NOT EXISTS notebook_owner ON TABLE notebook" in UP_SQL

    def test_no_other_table_gains_an_owner(self):
        """Sources, notes and Study_Artifacts inherit through `reference` and
        `artifact`. A second owner column would be a second place for an access
        check to consult, and the two would drift on an upstream merge."""
        owners = re.findall(r"DEFINE FIELD[^;]*?owner ON TABLE (\w+)", UP_SQL)
        assert owners == ["notebook"], (
            f"owner is defined on {owners}; it belongs on notebook alone"
        )


class TestShareRelation:
    """Requirements 6.1, 6.2 and 6.6, defined here so task 6.2 can enforce
    owner-or-Share from the outset."""

    def test_share_is_a_relation_from_member_to_notebook(self):
        assert (
            "DEFINE TABLE IF NOT EXISTS share TYPE RELATION FROM member TO notebook"
            in UP_SQL
        )

    def test_share_grants_exactly_one_role(self):
        """Requirement 6.6 met structurally: there is no role to widen to, and a
        second one cannot be created without passing this ASSERT."""
        role = re.search(r"DEFINE FIELD[^;]*?role ON TABLE share[^;]*;", UP_SQL)
        assert role is not None, "share.role is not defined"
        assert "ASSERT $value = 'viewer'" in role.group(0), (
            "share.role must be constrained to 'viewer' alone; without the ASSERT "
            "a wider role is one UPDATE away"
        )

    def test_one_share_per_member_per_notebook(self):
        """Two Shares for one pair would make revocation partial: deleting one
        would look like success while the other still granted access."""
        index = re.search(
            r"DEFINE INDEX IF NOT EXISTS share_member_notebook ON TABLE share[^;]*;",
            UP_SQL,
        )
        assert index is not None, "share has no uniqueness index"
        assert "UNIQUE" in index.group(0)

    def test_a_share_cannot_outlive_its_notebook_or_its_member(self):
        """The same class of defect as task 3.4's orphaned Sources: a grant
        pointing at a record that no longer exists. Handled at the database so no
        delete path can forget."""
        assert "share_cleanup_on_notebook_delete ON TABLE notebook" in UP_SQL
        assert "share_cleanup_on_member_delete ON TABLE member" in UP_SQL


class TestPreExistingContentGetsANamedOwner:
    """Requirement 7.5."""

    def test_the_backfill_uses_the_admin_identity_the_application_resolves(self):
        """If these drift apart the migration assigns content to an identity
        nobody can sign in as, and Requirement 7.5 is met in name only."""
        assert f"provider = '{ADMIN_PROVIDER}'" in UP_SQL
        assert f"subject = '{ADMIN_SUBJECT}'" in UP_SQL

    def test_unowned_notebooks_are_assigned(self):
        assert re.search(r"UPDATE notebook SET owner = \$admin WHERE owner IS NONE", UP_SQL)

    def test_the_backfill_cannot_write_a_null_owner(self):
        """On an empty database there is no admin to assign, and the statement
        must be a no-op rather than writing NONE over the field."""
        update = re.search(r"UPDATE notebook SET owner = \$admin[^;]*;", UP_SQL)
        assert update is not None
        assert "$admin != NONE" in update.group(0), (
            "the backfill is unguarded; on a database with no admin member it "
            "would write NONE into owner"
        )

    def test_orphans_are_adopted_rather_than_deleted(self):
        """Task 3.4 found four Sources with no Notebook. They are fail-closed for
        access - no Notebook means no access check can grant them - but once task
        6.2 scopes every route through a Notebook they become unreachable and
        undeletable. Adopting them narrows access and keeps them visible;
        deleting them in a startup migration would be unrecoverable."""
        assert "array::len(->reference) == 0" in UP_SQL, (
            "the migration does not look for orphaned Sources"
        )
        assert "array::len(->artifact) == 0" in UP_SQL, (
            "the migration does not look for orphaned notes"
        )
        assert "RELATE $orphan->reference->$recovery" in UP_SQL
        assert "DELETE source" not in UP_SQL and "DELETE note" not in UP_SQL, (
            "migration 25 must not delete content; it runs automatically on API "
            "startup, where nobody has agreed to lose anything"
        )

    def test_a_fresh_database_gains_no_phantom_identity(self):
        """The admin row is created only when there is something to assign, so
        installing eeroNotebook from scratch does not manufacture an owner."""
        create = re.search(r"LET \$new_admin = IF[^;]*;", UP_SQL)
        assert create is not None, "the admin member is created unconditionally"
        assert "$pre_existing > 0" in create.group(0)


class TestDownMigrationReversesTheSchema:
    def test_every_object_added_to_a_surviving_table_is_removed(self):
        """Dropping a table takes its own fields and indexes with it, so only the
        objects added to tables that outlive the rollback need an explicit REMOVE.
        Those are the ones that would otherwise leave `notebook` and `member`
        carrying half of migration 25 after a rollback.
        """
        dropped_tables = set(re.findall(r"REMOVE TABLE IF EXISTS (\w+)", DOWN_SQL))
        assert dropped_tables, "25_down drops no table; the share relation would survive"

        defined = re.findall(
            r"DEFINE (?:FIELD|INDEX|EVENT) IF NOT EXISTS (\w+) ON (?:TABLE )?(\w+)", UP_SQL
        )
        removed = {
            name
            for name, _ in re.findall(
                r"REMOVE (?:FIELD|INDEX|EVENT) IF EXISTS (\w+) ON (?:TABLE )?(\w+)", DOWN_SQL
            )
        }
        missing = {
            f"{name} on {table}"
            for name, table in defined
            if table not in dropped_tables and name not in removed
        }
        assert not missing, (
            f"migration 25 adds {sorted(missing)} to a table that survives the "
            "rollback, with nothing in 25_down to remove it"
        )

    def test_the_share_table_itself_is_dropped(self):
        assert "REMOVE TABLE IF EXISTS share" in DOWN_SQL

    def test_the_down_path_destroys_no_content(self):
        """A rollback must not be a data loss event. The recovery Notebook, the
        owner values and the admin member are all deliberately left in place."""
        for destructive in ("DELETE ", "REMOVE TABLE IF EXISTS notebook", "REMOVE TABLE IF EXISTS source", "REMOVE TABLE IF EXISTS note", "REMOVE TABLE IF EXISTS member"):
            assert destructive not in DOWN_SQL, (
                f"25_down contains {destructive!r}, which would destroy content"
            )

    def test_events_are_removed_before_the_table_they_fire_against(self):
        assert DOWN_SQL.index("REMOVE EVENT IF EXISTS share_cleanup_on_notebook_delete") < DOWN_SQL.index(
            "REMOVE TABLE IF EXISTS share"
        ), "dropping `share` before its cleanup events would fire them against a table that is halfway gone"


class TestNotebookOwnerField:
    """The model half. `owner` is the only field task 6.2's access check reads."""

    def test_owner_defaults_to_none(self):
        notebook = Notebook(name="Biology", description="course material")
        assert notebook.owner is None

    def test_owner_accepts_a_member_id(self):
        notebook = Notebook(name="Biology", description="", owner="member:abc123")
        assert notebook.owner == "member:abc123"

    def test_owner_accepts_the_record_id_surrealdb_returns(self):
        """Loading a Notebook yields a RecordID for a `record<member>` field, and
        an access check compares against a string."""
        notebook = Notebook(
            name="Biology", description="", owner=RecordID("member", "abc123")
        )
        assert notebook.owner == "member:abc123"
        assert isinstance(notebook.owner, str)

    @pytest.mark.parametrize("empty", [None, "", 0, False])
    def test_an_empty_owner_is_none_and_never_a_truthy_value(self, empty):
        """An owner of `""` would be a half-state: falsy, unequal to every member
        id, but not None either. Only one absent value, so task 6.2 has one case
        to fail closed on."""
        notebook = Notebook(name="Biology", description="", owner=empty)
        assert notebook.owner is None

    def test_owner_is_written_as_a_record_link(self):
        """`record<member>` refuses a plain string, so the save path must convert."""
        notebook = Notebook(name="Biology", description="", owner="member:abc123")
        data = notebook._prepare_save_data()
        assert isinstance(data["owner"], RecordID)
        assert str(data["owner"]) == "member:abc123"

    def test_saving_without_an_owner_cannot_unown_a_notebook(self):
        """The safety this whole design leans on: `repo_update` issues
        `UPDATE $target MERGE $data`, so dropping the key leaves the stored owner
        alone. If `owner` were written as NONE instead, renaming a Notebook would
        silently orphan it - and an owner-less Notebook is reachable by nobody."""
        notebook = Notebook(name="Biology", description="course material")
        assert "owner" not in notebook._prepare_save_data()

    def test_owner_round_trips_for_the_id_shapes_surrealdb_generates(self):
        """Ownership written to the wrong record is a silent access failure, so
        the id must survive the model unchanged - and must survive it repeatedly,
        since a Notebook is saved every time it is renamed or archived."""
        for key in (
            "r3on8d5wgnmhrddvzkgi",
            "abc123",
            "a",
            "with_underscore",
            "AbC123xyz",
        ):
            member_id = f"member:{key}"
            notebook = Notebook(name="n", description="", owner=member_id)
            assert notebook.owner == member_id
            written = str(notebook._prepare_save_data()["owner"])
            assert written == member_id
            # Saving the value that came back must not transform it again.
            resaved = Notebook(name="n", description="", owner=written)
            assert str(resaved._prepare_save_data()["owner"]) == member_id

    def test_a_numeric_member_key_is_escaped_and_does_not_round_trip(self):
        """A known limitation of `ensure_record_id`, pinned rather than fixed.

        `member:9999` parses to `member:⟨9999⟩` - SurrealDB escaping a key that
        would otherwise read as an integer - and parsing that again escapes it a
        second time. So a Notebook owned by a numerically-keyed member would point
        somewhere new on every save.

        It is left alone for two reasons. SurrealDB generates twenty-character
        alphanumeric keys, so an all-digit one is theoretical rather than
        expected; and the consequence is an owner pointing at a record that does
        not exist, which fails closed - the Notebook becomes unreachable, not
        public. `ensure_record_id` is shared with every other record link in the
        codebase, so changing it belongs to its own task.

        This test exists so the behaviour is documented where somebody debugging
        it will look, and so it fails loudly if the escaping rule ever widens to
        the ids that are actually issued.
        """
        once = str(ensure_record_id("member:9999"))
        assert once == "member:⟨9999⟩"
        assert str(ensure_record_id(once)) != once, (
            "escaping has become idempotent; the limitation above is fixed and "
            "this test should be replaced by the round-trip one"
        )
