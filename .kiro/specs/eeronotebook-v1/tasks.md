# Implementation Plan: eeroNotebook v1

## Overview

This plan stands up eeroNotebook on the Dev Server in two distinct movements. Tasks 1–4 deploy an **unmodified upstream Open Notebook** against Host Ollama and prove the infrastructure works — runtime, database, inference, embeddings, citations. Only then do tasks 5–9 begin diverging from upstream, because debugging a fork and debugging a deployment at the same time is avoidable.

Tasks marked **(\*)** implement a decision made by recommendation rather than discussion; see *Deferred decisions* in [design.md](design.md).

Models are already installed: `qwen2.5:14b` and `nomic-embed-text` were pulled during planning.

## Tasks

- [x] 1. Establish the fork and workspace
  - [x] 1.1 Fork and clone
    - Forked to `Najm557/eeroNotebook` — the account has no organisations, so the fork sits on the personal account
    - Workspace root is the repository; `upstream` points at `lfnovo/open-notebook`, `origin` at the fork
    - Tag `v1.14.0` checked out onto branch `eero`, which is pushed and tracking
    - _Requirements: 14.1, 14.2_
  - [x] 1.2 Retire evaluation clones
    - Removed `research/`, reclaiming 561 MB
    - `CONTEXT.md`, `docs/adr/` and `.kiro/specs/` survive and are committed
    - `.gitignore` needed a scoped negation: upstream's broad `specs/` rule would otherwise exclude `.kiro/specs`
  - [x] 1.3 Record the deployment target
    - `deploy/.env` holds `EERONOTEBOOK_HOST=10.17.8.52`, generated on the host, gitignored **(\*)**
    - `deploy/.env.example` is the committed template
    - _Requirements: 13.7_

- [ ] 2. Build the Inference Gateway
  - [x] 2.1 Establish how a container reaches the inference backend
    - Verified against the live host: a container reaches `http://host.docker.internal:11434` and receives `200`, with Ollama bound to `127.0.0.1`
    - Verified the host's LAN address does not work — `10.17.8.52:11434` answers nothing, from a container or from the host itself
    - Verified the model server cannot usefully be containerised here: `llama3.2:3b` ran at 87.8 tok/s native against 0.50 tok/s in a container, with `library=cpu` and no `/dev/dri`
    - Conclusion: Ollama stays on loopback as a backend; only the gateway crosses to it
    - _Requirements: 3.1_
  - [x] 2.2 Add the gateway container to the stack
    - `eeronotebook-inference` runs LiteLLM from `deploy/litellm-config.yaml`, no database, unpublished
    - Serves both routes; verified `/v1/embeddings` returns 768 dimensions and `/v1/chat/completions` answers
    - Reaches the backend via the host alias with `extra_hosts: host-gateway`, per `~/stacks/earovoice/`
    - Requires a credential: verified an unauthenticated request to `/v1/models` returns `401`
    - The image ships no `curl`, so the healthcheck probes with its own Python — a curl-based probe left the container permanently unhealthy and blocked the app on the dependency
    - _Requirements: 3.1, 3.6, 13.2, 13.9_
  - [x] 2.3 Map logical model names onto backend models
    - `eero-synthesis` → `qwen2.5:14b`, `eero-background` → `llama3.2:3b`, `eero-embed` → `nomic-embed-text`
    - Mapping lives only in gateway configuration
    - `qwen2.5:14b` confirmed tool-capable, which Requirement 8.2 depends on
    - No synthesis-to-background fallback configured: a silent downgrade would weaken grounded answers with no signal
    - _Requirements: 3.3, 3.7_
  - [ ] 2.4 Verify the boundary holds
    - Verified the app reaches chat and embedding routes through the gateway alone, and that responses carry the logical name (`model=eero-synthesis`) rather than the backend model
    - Verified no host address or model server address appears in the app container's environment
    - Container healthcheck in place; the Uptime Kuma monitor is task 4.2
    - **Outstanding:** stop the gateway and confirm the app reports a clear fault rather than failing silently
    - _Requirements: 3.5, 3.8, 13.10_

- [ ] 3. Deploy the stack, unmodified
  - [x] 3.1 Write the compose project
    - `deploy/docker-compose.yml`, project `eeronotebook`, deployed at `~/stacks/eeronotebook/`
    - App built from this branch rather than pulled, so image and source cannot drift
    - 8502 and 5055 published; database and gateway unpublished
    - Named volumes `db_data` and `app_data`; `eeronotebook_internal` plus external `monitoring_net`
    - Dozzle labels applied, matching the convention in `~/stacks/llm-server/`
    - _Requirements: 13.1, 13.2, 13.3_
  - [x] 3.2 Configure environment
    - All four secrets generated on the host into `deploy/.env`, mode 600, never transiting a client
    - SurrealDB credentials replaced; compose fails fast if any secret is unset
    - `OPEN_NOTEBOOK_WORKER_MAX_TASKS=1` applied to the worker supervisord runs in-image
    - `OPEN_NOTEBOOK_PASSWORD` set as the interim gate until task 5
    - _Requirements: 13.4, 3.4_
  - [x] 3.3 Point the application at the gateway
    - One `openai_compatible` credential at `http://eeronotebook-inference:4000/v1`; connection test passed
    - Discovery returned the three logical names; all registered
    - Defaults set to `eero-synthesis` for chat, transformation, tools and large-context, and `eero-embed` for embedding
    - **Outstanding:** confirm chunk sizing respects the embedder's 2048-token window
    - _Requirements: 1.3, 1.4, 3.1, 3.8_
  - [ ] 3.4 Validate the baseline end to end
    - Add a document, a web page, and a video URL as Sources; confirm each processes and that a failure in one does not block the others
    - Ask a question and confirm the answer carries citations that resolve to the correct passage
    - Confirm vector search returns results, proving embeddings are wired
    - Ask something the Sources do not cover and confirm the answer says so rather than drawing on model knowledge
    - Stop here and resolve any failure before task 5; this is the last point at which upstream behaviour is unmodified
    - _Requirements: 1.1, 1.2, 1.5, 2.1, 2.2, 2.3, 2.5_

