# Platform Adapters

The framework is platform-agnostic. Adapters translate its records and invariants into a particular runtime's configuration style.

## Generic Adapter

Use `adapters/generic/system-developer-adapter.md` to adapt the framework into a system or developer prompt for any agent runtime that supports project guidance.

## Optional Codex Adapter

Use `adapters/codex/` only when that runtime is intentionally selected. Official OpenAI documentation describes `AGENTS.md` as the project guidance file for Codex and documents project-scoped skill folders with `SKILL.md` entrypoints:

- https://learn.chatgpt.com/docs/agent-configuration/agents-md
- https://learn.chatgpt.com/docs/build-skills

The adapter keeps the always-loaded bootstrap concise and routes the detailed framework through on-demand docs and skills.
