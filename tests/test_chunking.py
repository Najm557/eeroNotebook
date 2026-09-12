"""
Unit tests for the open_notebook.utils.chunking module.

Tests content type detection and text chunking functionality.
"""

import re
from pathlib import Path
from typing import Optional

import pytest
import yaml  # type: ignore[import-untyped]  # transitive dep; stubs not installed

from open_notebook.utils.chunking import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    MIN_CHUNK_SIZE,
    ContentType,
    chunk_text,
    detect_content_type,
    detect_content_type_from_extension,
    detect_content_type_from_heuristics,
)
from open_notebook.utils.token_utils import token_count

# nomic-embed-text accepts 2048 tokens per input. This is a property of the
# model this deployment embeds with — see the "Embeddings" row in
# .kiro/specs/eeronotebook-v1/design.md and Requirement 1.4 — and not a limit
# the application imposes: open_notebook/utils/chunking.py only warns above
# 8192, four times this window, and clamps nothing. The deployment pins
# OPEN_NOTEBOOK_CHUNK_SIZE in deploy/.env, and this file is what keeps that pin
# honest.
EMBEDDING_MODEL_MAX_INPUT_TOKENS = 2048

# The application's own floor, from chunking._get_chunk_size(). Below this it
# silently substitutes 100, so a deploy value under it would not be the value in
# effect.
CHUNK_SIZE_FLOOR = 100

CHUNK_SIZE_VAR = "OPEN_NOTEBOOK_CHUNK_SIZE"
DEPLOY_DIR = Path(__file__).parent.parent / "deploy"
DEPLOY_ENV_EXAMPLE = DEPLOY_DIR / ".env.example"
DEPLOY_ENV_LIVE = DEPLOY_DIR / ".env"
DEPLOY_COMPOSE = DEPLOY_DIR / "docker-compose.yml"


def _build_text_with_max_tokens(fragment: str, max_tokens: int) -> str:
    """Build text that stays within a token budget."""
    text = ""
    while True:
        candidate = text + fragment
        if token_count(candidate) > max_tokens:
            return text
        text = candidate


def _build_text_exceeding_tokens(fragment: str, threshold_tokens: int) -> str:
    """Build text that exceeds a token threshold."""
    text = fragment
    while token_count(text) <= threshold_tokens:
        text += fragment
    return text


def _assert_chunks_within_token_limit(chunks: list[str]) -> None:
    """Assert chunks stay within the configured token window."""
    assert chunks
    for chunk in chunks:
        assert token_count(chunk) <= CHUNK_SIZE


def _read_env_assignment(path: Path, key: str) -> Optional[str]:
    """
    Return the effective value of key in a dotenv-style file.

    Commented lines are ignored and the last assignment wins, which is how the
    container runtime reads these files.
    """
    value: Optional[str] = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, raw = stripped.partition("=")
        if name.strip() == key:
            value = raw.strip().strip("\"'")
    return value


def _compose_app_environment() -> dict[str, str]:
    """Read the app service's environment block out of the deployment compose file."""
    compose = yaml.safe_load(DEPLOY_COMPOSE.read_text(encoding="utf-8"))
    env = compose["services"]["eeronotebook-app"]["environment"]
    if isinstance(env, list):
        # Compose accepts a list of KEY=VALUE strings as well as a mapping.
        pairs = (item.partition("=") for item in env)
        return {name.strip(): raw.strip() for name, _, raw in pairs}
    return {str(k): "" if v is None else str(v) for k, v in env.items()}

# ============================================================================
# TEST SUITE 1: Content Type Detection from Extension
# ============================================================================


