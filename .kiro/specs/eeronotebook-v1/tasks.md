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

- [x] 2. Build the Inference Gateway
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
  - [x] 2.4 Verify the boundary holds
    - Verified the app reaches chat and embedding routes through the gateway alone, and that responses carry the logical name (`model=eero-synthesis`) rather than the backend model
    - Verified no host address or model server address appears in the app container's environment
    - Container healthcheck in place; the Uptime Kuma monitor is task 4.2
    - Verified with the gateway stopped: chat answers `502` and vector search `500`, both naming the fault, and both recover when it restarts
    - Source ingestion did degrade silently: embedding is a separate fire-and-forget job, so an unembedded Source read `completed` with no error while the real failure sat on a command record nothing pointed at
    - Fixed by reporting a failed `embed_source` job as a failed Source, which lands in upstream's existing failed-source UI and needs no frontend change; suppressed once the Source has embeddings, so a successful retry clears it
    - Configuration could not carry this — both embedding paths are fire-and-forget — so it is the first application code change, arriving before task 3.4's unmodified-upstream checkpoint
    - The report appears when the embed job exhausts its five retries, roughly a minute, rather than on first failure
    - **Closed out against the live deployment; everything above still holds.** The findings from here up are the earlier session's and were re-checked rather than restated. The fix is committed as `2fef1cf` and is still the most recent change to `api/routers/sources.py` and `open_notebook/domain/notebook.py`, so nothing since has overwritten it, and it is present in the running image. `tests/test_source_embedding_failure_visibility.py` pins it with 12 checks: the reporting rule itself, all three endpoints a member sees, extraction failure taking precedence, and a stale failure cleared once embedded
    - Requirement 3.8 re-verified on both sides of the boundary. The app service in `deploy/docker-compose.yml` names no host and no model server address, and neither does the running container's environment — no `host.docker.internal`, no `10.17.8.52`, no `:11434`, no `OLLAMA_*` — and its `ExtraHosts` is empty, so the application has no route to the host even if something were misconfigured. Its other source of addresses is the stored credential, which reads `http://eeronotebook-inference:4000/v1`; the three registered models are the logical names only, with no backend model name anywhere in the application
    - **Requirement 3.8 is now held by a test rather than by a hand check.** `tests/test_inference_boundary_config.py` reads the compose project and fails if the app service names a host or model server address, if it declares `extra_hosts`, or if any service other than the gateway addresses the backend — plus one check that the gateway *does*, so the other three cannot pass vacuously. Verified it bites: adding `OLLAMA_API_BASE: http://host.docker.internal:11434` to the app service fails two of them by name. The tempting fix for an inference problem is to point the application straight at the model server, and that is the edit this catches
    - Boundary re-verified end to end: the application's own model tests reach both routes through the gateway (chat answered, embeddings 768 dimensions), and gateway responses still carry the logical names — `model=eero-synthesis`, `model=eero-embed` — rather than `qwen2.5:14b` or `nomic-embed-text`. An unauthenticated `/v1/models` from the app container still answers `401`
    - **The embed-failure report re-verified against real rows, with the gateway left running.** A throwaway Source carrying a failed `embed_source` record of the shape the real fault produces read `failed` on all three surfaces — detail, `/status` and the list — each naming `eero-embed`; embedding it for real then cleared the report, so the suppression branch works against live data and not only against mocks. That exercised the SurrealQL the unit tests mock out, which is the half that could have silently matched nothing. Everything created was deleted: sources, embeddings and notebooks are back to 0 and the queue back to 57 completed / 16 failed. Two of those 16 are genuine `embed_source` failures carrying the gateway-unreachable message, from before the fix
    - **Not re-verified, deliberately:** the chat `502` and vector search `500` measured with the gateway stopped. Stopping it now would take the application down on a shared host and alarm the monitor, to reproduce a result already recorded. The mechanism is consistent with those two numbers and readable in the code — graph nodes classify an unreachable backend through `classify_error`, `api/main.py` answers `NetworkError` and `ExternalServiceError` as 502, and `api/routers/search.py` wraps its own failure as `500 Search failed: …` before a handler can map it, which is why search was the odd number. `tests/test_search_api.py` covers the vector path raising rather than returning an empty result, which is the silent degradation 3.5 forbids
    - Requirement 13.10 confirmed, not rebuilt: Kuma monitor 23, `eeroNotebook Inference Gateway`, probes `http://eeronotebook-inference:4000/health/liveliness` by keyword every 60 s and its latest heartbeat is up (`200 - OK, keyword is found`), with the stack group reporting all children up. The container healthcheck is healthy, and the gateway is still unpublished while sitting on `monitoring_net`, so task 4.2's added reachability still grants nothing
    - Noticed while checking: the queue holds no `new` and no `running` records even though `eeronotebook-db` had restarted five hours earlier — the condition that silenced the worker in task 3.4. One observation, not a test of the watchdog, but the failure it exists for was not present
    - Gates: `uv run pytest tests/` 727 passed / 18 skipped — 723 as of task 5.6 plus the four new boundary checks — with `uv run ruff check .` and `uv run python -m mypy .` clean across 145 source files
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
    - **RESOLVED — see the watchdog note at the end of this task.** The paragraph below records the defect as first found; it is now detected and recovered automatically.
    - **Worker: it consumes reliably only while its LIVE query lives.** `surreal_commands` 1.3.1 scans `status = 'new'` exactly once at worker startup, then subscribes to a LIVE query on `command` with no reconnect, no polling fallback and no error path around the subscription itself. The SurrealDB restart five hours before this run had silenced it: four commands sat `new` for roughly two hours with nothing logged and the worker process healthy. Restarting `eeronotebook-app` drained them in under a second. Recovery is an app restart and **nothing detects the condition** — reported, not fixed, because a fix means either patching the library's listener or adding a queue-depth monitor, and neither is in this task
    - Chunk sizing confirmed by measurement, not by reading the default: inside the app container, against the live `source_embedding` rows, `CHUNK_SIZE=400 CHUNK_OVERLAP=60 MIN_CHUNK_SIZE=5` (all defaults, `OPEN_NOTEBOOK_CHUNK_SIZE` unset) produced 57 chunks whose largest was **393 tokens**, none over `CHUNK_SIZE` and none near `nomic-embed-text`'s 2048. Two of the three Sources genuinely required splitting, so this is not a small-input artefact. 3.5 codifies it
    - Cleanup: the previous attempt's `Worker live-query probe` Notebook and its Source are gone; the test's own teardown left nothing behind, so task 6.1 has no litter from tasks 3–4 to migrate. The queue holds no `new` and no `running` records. The one `running` record was marked `failed` by hand with its reason — its Source and Notebook no longer existed and nothing in the stack reconciles `running`. The 13 terminal `failed` records from earlier runs were left alone as a truthful log. Stack left up and healthy
    - **Requirement 2.5 does not hold under *partial* coverage, which this task did not test.** A later ad-hoc run asked one question mixing a covered fact with an uncovered one — the Vessey coefficient, present, and optimal study temperature, absent. The covered half was answered correctly from source; for the uncovered half the model supplied "70-75 degrees Fahrenheit" from its own knowledge, hedged with "not directly stated in the retrieved results", and placed a citation beside it. So the refusal instruction holds when *nothing* is covered and leaks when *something* is. The all-or-nothing case is what was verified above; the mixed case is open, and prompt wording alone has not secured it on a 14B model
    - **Watchdog: the silenced worker is now detected and recovered.** `scripts/worker_watchdog.py` runs as its own supervisord program — separate so it survives the failure it watches — reads the queue, and restarts the worker when a command stays unclaimed past a threshold. The restart is the point rather than a blunt instrument: the library already contains the recovery path in its startup scan and simply never runs it again, so restarting executes the code that drains the backlog instead of reaching into its listener. Needs a supervisord control socket, confined to a unix socket at mode 0700 and never TCP, since that interface is unauthenticated
    - **The first version could never have fired, and reproducing the fault is what found it.** It aged rows from a `created` column, and `command` has no timestamp at all — its fields are `app`, `args`, `context`, `error_message`, `id`, `name`, `result`, `status`. The library's own startup scan orders by that same absent column, which SurrealDB tolerates silently. Worse, the log line rendered the missing measurement as `0s`, so a detector that could not work read as a healthy queue. Unit tests passed throughout. `QueueState.describe()` now separates "first observation" from "0s" and a test asserts it
    - Persistence replaced age: an observer remembers which command ids it has seen unclaimed and for how long, which measures non-consumption directly and needs nothing from the library or the schema. Depth is explicitly not the signal — a busy worker legitimately has queued work, and treating that as a fault would restart a healthy worker under load
    - Proven against the real failure, not a simulation: restarting SurrealDB left work unclaimed, the watchdog tracked it 0s → 30s → 60s → 90s, fired, restarted the worker (uptime reset to 3s), the queue drained after 101s, and the Source reached `completed`. 18 tests cover the decision logic
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
    - Registered as a `tls.certificates` entry, not a second default: `mon-traefik`'s dtefault certificate belongs to learninglab, and a second default would leave which one wins undefined — SNI selects instead
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

