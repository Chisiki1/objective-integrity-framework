# Objective Integrity plugin

Keep long tasks on track and carry useful lessons into the next action.

This self-contained skills-only package contains the Objective Integrity skill,
its supporting instructions, synthetic practice cases and the project icon.
It requires no OIF account, MCP server, API key, Python runtime or trusted hook.
The host supplies the model, conversation and any tools used for the actual task.

## Use it

Load this package through a compatible host's supported plugin installation
flow. Then try: **"Resume this project and identify what still needs to be
completed."** Supply the relevant task source and current artifacts; do not paste
credentials or unrelated conversation history.

The skill preserves the requested outcome, identifies the next necessary work,
and keeps useful improvements connected to real later actions. It does not need
to interrupt a one-line question with process records. Supporting references live
under `skills/objective-integrity/references/` and are loaded only when relevant.

Use an existing task-owned record when writable files are available. Otherwise,
keep a concise checkpoint in the conversation and explain its persistence limit.
Installation does not create a global ledger, install other skills, enable hooks,
grant permissions or guarantee cross-conversation memory.

## Runtime and documentation

For executable objective journals, complete Skill packages, exact artifact reads,
operation-result capture and current/history reconciliation, use the optional
[complete OIF distribution](https://github.com/Chisiki1/objective-integrity-framework/blob/main/docs/adoption.md).
These Python tools are separate from this instruction-only package. Choose the
smallest useful mode; do not require a full installation for an ordinary task.

The [plugin guide](https://github.com/Chisiki1/objective-integrity-framework/blob/main/docs/plugin.md)
explains packaging, testing and versioned updates. The
[Japanese guide](https://github.com/Chisiki1/objective-integrity-framework/blob/main/docs/ja/README.md)
is optional reading.

## Data and support

OIF runs no hosted service and receives no task data from this package. Any
conversation data remains subject to the host's own data controls. Task files and
learning records stay where the user has authorized the agent to store them;
normal host tools retain their own permissions. See the included `PRIVACY.md`.

Report general issues at the project's
[issue tracker](https://github.com/Chisiki1/objective-integrity-framework/issues).
For sensitive reports, follow
[SECURITY.md](https://github.com/Chisiki1/objective-integrity-framework/blob/main/SECURITY.md).
Remove private task data before sharing a reproduction. The included `LICENSE`
contains the Apache License 2.0 terms; `NOTICE` retains the project attribution.