class TestDetectContentTypeFromExtension:
    """Test suite for extension-based content type detection."""

    def test_html_extensions(self):
        """Test HTML file extensions."""
        assert detect_content_type_from_extension("file.html") == ContentType.HTML
        assert detect_content_type_from_extension("file.htm") == ContentType.HTML
        assert detect_content_type_from_extension("file.xhtml") == ContentType.HTML
        assert detect_content_type_from_extension("/path/to/file.HTML") == ContentType.HTML

    def test_markdown_extensions(self):
        """Test Markdown file extensions."""
        assert detect_content_type_from_extension("file.md") == ContentType.MARKDOWN
        assert detect_content_type_from_extension("file.markdown") == ContentType.MARKDOWN
        assert detect_content_type_from_extension("file.mdown") == ContentType.MARKDOWN
        assert detect_content_type_from_extension("/path/to/README.MD") == ContentType.MARKDOWN

    def test_plain_text_extensions(self):
        """Test plain text file extensions."""
        assert detect_content_type_from_extension("file.txt") == ContentType.PLAIN
        assert detect_content_type_from_extension("file.text") == ContentType.PLAIN

    def test_code_extensions_as_plain(self):
        """Test code file extensions are treated as plain text."""
        assert detect_content_type_from_extension("file.py") == ContentType.PLAIN
        assert detect_content_type_from_extension("file.js") == ContentType.PLAIN
        assert detect_content_type_from_extension("file.json") == ContentType.PLAIN
        assert detect_content_type_from_extension("file.yaml") == ContentType.PLAIN

    def test_unknown_extensions(self):
        """Test unknown extensions return None."""
        assert detect_content_type_from_extension("file.xyz") is None
        assert detect_content_type_from_extension("file.docx") is None
        assert detect_content_type_from_extension("file.pdf") is None

    def test_no_extension(self):
        """Test files without extension."""
        assert detect_content_type_from_extension("Makefile") is None
        assert detect_content_type_from_extension("README") is None

    def test_none_input(self):
        """Test None input."""
        assert detect_content_type_from_extension(None) is None

    def test_empty_string(self):
        """Test empty string input."""
        assert detect_content_type_from_extension("") is None


# ============================================================================
# TEST SUITE 2: Content Type Detection from Heuristics
# ============================================================================


class TestDetectContentTypeFromHeuristics:
    """Test suite for heuristics-based content type detection."""

    def test_html_detection_doctype(self):
        """Test HTML detection with DOCTYPE."""
        html_text = "<!DOCTYPE html><html><body>Content</body></html>"
        content_type, confidence = detect_content_type_from_heuristics(html_text)
        assert content_type == ContentType.HTML
        assert confidence >= 0.8

    def test_html_detection_tags(self):
        """Test HTML detection with structural tags."""
        html_text = "<html><head><title>Test</title></head><body><div><p>Content</p></div></body></html>"
        content_type, confidence = detect_content_type_from_heuristics(html_text)
        assert content_type == ContentType.HTML
        assert confidence >= 0.5

    def test_markdown_detection_headers(self):
        """Test Markdown detection with headers."""
        md_text = """# Main Title

## Section 1

Some content here.

## Section 2

More content.

### Subsection

Details here.
"""
        content_type, confidence = detect_content_type_from_heuristics(md_text)
        assert content_type == ContentType.MARKDOWN
        assert confidence >= 0.3  # 4 headers give ~0.35 confidence

    def test_markdown_detection_links(self):
        """Test Markdown detection with links and headers for stronger signal."""
        md_text = """# Documentation

Check out [this link](https://example.com) and [another one](https://test.com).

## References

Here's some more text with [links](url) and `inline code`."""
        content_type, confidence = detect_content_type_from_heuristics(md_text)
        assert content_type == ContentType.MARKDOWN
        assert confidence >= 0.4

    def test_markdown_detection_code_blocks(self):
        """Test Markdown detection with code blocks."""
        md_text = """# Code Example

```python
def hello():
    print("Hello, World!")
```

Some explanation text.
"""
        content_type, confidence = detect_content_type_from_heuristics(md_text)
        assert content_type == ContentType.MARKDOWN
        assert confidence >= 0.5

    def test_plain_text_detection(self):
        """Test plain text detection."""
        plain_text = """This is just regular plain text.
It has multiple lines but no special formatting.
No headers, no links, no HTML tags.
Just regular sentences and paragraphs."""
        content_type, confidence = detect_content_type_from_heuristics(plain_text)
        assert content_type == ContentType.PLAIN

    def test_short_text(self):
        """Test short text defaults to plain."""
        content_type, confidence = detect_content_type_from_heuristics("Hi")
        assert content_type == ContentType.PLAIN

    def test_empty_text(self):
        """Test empty text defaults to plain."""
        content_type, confidence = detect_content_type_from_heuristics("")
        assert content_type == ContentType.PLAIN