- [-] 5. Identity
  - [x] 5.1 Add a dedicated GoTrue service
    - `eeronotebook-auth` (`public.ecr.aws/supabase/gotrue:v2.188.1`) and `eeronotebook-authdb` (`public.ecr.aws/supabase/postgres:17.6.1.084`) — **the same pairing as `lh-db`/`lh-auth` on this host**, both on `eeronotebook_internal` only.
    - **Started on `postgres:16-alpine` and abandoned it.** Supabase's GoTrue expects the `auth` schema, Supabase's role set, a `search_path`, and enum types its later migrations create; supplying those by hand took four attempts and each fix surfaced the next. Copying the pairing already working ten feet away was the correct move several attempts earlier. `deploy/auth-db-init/` existed for that bootstrap and is gone
    - Four wrong turns worth keeping, because each looked right: `POSTGRES_USER` decides both which role receives `POSTGRES_PASSWORD` *and* whether it can write the `auth` schema — in this image `postgres` is **not** the superuser, `supabase_admin` is, so the default yields `permission denied for schema auth`. `supabase_auth_admin` exists but never receives our password, so it cannot authenticate over the network. The image's `pg_hba` trusts `127.0.0.1` unconditionally, so a local `psql` password test **passes without checking the password** — that false positive is what made the role look usable. And `GOTRUE_JWT_ADMIN_ROLES` was absent, so no token counted as admin: with sign-up disabled, there was no way to create a member and nobody could ever have signed in
    - The application now carries `GOTRUE_JWT_SECRET`, because the boundary verifies tokens in that process. It is read only inside `open_notebook/identity/`, which a test enforces Verified: empty `PortBindings` on both, no host listener on 9999, and neither container on `monitoring_net` (Requirements 13.2, 13.9)
    - Deliberately separate from `lh-auth`: a shared auth database or JWT secret would make a compromise or migration in `legendary-hunts` reach this stack, and its settings are tuned for a different application — 6-character passwords against this stack's 12
    - `AUTH_DB_PASSWORD` and `GOTRUE_JWT_SECRET` generated on the host into `deploy/.env`, appended without reading or rewriting any existing line, mode 600 preserved. Verified compose fails fast: unsetting `AUTH_DB_PASSWORD` gives "required variable AUTH_DB_PASSWORD is missing a value" rather than a silent empty string. The DB password is hex on purpose — it is interpolated into a `postgres://` URL, where `@ : / ?` would terminate the URL early
    - **Sign-up disabled and verified, not assumed:** `POST /signup` answers `422 signup_disabled`. Members come from the operator via the admin path, which needs a `service_role` token task 5.2 mints
    - Access tokens expire in an hour with rotating refresh tokens. The shared password they replace never expired at all
    - `GOTRUE_MAILER_AUTOCONFIRM` is on because no SMTP exists on this host: a member required to confirm by email could never sign in. Revisit if self-service signup is ever enabled
    - **The `curl` check 5.1 asked for paid off:** the GoTrue image ships no `curl`, only `wget` and `nc`. Probed with `wget --spider`; Postgres with `pg_isready` against its own named database, since the default probe checks `postgres`, which is not the database GoTrue uses
    - **Three boot failures, each a prerequisite Supabase's own image supplies and a plain cluster does not**, and each visible only at the end of a full dump of the failing migration: `API_EXTERNAL_URL` is required and unprefixed, unlike every `GOTRUE_*` setting around it; the `auth` schema must already exist before the first migration's `CREATE TABLE IF NOT EXISTS auth.users`; and `20240612123726_enable_rls_update_grants` grants to `postgres` and `dashboard_user`, failing with `role "postgres" does not exist` on a cluster whose superuser is named otherwise
    - Fixed in `deploy/auth-db-init/01-auth-schema.sql` rather than by hand against the running database, so a fresh deployment works without a manual step. Since initdb scripts only re-run on an *empty* data directory, each discovery cost a volume wipe — which is why the role list is generous rather than minimal, all `NOLOGIN`, nothing authenticating as them. 23 `auth` tables migrated on the successful run
    - Nothing consumes this yet, by design: the application has no auth environment and no `depends_on` for it, verified by inspection, and stayed healthy throughout. Adding the dependency before anything reads it would only mean the app could not start when GoTrue could not
    - **Backup coverage added, and proven.** `deploy/backup/` now takes a schema-scoped `pg_dump` of the identity database — one artifact, not two, because `pg_dump` is Postgres's restore path of record while a data directory copy is locked to the server's major version and unsafe taken hot. `PGPASSWORD` is required even over the container's own socket; without it `pg_dump` prompts, writes nothing, and the result looks like an empty database rather than a refused connection, so the script now refuses an empty dump outright and records the member count in the `MANIFEST`. Verified by restoring into a throwaway Supabase Postgres on `--network none`: three members back with three password hashes, zero errors
    - Three beta members (`person1`–`person3`) exist for testing. Verified: sign-in returns an access and a refresh token, a wrong password gives 400, sign-up gives 422, a non-admin role is refused 403 by the admin API, and a real GoTrue token verifies through the boundary and resolves idempotently to one member. All seven token-tamper variants refused — an earlier "tampered token accepted" was an unsound shell test that appended `x` after stripping the last character, so a token ending in `x` was left untouched
    - _Requirements: 4.1, 13.2, 13.9_
  - [x] 5.2 Build the Identity_Boundary
    - `open_notebook/identity/` holds the `IdentityProvider` protocol, the GoTrue implementation, and one FastAPI dependency. A route depends on `current_member` and receives a `Member` — never a token, a claim set, or a provider name
    - HS256 verification with the secret read once at construction: GoTrue signs symmetrically, so there is no key material to fetch per request and nothing to refresh. A provider publishing a JWKS would cache differently behind the same protocol
    - Audience is always checked; **issuer only when one is expected**, because GoTrue omits `iss` unless configured and verifying an absent claim would reject every valid token. Both are tested
    - `open_notebook/domain/member.py` is the local identity everything else references, so application data carries a member id and never a provider's user id (Requirement 4.4). Provider-agnostic in prose as well as in code, and a test asserts the module names no provider — it caught the first draft, whose own docstring named GoTrue
    - **Migration 24 defines `member` with a UNIQUE index on (provider, subject), and that index is the concurrency guarantee, not the application code.** `Member.resolve` reads then writes, which is racy by construction; the index means the loser's insert fails and it re-reads, instead of both succeeding and leaving one person with two identities and two sets of Notebooks
    - Verified on a scratch SurrealDB on `--network none` before going near live data: the duplicate is refused with `already contains ['p', 's1']` and the count stays at 1, a different subject is accepted, the ASSERT refuses an empty subject, and the down path removes the table. Corrected an assumption while there — `SCHEMAFULL` **drops** an unknown field rather than rejecting the write, so it prevents field creation and the ASSERTs are what refuse bad values
    - `current_member` raises `AuthenticationError` and never returns `None`. 401 means "you are nobody" and an empty list means "you own nothing"; conflating them is how an access check passes while enforcing nothing. Task 6.2 depends on that distinction holding
    - Boundary enforced by test, not convention: `tests/test_identity_boundary.py` reads `open_notebook/`, `api/` and `commands/` and fails if any module outside the package imports a JWT library or reads a `GOTRUE_` variable. It also asserts the search found something, so it cannot pass vacuously. A stub provider satisfying the protocol resolves through the same seam untouched, which is the only mechanical check of Requirement 4.4 available
    - `pyjwt` is now declared in `pyproject.toml`. It was already installed transitively, which works until the parent that pulls it changes
    - **Migration numbering shifted:** the plan reserved 24 for 6.1, but 5.2 lands first and migrations are sequential, so owner scope becomes 25
    - Deployed and verified on the Dev Server: a backup was taken first, the live database moved 23 → 24 on API startup, `member` is queryable, all five containers healthy, API `/health` 200. 676 tests pass, `ruff` and `mypy` clean
    - **Task 2.4's embed-failure fix is now deployed** as a side effect — the host checkout had been pinned behind it, and this is the first deployment past that commit. Confirmed present in the running image
    - _Requirements: 4.2, 4.3, 4.4_
  - [x] 5.3 Replace the shared password
    - `MemberAuthMiddleware` resolves every request to a member or refuses it. `api/auth.py` and `PasswordAuthMiddleware` are deleted
    - A middleware rather than a global dependency, because it must refuse *before* routing. Division of labour is deliberate: the middleware answers who is calling, and routes answer what that member may reach by depending on `current_member` (task 6.2). `current_member` reuses what the middleware resolved, so a route and the gate can never disagree about the caller
    - **The admin password resolves to a member rather than bypassing resolution** (operator's decision). It maps to one `admin-password`/`operator` member, so task 6.2's ownership checks apply to the operator exactly as to anyone else — a credential that skipped resolution would skip every check built on it. Compared in constant time, as upstream did
    - **Authentication now fails closed.** Upstream skipped it entirely when no password was configured, so a deployment that forgot to set one served every notebook to anyone. There is deliberately no environment variable restoring that
    - The suite's several hundred route tests assert routing, not authentication, and would otherwise all assert 401. The only bypass lives in `tests/conftest.py` and requires running pytest; tests that must see the real gate opt out with `@pytest.mark.no_auth_bypass`. `tests/test_member_auth_middleware.py` adds 13 covering refusals, the fail-closed case, admin resolution, and the exclusion list
    - Exclusion list is upstream's verbatim and pinned by a test: `/api/config` and `/api/auth/status` are read by the frontend before sign-in, and the documentation routes are how an operator inspects a deployment that is refusing their credentials
    - A failed identity-store read answers **503**, never an empty list. "The database is down" must not look like "this member owns nothing", which is the shape that silently passes an access check
    - `GET /api/auth/status` now reports how to authenticate rather than whether a password is set. Exposes no secret: the method, that signup is closed, and whether an operator credential exists
    - Verified on the deployment: `/health`, `/api/config`, `/api/auth/status` answer 200 unauthenticated; `/api/notebooks` gives 401 with no header, with a bad token, and with a `Basic` scheme; 200 with the admin password and 200 with a real member token. Both identities exist as `member` rows — `admin-password/operator` and `gotrue/<uuid>`. 707 tests, `ruff` and `mypy` clean
    - **Found for 5.4:** `auth_url` currently reports `http://eeronotebook-auth:9999`, which no browser can reach — GoTrue is unpublished by design (Requirement 13.2). Sign-in must therefore be proxied by the app (`POST /api/auth/login` calling GoTrue server-side and returning the token) rather than the browser talking to GoTrue directly. Publishing GoTrue instead would trade a requirement away for convenience
    - _Requirements: 4.1, 4.5_
  - [x] 5.4 Move the frontend onto member sessions
    - **A sign-in proxy was a prerequisite nobody had planned for.** The provider is unpublished (Requirement 13.2), so the browser cannot reach it at all; the plan assumed the frontend would talk to GoTrue directly. The application performs the exchange instead. Publishing GoTrue would have traded a requirement for convenience and put an unauthenticated auth API on a shared network
    - `POST /api/auth/login` accepts a member's email and password, or the operator's password alone. The operator's is checked **locally and first**, so they can still sign in while the provider is down — which is exactly when somebody needs to. A bare password never reaches the provider, or the operator's credential would be sent to it
    - `POST /api/auth/refresh`, because access tokens last an hour where the shared password never expired; without it a member is signed out mid-session. `GET /api/auth/me` for the signed-in indicator
    - Provider error messages are **not** forwarded: they distinguish "no such user" from "wrong password", which is account enumeration for free. An unreachable provider answers 5xx, never 401 — telling a member their password is wrong while the service is down sends them resetting a password that was fine
    - `/status` no longer reports the provider's address, since it is unreachable from a browser and advertising it only invites a client to try
    - Verified on the deployment: member sign-in returns an access token, a refresh token and `expires_in` 3600; that token reaches `/api/notebooks` at 200; `/me` reports the right member; refresh rotates both tokens; a wrong password and an unknown address both give `401 Invalid credentials` with nothing distinguishing them; the operator signs in with the password alone as `is_admin` with no refresh token. 723 tests, `ruff` and `mypy` clean
    - Browser half done. `LoginForm` takes an email as well as a password, and an empty email means the operator. `auth-store` holds the refresh token and expiry, refreshes a minute ahead, and renews *before* testing the session rather than signing someone out for leaving a tab open. `checkAuth` now calls `/api/auth/me`, which also feeds the indicator
    - A refused refresh token ends the session; a **network** failure does not, so a blip no longer forces a fresh sign-in. That distinction is the difference between "your session is over" and "the wifi dropped"
    - The persisted key stays `auth-storage` and the access token stays at `state.token`, so `getAuthToken()` and the api client's interceptor are untouched, as the task required
    - The sidebar shows who is signed in beside the existing sign-out. With per-member notebooks, which member is looking is the difference between an empty list and a missing one
    - Errors carry a translation key rather than a hardcoded English string: the store lives outside React and cannot call `t()`, so it names the reason and the component translates it. Five new keys across **all fourteen** locales, since each `satisfies TranslationShape` and a missing key fails `tsc`
    - The locale parity test caught two mistakes of mine: three keys added without being wired to anything, and then a template literal that hid the key names from its scanner. Both fixed by referencing the keys literally
    - `npm run lint` (0 errors, 7 pre-existing warnings), `npm run test` 140 passed, `npm run build` clean, `tsc --noEmit` clean. Backend 723 passed
    - Verified through the app's own origin, as a browser would: member sign-in returns access and refresh tokens with `expires_in` 3600; `/api/auth/me` reports the right member; a refreshed token reaches `/api/notebooks` at 200; the operator path still returns `is_admin` with no refresh token; three members exist in the identity store
    - **Not verifiable from here, and unchanged:** the rendered DOM and the real browser session. `eeronotebook.local` resolves nowhere and the private CA is untrusted, both needing `sudo` on the Dev Server. `/login` answers 200 and the component and build are verified, but "a member types a password into a browser and lands on their notebooks" has not been observed
    - **Found, and a decision rather than a defect:** an old refresh token still returns 200 after GoTrue's `REUSE_INTERVAL` of 10s has passed — tested at 20s. A leaked refresh token therefore stays usable. Setting the interval to 0 makes reuse detection strict, but then two tabs refreshing at once can revoke the whole session, which in a classroom is a real cost. Left as configured and recorded, because the trade-off is the operator's to make
    - Keep the session under the existing `auth-storage` key so `apiClient`'s interceptor keeps working unchanged, and add refresh handling — access tokens expire where the password never did
    - Add sign-out, and surface the signed-in member in the layout
    - Every new string through `t('...')`, with keys added to all locales under `frontend/src/lib/locales/`; en-US is the reference and a missing key fails `tsc`
    - `npm run lint`, `npm run test`, `npm run build`
    - _Requirements: 4.1, 4.5_
  - [x] 5.5 Test the Identity_Boundary
    - Delivered with 5.2 rather than after it: `tests/test_identity_boundary.py` covers a valid token resolving to a member, and expired, wrong-signature, malformed, wrong-audience, wrong-issuer, missing-subject, `alg: none` and absent tokens each refused. All raise `AuthenticationError`, which `api/main.py`'s existing handler answers as 401
    - A stub provider satisfying the protocol resolves through the same seam with no change to the seam, and `IdentityProvider` is `runtime_checkable` so that is asserted mechanically rather than by inspection
    - A missing secret raises `ConfigurationError`, not `AuthenticationError`: nobody's credentials are wrong, the deployment is, and failing loudly beats verifying every token against an empty key
    - 18 tests. Optional in the plan, written anyway — the boundary's whole value is that it refuses things, and an unrefused token is invisible until it matters
    - _Requirements: 4.2, 4.3, 4.4, 4.5_
  - [x] 5.6 Checkpoint — identity
    - All six gates green: `uv run pytest tests/` 723 passed / 18 skipped, `ruff check .` clean, `mypy` clean across 144 source files, and the frontend's `npm run lint` (0 errors, the same 7 pre-existing warnings), `npm run test` 140 passed / 23 files, `npm run build` clean
    - `ruff` is not on `PATH` as a bare command in this environment; it runs as `uv run ruff check .`
    - **Also verified against the live deployment, which the checkpoint does not require but a demo does.** All five containers healthy; `tests/integration/test_baseline.py` ran 17/17 twice against `http://10.17.8.52:5055` — ingestion of all three Source kinds, embeddings, vector search, citations that resolve, refusal of an uncovered question, and cascade delete. So identity did not break the baseline that task 3.4 established
    - The two runs took 8m01s and 10m12s against 3.4's ~4m15s, and the cause is contention, not regression: they **overlapped**, and `OPEN_NOTEBOOK_WORKER_MAX_TASKS=1` serialises every ingestion in the deployment behind one worker. Worth knowing before several people upload at once
    - `member` was empty at the start of this check while GoTrue still held all three beta members, and that asymmetry is by design — `Member.resolve` recreates the local row on first sign-in, so the identity store is self-healing where the provider's user list is not. The admin password resolved to a fresh `member` row on the first authenticated call
    - The deployed checkout sits one commit behind `origin/eero` (`b658bf1` against `6e261a6`), and that commit touches only `tasks.md`, so the running code is current
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

- [ ] 6. Ownership and sharing
  - [-] 6.1 Add owner scope to the data model
    - Migration `25.surrealql` and `25_down.surrealql`, registered in `AsyncMigrationManager` at index 24 of both lists. `owner` on `notebook` is `option<record<member>>` with an index, plus the `share` relation, defined here rather than in 6.3 so 6.2 can enforce owner-or-Share from the outset. **Renumbered from 24** (5.2 took it), and taking 25 pushed 7.1 to 26 and 8.1 to 27 — both task bodies updated, since `tasks.md` had assigned 25 to 6.1 *and* 7.1
    - `owner` lives on `notebook` and nowhere else. Sources, notes and Study_Artifacts carry none of their own and inherit through the existing `reference` and `artifact` relations. One owner field is one place for an access check to consult; four would be four things to keep in agreement across an upstream merge, and a test asserts no second table gains one
    - **`owner` is optional, and that is a concession with a named cost.** A required field is the stronger guarantee — an unowned Notebook becomes unrepresentable — but SurrealDB does not rewrite existing rows when a field is defined: every Notebook predating the migration would become unwritable on its next UPDATE, and Notebook creation would fail outright until 6.2 wires an owner into the create path. The up path runs on API startup, so that is an app that will not boot. Optional instead, with the rule that `owner = NONE` means reachable by **nobody**. The dangerous reading of "inherit" is the opposite one, and `tests/test_owner_scope.py` exists to keep it from being written
    - Safe because `repo_update` issues `UPDATE $target MERGE $data`, not a replace, so `_prepare_save_data` dropping a `None` owner leaves the stored value alone. Were it a replace, renaming a Notebook would silently unown it — and an unowned Notebook is invisible to everyone. Pinned by a test, because it is a property of the repository layer that this design leans on rather than anything visible in the model
    - `role` on `share` is `ASSERT $value = 'viewer'`. That is Requirement 6.6 met structurally rather than by policy: there is no role to widen to, and a second one cannot exist without passing the ASSERT and a schema migration. A UNIQUE index on `(in, out)` gives one Share per member per Notebook — two would make revocation partial, deleting one while the other still granted access
    - Cascade events delete a Share when its Notebook or its member goes, at the database rather than in a route, so no delete path can forget. Verified: the share count went 1 → 0 on a notebook delete
    - **Verified up and down on a scratch SurrealDB 2.6.5 running the real `AsyncMigrationManager`, not a hand-written approximation of it** — 45 checks across an empty database and a populated one, all green. The populated case reproduced both carried findings: four orphaned Sources (3.4) and an *empty* `member` table (5.6). Down reverses the schema, destroys no content, and up runs again afterwards without wedging
    - Four SurrealQL behaviours the migration depends on were measured rather than assumed, and each could have broken it silently: `DEFINE FIELD` followed by an `UPDATE` using that field **does** work in one migration query; `WHERE owner IS NONE` **does** match rows created before the field existed; `IF … THEN … ELSE … END` works as a `LET` expression; and `array::first([])` is `NONE`. Also worth knowing for the next schema test: `INFO FOR DB` renders `FROM member TO notebook` back as `IN member OUT notebook`, which failed an assertion of mine that was wrong about the schema rather than the schema being wrong
    - **Requirement 7.5's owner is the admin member, created by the migration when it is absent.** 5.6's finding makes this necessary rather than tidy: `member` can be empty while the provider holds accounts, because `Member.resolve` recreates the local row on first sign-in, so the migration cannot assume an owner exists to point at. It creates `admin-password`/`operator` — the identity the shared password resolves to, and the honest answer to "who made this", since everything predating task 5 was created by whoever held that password. `Member.resolve` finds it by (provider, subject) afterwards and reuses it; migration 24's UNIQUE index makes a second one impossible. A test asserts the migration's literals match `ADMIN_PROVIDER` and `ADMIN_SUBJECT`, because if they drift the content is assigned to an identity nobody can sign in as and 7.5 is met in name only
    - Created **only when there is something to assign**, so installing from scratch manufactures no phantom owner — confirmed: 0 members and 0 notebooks after the migration on an empty database. The backfill `UPDATE` is guarded on `$admin != NONE` so it can never write a null over an owner, and there is no silent path: if the admin were needed and could not be created, the CREATE fails and the migration fails with it
    - **The orphaned Sources from 3.4: fail-closed, and the decision is to adopt them rather than delete or ignore them.** For access they are already fail-closed — an orphan resolves to no Notebook, so no check can grant it, and it is not a disclosure. The problem is at the other end: once 6.2 scopes every route through a Notebook, orphaned content can never be read or deleted through the application again, so leaving it means storage nobody can reach and nobody can clear. So the migration collects orphaned Sources *and* notes into one `Recovered content (migration 25)` Notebook owned by the admin. That **narrows** access — from anyone holding the shared password to the admin alone — and leaves the content visible enough for an operator to review. Deleting them was the alternative and was rejected: a migration that destroys content on startup is not a failure anyone can undo. The Notebook is created only when there is something to put in it, and a test forbids `DELETE source` / `DELETE note` appearing in the file
    - **Orphaning is ongoing, not historical, and that is the more useful half of 3.4's finding.** Measured directly: deleting a `notebook` record makes SurrealDB delete the `reference` and `artifact` edges itself while **keeping** the Source. So `Notebook.delete()` did not forget to clean up — its default `delete_exclusive_sources=False` deliberately keeps Sources, because upstream treats a Source as library content that may live in zero Notebooks. Migration 25 is therefore a one-time sweep and cannot prevent the next one. **For 6.2:** either deleting a Notebook takes its exclusive Sources with it, or orphans need a home, but the current default quietly produces unreachable content on every delete. This also explains 3.4's confusion about why its own cascade check passed while a hand-made Notebook left four Sources behind — the two paths differ in that flag
    - Notes are handled alongside Sources because `SourceInsight.save_as_note()` takes `notebook_id=None`, so a note with no Notebook is a reachable state and not corruption
    - Also found, recorded and deliberately **not** fixed: `ensure_record_id` is not idempotent for a numerically-keyed id — `member:9999` → `member:⟨9999⟩` → `member:⟨⟨9999\⟩⟩`, so such an owner would point somewhere new on every save. SurrealDB issues twenty-character alphanumeric keys, so an all-digit one is theoretical; the consequence is an owner pointing at a nonexistent record, which fails closed; and `ensure_record_id` is shared with every record link in the codebase, so changing it is its own task. Pinned by a test that fails if the escaping ever widens to the ids actually issued
    - `tests/test_owner_scope.py`, 32 checks over the schema text and the model field. Mutation-checked rather than assumed: widening `role` to include `editor`, dropping the `$admin != NONE` guard, and swapping orphan adoption for `DELETE source` each fail exactly the test meant to catch them. One check covers all 50 migration files rather than only 25 — `AsyncMigration.from_file` joins every line into one, so a comment after SQL on the same line would comment out every statement that follows, and nothing else would report it
    - Gates: `uv run pytest tests/` **759 passed / 18 skipped** (727 as of 2.4, plus these 32), `uv run ruff check .` clean, `uv run python -m mypy .` clean across 146 source files
    - **Not applied to the Dev Server, and not verifiable from here.** `ssh` to the host is refused (publickey), so `deploy/backup/backup.sh` — which needs docker access to the `eeronotebook_db_data` volume — cannot run, and the live migration must not precede the backup. The API answers `/health` 200 but `/api/notebooks` 401, and `deploy/.env` is gitignored and absent from this workspace, so the live notebook, Source and note counts were **not** confirmed either: 2.4's "back to 0" is carried forward unverified rather than re-measured. Requirement 13.2 does still hold — the Dev Server's SurrealDB is unpublished, and the `200` on port 8000 is `labplatform-app` (uvicorn, no `/version`), not the database
    - **Left to the operator, in this order:** run `deploy/backup/backup.sh` on the host and confirm three artifacts plus a `MANIFEST`; update the host checkout and restart `eeronotebook-app`; watch the startup log for the version moving 24 → 25, because a failed up path stops the app. Then check whether a `Recovered content (migration 25)` Notebook appeared — if one did, the orphan sweep found pre-task-5 content that 2.4's counts said was gone, and what is inside it is worth reading before deleting. Do **not** reach for `restore.sh --production` casually; it has still never been run against live data
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
  - [~] 6.4 Scope search and stop disclosing existence
    - `text_search` and `vector_search` in `open_notebook/domain/notebook.py` take the resolved member and filter inside the SurrealQL, not after it — filtering the result set in Python still leaks counts and scores
    - `POST /api/search` and the ask graph pass the member through; the graph nodes are sync and reach async code through the existing event-loop workaround, so follow `chat.py`'s pattern exactly rather than inventing a second one
    - A Notebook the member cannot reach returns 404 rather than 403, on every route that addresses one by id, so the response does not confirm it exists
    - Same for Sources, notes and Study_Artifacts addressed directly by id
    - _Requirements: 7.3, 7.4_
  - [~] 6.5 Build the access enforcement suite
    - `tests/test_access_enforcement.py`, parameterised over the registered route table, so a route added later without scoping fails a test instead of passing unnoticed
    - Cover: a member with no access gets 401 or 404 and never content; a Viewer's writes are refused; a revoked Share ends access; search excludes inaccessible material; existence is not disclosed
    - Deliberately not marked optional. Requirement 14.4 re-runs this after every upstream merge, and the design names access enforcement the highest-consequence divergence — an optional suite would be skipped exactly when it matters
    - _Requirements: 7.1, 7.3, 7.4, 5.4, 5.5, 6.4, 6.5_
  - [~] 6.6 Settle the MCP interface
    - There is no MCP server in this repository. `open-notebook-mcp` is a separate package documented in `docs/5-CONFIGURATION/mcp-integration.md`; it holds no database access and reaches the API over HTTP, so it inherits whatever 6.2 and 6.4 enforce rather than enforcing anything itself
    - Confirm that rather than assume it: check its published tool list against the scoped routes, then point it at the deployed API as a member with no access and assert it returns nothing
    - It authenticates with `OPEN_NOTEBOOK_PASSWORD`, which 5.3 removes. Either give an MCP client a way to hold a member token, or record that the interface is unavailable at v1 — leaving it half-configured is the outcome to avoid
    - Write the conclusion into `docs/`, since a reader who finds Requirement 7.2 will otherwise look for a second enforcement point in this codebase and not find one
    - _Requirements: 7.2_
  - [~] 6.7 Checkpoint — access control
    - Ensure all tests pass, ask the user if questions arise.
    - 6.5's suite must be green before task 8 adds a new category of Notebook-owned data

- [ ] 7. Study progress
  - [~] 7.1 Model Study_Progress per member
    - Flashcard scheduling state and quiz attempts keyed to member and Study_Artifact, stored separately from the artifact
    - Migration `26.surrealql` and its down file, plus the `AsyncMigrationManager` entry. **Renumbered from 25:** task 6.1 took 25 for owner scope and the `share` relation, and migrations are sequential
    - Domain model in a new `open_notebook/domain/study.py`; every read and write filters on the resolved member, with no code path that takes a member id from the request body
    - Deleting a Share leaves progress rows untouched; deleting the artifact is the only thing that removes them **(\*)**
    - No endpoint returns another member's progress, and none exposes an aggregate across members — an average over a class of two identifies both **(\*)**
    - _Requirements: 9.3, 9.4, 10.2, 10.4_
  - [~] 7.2 Test progress isolation
    - Two members on one shared artifact: each sees only their own state, and one member's review does not change the other's next due card
    - Revoking a Share leaves the revoked member's rows present, and readable again if access is restored
    - _Requirements: 9.3, 9.4, 10.2, 10.4_

- [ ] 8. Study artifacts
  - [~] 8.1 Structured generation
    - Generate against `qwen2.5:14b` using tool calling to constrain output to a schema; do not parse free text
    - Validate every response against the schema, retry on failure, and never persist an invalid artifact
    - Record the Sources each Study_Artifact was generated from
    - Migration `27.surrealql` and its down file for `study_artifact`, plus the `AsyncMigrationManager` entry; the record holds its Notebook, its type, the Sources it came from, and the validated payload. **Renumbered from 26:** 6.1 took 25 and 7.1 took 26, and migrations are sequential
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
  - [~] 8.6 Regenerate an artifact after its Sources change
    - Owner-only route that regenerates in place and records the new Source set
    - Decide what happens to Study_Progress: a card that no longer exists cannot keep a schedule, and quietly discarding a member's history would be the wrong default. Implement the choice and record it
    - _Requirements: 8.5_
  - [~] 8.7 Test schema validation and retry
    - For a response that fails validation, nothing is persisted and the job retries; this is the structured-output risk the design's Risks table names
    - A response that never validates ends as a failed job carrying a reason, not as a silent absence
    - The Sources recorded on an artifact are the Sources fed into it
    - _Requirements: 8.2, 8.3, 8.4_
  - [~] 8.8 Checkpoint — study artifacts
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
