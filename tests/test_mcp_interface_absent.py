"""Pins the two facts spec task 6.6 settled about the MCP interface.

Requirement 7.2 asks that the MCP interface enforce the same access rules as the
REST API. It is satisfied here by there being exactly *one* enforcement point:
`open_notebook/domain/access.py`, consulted by the `require_*` functions the routes
depend on, behind `MemberAuthMiddleware`. `open-notebook-mcp` is a separately
published package that holds no database access and reaches this API over HTTP, so
it inherits those checks and cannot enforce less.

These tests are cheap on purpose. They do not re-measure the enforcement — task
6.2's and 6.4's suites do that, and 6.5's sweep covers the route table. They guard
the *decision* (ADR-010) against being undone silently:

1. An MCP server appearing in this repository would create the second enforcement
   point Requirement 7.2 is actually about, and would need to re-derive
   owner-or-Share for itself.
2. The documentation page's upstream instructions still work against this API and
   sign the client in as the operator, so the notice warning against them is the
   only thing standing between a reader and that configuration.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
FIRST_PARTY = ("api", "open_notebook", "commands")

MCP_PAGE = REPO / "docs" / "5-CONFIGURATION" / "mcp-integration.md"
ADR = REPO / "docs" / "7-DEVELOPMENT" / "decisions" / "ADR-010-mcp-not-offered-at-v1.md"


def first_party_sources():
    for package in FIRST_PARTY:
        yield from (REPO / package).rglob("*.py")


class TestNoMcpServerInThisRepository:
    """The structural half of Requirement 7.2."""

    # `FastMCP`/`mcp.server` is how every Python MCP server is declared, including
    # the published open-notebook-mcp. Matched as an import rather than as a bare
    # word so a comment mentioning MCP does not fail the test.
    SERVER_IMPORT = re.compile(
        r"^\s*(?:from\s+(?:mcp|fastmcp)[\w.]*\s+import|import\s+(?:mcp|fastmcp)\b)",
        re.MULTILINE,
    )

    def test_no_module_declares_an_mcp_server(self) -> None:
        offenders = [
            str(path.relative_to(REPO))
            for path in first_party_sources()
            if self.SERVER_IMPORT.search(path.read_text(encoding="utf-8"))
        ]
        assert not offenders, (
            "An MCP server in this repository would be a second place access is "
            f"enforced — see ADR-010 before adding one: {offenders}"
        )

    def test_the_project_publishes_no_mcp_entry_point(self) -> None:
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        assert "mcp" not in pyproject.lower().replace("open-notebook-mcp", ""), (
            "pyproject.toml gained an MCP dependency or script; ADR-010 assumes "
            "this repository ships no MCP server"
        )


class TestTheDocumentationWarnsRatherThanOmits:
    """The upstream instructions still work, so silence would not be enough."""

    @pytest.mark.parametrize(
        "marker",
        [
            # The status, stated before the instructions a reader would copy.
            "Not available on eeroNotebook at v1",
            # Why silence is insufficient: the credential the page names is the
            # operator's, and it authenticates as the admin member.
            "operator",
            # Where the reasoning lives.
            "ADR-010-mcp-not-offered-at-v1.md",
        ],
    )
    def test_the_page_carries_the_v1_status(self, marker: str) -> None:
        text = MCP_PAGE.read_text(encoding="utf-8")
        notice, _, rest = text.partition("## What is MCP?")
        assert rest, "the page's upstream body is gone; revisit this test"
        assert marker in notice, (
            f"{MCP_PAGE.name} must say {marker!r} above its configuration "
            "instructions — they still work, and grant operator access"
        )

    def test_the_decision_record_exists_and_is_indexed(self) -> None:
        assert ADR.exists(), "ADR-010 is what the MCP page and security.md point at"
        index = (ADR.parent / "README.md").read_text(encoding="utf-8")
        assert ADR.name in index, "ADR-010 is missing from the decision log index"