# ============================================================================
# TEST SUITE 3: Combined Content Type Detection
# ============================================================================


class TestDetectContentType:
    """Test suite for combined content type detection."""

    def test_extension_takes_priority(self):
        """Test that file extension takes priority over heuristics."""
        # Text looks like markdown but file is .txt
        md_text = "# Header\n\nSome [link](url) content"
        content_type = detect_content_type(md_text, "file.txt")
        # Should use extension (plain) unless heuristics are very high confidence
        # In this case, markdown confidence might override
        assert content_type in (ContentType.PLAIN, ContentType.MARKDOWN)

    def test_no_extension_uses_heuristics(self):
        """Test that heuristics are used when no extension is available."""
        html_text = "<!DOCTYPE html><html><body>Test</body></html>"
        content_type = detect_content_type(html_text, None)
        assert content_type == ContentType.HTML

    def test_extension_html(self):
        """Test HTML extension detection."""
        content_type = detect_content_type("some text", "file.html")
        assert content_type == ContentType.HTML

    def test_extension_markdown(self):
        """Test Markdown extension detection."""
        content_type = detect_content_type("some text", "file.md")
        assert content_type == ContentType.MARKDOWN

    def test_high_confidence_override(self):
        """Test that very high confidence heuristics can override plain extension."""
        # Strong HTML indicators in a .txt file
        html_text = "<!DOCTYPE html><html><head><title>Test</title></head><body><div><p>Content</p></div></body></html>"
        content_type = detect_content_type(html_text, "file.txt")
        # High confidence HTML should override .txt extension
        assert content_type == ContentType.HTML


# ============================================================================
# TEST SUITE 4: Text Chunking
# ============================================================================


