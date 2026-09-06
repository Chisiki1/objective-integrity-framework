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

The result may be `WATCH`, `CONSUME`, `REFUTE`, `CORRECT`, `USER_DECISION`, or `WAIT_AT_DECISION_WINDOW`. A status-only event or fixed polling returns a dependent hold. Slow work with concrete evidence progress may wait at a bounded decision window. The result does not authorize edits or external actions.
