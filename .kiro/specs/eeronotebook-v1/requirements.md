# Requirements Document

## Introduction

eeroNotebook is a private, self-hosted study notebook service for a small team, including classroom use. Members add their own sources, ask questions answered only from those sources with verifiable citations, and generate study material — flashcards, quizzes, and mind maps — from them. It runs on the team's own Dev Server and performs all inference locally, so no source material or query leaves the premises.

It is built as a maintained code fork of Open Notebook v1.14.0 (MIT), which supplies source ingestion, grounded chat with citations, notes, search, and a provider-agnostic model layer. eeroNotebook adds per-user ownership, selective sharing, and study artifacts, none of which upstream provides.

Every component of eeroNotebook runs in a container. Inference is reached through a containerised gateway rather than by addressing a model server, so the backend can move without the application changing.

Canonical domain vocabulary is defined in [CONTEXT.md](../../../CONTEXT.md) and is not duplicated here. Decisions behind these requirements are recorded in [ADR 0001](../../../docs/adr/0001-code-fork-of-open-notebook.md), [ADR 0002](../../../docs/adr/0002-per-user-notebooks.md), [ADR 0003](../../../docs/adr/0003-pluggable-identity-boundary.md), and [ADR 0004](../../../docs/adr/0004-containerised-inference-gateway.md).

## Glossary

Domain terms — **eeroNotebook**, **Dev Server**, **Host Ollama**, **Notebook owner**, **Share**, **Viewer** — are defined in `CONTEXT.md`. The names below are component and concept names local to this specification.

- **Notebook**: A single collection of sources, notes, and generated material, owned by exactly one member
- **Source**: One ingested item of material within a Notebook — a document, web page, or transcript
- **Grounded_Answer**: A response to a member's question derived only from the Sources of one Notebook, carrying citations to the passages it relied on
- **Identity_Boundary**: The single internal seam that resolves an incoming request to an authenticated member, isolating provider-specific token verification from the rest of the application
- **Identity_Provider**: The external service that authenticates members and issues tokens; GoTrue at v1
- **Inference_Gateway**: The containerised, OpenAI-compatible endpoint that is eeroNotebook's only source of inference, forwarding to whichever backend currently serves the models
- **Inference_Backend**: The model server behind the Inference_Gateway; natively-hosted Ollama on the Dev Server at v1
- **Study_Artifact**: Generated study material belonging to a Notebook — a flashcard deck, a quiz, or a mind map
- **Study_Progress**: A member's personal state against a Study_Artifact, such as review scheduling or quiz attempts
- **Synthesis_Model**: The language model used for grounded answers and Study_Artifact generation; `qwen2.5:14b` at v1
- **Background_Model**: The smaller language model used for cheap ancillary work such as titling and summarising; `llama3.2:3b` at v1
- **Embedding_Model**: The model producing vectors for search; `nomic-embed-text` at v1
- **Notebook_Stack**: The Docker Compose project deploying eeroNotebook on the Dev Server

## Requirements

### Requirement 1: Source Ingestion

**User Story:** As a team member, I want to add course material of several kinds to a Notebook, so that everything I am studying is answerable in one place.

#### Acceptance Criteria

1. THE Notebook SHALL accept Sources supplied as uploaded documents, web page URLs, and video URLs
2. WHEN a member adds a Source, THE Notebook SHALL process it into retrievable text and report completion or failure for that Source individually
3. WHEN a Source is processed, THE Notebook SHALL generate embeddings using the Embedding_Model and make the Source available to search
4. THE Notebook SHALL divide Source text into chunks that do not exceed the Embedding_Model's 2048-token input window
5. IF a Source fails to process, THEN THE Notebook SHALL retain the failure reason against that Source and SHALL NOT block the processing of other Sources

### Requirement 2: Grounded Answers with Citations

**User Story:** As a team member, I want answers drawn only from my own material with references I can check, so that I can trust what I am studying from.

#### Acceptance Criteria

1. WHEN a member asks a question of a Notebook, THE eeroNotebook SHALL produce a Grounded_Answer derived only from that Notebook's Sources
2. THE Grounded_Answer SHALL carry citations that resolve to the specific Source and passage relied upon
3. WHEN a member selects a citation, THE eeroNotebook SHALL present the cited passage in its surrounding context
4. THE eeroNotebook SHALL NOT incorporate material from Sources belonging to any other Notebook into a Grounded_Answer
5. IF no Source supports an answer, THEN THE eeroNotebook SHALL state that the material does not cover the question rather than answering from model knowledge

### Requirement 3: Local Inference

**User Story:** As the team's owner of this system, I want all inference to happen on our own hardware, so that private course material and member queries never leave our premises.

#### Acceptance Criteria

