"""Baseline validation of the deployed stack, driven through the REST API.

This is spec task 3.4's gate: it proves the *unmodified* upstream deployment
ingests all three kinds of Source, reports each one's fate individually,
answers only from the Sources it holds, and cites what it used — before task 5
starts diverging from upstream. Debugging a fork and a deployment at the same
time is the thing this ordering exists to avoid.

The checks are the ones already catalogued in `.codex/agents/smoke-e2e.toml`
(health, notebook, sources, processing, embeddings, chat, ask, vector search,
delete-and-cascade), narrowed to the requirements this task is the gate for.
Podcasts are a v1 non-goal and transformations belong to task 8.5, so the two
catalogue checks covering them are deliberately absent.

Opt-in by design: with EERONOTEBOOK_API_URL or EERONOTEBOOK_API_TOKEN unset,
the whole module skips, so `uv run pytest tests/` stays green with no stack
running. Run it against a deployment with, for example:

    EERONOTEBOOK_API_URL=http://10.17.8.52:5055 \\
    EERONOTEBOOK_API_TOKEN="$(...read it from the host, never the repo...)" \\
    uv run pytest tests/integration/test_baseline.py -v

Two preconditions are the deployment's, not this test's:

- The surreal-commands worker must be running. Extraction and embedding are
  background jobs; with no worker they queue forever and report nothing, so
  this module fails fast and names the worker rather than sitting out its
  ingestion timeout.
- OPEN_NOTEBOOK_WORKER_MAX_TASKS=1 on the Dev Server, so ingestion is
  serialised. Expect minutes, not seconds, and read the timeouts accordingly.

Everything the run creates is named with a run-scoped marker and is deleted in
teardown, because task 6.1 later has to migrate whatever tasks 3-4 left behind
to a named Notebook owner.
"""

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
import pytest

API_URL = (os.getenv("EERONOTEBOOK_API_URL") or "").rstrip("/")
API_TOKEN = os.getenv("EERONOTEBOOK_API_TOKEN") or ""

pytestmark = pytest.mark.skipif(
    not (API_URL and API_TOKEN),
    reason=(
        "live-stack test: set EERONOTEBOOK_API_URL and EERONOTEBOOK_API_TOKEN "
        "to run it against a deployment"
    ),
)

# Ingestion is serialised behind one worker on a host that shares its inference
# backend with another stack, so these are deliberately generous.
INGEST_TIMEOUT = float(os.getenv("EERONOTEBOOK_INGEST_TIMEOUT", "900"))
# How long every Source may sit unclaimed before we conclude nothing is
# consuming the queue. Reaching this means the worker, not the timeout.
WORKER_SILENT_AFTER = 180.0
# Statuses that mean "no worker has touched this yet". `new` is the status a
# freshly submitted command record carries; it is not a synonym for progress,
# and reading it as one is what turns a deaf worker into a 15-minute timeout.
UNCLAIMED = (None, "new", "queued")
POLL_INTERVAL = 5.0
# A single ask traverses strategy, per-search answers and a final synthesis
# against a 14B model.
LLM_TIMEOUT = 600.0

# A fact no model can supply from its own knowledge: real spacing schedules are
# 1/3/7 days, so an answer that says 23 days can only have read the Source.
UPLOAD_MARKER = "eeroNotebook baseline retention protocol"
UPLOAD_BODY = f"""# Baseline retention protocol

The {UPLOAD_MARKER} schedules exactly three review passes for any newly studied
topic: the first 2 days after initial study, the second 9 days after, and the
third 23 days after. A pass that is recalled without hesitation is not
repeated; a pass that fails restarts the sequence from the first interval.

Each pass is a free-recall attempt written from memory before the material is
reopened, because recognising a page is not the same as being able to
reconstruct it.
"""

