# Codex Adapter

This optional adapter is for projects that intentionally use Codex. It is not the identity of Objective Integrity Framework.

Official OpenAI documentation describes project guidance through `AGENTS.md` and skill folders with `SKILL.md` entrypoints:

- [Project guidance](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Building Skills](https://learn.chatgpt.com/docs/build-skills)

The adapter keeps `AGENTS.md` concise and points deeper content to `framework/`, `profiles/`, `templates/`, and `.agents/skills/`.

Project-local bootstrap writes the complete package below the selected destination's `.oif` directory and places only the adapter's project guidance at the integration point. Preview the plan before applying it. This adapter does not enable hooks, trust files, global skills, or background tasks by declaration; connect optional host events only through the host's supported configuration and the explicit objective-ledger contract.

When connecting host events, bind the ledger to the stable task or session identifier actually exposed by that host integration. Treat the identifier as routing input, not semantic authority, and verify child-event distinctions at the host boundary. Never infer continuity from a title, model, working directory, or repository name.

For optional [hooks](https://learn.chatgpt.com/docs/hooks), check the actual host's event payloads, trust requirements and tool coverage. Configuration, trusted activation, delivered context and observed owner behavior are different boundaries. Keep a manual owner-read fallback. This package does not install a background continuation or change trust settings.

The default workflow remains one Codex task with one primary owner. Independent review can use a separate bounded reviewer while remaining read-only toward the target. Legacy split-task recovery is available only for an explicitly existing lease.
