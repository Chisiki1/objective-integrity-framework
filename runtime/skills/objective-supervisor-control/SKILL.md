---
name: objective-supervisor-control
description: Turn a same-conversation or legacy decision-bearing event into a bounded source-bound objective-control disposition without fixed polling or status narration. Use when consuming a result, correcting objective drift, or deciding whether a wait has value; never treat it as independent review or action authority.
---

# Objective Supervisor Control

Use when the primary owner consumes a material event, chooses a source-bound correction, or evaluates a wait. The historical name does not require separate supervisor and worker conversations. Independent refutation remains separate from this self-control pass.

Provide the frozen objective, event identity, objective evidence delta, omission consequence, shorter alternative, induced rework, exact return step, and later-effect plan. Run:

```bash
python -B scripts/supervisor_control.py --input <event.json>
```

The requested decision may be `WATCH`, `CONSUME`, `REFUTE`, `CORRECT`, `USER_DECISION`, `WAIT_AT_DECISION_WINDOW`, or v2 `ACT`. The result returns `ADMIT` or `HOLD` with that decision. A status-only event or fixed polling returns a dependent hold. Slow work with concrete evidence progress may wait at a bounded decision window. The result does not authorize edits or external actions.

Source-wide implementation uses `objective-supervisor-control-v2`: the v1 fields plus `source_sha256`, `owner_chat_id` and `work_phase`. Read the sibling lifecycle [source-wide contract](../master-guided-skill-lifecycle/references/source-wide-execution.md), bind the same owner scope/state as dispatch and transition admission, and consume `work_phase.next_actions`. `ACT` selects that action; `CORRECT` requires grouped `REPAIR_FINDINGS`, not the first failure in an unfinished sweep. Legacy v1 remains usable for ordinary non-product events and explicitly has no source-wide selection.

At a missing consumer connection, recurring family or support-heavy result, use the sibling [convergence route](../master-guided-skill-lifecycle/references/convergence.md). Change the next actual input, shared builder or handoff when supported, preserve useful parallel components and unchanged evidence, and return to the required consumer. A control record is not itself a correction or a reason for fixed polling.