WEB_PAGE_URL = "https://en.wikipedia.org/wiki/Spaced_repetition"
# Public TED talk with a published transcript; the video path depends on the
# transcript, not on audio transcription, which is not configured here.
VIDEO_URL = "https://www.youtube.com/watch?v=arj7oStGLkU"
# .invalid is reserved by RFC 2606 and can never resolve, so this Source's
# failure is guaranteed and is entirely its own.
UNRESOLVABLE_URL = "https://eeronotebook-baseline.invalid/no-such-page"

COVERED_QUESTION = (
    f"According to the material, what review intervals does the {UPLOAD_MARKER} use?"
)
UNCOVERED_QUESTION = (
    "What are the enzymatic steps of the Krebs cycle and which enzymes catalyse them?"
)
# Substantive Krebs-cycle content. Any of these in an answer means the model
# answered from its own knowledge, which is what Requirement 2.5 forbids.
KREBS_LEAKAGE = (
    "citrate",
    "isocitrate",
    "succinate",
    "fumarate",
    "malate",
    "oxaloacetate",
    "acetyl-coa",
    "citrate synthase",
    "aconitase",
    "dehydrogenase",
)
NON_COVERAGE_PHRASES = (
    "do not contain",
    "does not contain",
    "do not cover",
    "does not cover",
    "not covered",
    "do not address",
    "does not address",
    "do not mention",
    "does not mention",
    "no information",
    "nothing relevant",
    "not discussed",
    "do not include",
    "does not include",
    "no relevant",
    "not found in",
    # "The retrieved documents did not provide information about ..." is the
    # phrasing the deployed model actually used. It is a statement of
    # non-coverage by any reading; the list was simply short of it.
    "did not provide",
    "do not provide",
    "does not provide",
)

CITATION_RE = re.compile(r"\[([a-z_]+:[a-z0-9]+)\]")


@dataclass
class IngestedSource:
    """One Source we created, and how it ended up."""

    label: str
    kind: str
    source_id: str
    create_status: int
    status: Optional[str] = None
    error: Optional[str] = None
    full_text_len: int = 0
    embedded: bool = False
    embedded_chunks: int = 0
    asset: Optional[dict] = None
    settled_after: float = 0.0


@dataclass
class Baseline:
    """Everything the live run established, gathered once per session."""

    notebook_id: str
    health: dict
    defaults: dict
    sources: dict[str, IngestedSource]
    submit_order: list[str] = field(default_factory=list)
    ingest_seconds: float = 0.0
    answers: dict[str, str] = field(default_factory=dict)
    vector_results: list[dict] = field(default_factory=list)
    chat_reply: str = ""
    deleted: dict[str, Any] = field(default_factory=dict)


def _client(timeout: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API_URL,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
    )


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


async def _create_notebook(client: httpx.AsyncClient, marker: str) -> str:
    response = await client.post(
        "/api/notebooks",
        json={
            "name": f"Baseline validation {marker}",
            "description": "Spec task 3.4 gate. Created by tests; deleted on teardown.",
        },
    )
    response.raise_for_status()
    return str(response.json()["id"])


async def _create_source(
    client: httpx.AsyncClient,
    notebook_id: str,
    label: str,
    kind: str,
    *,
    data: dict[str, str],
    files: Optional[dict[str, tuple[str, bytes, str]]] = None,
) -> IngestedSource:
    """Create one Source. Multipart, as the API requires, with embedding on."""
    payload = {
        "type": kind,
        "notebooks": f'["{notebook_id}"]',
        "async_processing": "true",
        "embed": "true",
        **data,
    }
    response = await client.post("/api/sources", data=payload, files=files)
    response.raise_for_status()
    return IngestedSource(
        label=label,
        kind=kind,
        source_id=str(response.json()["id"]),
        create_status=response.status_code,
        asset=response.json().get("asset"),
    )


async def _read_source(client: httpx.AsyncClient, source_id: str) -> dict:
    response = await client.get(f"/api/sources/{source_id}")
    response.raise_for_status()
    return dict(response.json())


