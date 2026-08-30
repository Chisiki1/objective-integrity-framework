# Platform Adapters

The framework is platform-agnostic. Adapters translate its records and invariants into a particular runtime's configuration style.

## Generic Adapter

Use `adapters/generic/system-developer-adapter.md` to adapt the framework into a system or developer prompt for any agent runtime that supports project guidance.

- Assumption: the runtime supports project-local guidance or an equivalent prompt/configuration layer.
- Boundary: copying this adapter should be an explicit project-local action, not a global installation side effect.

## Generic Skill Book

Use [Skill Book Plane](skill-book.md) and the `tools/skill_resolver.py` receipt pattern when your runtime has multiple reusable procedures or deterministic helpers. The pattern is not tied to one product: the required inputs are current work facts, skill roots, registry identity, selected and rejected reasons, path and hash identity, blind-safety status, and proof ceiling.

## Optional Codex Adapter

Use `adapters/codex/` only when that runtime is intentionally selected. Official OpenAI documentation describes `AGENTS.md` as Codex's guidance file and documents skills as folders with `SKILL.md` entrypoints and progressive disclosure:

- https://learn.chatgpt.com/docs/agent-configuration/agents-md
- https://learn.chatgpt.com/docs/build-skills

The adapter keeps the always-loaded bootstrap concise and routes the detailed framework through on-demand docs and skills. Product-specific discovery behavior informs only this adapter, not the generic framework.

- Assumption: the target runtime uses `AGENTS.md` for guidance and `SKILL.md` folders for skills.
- Boundary: this adapter is optional packaging, not the framework identity.
