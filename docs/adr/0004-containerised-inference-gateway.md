# 0004: Inference is consumed through a containerised gateway

**Status:** Accepted
**Date:** 2026-09-09

Every eeroNotebook service runs in a container; nothing is placed on the host. Inference is the one dependency that cannot follow that rule on the current Dev Server. The server is an Apple Silicon Mac, where Linux containers run inside a virtual machine that has no path to the GPU: Metal is a macOS userspace API with no device node to pass through, so a containerised model server falls back to CPU. Measured on the Dev Server with `llama3.2:3b` and an identical prompt, native inference reached 87.8 tokens per second while the same model in a container reached 0.50 — roughly 176 times slower, with Ollama's own log reporting `library=cpu` after finding no GPU. A containerised 14B is therefore not viable here.

eeroNotebook consequently does not talk to a model server directly. The stack contains an inference gateway — a containerised, OpenAI-compatible endpoint that the application treats as its only source of inference. The gateway forwards to whatever backend is available; today that is Ollama running natively on the Dev Server, reached over the container runtime's host alias.

This keeps every eeroNotebook component containerised, gives the application a stable in-stack endpoint that carries no knowledge of the host, and makes the backend a swappable dependency: moving inference to a GPU-equipped Linux host, where the model server itself can be containerised, becomes a gateway configuration change rather than an application change.

## Alternatives considered

- **Containerise the model server on this host:** Honours the containerisation rule literally, but costs the GPU. At the measured CPU rate the study artifacts this product exists to produce would not be generatable. Rejected on capability, not principle.
- **Point the application directly at native Ollama:** What the previous design did. Rejected because it makes a host process an architectural dependency of the application and encodes a host address in application configuration.
- **Move inference to a GPU-equipped Linux host now:** The preferred end state, where the model server is containerised and accelerated. Deferred only because it requires hardware the team does not yet have; this decision is what makes that migration cheap.

## Consequences

- The native Ollama process remains on the Dev Server as a backend, but it is no longer part of eeroNotebook's architecture — it sits behind the gateway, and only the gateway knows it exists.
- Ollama stays bound to loopback. The gateway reaches it through the runtime's host alias, so no unauthenticated inference API is exposed to the network.
- The gateway is a single point of failure for all inference and must be covered by health monitoring.
- Model names become gateway configuration, so changing the backing model does not touch application settings.
- This decision should be revisited when inference moves to hardware where the model server can itself be containerised.