1. THE eeroNotebook SHALL obtain all language model and embedding inference through the Inference_Gateway, and SHALL NOT address a model server directly
2. THE eeroNotebook SHALL NOT transmit Source content, member queries, or generated material to any third-party inference service
3. THE eeroNotebook SHALL use the Synthesis_Model for Grounded_Answers and Study_Artifact generation, and MAY use the Background_Model for ancillary work
4. THE Notebook_Stack SHALL limit itself to one concurrent background task, so that it does not exhaust inference capacity shared with other services on the Dev Server
5. IF the Inference_Gateway or its backend is unreachable, THEN THE eeroNotebook SHALL report the fault to the member rather than degrading silently
6. THE Inference_Gateway SHALL serve both a chat completion route and an embedding route, since Source ingestion depends on the Embedding_Model as much as chat depends on the Synthesis_Model
7. WHEN the inference backend is replaced, THE eeroNotebook SHALL require changes only to Inference_Gateway configuration, and SHALL NOT require application changes
8. THE eeroNotebook SHALL hold no host address and no model server address in its own configuration

### Requirement 4: Member Authentication

**User Story:** As a team member, I want my own login, so that my material is mine rather than shared behind one password.

#### Acceptance Criteria

1. THE eeroNotebook SHALL require each member to authenticate as an individual before reaching any Notebook
2. THE eeroNotebook SHALL resolve every incoming request to an authenticated member through the Identity_Boundary
3. THE eeroNotebook SHALL confine all Identity_Provider-specific token verification to the Identity_Boundary, so that no other part of the application depends on which provider is in use
4. WHEN the Identity_Provider is replaced, THE eeroNotebook SHALL require changes only within the Identity_Boundary and its configuration
5. IF a request cannot be resolved to an authenticated member, THEN THE eeroNotebook SHALL refuse it

### Requirement 5: Notebook Ownership

**User Story:** As a team member, I want sole control of the Notebooks I create, so that nobody else can alter my material.

#### Acceptance Criteria

1. WHEN a member creates a Notebook, THE eeroNotebook SHALL record that member as its Notebook owner
2. THE eeroNotebook SHALL permit only the Notebook owner to add or remove Sources, edit notes, or change Notebook settings
3. THE eeroNotebook SHALL apply the access of a Notebook to its Sources, notes, and Study_Artifacts
4. THE eeroNotebook SHALL make a Notebook visible to no member other than its owner unless a Share exists
5. WHILE a member is authenticated, THE eeroNotebook SHALL expose only Notebooks that member owns or has been granted access to through a Share

### Requirement 6: Selective and Revocable Sharing

**User Story:** As an instructor, I want to give specific people access to a Notebook and withdraw it later, so that I can run a class without exposing everything to everyone.

#### Acceptance Criteria

1. THE Notebook owner SHALL be able to create a Share granting one named member access to one Notebook
2. THE eeroNotebook SHALL grant a member holding a Share the role of Viewer on that Notebook
3. THE Viewer SHALL be able to read Sources, ask questions receiving Grounded_Answers, and use existing Study_Artifacts
4. THE eeroNotebook SHALL NOT permit a Viewer to add or remove Sources, edit notes, or change Notebook settings
5. WHEN a Notebook owner revokes a Share, THE eeroNotebook SHALL end that member's access immediately and SHALL leave the Notebook's contents unaltered
6. THE eeroNotebook SHALL NOT make a Notebook visible to the team at large through any single action

### Requirement 7: Access Enforcement Across All Interfaces

**User Story:** As the team's owner of this system, I want access rules enforced everywhere the data is reachable, so that a second interface does not become a way around them.

#### Acceptance Criteria

1. THE eeroNotebook SHALL enforce Notebook ownership and Share access on every route of its REST API
2. THE eeroNotebook SHALL enforce the same access rules on its MCP interface as on its REST API
3. THE eeroNotebook SHALL exclude Sources, notes, and Study_Artifacts a member cannot access from all search results
4. THE eeroNotebook SHALL NOT expose the existence of a Notebook to a member who has neither ownership nor a Share
5. WHEN content created before member authentication existed is migrated, THE eeroNotebook SHALL assign it to a named Notebook owner rather than leaving it unscoped

### Requirement 8: Study Artifact Generation

**User Story:** As a team member, I want study material generated from my sources rather than written by hand, so that preparing to study is not itself the work.

#### Acceptance Criteria

1. THE eeroNotebook SHALL generate Study_Artifacts from the Sources of a single Notebook
2. THE eeroNotebook SHALL constrain Study_Artifact generation to a defined schema using the Synthesis_Model's tool-calling capability rather than parsing free-form text
3. WHEN a generated Study_Artifact fails schema validation, THE eeroNotebook SHALL retry generation and SHALL NOT persist an invalid artifact
4. THE eeroNotebook SHALL record which Sources each Study_Artifact was generated from
5. THE Notebook owner SHALL be able to regenerate a Study_Artifact after its Sources change

### Requirement 9: Flashcards

**User Story:** As a team member, I want flashcards with spaced repetition, so that I retain material rather than merely reviewing it.