def _is_settled(body: dict) -> bool:
    """A Source has settled when its fate can no longer change.

    `failed` is terminal. `completed` is not on its own: extraction and
    embedding are separate jobs and only the first is linked to the Source, so
    a Source reads `completed` the moment its text lands and before its
    embeddings exist. Waiting for chunks is what makes Requirement 1.3
    observable rather than assumed.
    """
    status = body.get("status")
    if status == "failed":
        return True
    return status == "completed" and int(body.get("embedded_chunks") or 0) > 0


async def _settle_sources(
    client: httpx.AsyncClient, sources: list[IngestedSource]
) -> float:
    """Poll until every Source settles. Raises when the worker is not consuming."""
    started = time.monotonic()
    pending = {s.source_id: s for s in sources}
    seen_progress = False

    while pending:
        elapsed = time.monotonic() - started
        for source_id, source in list(pending.items()):
            body = await _read_source(client, source_id)
            source.status = body.get("status")
            if source.status not in UNCLAIMED:
                seen_progress = True
            if _is_settled(body):
                source.error = (body.get("processing_info") or {}).get("error")
                source.full_text_len = len(body.get("full_text") or "")
                source.embedded = bool(body.get("embedded"))
                source.embedded_chunks = int(body.get("embedded_chunks") or 0)
                source.asset = body.get("asset")
                source.settled_after = elapsed
                del pending[source_id]

        if not pending:
            break

        if not seen_progress and elapsed > WORKER_SILENT_AFTER:
            raise RuntimeError(
                f"No Source was claimed within {WORKER_SILENT_AFTER:.0f}s; all are "
                f"still {sorted(str(s.status) for s in pending.values())}. Extraction "
                "and embedding are background jobs, so nothing is consuming the "
                "queue. The worker process being alive is not sufficient: it "
                "notices new work through a SurrealDB LIVE query, which does not "
                "survive a database restart, and it logs nothing when that "
                "happens. Restart the app container and retry."
            )

        if elapsed > INGEST_TIMEOUT:
            stuck = {s.label: s.status for s in pending.values()}
            raise RuntimeError(
                f"Sources still unsettled after {INGEST_TIMEOUT:.0f}s: {stuck}. "
                "Worker concurrency is 1 on the Dev Server, so raise "
                "EERONOTEBOOK_INGEST_TIMEOUT before suspecting a fault."
            )

        await asyncio.sleep(POLL_INTERVAL)

    return time.monotonic() - started


async def _ask(client: httpx.AsyncClient, question: str, model_id: str) -> str:
    response = await client.post(
        "/api/search/ask/simple",
        json={
            "question": question,
            "strategy_model": model_id,
            "answer_model": model_id,
            "final_answer_model": model_id,
        },
    )
    response.raise_for_status()
    return str(response.json()["answer"])


async def _chat(
    client: httpx.AsyncClient,
    notebook_id: str,
    message: str,
    source_ids: list[str],
) -> str:
    """Chat is the notebook-scoped path: its context is built from one Notebook.

    Every Source is requested as "full content" deliberately. An empty
    `context_config` is not a neutral default — it takes the *short* context
    path, which carries only each Source's id, title and insights and no text
    at all. Asked a factual question on that context the model invents an
    answer and cites a Source for it, so the check would be measuring
    hallucination rather than grounding.
    """
    session = await client.post("/api/chat/sessions", json={"notebook_id": notebook_id})
    session.raise_for_status()

    context = await client.post(
        "/api/chat/context",
        json={
            "notebook_id": notebook_id,
            "context_config": {
                "sources": {source_id: "full content" for source_id in source_ids},
            },
        },
    )
    context.raise_for_status()

    reply = await client.post(
        "/api/chat/execute",
        json={
            "session_id": session.json()["id"],
            "message": message,
            "context": context.json()["context"],
        },
    )
    reply.raise_for_status()
    ai_messages = [
        m.get("content", "")
        for m in reply.json().get("messages", [])
        if m.get("type") == "ai"
    ]
    return ai_messages[-1] if ai_messages else ""


