# 0001: eeroNotebook is a code fork of Open Notebook

**Status:** Accepted
**Date:** 2026-09-09

eeroNotebook needs capabilities the upstream Open Notebook project does not provide and has not committed to providing: per-user identity and access control, and study artifacts such as flashcards, quizzes, and mind maps. Upstream publishes a maintained container image and releases frequently, so consuming it unmodified would be materially cheaper to operate.

eeroNotebook is therefore a code fork rather than a deployment overlay on the published image. The required changes reach into the data model and the request path, which configuration and environment variables cannot express.

## Alternatives considered

- **Deployment overlay:** Run the published image and keep only compose files, environment, and prompt configuration in our repository. Rejected because owner-scoped data and new artifact types cannot be added from outside the application.

## Consequences

- Upstream changes must be merged deliberately rather than pulled, and upstream releases frequently.
- eeroNotebook-specific work is kept on a long-lived branch so that merges stay reviewable.
- Text-shaped artifacts that upstream's transformation mechanism can already express are implemented as configuration, not code, to keep the divergence as small as the goals allow.
