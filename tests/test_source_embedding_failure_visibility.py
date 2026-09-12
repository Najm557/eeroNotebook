"""Embedding failures must reach the member, not just the logs.

Text extraction and embedding run as two separate background jobs, and only the
first is linked to the source via its `command` field. A source whose text
extracted fine but whose embedding job failed therefore used to read
`completed` with no error and no embeddings — so an unreachable Inference
Gateway was invisible to the member (Requirement 3.5).

These tests pin the reporting: a failed embedding job surfaces as a failed
source, an extraction failure still takes precedence, and a failure that has
since been superseded by a successful re-embed is not reported.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.routers.sources import _apply_embedding_failure

EMBED_ERROR = (
    "Failed to generate embeddings using model 'eero-embed' (batch 1/1, 1 texts): "
    "Failed to generate embeddings: [Errno -2] Name or service not known"
)


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


class TestApplyEmbeddingFailure:
    """The reporting rule itself, independent of any endpoint."""

    def test_completed_extraction_with_failed_embedding_reports_failed(self):
        status, info = _apply_embedding_failure(
            "completed", {"started_at": "t0", "error": None}, EMBED_ERROR
        )

        assert status == "failed"
        assert info is not None
        assert "eero-embed" in info["error"]
        # Unrelated fields are preserved, not replaced.
        assert info["started_at"] == "t0"

    def test_no_embedding_failure_changes_nothing(self):
        original = {"started_at": "t0", "error": None}

        status, info = _apply_embedding_failure("completed", original, None)

        assert status == "completed"
        assert info is original

    def test_extraction_failure_takes_precedence(self):
        """The earlier fault is the one the member needs to see first."""
        original = {"error": "Could not extract text from file"}

        status, info = _apply_embedding_failure("failed", original, EMBED_ERROR)

        assert status == "failed"
        assert info is not None
        assert info["error"] == "Could not extract text from file"

    def test_error_text_is_truncated(self):
        status, info = _apply_embedding_failure("completed", None, "x" * 500)

        assert status == "failed"
        assert info is not None
        assert len(info["error"]) < 500

    def test_failure_without_prior_processing_info_still_reports(self):
        status, info = _apply_embedding_failure(None, None, EMBED_ERROR)

        assert status == "failed"
        assert info is not None
        assert "eero-embed" in info["error"]


class TestSourceDetailReportsEmbeddingFailure:
    """GET /api/sources/{id}"""

    @pytest.mark.asyncio
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    @patch("api.routers.sources._stamp_source_view", new_callable=AsyncMock)
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    async def test_failed_embedding_reported_as_failed_source(
        self, mock_get, mock_stamp, mock_repo_query, client
    ):
        mock_repo_query.return_value = []
        source = MagicMock(spec=[
            "id", "title", "topics", "asset", "full_text", "created", "updated",
            "command", "get_status", "get_processing_progress",
            "get_embedding_failure", "get_embedded_chunks",
        ])
        source.id = "source:abc"
        source.title = "Course notes"
        source.topics = []
        source.asset = None
        source.full_text = "some text"
        source.created = "2026-01-01"
        source.updated = "2026-01-01"
        source.command = "command:extract"
        source.get_status = AsyncMock(return_value="completed")
        source.get_processing_progress = AsyncMock(return_value={"error": None})
        source.get_embedding_failure = AsyncMock(return_value=EMBED_ERROR)
        source.get_embedded_chunks = AsyncMock(return_value=0)
        mock_get.return_value = source

        response = client.get("/api/sources/source:abc")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert "eero-embed" in body["processing_info"]["error"]
        assert body["embedded"] is False

    @pytest.mark.asyncio
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    @patch("api.routers.sources._stamp_source_view", new_callable=AsyncMock)
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    async def test_successful_embedding_still_reports_completed(
        self, mock_get, mock_stamp, mock_repo_query, client
    ):
        mock_repo_query.return_value = []
        source = MagicMock(spec=[
            "id", "title", "topics", "asset", "full_text", "created", "updated",
            "command", "get_status", "get_processing_progress",
            "get_embedding_failure", "get_embedded_chunks",
        ])
        source.id = "source:abc"
        source.title = "Course notes"
        source.topics = []
        source.asset = None
        source.full_text = "some text"
        source.created = "2026-01-01"
        source.updated = "2026-01-01"
        source.command = "command:extract"
        source.get_status = AsyncMock(return_value="completed")
        source.get_processing_progress = AsyncMock(return_value={"error": None})
        source.get_embedding_failure = AsyncMock(return_value=None)
        source.get_embedded_chunks = AsyncMock(return_value=3)
        mock_get.return_value = source

        response = client.get("/api/sources/source:abc")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["embedded"] is True


class TestSourceStatusReportsEmbeddingFailure:
    """GET /api/sources/{id}/status"""

    @pytest.mark.asyncio
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    async def test_message_names_the_embedding_failure(self, mock_get, client):
        source = MagicMock(spec=[
            "command", "get_status", "get_processing_progress",
            "get_embedding_failure",
        ])
        source.command = "command:extract"
        source.get_status = AsyncMock(return_value="completed")
        source.get_processing_progress = AsyncMock(return_value={"error": None})
        source.get_embedding_failure = AsyncMock(return_value=EMBED_ERROR)
        mock_get.return_value = source

        response = client.get("/api/sources/source:abc/status")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert "Embedding failed" in body["message"]
        assert "eero-embed" in body["message"]

    @pytest.mark.asyncio
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    async def test_extraction_failure_message_unchanged(self, mock_get, client):
        source = MagicMock(spec=[
            "command", "get_status", "get_processing_progress",
            "get_embedding_failure",
        ])
        source.command = "command:extract"
        source.get_status = AsyncMock(return_value="failed")
        source.get_processing_progress = AsyncMock(
            return_value={"error": "Could not extract text"}
        )
        source.get_embedding_failure = AsyncMock(return_value=EMBED_ERROR)
        mock_get.return_value = source

        response = client.get("/api/sources/source:abc/status")

        assert response.status_code == 200
        assert response.json()["message"] == "Source processing failed"

    @pytest.mark.asyncio
    @patch("api.routers.sources.Source.get", new_callable=AsyncMock)
    async def test_legacy_source_with_failed_embedding_reports_it(
        self, mock_get, client
    ):
        source = MagicMock(spec=["command", "get_embedding_failure"])
        source.command = None
        source.get_embedding_failure = AsyncMock(return_value=EMBED_ERROR)
        mock_get.return_value = source

        response = client.get("/api/sources/source:legacy/status")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert "Embedding failed" in body["message"]


class TestSourceListReportsEmbeddingFailure:
    """GET /api/sources — the card the member sees first."""

    def _row(self, embedded: bool, embedding_error: str | None):
        return {
            "id": "source:abc",
            "title": "Course notes",
            "topics": [],
            "asset": None,
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "insights_count": 0,
            "embedded": embedded,
            "embedding_error": embedding_error,
            "command": {
                "id": "command:extract",
                "status": "completed",
                "result": {"execution_metadata": {}},
                "error_message": "",
            },
        }

    @pytest.mark.asyncio
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    async def test_unembedded_source_with_failed_job_reports_failed(
        self, mock_repo_query, client
    ):
        mock_repo_query.return_value = [self._row(False, EMBED_ERROR)]

        response = client.get("/api/sources")

        assert response.status_code == 200
        entry = response.json()[0]
        assert entry["status"] == "failed"
        assert "eero-embed" in entry["processing_info"]["error"]

    @pytest.mark.asyncio
    @patch("api.routers.sources.repo_query", new_callable=AsyncMock)
    async def test_stale_failure_not_reported_once_embedded(
        self, mock_repo_query, client
    ):
        """A retry that succeeded must clear the report, not leave it stuck."""
        mock_repo_query.return_value = [self._row(True, EMBED_ERROR)]

        response = client.get("/api/sources")

        assert response.status_code == 200
        assert response.json()[0]["status"] == "completed"
