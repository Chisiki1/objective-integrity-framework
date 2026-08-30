# Skill Book Fallback Example

This example shows how a Skill Book supports the work without replacing the workflow.

## Request

A user asks an agent to repair a data export script and publish a small patch. The team has a reusable deployment skill and a reusable parser-preflight skill.

## Resolution

The resolver receives current work facts:

- Job: repair.
- Action: edit and test.
- Tool: shell command.
- Environment: local repository.
- Resource: export script.
- Risk: mechanical parser error.

It returns:

- `parser-preflight`: selected because it matches the command risk and is active-bounded.
- `deployment-release`: rejected because the current action is not a deployment and no external write is authorized.

The repair continues under the normal workflow. The rejected deployment skill does not block local diagnosis, and the selected parser preflight does not prove the patch is correct. It only checks the exact command representation for its bounded mechanical family.

## Effect Record

After use, the project-local effect record says:

- Planned objective delta: avoid repeating a known command-parser mistake before running the test command.
- Actual objective delta: command representation passed the mechanical preflight and the test command executed.
- Consumer result: product behavior still requires ordinary test and review evidence.
- Proof ceiling: mechanical preflight only.

No sanitized global lesson is promoted until the team has enough evidence that the mechanism improves outcomes without adding excessive cost or false blocks.