#### Acceptance Criteria

1. THE eeroNotebook SHALL generate a flashcard deck as a Study_Artifact from a Notebook's Sources
2. THE eeroNotebook SHALL schedule each card for review per member according to that member's own recall history
3. THE eeroNotebook SHALL maintain Study_Progress for flashcards separately for each member, including on a shared Notebook
4. THE eeroNotebook SHALL NOT allow one member's review activity to alter the schedule presented to another member

### Requirement 10: Quizzes

**User Story:** As a team member, I want to test myself against my material and see where I was wrong, so that I can find the gaps before an exam does.

#### Acceptance Criteria

1. THE eeroNotebook SHALL generate a quiz as a Study_Artifact from a Notebook's Sources, with an answer and an explanation for each question
2. WHEN a member completes a quiz attempt, THE eeroNotebook SHALL record that attempt and its score as that member's Study_Progress
3. THE eeroNotebook SHALL present a member's own attempt history to that member
4. THE eeroNotebook SHALL retain a member's quiz Study_Progress when a Share granting access to the Notebook is revoked

### Requirement 11: Mind Maps

**User Story:** As a team member, I want to see how the concepts in my material relate, so that I can grasp a topic's structure rather than only its details.

#### Acceptance Criteria

1. THE eeroNotebook SHALL generate a mind map as a Study_Artifact expressing the hierarchical relationships among concepts in a Notebook's Sources
2. THE eeroNotebook SHALL persist a mind map as structured data rather than as a rendered image, so that it remains navigable
3. THE eeroNotebook SHALL present a mind map such that a member can expand and collapse its branches

### Requirement 12: Text Study Material

**User Story:** As a team member, I want study guides and briefing documents from my sources, so that I have written material to revise from.

#### Acceptance Criteria

1. THE eeroNotebook SHALL produce study guides, briefing documents, FAQs, and timelines from a Notebook's Sources
2. THE eeroNotebook SHALL implement these outputs as configured prompt templates within upstream's transformation mechanism, without application code
3. THE eeroNotebook SHALL retain generated text material as notes within the Notebook that produced it

### Requirement 13: Deployment and Operation

**User Story:** As the operator of the Dev Server, I want eeroNotebook to behave like the other stacks on the host, so that running it requires no special knowledge.

#### Acceptance Criteria

1. THE Notebook_Stack SHALL deploy as a Docker Compose project under the Dev Server's stacks directory, alongside the existing stacks
2. THE Notebook_Stack SHALL publish only its web interface and its API to the host, and SHALL NOT publish its database or Identity_Provider
3. THE Notebook_Stack SHALL avoid host ports already held by other stacks on the Dev Server
4. THE Notebook_Stack SHALL encrypt stored provider credentials at rest and SHALL NOT retain the upstream default database credentials
5. THE eeroNotebook SHALL be reached over TLS, terminated by the Dev Server's existing reverse proxy, so that member credentials and session tokens do not cross the network in clear text
6. THE Notebook_Stack SHALL expose a health endpoint to the Dev Server's existing uptime monitoring
7. THE Notebook_Stack SHALL hold the Dev Server's address in a single configuration value, so that relocating the host requires one change
8. THE Notebook_Stack SHALL have its persistent data backed up on a schedule, and the backup SHALL be proven by a restore
9. THE Notebook_Stack SHALL run every component of eeroNotebook as a container, and SHALL place no eeroNotebook service directly on the host operating system
10. THE Notebook_Stack SHALL expose a health check for the Inference_Gateway to uptime monitoring, since all inference depends on it

### Requirement 14: Upstream Divergence Discipline

**User Story:** As a maintainer, I want our changes to stay mergeable with upstream, so that we keep receiving improvements instead of stranding ourselves on a fork.

#### Acceptance Criteria

1. THE eeroNotebook SHALL track a pinned upstream release rather than upstream's default branch
2. THE eeroNotebook SHALL carry its changes on a dedicated long-lived branch
3. THE eeroNotebook SHALL express any capability achievable through upstream configuration as configuration rather than as code
4. WHEN upstream changes are merged, THE eeroNotebook SHALL verify that access enforcement per Requirement 7 still holds

## Non-Goals

Deliberately excluded from v1, recorded so their absence reads as a decision rather than an omission.

- **Podcasts and Audio Overviews.** The only capability that would have required egress. Local text-to-speech already exists on the Dev Server for a later pass.
- **Video overviews, infographics, and slide export.**
- **Instructor visibility of member scores.** Study_Progress is private to each member. Making it reportable is a materially different design and is cheaper to add deliberately than to retrofit.
- **Roles beyond Notebook owner and Viewer.** No editor role, no per-Notebook role matrix.
- **Team-wide notebook libraries.** Access is granted per person, never to the team at large.
- **Capacity, latency, and concurrency targets.** No performance thresholds have been agreed; the Dev Server is shared with other stacks and v1 is scoped to a small team.
