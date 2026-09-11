---
name: objective-supervisor-control
description: Turn a same-chat or legacy decision-bearing event into a bounded source-bound objective-control disposition without fixed polling or status narration. Use when consuming a result, correcting objective drift or deciding whether a wait has value; never treat this control pass as independent review or action authority.
---

# Objective supervisor control

Use when the primary owner consumes a job event, decides a source-bound correction or evaluates a wait. The historical name does not require separate Supervisor/Worker chats. Independent refutation remains separate from this self-control pass. It is not a tool interceptor and does not authorize edits or external actions.

Provide the frozen objective, event identity, objective evidence delta, omission consequence, shorter alternative, induced rework, exact return step, and later-effect plan.  Run `python -B scripts/supervisor_control.py --input <event.json>`.

The requested decision may be `WATCH`, `CONSUME`, `REFUTE`, `CORRECT`, `USER_DECISION`, `WAIT_AT_DECISION_WINDOW`, or v2 `ACT`. The script returns `ADMIT` or `HOLD` and the requested decision; neither is outcome proof. A status-only event or fixed polling returns a dependent hold. Slow work with stated evidence progress may wait at a bounded decision window.

For source-wide implementation, use `objective-supervisor-control-v2`: v1 event fields plus `source_sha256`, `owner_chat_id`, and `work_phase`. Read the sibling lifecycle Skill's `references/source-wide-execution.md`; bind the same exact owner scope/state used by work_io and transition admission. `ACT` selects the bound action. `CORRECT` must bind `REPAIR_FINDINGS`, not a first finding while the whole sweep is still collecting. Inspect the returned `work_phase.next_actions` and act on the eligible implementation, remaining check or grouped repair; do not merely save the result. V1 remains readable for legacy/ordinary non-product events, with explicit absence of source-wide evaluation. Raw and unknown action effects remain in the authoritative ledger even when new work is held.

At an integration gap, repeated causal family or support-heavy result, also apply the installed lifecycle Skill's `references/convergence.md`. Use existing fields to identify the next required consumer connection, valid unchanged evidence, changed action or handoff, and return step. Consume eligible components into the actual deliverable before accumulating disconnected work when that has higher value. Preserve useful independent work and component proof ceilings; never require full runtime completion for every finite unit. A control record alone is not a correction: carry the chosen source-authorized change to its next real use and assess its effect. This is event-driven and adds no packet, fixed time limit, automatic restart or new authority.