async def _provision() -> Baseline:
    """Stand up the whole baseline once, and tear it down if setup fails."""
    marker = time.strftime("%Y%m%d-%H%M%S")
    notebook_id = ""

    async with _client(timeout=LLM_TIMEOUT) as client:
        health = await client.get("/health")
        health.raise_for_status()

        defaults = await client.get("/api/models/defaults")
        defaults.raise_for_status()
        defaults_body = dict(defaults.json())
        model_id = defaults_body.get("default_chat_model")
        if not model_id:
            raise RuntimeError(
                "No default chat model configured; spec task 3.3 sets these."
            )

        try:
            notebook_id = await _create_notebook(client, marker)

            # Submit order matters for Requirement 1.5: the unresolvable URL goes
            # in second, so the web page and the video are queued behind a Source
            # that is certain to fail. Both completing is what "does not block"
            # means on a worker that runs one task at a time.
            document = await _create_source(
                client,
                notebook_id,
                "document",
                "upload",
                data={"title": f"Retention protocol {marker}"},
                files={
                    "file": (
                        f"retention-protocol-{marker}.md",
                        UPLOAD_BODY.encode("utf-8"),
                        "text/markdown",
                    )
                },
            )
            unresolvable = await _create_source(
                client,
                notebook_id,
                "unresolvable",
                "link",
                data={"url": UNRESOLVABLE_URL},
            )
            web_page = await _create_source(
                client, notebook_id, "web_page", "link", data={"url": WEB_PAGE_URL}
            )
            video = await _create_source(
                client, notebook_id, "video", "link", data={"url": VIDEO_URL}
            )

            ordered = [document, unresolvable, web_page, video]
            ingest_seconds = await _settle_sources(client, ordered)

            vector = await client.post(
                "/api/search",
                json={"query": UPLOAD_MARKER, "type": "vector", "limit": 10},
            )
            vector.raise_for_status()

            answers = {
                "covered": await _ask(client, COVERED_QUESTION, model_id),
                "uncovered": await _ask(client, UNCOVERED_QUESTION, model_id),
            }
            chat_reply = await _chat(
                client,
                notebook_id,
                "According to the sources, how many days after initial study is the "
                f"third review pass of the {UPLOAD_MARKER}?",
                [s.source_id for s in ordered if s.status == "completed"],
            )

            return Baseline(
                notebook_id=notebook_id,
                health=dict(health.json()),
                defaults=defaults_body,
                sources={s.label: s for s in ordered},
                submit_order=[s.label for s in ordered],
                ingest_seconds=ingest_seconds,
                answers=answers,
                vector_results=list(vector.json().get("results") or []),
                chat_reply=chat_reply,
            )
        except BaseException:
            if notebook_id:
                await _delete_notebook(client, notebook_id)
            raise


async def _delete_notebook(client: httpx.AsyncClient, notebook_id: str) -> None:
    """Idempotent: teardown must not care whether a test already deleted it."""
    try:
        await client.delete(
            f"/api/notebooks/{notebook_id}",
            params={"delete_exclusive_sources": "true"},
        )
    except httpx.HTTPError:
        pass


async def _teardown(baseline: Baseline) -> None:
    async with _client() as client:
        await _delete_notebook(client, baseline.notebook_id)


@pytest.fixture(scope="session")
def baseline():
    """One live run for the whole module.

    Provisioning is driven by asyncio.run rather than a session-scoped event
    loop: every test then gets its own loop and its own client, and nothing
    async is shared across loops.
    """
    state = asyncio.run(_provision())
    try:
        yield state
    finally:
        asyncio.run(_teardown(state))