class TestChunkText:
    """Test suite for text chunking functionality."""

    def test_empty_text(self):
        """Test chunking empty text."""
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_short_text_no_chunking(self):
        """Test that short text is not chunked."""
        text = "This is a short text."
        chunks = chunk_text(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_text_at_chunk_limit(self):
        """Test text within the token chunk size limit."""
        text = _build_text_with_max_tokens("This is a sentence. ", CHUNK_SIZE)
        assert token_count(text) <= CHUNK_SIZE
        chunks = chunk_text(text)
        assert len(chunks) == 1

    def test_long_text_is_chunked(self):
        """Test that long English text is chunked by token budget."""
        text = _build_text_exceeding_tokens("This is a sentence. ", CHUNK_SIZE)
        chunks = chunk_text(text)
        assert len(chunks) > 1
        _assert_chunks_within_token_limit(chunks)

    def test_cjk_text_is_chunked_by_tokens(self):
        """Test that long CJK text is chunked using token measurement."""
        text = _build_text_exceeding_tokens("這是一段中文內容，用來驗證分塊邏輯。", CHUNK_SIZE)
        chunks = chunk_text(text, content_type=ContentType.PLAIN)
        assert len(chunks) > 1
        _assert_chunks_within_token_limit(chunks)

    def test_mixed_language_text_is_chunked_by_tokens(self):
        """Test that mixed-language text is chunked using token measurement."""
        fragment = "This paragraph mixes English and 中文內容 to verify token-based chunking. "
        text = _build_text_exceeding_tokens(fragment, CHUNK_SIZE)
        chunks = chunk_text(text, content_type=ContentType.PLAIN)
        assert len(chunks) > 1
        _assert_chunks_within_token_limit(chunks)

    def test_explicit_content_type_html(self):
        """Test chunking with explicit HTML content type."""
        html_text = """<html>
<body>
<h1>Main Title</h1>
<p>First paragraph with lots of content.</p>
<h2>Section</h2>
<p>Second paragraph.</p>
</body>
</html>"""
        chunks = chunk_text(html_text, content_type=ContentType.HTML)
        assert len(chunks) >= 1

    def test_explicit_content_type_markdown(self):
        """Test chunking with explicit Markdown content type."""
        md_text = """# Main Title

Introduction paragraph.

## Section 1

Content for section 1.

## Section 2

Content for section 2.
"""
        chunks = chunk_text(md_text, content_type=ContentType.MARKDOWN)
        assert len(chunks) >= 1

    def test_explicit_content_type_plain(self):
        """Test chunking with explicit plain content type."""
        plain_text = _build_text_exceeding_tokens("Word ", CHUNK_SIZE)
        chunks = chunk_text(plain_text, content_type=ContentType.PLAIN)
        assert len(chunks) > 1
        _assert_chunks_within_token_limit(chunks)

    def test_file_path_detection(self):
        """Test chunking with file path for content type detection."""
        text = "Some content here"
        chunks = chunk_text(text, file_path="document.md")
        assert len(chunks) == 1

    def test_secondary_chunking_for_large_sections(self):
        """Test that large Markdown sections are further chunked by tokens."""
        large_section = _build_text_exceeding_tokens(
            "這是一段很長的章節內容，用來測試次級分塊。", CHUNK_SIZE
        )
        md_text = f"# Title\n\n{large_section}"
        chunks = chunk_text(md_text, content_type=ContentType.MARKDOWN)
        assert len(chunks) > 1
        _assert_chunks_within_token_limit(chunks)

    def test_drops_degenerate_short_chunks(self):
        """Header splitters can emit single-char chunks; they must be filtered."""
        large_section = _build_text_exceeding_tokens(
            "This is a paragraph with enough content to be useful. ", CHUNK_SIZE
        )
        # A trailing micro-section ("# .") would otherwise produce a "." chunk.
        md_text = f"# Real Title\n\n{large_section}\n\n# .\n"
        chunks = chunk_text(md_text, content_type=ContentType.MARKDOWN)
        assert len(chunks) >= 1
        assert all(token_count(c) >= MIN_CHUNK_SIZE for c in chunks)
        assert all(c.strip() not in (".", ",", ";", "#") for c in chunks)

    def test_filter_never_empties_result(self):
        """Even if every chunk would be dropped, at least one survives."""
        # Force the minimum threshold higher than any chunk could possibly be.
        # We exercise the safety branch by passing PLAIN content that splits
        # into multiple very-small chunks.
        text = ". " * 200  # Many tiny fragments after splitting
        chunks = chunk_text(text, content_type=ContentType.PLAIN)
        # The function must always return at least one chunk for non-empty input.
        assert len(chunks) >= 1


# ============================================================================
# TEST SUITE 5: Chunk Size Against the Embedding Model's Input Window
# ============================================================================


class TestChunkSizeWithinEmbeddingWindow:
    """
    Requirement 1.4: chunks must not exceed the Embedding_Model's 2048-token
    input window.

    The application cannot enforce this on its own — the window belongs to the
    embedding model, not to the chunker, and chunking._get_chunk_size() accepts
    anything up to 8192 with only a log line. So the constraint lives in
    deployment configuration, and these tests are what stop that configuration
    drifting past the window. A chunk over the window is not an error anywhere:
    the provider truncates it and returns a vector for the part it read, which
    degrades retrieval silently.
    """

    def test_resolved_chunk_size_within_window(self):
        """The chunk size this process resolved must fit the embedder's window."""
        assert CHUNK_SIZE <= EMBEDDING_MODEL_MAX_INPUT_TOKENS, (
            f"{CHUNK_SIZE_VAR} resolved to {CHUNK_SIZE} tokens, over "
            f"nomic-embed-text's {EMBEDDING_MODEL_MAX_INPUT_TOKENS}-token window"
        )

    def test_overlap_is_carried_inside_the_chunk_budget(self):
        """
        Overlap must stay below the chunk size, which is what makes the window
        bound above sufficient: the splitter counts overlap inside chunk_size
        rather than adding it on top.
        """
        assert 0 <= CHUNK_OVERLAP < CHUNK_SIZE

    def test_deploy_template_pins_chunk_size_within_window(self):
        """
        deploy/.env.example is the committed template every deployment is copied
        from, so the pin has to be present and in range there.
        """
        raw = _read_env_assignment(DEPLOY_ENV_EXAMPLE, CHUNK_SIZE_VAR)
        assert raw is not None, (
            f"{CHUNK_SIZE_VAR} is not set in {DEPLOY_ENV_EXAMPLE.name}; the "
            f"application default would apply, and nothing holds it to the "
            f"embedder's window"
        )
        assert raw.isdigit(), f"{CHUNK_SIZE_VAR}={raw!r} is not an integer"
        pinned = int(raw)
        assert CHUNK_SIZE_FLOOR <= pinned <= EMBEDDING_MODEL_MAX_INPUT_TOKENS, (
            f"{DEPLOY_ENV_EXAMPLE.name} pins {CHUNK_SIZE_VAR}={pinned}, outside "
            f"the usable range {CHUNK_SIZE_FLOOR}–"
            f"{EMBEDDING_MODEL_MAX_INPUT_TOKENS} tokens"
        )

    def test_live_deploy_env_pins_chunk_size_within_window(self):
        """
        The same check against the live deploy/.env when one is present. It is
        gitignored and host-local, so this skips off the Dev Server rather than
        failing.
        """
        if not DEPLOY_ENV_LIVE.exists():
            pytest.skip(f"{DEPLOY_ENV_LIVE} is host-local and gitignored")
        raw = _read_env_assignment(DEPLOY_ENV_LIVE, CHUNK_SIZE_VAR)
        assert raw is not None, f"{CHUNK_SIZE_VAR} is not set in deploy/.env"
        assert raw.isdigit(), f"{CHUNK_SIZE_VAR}={raw!r} is not an integer"
        assert CHUNK_SIZE_FLOOR <= int(raw) <= EMBEDDING_MODEL_MAX_INPUT_TOKENS

    def test_compose_forwards_chunk_size_into_the_app_container(self):
        """
        Compose reads deploy/.env for interpolation only — it does not inject it
        into containers. Without this passthrough the pin is inert, and the
        symptom would be an environment file that looks correct while the
        application runs on its default.
        """
        env = _compose_app_environment()
        assert CHUNK_SIZE_VAR in env, (
            f"deploy/docker-compose.yml does not pass {CHUNK_SIZE_VAR} to "
            f"eeronotebook-app, so setting it in deploy/.env has no effect"
        )
        spec = env[CHUNK_SIZE_VAR]
        assert CHUNK_SIZE_VAR in spec, (
            f"{CHUNK_SIZE_VAR} should be interpolated from the environment, not "
            f"hardcoded in compose as {spec!r}"
        )
        # A compose-level fallback applies whenever the variable is absent from
        # .env, so it is a configured chunk size too and has to be in range.
        fallback = re.search(r":-\s*(\d+)\s*}", spec)
        assert fallback is not None, (
            f"{CHUNK_SIZE_VAR} has no compose fallback; an unset variable would "
            f"leave the application on its own default"
        )
        assert (
            CHUNK_SIZE_FLOOR
            <= int(fallback.group(1))
            <= EMBEDDING_MODEL_MAX_INPUT_TOKENS
        )

    def test_chunks_stay_within_the_window_at_the_pinned_size(self):
        """
        End of the chain: text well over the budget still yields chunks the
        embedder can accept whole. Guards the window rather than CHUNK_SIZE, so
        it stays meaningful if the pin is raised.
        """
        text = _build_text_exceeding_tokens("This is a sentence. ", CHUNK_SIZE * 3)
        chunks = chunk_text(text, content_type=ContentType.PLAIN)
        assert len(chunks) > 1
        for chunk in chunks:
            assert token_count(chunk) <= EMBEDDING_MODEL_MAX_INPUT_TOKENS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
