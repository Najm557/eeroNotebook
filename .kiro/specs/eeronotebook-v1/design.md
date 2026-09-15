# Design Document: eeroNotebook v1

## Overview

eeroNotebook is a private, self-hosted study notebook service for a small team, including classroom use. Members add sources, ask questions answered only from those sources with verifiable citations, and generate study material — flashcards, quizzes, and mind maps — from them. It runs entirely on the team's own Dev Server and performs all inference locally.

It is built as a maintained code fork of [Open Notebook](https://github.com/lfnovo/open-notebook) v1.14.0 (MIT), which already provides source ingestion, grounded chat with citations, notes, search, and a provider-agnostic model layer. eeroNotebook adds what upstream deliberately does not: per-user ownership and sharing, and study artifacts.

Decisions behind this design are recorded in [ADR 0001](../../../docs/adr/0001-code-fork-of-open-notebook.md), [ADR 0002](../../../docs/adr/0002-per-user-notebooks.md), and [ADR 0003](../../../docs/adr/0003-pluggable-identity-boundary.md). Project vocabulary is defined in [CONTEXT.md](../../../CONTEXT.md).

### Design Philosophy

Stay as close to upstream as the goals allow. Every divergence is a merge cost paid on every future release, so anything expressible as configuration — prompt templates, provider settings, transformation definitions — is configuration. Code changes are reserved for ownership, access control, and study artifacts, which cannot be expressed from outside the application.

Inference is local and unmetered but finite. One Ollama instance on one machine serves eeroNotebook alongside an existing service, so the design constrains concurrency rather than assuming elastic capacity.

### Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Base | Code fork of Open Notebook v1.14.0 | Closest open-source feature match; MIT; citations already implemented |
| Divergence strategy | Long-lived `eero` branch, pinned upstream tag | Upstream releases frequently; merges stay deliberate and reviewable |
| Inference access | Containerised Inference Gateway, OpenAI-compatible | Every eeroNotebook service is containerised; the gateway gives the app one stable endpoint and makes the backend swappable (ADR 0004) |
| Inference backend | Native Ollama on the Dev Server, behind the gateway | Measured 87.8 tok/s native versus 0.50 tok/s containerised; Metal has no device node to pass into a Linux container |
| Synthesis model | `qwen2.5:14b` (Q4_K_M, 32K context, tool-capable) | Same family and quantisation as the 14B previously run here; tool calling is the lever for reliable structured output |
| Background model | `llama3.2:3b` | Cheap summarisation and titling without occupying the 14B |
| Embeddings | `nomic-embed-text` | Local, on the same Ollama; 2048-token window constrains chunk size |
| Database | SurrealDB v2 (upstream's choice) | Retained; changing the datastore would fork the data layer wholesale |
| Identity | Dedicated GoTrue behind an internal boundary | Already-operated technology, isolated from other stacks, replaceable per ADR 0003 |
| Sharing | Owner plus explicit revocable per-person grants | Classroom use requires selective sharing and later revocation |
| Study progress | Per person, private | One member's review schedule must not affect another's; shared scores would expose classmates' results |
| Container runtime | OrbStack (already installed) | Present and running; note the commercial-licence caveat under Risks |
| Ingress | Existing `mon-traefik` | A second reverse proxy on the same host would contend for 80/443 |
| Podcasts | Deferred | The only feature needing egress; local Piper TTS exists on this host for a later pass |

## Deployment Context

The Dev Server is a **Mac16,7 — Apple M4 Pro, 14 cores, 48 GB unified memory, macOS 26.5.2**, currently at `10.17.8.52`. That address is expected to change, so it is held in a single variable and referenced nowhere else. The `10.17.8.156` in `EarWig/.kiro/docs/MIGRATION_PLAN.md` is stale, as is that document's `apt`-based provisioning and its `/opt/stacks` path — this host is macOS and its stacks live in `~/stacks/`.

Container runtime is **OrbStack 2.2.3** (Docker 29.4.0, Compose v5.1.2). Already deployed alongside eeroNotebook: the `monitoring` stack (`mon-traefik` on 80/443/8080, `mon-uptime-kuma` on 3001, `mon-dozzle` on 8888), `legendary-hunts` (3000, 54321, 54322), `earovoice-api` (8741), and `learninglab` (3080, 3081, 8000).

**Host Ollama** is `0.32.3` at `/opt/homebrew/bin/ollama`, a native macOS process bound to `127.0.0.1:11434`, serving `qwen2.5:14b`, `qwen2.5:7b`, `llama3.2:3b`, and `nomic-embed-text`. It is a *backend behind the Inference Gateway*, not a component eeroNotebook knows about.

### Why the model server is not containerised

Containers on macOS are Linux containers, and macOS has no Linux kernel, so the runtime boots a Linux virtual machine and runs them inside it. `docker info` on this host reports **14 CPUs and 16.8 GB** — the VM's allocation, against the machine's 48 GB.

GPU access into a container works by mapping device nodes such as `/dev/dri` or `/dev/nvidia0` into the container's namespace. Apple Silicon exposes its GPU only through Metal, a macOS userspace API with no device node and no Linux driver, and the hypervisor presents no virtual GPU for compute. There is nothing to pass through, which is why this host offers only `runc` runtimes and no GPU runtime.

Measured on the Dev Server, `llama3.2:3b` with an identical prompt:

| | Native (Metal) | Containerised |
|---|---|---|
| eval rate | 87.80 tok/s | 0.50 tok/s |
| prompt eval rate | 529 tok/s | 17.4 tok/s |

Ollama's log inside the container reports `inference compute id=cpu library=cpu` after finding no GPU, and `/dev/dri` is absent. The existing `llm-server` stack is the same lesson at 14B scale: containerised `llama.cpp` asking for `--n-gpu-layers 99`, running on CPU, repeatedly `OOMKilled` inside the 16.8 GB VM.

The conclusion is narrow: the *model server* cannot usefully be containerised on this host. Everything eeroNotebook owns still is.

## Architecture

```
┌──────────────── Dev Server (macOS, Apple M4 Pro, 48 GB) ─────────────────┐
│                                                                          │
│   Backend, outside eeroNotebook's boundary (Metal-accelerated)            │
│   ┌────────────────────────────────────────────┐                         │
│   │ Host Ollama  127.0.0.1:11434               │◄────────┐               │
│   │  qwen2.5:14b · llama3.2:3b                 │         │ host alias    │
│   │  nomic-embed-text                          │         │ (gateway only)│
│   └────────────────────────────────────────────┘         │               │
│                                                           │              │
│   OrbStack ─ containers                                   │              │
│   ┌──── stack: eeronotebook ────────────────────────────┐ │              │
│   │                                                      │ │              │
│   │  eeronotebook-app    :8502 UI   :5055 API            │ │              │
│   │        │                                             │ │              │
│   │        ├──► eeronotebook-db      (SurrealDB, unpublished)             │
│   │        ├──► eeronotebook-auth    (GoTrue, unpublished)                │
│   │        │                                             │ │              │
│   │        └──► eeronotebook-inference ──────────────────┼─┘              │
│   │             (Inference Gateway, OpenAI-compatible,                     │
│   │              unpublished — the app's only inference source)            │
│   │                                                      │                │
│   │  networks: eeronotebook_internal, monitoring_net     │                │
│   └────────────────────────────┬─────────────────────────┘                │
│                                │ monitoring_net                           │
│   ┌────────────────────────────┴───────────────────────┐                  │
│   │ mon-traefik :80 :443   (TLS, routing)              │                  │
│   │ mon-uptime-kuma :3001  mon-dozzle :8888            │                  │
│   └────────────────────────────────────────────────────┘                  │
└──────────────────────────────────────────────────────────────────────────┘
```

### Inference path

The application's only source of inference is `eeronotebook-inference`, a containerised OpenAI-compatible Inference Gateway in the stack (ADR 0004). The app is configured with one provider — the gateway's in-stack address — and holds no host address, no model server address, and no knowledge of where inference physically runs.

The gateway is the only component that crosses to the host. It forwards to Host Ollama at `http://host.docker.internal:11434`, which works with Ollama bound to `127.0.0.1` because the runtime maps that alias to the host's loopback interface — verified from inside a container on this host. `extra_hosts: ["host.docker.internal:host-gateway"]` is declared for portability, following `~/stacks/earovoice/`.

The Dev Server's LAN address is not usable and not used: `10.17.8.52:11434` answers nothing, from a container or from the host itself, because Ollama listens only on loopback. Reaching it that way would mean rebinding Ollama to `0.0.0.0` and exposing an unauthenticated inference API to the network for no benefit. Ollama stays on loopback, and the gateway is the single controlled path to it.

Two properties follow, and both are the point of the design:

- **Swappability.** Moving inference to a GPU-equipped Linux host, where the model server can itself be containerised, is a change to gateway configuration. The application is untouched. The same applies to substituting a hosted provider, should the local-only boundary ever be revisited.
- **Model names as configuration.** The gateway maps logical names — a synthesis model and an embedding model — onto backend models, so changing the backing model does not alter application settings.

The gateway must serve both a chat completion route and an embedding route, since source ingestion depends on `nomic-embed-text` as much as chat depends on `qwen2.5:14b`.

**Implementation: LiteLLM**, run from a static configuration file with no database, keeping the gateway to a single container. It was chosen over a plain reverse proxy because model aliasing and cross-provider schema translation are what make backend replacement a configuration change; a proxy can only forward Ollama's dialect onward, which would reduce swappability to "any backend that also speaks Ollama". LiteLLM's virtual keys also supply the gateway credential the security posture requires. Its database-backed features — spend tracking, per-user keys — are deliberately not enabled at v1.

Concurrency is capped with `OPEN_NOTEBOOK_WORKER_MAX_TASKS=1` so background jobs do not contend with `earovoice-api`, which shares the same backend Ollama.

### Identity and access

GoTrue authenticates users and issues JWTs. The application does not consume GoTrue directly: a single internal boundary resolves a request to an authenticated user, and every authorization check depends only on that resolved user (ADR 0003).

Ownership and access rules:

- Every notebook has exactly one owner, set at creation.
- Only the owner may add or remove sources, edit notes, or change settings.
- Access for anyone else exists only as an explicit grant to a named user, revocable at any time.
- A viewer may read sources and ask grounded questions; a viewer may not alter the notebook.
- Sources, notes, and generated artifacts inherit the access of their notebook.
- Study progress is owned by the individual, never by the notebook. Quiz attempts are additionally readable by the notebook's owner; flashcard review state is not.
- The shared instance password resolves to a single **admin member**, rather than bypassing member resolution. Every access check then runs the same way for the admin as for anyone else, and the admin is a real owner that pre-existing content can be assigned to.

### Data model additions

Upstream's domain objects gain an owner scope; three concerns are new:

| Concern | Shape |
|---------|-------|
| Ownership | An owner reference on notebooks, inherited by sources, notes, and artifacts |
| Sharing | A grant linking one notebook to one user with role `viewer`, revocable |
| Study progress | Per user, per artifact: flashcard scheduling state, quiz attempts and scores |

Study progress is deliberately separate from artifacts so that revoking access does not destroy a person's history, and so a shared deck carries no shared state.

### Regeneration without losing history

Regenerating an artifact after its sources change must not silently discard what members have already done, and must not leave schedules attached to material that no longer exists. Two mechanisms, chosen because they make the good outcome automatic rather than requiring a reconciliation step:

**Flashcard progress is content-addressed.** A member's review state is keyed to a stable hash of the card's own content, not to a positional id. A card that survives regeneration keeps its history because it hashes the same; a card whose wording changed is a different card with fresh history, which is honest — the thing being recalled changed. History belonging to cards that have disappeared stays in place, unreferenced and harmless, and can be reported to the member rather than deleted on their behalf.

**Artifacts are versioned rather than mutated.** Regeneration writes a new version and records the source set it came from. A quiz attempt references the version it was taken against, so an owner reviewing a member's answers sees the questions that member actually faced, not the questions the quiz asks today. Without this, instructor review silently misrepresents what happened.

### Study artifacts

Flashcards, quizzes, and mind maps are generated from a notebook's sources as structured output, using `qwen2.5:14b`'s tool-calling to constrain the response to a schema rather than parsing free text. Each artifact records the sources it was generated from so it can be regenerated and traced.

Text-shaped outputs — study guide, briefing document, FAQ, timeline — are implemented as upstream transformation prompt templates, with no code change.

### Port allocation

| Port | Service | Exposure |
|------|---------|----------|
| 8502 | eeroNotebook UI | Host, then fronted by `mon-traefik` |
| 5055 | eeroNotebook API | Host |
| — | SurrealDB | Not published; reachable only on the stack network |
| — | GoTrue | Not published; reachable only on the stack network |
| — | Inference Gateway | Not published; reachable only on the stack network |

Port 8000, which upstream's compose publishes for SurrealDB, is already taken by `labplatform-app`. The host binding is only ever a debugging convenience, so it is dropped rather than moved.

### Security posture

- SurrealDB, GoTrue, and the Inference Gateway are unpublished; upstream's default `root:root` credentials are replaced.
- `OPEN_NOTEBOOK_ENCRYPTION_KEY` is set, since it encrypts stored provider credentials at rest.
- TLS terminates at `mon-traefik`. Until it does, GoTrue passwords and JWTs would cross the network in clear text, so TLS is not optional for a service with per-user logins.
- Host Ollama stays bound to `127.0.0.1`, and only the Inference Gateway crosses to it. No unauthenticated inference API is exposed to the network, and the gateway is the single auditable path to the backend.
- The gateway requires a credential from the application, so a compromised container elsewhere on the shared network cannot use it as an open inference proxy.

## Deferred decisions

Decided by recommendation and consciously not debated. Each is revisitable.

- DNS hostname in place of an address variable, once internal DNS is confirmed.
- `viewer` as the role label, presented as "student" in classroom UI if wanted.
- Quiz attempts are visible to the Notebook owner, so an instructor can assist. Visibility runs upward only — never between Viewers — and flashcard review state stays private even from the owner. Members are told before they attempt.
- TLS via a private CA, which client devices must trust.
- Monitoring beyond an Uptime Kuma check and Dozzle logs.
- Podcasts and TTS. Piper is already on this host over gRPC `:50053` when wanted.
- Remote access from outside the lab network. Pangolin is the candidate — an identity-aware tunneled reverse proxy — deferred to v2 or v3. It needs a public-facing VPS, and its edge access control overlaps eeroNotebook's own identity, so adopting it is a branch to design rather than a component to add. Nothing in v1 depends on it, and `Newt` can join the Dev Server later without altering this architecture.

## Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| OrbStack commercial licence | Business use requires a paid licence; free tier is personal-use only | Confirm coverage, or migrate to Colima, which is free |
| Structured output reliability | Flashcards, quizzes, and mind maps depend on schema-valid generation from a 14B model | Tool calling rather than text parsing; validate and retry on schema failure |
| Single shared Ollama | eeroNotebook jobs can slow `earovoice-api` | Worker concurrency capped at 1; the 3B model absorbs cheap work |
| Inference Gateway is a single point of failure | All inference stops if the gateway container stops | Health check on the gateway, monitored by Uptime Kuma alongside the app |
| Backend remains a host process | The one part of the system not containerised, so it is not covered by container restart policy or Dozzle logs | Supervise it on the host; revisit when inference moves to GPU-equipped Linux hardware, per ADR 0004 |
| Upstream merge drift | Ownership changes touch the request path, where upstream also changes | Pinned tags, isolated boundary, ownership concentrated rather than scattered |
| Embedding window | `nomic-embed-text` accepts 2048 tokens | Chunk sizing must respect it; changing embedder later requires re-embedding every source |
| macOS container file I/O | SurrealDB on a bind mount is slow under file sharing | Named volumes, not bind mounts |