class TestDeploymentIsReady:
    """Catalogue phase 0. A red check here invalidates everything below it."""

    def test_api_reports_healthy(self, baseline: Baseline):
        assert baseline.health == {"status": "healthy"}

    def test_chat_and_embedding_models_are_configured(self, baseline: Baseline):
        assert baseline.defaults.get("default_chat_model")
        assert baseline.defaults.get("default_embedding_model")


class TestSourceIngestion:
    """Requirements 1.1, 1.2, 1.3."""

    def test_all_three_kinds_of_source_are_accepted(self, baseline: Baseline):
        """Requirement 1.1: uploaded documents, web page URLs, video URLs."""
        document = baseline.sources["document"]
        web_page = baseline.sources["web_page"]
        video = baseline.sources["video"]

        for source in (document, web_page, video):
            assert source.create_status == 200, source.label
            assert source.source_id.startswith("source:"), source.label

        assert (document.asset or {}).get("file_path"), "upload kept no file path"
        assert (web_page.asset or {}).get("url") == WEB_PAGE_URL
        assert (video.asset or {}).get("url") == VIDEO_URL

    @pytest.mark.parametrize("label", ["document", "web_page", "video"])
    def test_each_source_becomes_retrievable_text(
        self, baseline: Baseline, label: str
    ):
        """Requirements 1.2 and 1.3: processed, reported, embedded, searchable."""
        source = baseline.sources[label]

        assert source.status == "completed", (
            f"{label} ended {source.status}: {source.error}"
        )
        assert source.full_text_len > 0, f"{label} extracted no text"
        assert source.embedded is True, f"{label} was not embedded"
        assert source.embedded_chunks > 0, f"{label} produced no chunks"


class TestPerSourceIsolation:
    """Requirement 1.5."""

    def test_unresolvable_url_fails_and_retains_its_reason(self, baseline: Baseline):
        failed = baseline.sources["unresolvable"]

        assert failed.status == "failed"
        assert failed.error, "a failed Source must retain why it failed"

    def test_the_failure_did_not_block_the_sources_queued_behind_it(
        self, baseline: Baseline
    ):
        """The queue runs one task at a time, so order carries the argument.

        The unresolvable URL was submitted before the web page and the video.
        Both of those completing means its failure neither stopped the worker
        nor leaked onto its neighbours.
        """
        order = baseline.submit_order
        assert order.index("unresolvable") < order.index("web_page")
        assert order.index("unresolvable") < order.index("video")

        for label in ("document", "web_page", "video"):
            neighbour = baseline.sources[label]
            assert neighbour.status == "completed", label
            assert not neighbour.error, f"{label} inherited an error it did not cause"


class TestVectorSearch:
    """Requirement 1.3: embedded Sources are reachable by vector search."""

    def test_vector_search_returns_scored_results(self, baseline: Baseline):
        assert baseline.vector_results, "vector search returned nothing"

        for result in baseline.vector_results:
            similarity = float(result["similarity"])
            assert 0.0 < similarity <= 1.0, result

    def test_results_reach_the_sources_this_run_created(self, baseline: Baseline):
        ours = {s.source_id for s in baseline.sources.values()}
        hits = {
            str(r.get("parent_id") or r.get("id")) for r in baseline.vector_results
        }

        assert ours & hits, f"none of {ours} appeared in {hits}"


