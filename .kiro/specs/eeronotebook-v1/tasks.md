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
  - [-] 2.4 Verify the boundary holds
    - Verified the app reaches chat and embedding routes through the gateway alone, and that responses carry the logical name (`model=eero-synthesis`) rather than the backend model
    - Verified no host address or model server address appears in the app container's environment
    - Container healthcheck in place; the Uptime Kuma monitor is task 4.2
    - Verified with the gateway stopped: chat answers `502` and vector search `500`, both naming the fault, and both recover when it restarts
    - Source ingestion did degrade silently: embedding is a separate fire-and-forget job, so an unembedded Source read `completed` with no error while the real failure sat on a command record nothing pointed at
    - Fixed by reporting a failed `embed_source` job as a failed Source, which lands in upstream's existing failed-source UI and needs no frontend change; suppressed once the Source has embeddings, so a successful retry clears it
    - Configuration could not carry this — both embedding paths are fire-and-forget — so it is the first application code change, arriving before task 3.4's unmodified-upstream checkpoint
    - The report appears when the embed job exhausts its five retries, roughly a minute, rather than on first failure
    - _Requirements: 3.5, 3.8, 13.10_

- [x] 3. Deploy the stack, unmodified
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
  - [x] 3.4 Validate the baseline end to end
    - `tests/integration/test_baseline.py` drives the deployed API, skipped unless `EERONOTEBOOK_API_URL` and `EERONOTEBOOK_API_TOKEN` are both set. With them unset, `uv run pytest tests/` is 649 passed and 17 skipped; the checks are the subset of `.codex/agents/smoke-e2e.toml` this task gates, so podcasts and transformations are deliberately absent
    - Run against `http://10.17.8.52:5055`, not `https://api.eeronotebook.local` — task 4.1's front door has no DNS entry and a certificate from a CA nothing trusts, so it is not reachable from a client yet. Password read from the host's `deploy/.env` into the run's environment only
    - **17 of 17 green, twice consecutively, ~4m15s per run.** Every criterion was exercised against the live stack: all three Source kinds accepted and extracted (uploaded document 112 tokens, Wikipedia page 13,500, TED transcript 2,982), each embedded with chunks present; the unresolvable `.invalid` URL failed alone and kept its reason while the three Sources queued around it completed; vector search returned scored results reaching this run's Sources (0.82 on the uploaded document, 0.52 on the web page); the answer carried citations that resolved to real Source ids over the API, and a retrieved passage was located inside the cited Source's own text; the uncovered question was refused with none of the Krebs-cycle intermediates present; deleting the Notebook cascaded and left no orphan Source
    - The first run failed two checks and **both were defects in the test, not the deployment**. The refusal check's phrase list did not contain "did not provide information", which is the wording the model actually used — widened. And the chat check passed `context_config: {}`, which is not a neutral default: `build_notebook_context` then takes the *short* context path, carrying each Source's id, title and insights and **no text at all**. Asked for a specific interval on that context the model answered "21 days" where the document says 23, and cited the document for it. Fixed by requesting `"full content"` per Source, after which the answer is correct
    - That accident is worth keeping: commit `c3b7967`'s grounding instructions live in `prompts/ask/*` only, so the **chat path carries no such instruction** and will invent a cited answer from an empty context. Requirement 2.5 is currently met on the ask path and unenforced on chat
    - Requirements 2.2, 2.3 and 2.5 held on all three runs (2.5 refused correctly even on the first; only the assertion's vocabulary disagreed). Two consecutive clean runs is what was established, not a guarantee — these are model-dependent behaviours and a 14B model can regress on any one of them
    - **Honest scope of what "unmodified upstream" now means here.** The deployed image is upstream `v1.14.0` plus `c3b7967` (the `prompts/ask/*` grounding change, made during an earlier attempt at this task and kept) plus deploy configuration. Task 2.4's embed-failure report is **not** deployed: the host checkout sits at `c3b7967` and carries neither the `api/routers/sources.py` nor the `open_notebook/domain/notebook.py` change, which exist only in the workspace tree. So the 2.5 refusal result above is measured **with** the prompt change in place, and whether upstream refuses unaided was not established — the change was committed and deployed before this run began, and reverting it to find out was outside this task
    - **Worker: it consumes reliably only while its LIVE query lives.** `surreal_commands` 1.3.1 scans `status = 'new'` exactly once at worker startup, then subscribes to a LIVE query on `command` with no reconnect, no polling fallback and no error path around the subscription itself. The SurrealDB restart five hours before this run had silenced it: four commands sat `new` for roughly two hours with nothing logged and the worker process healthy. Restarting `eeronotebook-app` drained them in under a second. Recovery is an app restart and **nothing detects the condition** — reported, not fixed, because a fix means either patching the library's listener or adding a queue-depth monitor, and neither is in this task
    - Chunk sizing confirmed by measurement, not by reading the default: inside the app container, against the live `source_embedding` rows, `CHUNK_SIZE=400 CHUNK_OVERLAP=60 MIN_CHUNK_SIZE=5` (all defaults, `OPEN_NOTEBOOK_CHUNK_SIZE` unset) produced 57 chunks whose largest was **393 tokens**, none over `CHUNK_SIZE` and none near `nomic-embed-text`'s 2048. Two of the three Sources genuinely required splitting, so this is not a small-input artefact. 3.5 codifies it
    - Cleanup: the previous attempt's `Worker live-query probe` Notebook and its Source are gone; the test's own teardown left nothing behind, so task 6.1 has no litter from tasks 3–4 to migrate. The queue holds no `new` and no `running` records. The one `running` record was marked `failed` by hand with its reason — its Source and Notebook no longer existed and nothing in the stack reconciles `running`. The 13 terminal `failed` records from earlier runs were left alone as a truthful log. Stack left up and healthy
    - **Requirement 2.5 does not hold under *partial* coverage, which this task did not test.** A later ad-hoc run asked one question mixing a covered fact with an uncovered one — the Vessey coefficient, present, and optimal study temperature, absent. The covered half was answered correctly from source; for the uncovered half the model supplied "70-75 degrees Fahrenheit" from its own knowledge, hedged with "not directly stated in the retrieved results", and placed a citation beside it. So the refusal instruction holds when *nothing* is covered and leaks when *something* is. The all-or-nothing case is what was verified above; the mixed case is open, and prompt wording alone has not secured it on a 14B model
    - **A Notebook delete left four orphaned Sources**, contradicting the cascade result recorded above. Observed on Sources attached at creation via the `notebook_id` form field rather than through the test's own path, so the two may differ; the Sources and their 70 embeddings had to be deleted individually. Worth resolving before task 6.1 migrates ownership, since an orphaned Source has no Notebook to inherit access from
    - _Requirements: 1.1, 1.2, 1.5, 2.1, 2.2, 2.3, 2.5_
  - [x] 3.5 Pin chunk sizing to the embedder's window
    - Closes the item 3.3 left open. Chunk size is now stated in configuration and held there by tests, rather than inherited from a default that happened to be safe
    - `OPEN_NOTEBOOK_CHUNK_SIZE=400` set in the host's `deploy/.env` (appended, mode 600 preserved, no other line read or touched) and in the committed `deploy/.env.example`. 400 is what 3.4 measured the stack already producing, so the pin changed no behaviour — the point is that changing it now has to be deliberate
    - **The `.env` entry on its own would have done nothing.** Compose reads `.env` for interpolation and injects none of it into containers, and the app service listed no chunking variable, so the pin also needed `OPEN_NOTEBOOK_CHUNK_SIZE: ${OPEN_NOTEBOOK_CHUNK_SIZE:-400}` in `deploy/docker-compose.yml`. A test asserts that passthrough exists and that its fallback is in range, because an environment file that reads correctly while the application runs on its default is exactly the failure this task exists to prevent
    - `open_notebook/utils/chunking.py` was deliberately left untouched. Its 8192 warning threshold is upstream's generic advice about "some embedding models"; 2048 belongs to `nomic-embed-text`, not to the chunker, so narrowing it there would put model-specific drift into a file upstream keeps changing, against Requirement 14.3. No startup guard either: an over-window chunk degrades retrieval, and a guard that refused to boot would trade that for an outage — a worse failure than the one it prevents
    - `tests/test_chunking.py` gained `TestChunkSizeWithinEmbeddingWindow`: six checks around a named `EMBEDDING_MODEL_MAX_INPUT_TOKENS = 2048` carrying its provenance — the resolved `CHUNK_SIZE`, overlap staying inside the chunk budget rather than adding to it, the pin in `.env.example`, the pin in the live `deploy/.env` when one is present (skipped off the Dev Server, since it is gitignored), the compose passthrough, and real chunking at three times the budget. Verified they bite rather than assuming: under `OPEN_NOTEBOOK_CHUNK_SIZE=4096` the configuration check fails and the behavioural one catches an actual 4094-token chunk
    - Mean pooling: **no over-window chunk can reach the embedder, but only because `CHUNK_SIZE` is inside the window.** `generate_embedding` embeds text at or under `CHUNK_SIZE` whole and never looks at it again — that branch contains no chunking at all, so its safety is precisely the value pinned above and nothing else. Above `CHUNK_SIZE` it splits, and every splitter path lands within budget, though HTML and Markdown split on headers rather than tokens and depend on `_apply_secondary_chunking` to come back under it. Now asserted rather than read: a test in `tests/test_embedding.py` captures every string handed to the model across all four content-type paths and fails if any exceeds the budget
    - `token_count` measures with tiktoken's `o200k_base`, not `nomic-embed-text`'s WordPiece vocabulary, so the budget is a proxy that reads low against what the embedder itself counts. At 400 the 5x headroom absorbs the error; a pin set at 2048 would not, which is why 2048 is a ceiling and not a target
    - Verified in the deployment and not only in the repo: app recreated, `OPEN_NOTEBOOK_CHUNK_SIZE=400` present in the container and `CHUNK_SIZE=400` resolved at import. A real ingestion through the API — one 5,600-word text Source — reached `completed` and `embedded`, producing 20 chunks whose largest was **385 tokens**, none over `CHUNK_SIZE` and none near 2048. `source_embedding` was empty beforehand, 3.4's data having been cleaned up, so this is the only measurement in the live database. Notebook deleted afterwards and both reads answer 404; `notebook`, `source`, `source_embedding` and `note` are all back to zero, so 6.1 inherits nothing from here
    - `uv run pytest tests/` is 658 passed and 18 skipped, up from 649 and 17; `ruff` and `mypy` clean. `yaml` needed an inline `import-untyped` ignore — it is a transitive dependency whose stubs exist on typeshed, which mypy reports regardless of `ignore_missing_imports`
    - **A concurrent actor redeployed the stack mid-task, and it cost the deploy configuration.** At 14:52 UTC the host checkout went clean at `04e49e6`, discarding the uncommitted `deploy/` edits it had been carrying, then rebuilt the image and recreated both containers. That took 4.1's `API_URL` and 4.2's `monitoring_net` on the gateway down with this task's passthrough: for twelve minutes the UI's TLS origin was wrong and the gateway monitor unreachable. Restored by copying the workspace's `deploy/` files back and running `docker compose up -d`, which recreated gateway and app without rebuilding — all three healthy, API `/health` 200, gateway back on `monitoring_net`. Nothing under `deploy/` is committed, so any actor with a clean checkout can undo it again; the durable fix is committing the deploy configuration, which is not this task's to do
    - Also changed under us: the running image is now built from `04e49e6` rather than `c3b7967` as 3.4 recorded, so the deployed code carries the later refusal-step change too
    - Left to the operator: raising the pin later means re-embedding every Source, since stored vectors keep whatever size they were built with, and restarting `eeronotebook-app`, since chunking constants are read at import
    - _Requirements: 1.3, 1.4_

- [x] 4. Bring the stack into operations
  - [x] 4.1 Route through the existing reverse proxy **(\*)**
    - `mon-traefik` runs the file provider only and reads no Docker labels, so the app registers as a file in its watched dynamic directory rather than through compose labels
    - `deploy/traefik/eeronotebook.yml.template`, rendered and installed by `setup-ingress.sh`; Traefik reloaded on its own, with no restart and no interruption to any other stack
    - `eeronotebook.local` → 8502 and `api.eeronotebook.local` → 5055, both over `websecure`, reached by container name on `monitoring_net`, so no host port was added and none could collide
    - Plain HTTP redirects with 301; the redirect is attached to the two eeroNotebook routers, not to the `web` entrypoint, because every other stack behind this proxy is HTTP-only
    - Private CA per the `EarWig/traefik/` pattern, generated on the host; the script adds a `serverAuth` EKU and an 825-day leaf, both of which modern clients require and that script predates
    - Registered as a `tls.certificates` entry, not a second default: `mon-traefik`'s default certificate belongs to learninglab, and a second default would leave which one wins undefined — SNI selects instead
    - `API_URL` had to be set explicitly: left to auto-detect, the app told the browser to call `https://eeronotebook.local:5055`, a port the proxy neither serves nor terminates TLS on. Consequence is that the UI is now reached by name; `http://<host>:8502` remains only a debugging path
    - Verified on the host and again across the network: UI 307 → `/notebooks` 200, API health, and authenticated `/api/notebooks` 200 both directly and through the UI origin, all with the certificate verifying against the private CA and failing without it
    - Verified all eight pre-existing routes return their pre-change codes, and that learninglab's default certificate and its by-IP HTTPS fallback are untouched
    - **Not done, and not verifiable from here:** the `/etc/hosts` entry and the CA trust both need `sudo`, which this host does not grant without a password, and client devices are out of reach. Verification worked around both with `--resolve` and `--cacert`; `setup-ingress.sh` prints the two commands an operator must run
    - _Requirements: 13.5_
  - [x] 4.2 Monitoring and backups **(\*)**
    - Two monitors under a `📓 eeroNotebook Stack` group in `mon-uptime-kuma`: the API's `/health` and the gateway's `/health/liveliness` (Requirement 13.10), both `keyword` type so a 200 carrying the wrong body still fails
    - Both probe by container name over `monitoring_net`, as every other monitor on this host does — deliberately not `https://api.eeronotebook.local`, which has no DNS entry and a certificate from a CA nothing trusts yet, so a monitor there would alarm on task 4.1's outstanding operator steps rather than on the stack. A third monitor on the TLS front door is worth adding once those are done
    - Kuma 1.x has no API for creating monitors — only the socket.io channel the browser uses, which needs the admin login — so `deploy/monitoring/setup-monitors.sh` writes the rows into Kuma's own SQLite: copy `kuma.db` aside, stop the container, insert, start. Kuma reads its monitor list once at boot, so it cannot be done without the restart
    - `eeronotebook-inference` had to join `monitoring_net` to be reachable at all; it is still unpublished, and an unauthenticated `/v1/models` still answers `401`, so the added reachability grants nothing
    - Verified both report `200 - OK, keyword is found`, and that all 18 pre-existing monitors still report up after the Kuma restart
    - Verified down detection rather than assuming it: stopping the gateway moved the monitor to pending after ~30 s and to down at 2 m 45 s — interval 60 s plus two 60 s retries — and back to up within a minute of restarting it
    - Dozzle confirmed through its own log API, not by the presence of labels: 46, 40 and 38 real log messages returned for app, database and gateway. The `dozzle.enable` labels are in fact inert here — `DOZZLE_FILTER` is unset, so Dozzle shows every container regardless
    - Backups are `deploy/backup/`, scheduled as a launchd **user agent** at 03:30. A `LaunchDaemon` would need the `sudo` this host withholds, and would run before login, where OrbStack and therefore `docker` do not exist
    - Three artifacts per run, because the database is backed up twice on purpose: `db_data.tar.gz`, a byte-exact copy taken with SurrealDB stopped, which is the restore path of record; `database.surql.gz`, a hot logical export, which is the only thing that survives the rocksdb files being tied to the SurrealDB build that wrote them under a floating `:v2` tag; and `app_data.tar.gz`. A `MANIFEST` carries checksums, the SurrealDB version, and a fingerprint — not a copy — of the encryption key
    - `~/backups/eeronotebook` is `0700` and every artifact `0600`: the export contains the `credential` table
    - Two defects in SurrealDB 2.6.5's own export, both found by actually restoring what it produced. Exporting to stdout corrupts the artifact — the CLI writes its "exported successfully" log line into the same stream — so the export goes to a file inside the container and is copied out, and the script rejects any export containing CLI output. And SurrealDB cannot import its own export: `DEFINE FIELD in ON <relation table>` and `embedding[*]` collide with definitions the `DEFINE TABLE` already implied, so `restore.sh` rewrites every `DEFINE FIELD` to `DEFINE FIELD OVERWRITE`. Both are why the volume copy leads and the export follows
    - Verified the job runs through launchd, three times, exit 0, artifacts each time. The database was down 2–3 s and the application needed no restart — it opens a connection per query rather than holding a pool — and a database-backed API call answered `200` immediately after
    - **Restore proven, twice, without touching production:** artifacts extracted into scratch volumes and opened by a throwaway SurrealDB on `--network none`. All 19 tables came back at the live record count from *both* database artifacts (131 records), SurrealDB opened the restored rocksdb store directly, embeddings returned at 768 dimensions, the full-text index answered on restored data, and `app_data`'s four files were byte-identical to the live volume. Scratch containers and volumes torn down after
    - `restore.sh --production` is written but deliberately never run against live data: it takes a fresh safety backup first, requires typing `restore`, and stops the app and database while it replaces both volumes
    - **Left to the operator:** keep `OPEN_NOTEBOOK_ENCRYPTION_KEY` somewhere outside the backups, or a restored `credential` table decrypts to nothing. No notification channel is configured anywhere on this Kuma, so a red monitor currently tells nobody. And the backups sit on the same disk as the data they protect, so an off-host copy is still owed
    - Noticed and left alone: `mon-dozzle` is configured with `DOZZLE_REMOTE_AGENT=10.17.8.52:7007`, which fails every five seconds because `labplatform-containment` binds 7007 to loopback only. Pre-existing, and another stack's configuration
    - _Requirements: 13.6, 13.8_

- [ ] 5. Identity
  - [~] 5.1 Add a dedicated GoTrue service
    - Unpublished, on the stack network only, with its own database and JWT secret
    - Deliberately separate from `lh-auth`, so a compromise or migration in `legendary-hunts` does not reach eeroNotebook
    - Add `eeronotebook-auth` and its own Postgres to `deploy/docker-compose.yml`, both on `eeronotebook_internal` only, with no host ports — GoTrue is a component of eeroNotebook, so it is a container like everything else
    - Generate `GOTRUE_JWT_SECRET` and the Postgres password on the host into `deploy/.env`, and make compose fail fast if either is unset, as the existing four secrets already do
    - Disable open sign-up; members are created by the operator, which is the whole population of a small team
    - Give both containers a healthcheck the app can depend on, and check first whether the image ships `curl` — 2.2 lost time to a probe that used a binary the image did not have
    - _Requirements: 4.1, 13.2, 13.9_
  - [~] 5.2 Build the Identity_Boundary
    - One internal seam that resolves a request to an authenticated member; provider-specific token verification lives only there
    - No route outside the seam may reference GoTrue directly
    - New package `open_notebook/identity/`: verify the token, resolve it to a member, raise `AuthenticationError` when it cannot, so the existing handler returns 401 rather than a bare `HTTPException`
    - Expose exactly one FastAPI dependency for routers to depend on; nothing else in the seam is public
    - Persist a `member` record keyed on the provider's subject claim, so application data references a local id and not a provider id — this is what makes Requirement 4.4 achievable rather than aspirational
    - Cache verification material rather than fetching it per request, and keep the whole path async
    - Add a test asserting no module outside `open_notebook/identity/` imports a GoTrue client or reads a GoTrue environment variable; the seam is only real if something enforces it
    - _Requirements: 4.2, 4.3, 4.4_
  - [~] 5.3 Replace the shared password
    - Move the application from single-password access to authenticated sessions
    - Decide the fate of `OPEN_NOTEBOOK_PASSWORD`: remove it, or retain it as an outer gate
    - Replace `PasswordAuthMiddleware` in `api/main.py` with the boundary's dependency, and retire `api/auth.py` with it
    - Keep `/`, `/health`, `/docs`, `/openapi.json`, `/redoc`, `/api/config` and the login route unauthenticated, matching the current `excluded_paths` exactly — anything else silently loses or gains protection
    - `GET /api/auth/status` reports whether a password is set; change it to report how to authenticate, since the frontend reads it on load
    - If `OPEN_NOTEBOOK_PASSWORD` is retained as an outer gate, no route may treat it as sufficient on its own
    - An unresolvable request returns 401, never an empty result set
    - _Requirements: 4.1, 4.5_
  - [ ] 5.4 Move the frontend onto member sessions
    - Rewrite `frontend/src/app/(auth)/login/` to authenticate a member instead of posting a shared password
    - Keep the session under the existing `auth-storage` key so `apiClient`'s interceptor keeps working unchanged, and add refresh handling — access tokens expire where the password never did
    - Add sign-out, and surface the signed-in member in the layout
    - Every new string through `t('...')`, with keys added to all locales under `frontend/src/lib/locales/`; en-US is the reference and a missing key fails `tsc`
    - `npm run lint`, `npm run test`, `npm run build`
    - _Requirements: 4.1, 4.5_
  - [ ]* 5.5 Test the Identity_Boundary
    - A valid token resolves to a member; expired, malformed, wrong-issuer, wrong-audience and absent tokens each return 401
    - A second provider stub satisfying the same seam passes the same tests unchanged, which is the only honest check of Requirement 4.4
    - _Requirements: 4.2, 4.3, 4.4, 4.5_
  - [ ] 5.6 Checkpoint — identity
    - Ensure all tests pass, ask the user if questions arise.
    - `uv run pytest tests/`, `ruff check . --fix`, `uv run python -m mypy .`, and the frontend's `npm run lint && npm run test && npm run build`

- [ ] 6. Ownership and sharing
  - [~] 6.1 Add owner scope to the data model
    - Owner reference on Notebooks; Sources, notes, and Study_Artifacts inherit Notebook access
    - Migrate content created during tasks 3–4 to a named Notebook owner rather than leaving it unscoped
    - Migration `24.surrealql` plus `24_down.surrealql`, and the matching entry in `AsyncMigrationManager` — migrations are hard-coded, not discovered, so a new file alone does nothing
    - Add `owner` to the `Notebook` model in `open_notebook/domain/notebook.py`; Sources, notes and Study_Artifacts carry no owner of their own and inherit through the existing `reference` and `artifact` relations
    - Define the `share` relation in the same migration, so 6.2 can enforce owner-or-Share from the outset instead of being rewritten by 6.3
    - The migration must leave both an empty database and the populated one on the Dev Server valid; the up path runs automatically on API startup, so a failure there stops the app
    - Take a backup before the first run, and confirm the down migration on a scratch database rather than on live data
    - _Requirements: 5.1, 5.2, 5.3, 7.5_
  - [~] 6.2 Enforce ownership on every REST route
    - No Notebook, Source, note, or search result may be reachable outside the resolved member's access
    - Treat any unscoped route as a data leak, not a bug
    - Add one access module — `open_notebook/domain/access.py` — resolving a member and a Notebook id to read or write permission; every route calls it rather than writing its own filter, because scattered filters are what drift on an upstream merge
    - `api/main.py` registers 22 routers; that is the list to work through. The earlier figure of 24 counted files in `api/routers/`, which includes `__init__.py` and the non-router `_chat_shared.py`
    - Routes addressing a Source, note or insight resolve its Notebook first; logic belongs in the `*_service.py` layer, with routers staying thin
    - Owner-only for writes, owner-or-Viewer for reads
    - `GET /api/notebooks` returns only what the member owns or holds a Share on
    - _Requirements: 7.1, 5.2, 5.4, 5.5_
  - [~] 6.3 Implement Shares
    - Grant access to a single named member per Notebook, role Viewer, revocable **(\*)**
    - Notebook owner edits; Viewer reads Sources and asks questions but changes nothing
    - New router for create, list and revoke, owner only, registered in `api/main.py`
    - Role is `viewer` and nothing else: no endpoint accepts a list of members, a group, or a wildcard, which is how Requirement 6.6 is met structurally rather than by policy
    - Revocation deletes the relation and takes effect on the next request, leaving the Notebook's contents untouched
    - Share management in the Notebook UI under `frontend/src/components/notebooks/`, with i18n keys in every locale
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.6_
  - [ ] 6.4 Scope search and stop disclosing existence
    - `text_search` and `vector_search` in `open_notebook/domain/notebook.py` take the resolved member and filter inside the SurrealQL, not after it — filtering the result set in Python still leaks counts and scores
    - `POST /api/search` and the ask graph pass the member through; the graph nodes are sync and reach async code through the existing event-loop workaround, so follow `chat.py`'s pattern exactly rather than inventing a second one
    - A Notebook the member cannot reach returns 404 rather than 403, on every route that addresses one by id, so the response does not confirm it exists
    - Same for Sources, notes and Study_Artifacts addressed directly by id
    - _Requirements: 7.3, 7.4_
  - [ ] 6.5 Build the access enforcement suite
    - `tests/test_access_enforcement.py`, parameterised over the registered route table, so a route added later without scoping fails a test instead of passing unnoticed
    - Cover: a member with no access gets 401 or 404 and never content; a Viewer's writes are refused; a revoked Share ends access; search excludes inaccessible material; existence is not disclosed
    - Deliberately not marked optional. Requirement 14.4 re-runs this after every upstream merge, and the design names access enforcement the highest-consequence divergence — an optional suite would be skipped exactly when it matters
    - _Requirements: 7.1, 7.3, 7.4, 5.4, 5.5, 6.4, 6.5_
  - [ ] 6.6 Settle the MCP interface
    - There is no MCP server in this repository. `open-notebook-mcp` is a separate package documented in `docs/5-CONFIGURATION/mcp-integration.md`; it holds no database access and reaches the API over HTTP, so it inherits whatever 6.2 and 6.4 enforce rather than enforcing anything itself
    - Confirm that rather than assume it: check its published tool list against the scoped routes, then point it at the deployed API as a member with no access and assert it returns nothing
    - It authenticates with `OPEN_NOTEBOOK_PASSWORD`, which 5.3 removes. Either give an MCP client a way to hold a member token, or record that the interface is unavailable at v1 — leaving it half-configured is the outcome to avoid
    - Write the conclusion into `docs/`, since a reader who finds Requirement 7.2 will otherwise look for a second enforcement point in this codebase and not find one
    - _Requirements: 7.2_
  - [ ] 6.7 Checkpoint — access control
    - Ensure all tests pass, ask the user if questions arise.
    - 6.5's suite must be green before task 8 adds a new category of Notebook-owned data

- [ ] 7. Study progress
  - [~] 7.1 Model Study_Progress per member
    - Flashcard scheduling state and quiz attempts keyed to member and Study_Artifact, stored separately from the artifact
    - Migration `25.surrealql` and its down file, plus the `AsyncMigrationManager` entry
    - Domain model in a new `open_notebook/domain/study.py`; every read and write filters on the resolved member, with no code path that takes a member id from the request body
    - Deleting a Share leaves progress rows untouched; deleting the artifact is the only thing that removes them **(\*)**
    - No endpoint returns another member's progress, and none exposes an aggregate across members — an average over a class of two identifies both **(\*)**
    - _Requirements: 9.3, 9.4, 10.2, 10.4_
  - [ ]* 7.2 Test progress isolation
    - Two members on one shared artifact: each sees only their own state, and one member's review does not change the other's next due card
    - Revoking a Share leaves the revoked member's rows present, and readable again if access is restored
    - _Requirements: 9.3, 9.4, 10.2, 10.4_

- [ ] 8. Study artifacts
  - [~] 8.1 Structured generation
    - Generate against `qwen2.5:14b` using tool calling to constrain output to a schema; do not parse free text
    - Validate every response against the schema, retry on failure, and never persist an invalid artifact
    - Record the Sources each Study_Artifact was generated from
    - Migration `26.surrealql` and its down file for `study_artifact`, plus the `AsyncMigrationManager` entry; the record holds its Notebook, its type, the Sources it came from, and the validated payload
    - Reach the model through `provision_langchain_model()` like every other LLM call, never a provider client directly, and bind a Pydantic schema as a tool. The gateway serves the logical name `eero-synthesis`, so nothing here names `qwen2.5:14b`
    - Validation is not a separable step from persistence: the schema check is the decision to write. Raise `ValueError` for a permanent failure so the `stop_on` blocklist in `commands/` stops retrying, and anything else to let it retry
    - Run generation as a background command in `commands/study_commands.py`, submitted fire-and-forget; the worker must be running or nothing happens and nothing complains
    - Wrap the call with `classify_error()` and strip thinking output with `clean_thinking_content()`, following the existing graph nodes
    - Surface a failed generation against the request the way 2.4 surfaced a failed embedding — a fire-and-forget job that fails invisibly is the defect that task already paid for once
    - _Requirements: 8.1, 8.2, 8.3, 8.4_
  - [~] 8.2 Flashcards
    - Deck generation from a Notebook's Sources, with review scheduling driven by the per-member state from task 7
    - Card schema carrying front, back, and the Source it came from, so a card remains traceable like a citation
    - Routes to generate a deck, fetch the calling member's due cards, and record a review; the deck itself holds no schedule
    - Frontend for review under a new `frontend/src/components/study/`, with i18n keys in every locale
    - _Requirements: 9.1, 9.2_
  - [~] 8.3 Quizzes
    - Question generation with answers and explanations; attempts and scores recorded per member and visible to that member
    - Schema requires an answer and an explanation per question, so an unexplained answer fails validation rather than reaching a member
    - Routes to generate a quiz, submit an attempt, and list the calling member's own attempts
    - Frontend and i18n as for flashcards
    - _Requirements: 10.1, 10.3_
  - [~] 8.4 Mind maps
    - Hierarchical concept structure extracted from Sources, persisted as data rather than a rendered image so it stays navigable and can expand and collapse
    - Bound depth and node count in the schema; an unbounded graph from a 14B model is unrenderable and there is no way to recover it after the fact
    - Frontend renders expand and collapse from the stored structure, with i18n
    - _Requirements: 11.1, 11.2, 11.3_
  - [~] 8.5 Text artifacts as configuration
    - Study guide, briefing document, FAQ, and timeline as transformation prompt templates, retained as notes in the originating Notebook
    - No application code; this is the cheap half of NotebookLM parity, and the clearest instance of preferring configuration over divergence
    - Templates go under `prompts/transformation/` and are registered as transformations; retention as notes is upstream behaviour already
    - Templates are cached, so restart the app after editing one
    - _Requirements: 12.1, 12.2, 12.3, 14.3_
  - [ ] 8.6 Regenerate an artifact after its Sources change
    - Owner-only route that regenerates in place and records the new Source set
    - Decide what happens to Study_Progress: a card that no longer exists cannot keep a schedule, and quietly discarding a member's history would be the wrong default. Implement the choice and record it
    - _Requirements: 8.5_
  - [ ]* 8.7 Test schema validation and retry
    - For a response that fails validation, nothing is persisted and the job retries; this is the structured-output risk the design's Risks table names
    - A response that never validates ends as a failed job carrying a reason, not as a silent absence
    - The Sources recorded on an artifact are the Sources fed into it
    - _Requirements: 8.2, 8.3, 8.4_
  - [ ] 8.8 Checkpoint — study artifacts
    - Ensure all tests pass, ask the user if questions arise.
    - Re-run 6.5's suite: task 8 added a new category of Notebook-owned data, and every new route is a new place for access to leak

- [ ] 9. Release readiness
  - [~] 9.1 Verify against the v1 goals
    - A member can create a private Notebook, share it with one named person, and revoke that Share
    - A Viewer can study from a shared Notebook without altering it
    - Flashcards, quizzes, and mind maps generate from real course material and survive schema validation
    - Confirm no Source content or query reached any third-party service, and that no Grounded_Answer drew on another Notebook's Sources
    - `tests/integration/test_v1_acceptance.py`, guarded by the same environment check as 3.4, driving the above through the API against the deployed stack
    - Prove the cross-Notebook claim rather than asserting it: seed two Notebooks with disjoint, checkable content and confirm neither answer contains the other's material
    - Prove the no-egress claim from configuration: the app's only registered credential is the gateway, and `deploy/litellm-config.yaml` names only the local backend. Assert no external endpoint appears in the app container's environment, which 2.4 already verified once and which any later credential could undo
    - _Requirements: 2.4, 3.2, 6.5_
  - [~] 9.2 Confirm outstanding risks
    - Resolve whether OrbStack's commercial licence is covered, or migrate to Colima
    - Re-verify access enforcement after the first upstream merge
    - Merge the next upstream tag onto `eero` and re-run 6.5's suite; Requirement 14.4 is a standing obligation, not a one-off
    - Add a check to `.github/workflows/test.yml` asserting the branch point is a pinned upstream tag rather than upstream's default branch
    - **Manual, not automatable:** the OrbStack licence question is a commercial decision, not a code change
    - **Manual, carried from 4.1:** the `/etc/hosts` entry and trusting the private CA on client devices both need `sudo` this host does not grant, and the devices are out of reach from here
    - **Manual, carried from 4.2:** an off-host copy of the backups, a notification channel on Kuma so a red monitor tells somebody, and `OPEN_NOTEBOOK_ENCRYPTION_KEY` held outside the backups
    - _Requirements: 14.1, 14.4_

## Notes

- Sub-tasks marked `*` are optional and may be skipped for a faster route to something usable. Top-level tasks are never optional
- 6.5 is a test task and is deliberately **not** optional: Requirement 14.4 re-runs it after every upstream merge, so it is infrastructure rather than assurance
- Sub-task numbers are stable identifiers, so new work appends rather than renumbers. That is why 3.5, 6.4–6.7 and 8.6–8.8 sit after sub-tasks they logically follow
- `[~]` and `[ ]` both mean not started; `[-]` means in progress and `[x]` complete
- Tasks 1–4 prove the deployment against unmodified upstream; 5–9 diverge from it. 3.4 is the gate between them. One application change already landed early — the embed-failure report in 2.4 — because both embedding paths are fire-and-forget and configuration could not reach them
- Start order is database, API, worker, frontend. The worker is not optional: source processing, embeddings and artifact generation are all async jobs that queue silently without it
- Every database query, graph invocation and model call is awaited. Migrations are hard-coded in `AsyncMigrationManager` and run on API startup, so a bad migration stops the app
- Out of scope, recorded so it is not mistaken for an omission: `EarWig/.kiro/docs/MIGRATION_PLAN.md` carries a stale address, `apt` provisioning and `/opt/stacks` paths that do not match this macOS host. It belongs to another repository and is not an eeroNotebook task, but it will mislead the next reader
- Also out of scope per the requirements' Non-Goals: podcasts and TTS, video overviews, infographics, slide export, instructor visibility of member scores, any role beyond owner and Viewer, team-wide libraries, and performance targets

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["2.4"] },
    { "id": 1, "tasks": ["3.4"] },
    { "id": 2, "tasks": ["3.5"] },
    { "id": 3, "tasks": ["5.1"] },
    { "id": 4, "tasks": ["5.2"] },
    { "id": 5, "tasks": ["5.3"] },
    { "id": 6, "tasks": ["5.4", "6.1"] },
    { "id": 7, "tasks": ["5.5", "6.2"] },
    { "id": 8, "tasks": ["6.3"] },
    { "id": 9, "tasks": ["6.4"] },
    { "id": 10, "tasks": ["6.5", "6.6", "7.1"] },
    { "id": 11, "tasks": ["7.2", "8.1"] },
    { "id": 12, "tasks": ["8.2", "8.5"] },
    { "id": 13, "tasks": ["8.3"] },
    { "id": 14, "tasks": ["8.4"] },
    { "id": 15, "tasks": ["8.6"] },
    { "id": 16, "tasks": ["8.7"] },
    { "id": 17, "tasks": ["9.1"] },
    { "id": 18, "tasks": ["9.2"] }
  ]
}
```
