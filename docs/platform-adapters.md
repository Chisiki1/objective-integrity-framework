# Platform Adapters

The framework is platform-agnostic. Adapters translate its records and invariants into a particular runtime's configuration style without changing the core objective or evidence model.

## Generic Adapter

Use `adapters/generic/system-developer-adapter.md` to adapt the framework into a system or developer prompt for any agent runtime that supports project guidance.

- Assumption: the runtime supports project-local guidance or an equivalent prompt/configuration layer.
- Boundary: copying this adapter should be an explicit project-local action, not a global installation side effect.
- Default topology: one same-conversation primary owner with read-only independent review when needed.

## Generic Skill Book

Use [Skill Book Plane](skill-book.md) and the runtime resolver package when your environment has multiple reusable procedures or deterministic helpers. The pattern is not tied to one product: it compiles source and finalized-action facts, resolves exact and rejected candidates, binds path and member hashes, and keeps selection, application, result, and effect separate.

## Optional Codex Adapter

Use `adapters/codex/` only when that runtime is intentionally selected. Official OpenAI documentation describes `AGENTS.md` as Codex's guidance file and documents skills as folders with `SKILL.md` entrypoints and progressive disclosure:

- https://learn.chatgpt.com/docs/agent-configuration/agents-md
- https://learn.chatgpt.com/docs/build-skills

The adapter keeps the always-loaded bootstrap concise and routes the detailed framework through on-demand docs and skills. Product-specific discovery behavior informs only this adapter, not the generic framework.

- Assumption: the target runtime uses `AGENTS.md` for guidance and `SKILL.md` folders for skills.
- Boundary: this adapter is optional packaging, not the framework identity.

The adapter does not claim that host hooks are installed, trusted, or delivering events. Host integration is a separate explicit configuration step and should be checked at the event-consumer boundary.

## Optional PowerShell Adapter

Use `adapters/powershell/` when a finalized action is PowerShell and native parser/token evidence is valuable. The native implementation reports `PASS`, `BLOCK`, and `ERROR` separately and preserves member-level results for batches.

The portable Python checker remains a useful conservative screen. Its pass is not promoted to native PowerShell parse success. Neither checker decides semantic safety, authority, or final consumer outcome.

## Optional Hermes Adapter

Use `adapters/hermes/` only when that runtime is intentionally selected. It binds the ledger to the host's stable session identity, uses explicit `host_binding` / `projection_filename` configuration, connects prompt capture and recovery injection through the host's supported hooks, and documents host-side exact-action screening for that host's tool payloads. It is optional packaging, not the framework identity; delivered context, trust and tool coverage remain host-specific and must be checked at the event-consumer boundary.

## Legacy Split-Task Compatibility

The durable status package under `runtime/skills/durable-supervisor-status/` supports recovery of an explicitly existing split-task lease. New work should use the same-conversation objective ledger. Legacy status does not create action authority or an independent review key.