class TestGroundedAnswers:
    """Requirements 2.1, 2.2, 2.3, 2.5."""

    def test_answer_carries_citations(self, baseline: Baseline):
        """Requirement 2.2, first half: the answer says what it relied on."""
        answer = baseline.answers["covered"]

        assert answer.strip(), "no answer produced"
        assert CITATION_RE.findall(answer), f"answer carries no citation: {answer!r}"

    @pytest.mark.asyncio
    async def test_every_citation_resolves_to_a_real_source(self, baseline: Baseline):
        """Requirement 2.2: a citation names a Source, not a plausible id."""
        cited = set(CITATION_RE.findall(baseline.answers["covered"]))
        ours = {s.source_id for s in baseline.sources.values()}

        assert cited & ours, f"nothing cited from this notebook: {cited}"

        async with _client() as client:
            for document_id in cited:
                kind = document_id.split(":", 1)[0]
                endpoint = {"source": "sources", "note": "notes"}.get(kind)
                if endpoint is None:
                    continue
                response = await client.get(f"/api/{endpoint}/{document_id}")
                assert response.status_code == 200, (
                    f"cited {document_id} does not resolve"
                )

    @pytest.mark.asyncio
    async def test_cited_passage_sits_inside_its_source_text(
        self, baseline: Baseline
    ):
        """Requirement 2.3, at the API layer the UI reads.

        Selecting a citation shows the passage in context, which requires the
        retrieved passage to be locatable within the whole Source text. That is
        what is checked here; the click itself is a frontend concern.
        """
        cited = set(CITATION_RE.findall(baseline.answers["covered"]))
        ours = {s.source_id for s in baseline.sources.values()}
        candidates = cited & ours
        assert candidates, "no citation from this notebook to resolve"

        async with _client() as client:
            search = await client.post(
                "/api/search",
                json={"query": COVERED_QUESTION, "type": "vector", "limit": 10},
            )
            search.raise_for_status()

            located = []
            for result in search.json().get("results") or []:
                source_id = str(result.get("parent_id") or result.get("id"))
                if source_id not in candidates:
                    continue
                body = await _read_source(client, source_id)
                haystack = _normalise(body.get("full_text") or "")
                for passage in result.get("matches") or []:
                    if isinstance(passage, str) and _normalise(passage) in haystack:
                        located.append((source_id, len(passage)))

        assert located, (
            "no retrieved passage could be located inside the text of the Source "
            f"cited for it (candidates: {candidates})"
        )

    def test_uncovered_question_is_refused(self, baseline: Baseline):
        """Requirement 2.5: say the material does not cover it."""
        answer = _normalise(baseline.answers["uncovered"])

        assert any(phrase in answer for phrase in NON_COVERAGE_PHRASES), (
            f"no statement of non-coverage in: {baseline.answers['uncovered']!r}"
        )

    def test_uncovered_question_is_not_answered_from_model_knowledge(
        self, baseline: Baseline
    ):
        """Requirement 2.5, the half that matters.

        Refusing and then answering anyway is the failure mode; naming any of
        the cycle's intermediates is only possible from model knowledge, since
        no Source mentions them.
        """
        answer = _normalise(baseline.answers["uncovered"])
        leaked = [term for term in KREBS_LEAKAGE if term in answer]

        assert not leaked, (
            f"answered from model knowledge ({leaked}): "
            f"{baseline.answers['uncovered']!r}"
        )

    def test_chat_answers_from_this_notebooks_sources(self, baseline: Baseline):
        """Requirement 2.1, on the notebook-scoped path.

        Chat's context is built from one Notebook, so a correct interval here
        can only have come from this Notebook's uploaded document: 23 days is
        not a spacing interval any model would produce unprompted.
        """
        assert baseline.chat_reply.strip(), "chat produced no answer"
        assert "23" in baseline.chat_reply, (
            f"chat did not answer from the Source: {baseline.chat_reply!r}"
        )


class TestCleanup:
    """Catalogue check 1.10. Runs last: it removes what the run created."""

    @pytest.mark.asyncio
    async def test_deleting_the_notebook_removes_its_sources(
        self, baseline: Baseline
    ):
        async with _client() as client:
            response = await client.delete(
                f"/api/notebooks/{baseline.notebook_id}",
                params={"delete_exclusive_sources": "true"},
            )
            assert response.status_code == 200, response.text

            gone = await client.get(f"/api/notebooks/{baseline.notebook_id}")
            assert gone.status_code == 404

            for source in baseline.sources.values():
                orphan = await client.get(f"/api/sources/{source.source_id}")
                assert orphan.status_code == 404, (
                    f"{source.label} survived its only notebook"
                )
