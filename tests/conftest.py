"""
Pytest configuration file.

This file ensures that the project root is in the Python path,
allowing tests to import from the api and open_notebook modules.
"""

import os
import sys
from pathlib import Path

# Ensure password auth is disabled for tests BEFORE any imports
# The PasswordAuthMiddleware skips auth when this env var is not set
# Set to empty string instead of deleting to prevent it from being reloaded
os.environ["OPEN_NOTEBOOK_PASSWORD"] = ""

# Load environment variables from .env file
# This must be done BEFORE any imports that depend on environment variables
from dotenv import load_dotenv

# Load .env file from project root
dotenv_path = Path(__file__).parent.parent / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)
    print(f"Loaded environment variables from {dotenv_path}")
else:
    print(f"Warning: .env file not found at {dotenv_path}")

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def authenticated_requests(request):
    """Let route tests reach their routes.

    Every request now resolves to a member or is refused (spec task 5.3), so the
    suite's several hundred route tests would otherwise all assert 401 instead of
    the behaviour they were written for.

    This bypass lives here rather than in the application because the alternative
    is a production fail-open: upstream skipped authentication entirely when no
    password was configured, and a deployment that forgets to set one would then
    serve every notebook to anyone. There is deliberately no environment variable
    that does this — the only way to switch it on is to be running pytest.

    Authentication itself is covered by tests/test_identity_boundary.py and
    tests/test_member_auth_middleware.py, which opt out with:

        @pytest.mark.no_auth_bypass
    """
    if request.node.get_closest_marker("no_auth_bypass"):
        yield
        return

    from open_notebook.domain.member import Member
    from open_notebook.identity import middleware as identity_middleware

    test_member = Member(provider="test-suite", subject="test-subject")

    async def dispatch(self, http_request, call_next):
        if (
            http_request.url.path in self.excluded_paths
            or http_request.method == "OPTIONS"
        ):
            return await call_next(http_request)
        http_request.state.member = test_member
        return await call_next(http_request)

    with patch.object(
        identity_middleware.MemberAuthMiddleware, "dispatch", dispatch
    ):
        yield


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_auth_bypass: exercise real authentication instead of the test bypass",
    )