- [ ] 4. Bring the stack into operations
  - [ ] 4.1 Route through the existing reverse proxy **(\*)**
    - Register the app with `mon-traefik` rather than adding a second proxy; 80/443/8080 are already held
    - Terminate TLS with a private CA following the `EarWig/traefik/` pattern; distribute the CA to client devices
    - _Requirements: 13.5_
  - [ ] 4.2 Monitoring and backups **(\*)**
    - Add an Uptime Kuma monitor against the API health endpoint
    - Confirm Dozzle is collecting container logs
    - Schedule volume backups for database and application data as a launchd job, and prove the backup by performing a restore
    - _Requirements: 13.6, 13.8_

- [ ] 5. Identity
  - [ ] 5.1 Add a dedicated GoTrue service
    - Unpublished, on the stack network only, with its own database and JWT secret
    - Deliberately separate from `lh-auth`, so a compromise or migration in `legendary-hunts` does not reach eeroNotebook
    - _Requirements: 4.1, 13.2_
  - [ ] 5.2 Build the Identity_Boundary
    - One internal seam that resolves a request to an authenticated member; provider-specific token verification lives only there
    - No route outside the seam may reference GoTrue directly
    - _Requirements: 4.2, 4.3, 4.4_
  - [ ] 5.3 Replace the shared password
    - Move the application from single-password access to authenticated sessions
    - Decide the fate of `OPEN_NOTEBOOK_PASSWORD`: remove it, or retain it as an outer gate
    - _Requirements: 4.1, 4.5_

- [ ] 6. Ownership and sharing
  - [ ] 6.1 Add owner scope to the data model
    - Owner reference on Notebooks; Sources, notes, and Study_Artifacts inherit Notebook access
    - Migrate content created during tasks 3–4 to a named Notebook owner rather than leaving it unscoped
    - _Requirements: 5.1, 5.2, 5.3, 7.5_
  - [ ] 6.2 Enforce ownership on every path
    - Audit all 24 API routers; no Notebook, Source, note, or search result may be reachable outside the resolved member's access
    - Verify the MCP interface independently of the REST API, since both reach the same data
    - Confirm a Notebook's existence is not disclosed to members without access
    - Treat any unscoped route as a data leak, not a bug
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 5.4, 5.5_
  - [ ] 6.3 Implement Shares
    - Grant access to a single named member per Notebook, role Viewer, revocable **(\*)**
    - Notebook owner edits; Viewer reads Sources and asks questions but changes nothing
    - Confirm revocation ends access immediately without altering the Notebook
    - Confirm no single action exposes a Notebook to the whole team
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

- [ ] 7. Study progress
  - [ ] 7.1 Model Study_Progress per member
    - Flashcard scheduling state and quiz attempts keyed to member and Study_Artifact, stored separately from the artifact
    - Confirm revoking a Share leaves that member's history intact
    - Confirm no member can observe another's progress or scores, and that one member's reviews do not alter another's schedule **(\*)**
    - _Requirements: 9.3, 9.4, 10.2, 10.4_

- [ ] 8. Study artifacts
  - [ ] 8.1 Structured generation
    - Generate against `qwen2.5:14b` using tool calling to constrain output to a schema; do not parse free text
    - Validate every response against the schema, retry on failure, and never persist an invalid artifact
    - Record the Sources each Study_Artifact was generated from, and support regeneration
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_
  - [ ] 8.2 Flashcards
    - Deck generation from a Notebook's Sources, with review scheduling driven by the per-member state from task 7
    - _Requirements: 9.1, 9.2_
  - [ ] 8.3 Quizzes
    - Question generation with answers and explanations; attempts and scores recorded per member and visible to that member
    - _Requirements: 10.1, 10.3_
  - [ ] 8.4 Mind maps
    - Hierarchical concept structure extracted from Sources, persisted as data rather than a rendered image so it stays navigable and can expand and collapse
    - _Requirements: 11.1, 11.2, 11.3_
  - [ ] 8.5 Text artifacts as configuration
    - Study guide, briefing document, FAQ, and timeline as transformation prompt templates, retained as notes in the originating Notebook
    - No application code; this is the cheap half of NotebookLM parity, and the clearest instance of preferring configuration over divergence
    - _Requirements: 12.1, 12.2, 12.3, 14.3_

- [ ] 9. Release readiness
  - [ ] 9.1 Verify against the v1 goals
    - A member can create a private Notebook, share it with one named person, and revoke that Share
    - A Viewer can study from a shared Notebook without altering it
    - Flashcards, quizzes, and mind maps generate from real course material and survive schema validation
    - Confirm no Source content or query reached any third-party service, and that no Grounded_Answer drew on another Notebook's Sources
    - _Requirements: 2.4, 3.2, 6.5_
  - [ ] 9.2 Confirm outstanding risks
    - Resolve whether OrbStack's commercial licence is covered, or migrate to Colima
    - Re-verify access enforcement after the first upstream merge
    - _Requirements: 14.4_
  - [ ] 9.3 Optional cleanup outside this repository
    - `EarWig/.kiro/docs/MIGRATION_PLAN.md` carries a stale address, `apt` provisioning, and `/opt/stacks` paths that do not match this macOS host
    - Correcting it is out of scope for eeroNotebook but will otherwise mislead the next reader
