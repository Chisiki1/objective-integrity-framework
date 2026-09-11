# Objective Continuity

Use one durable objective ledger when a task can resume, compact, queue, retry, receive later instructions, or outlive one turn.

## State Model

- Keep exact source events immutable.
- Maintain an append-only journal, machine projection, and concise human projection as separate consumers of the same replayed state.
- Use one logical objective-tree identity across model, title, workspace, and context changes.
- Bind semantic writes to the current head so stale writers do not append.
- Keep open outcomes, blockers and return steps, pending effects, completed evidence, and next eligible work explicit.
- Reuse the existing logical-task ledger on additional instructions and resume; give forks separate identities. Read on user updates, material results, completed items, compaction recovery and before final delivery.
- Keep the human view bounded: the card holds counts, pointers and continuity frontiers, while full source bindings, clause locators and open outcome history remain in the same-ledger `EVIDENCE-INDEX.json`. Replace superseded current pointers, not immutable source or outcome history.
- Link one current whole-scope work record; child jobs must not create competing definitions of completion.

## Source Events

Classify later events as `ADD`, `CLARIFY`, `CORRECT`, `REPLACE`, or `WITHDRAW`. Only exact source authority can replace or withdraw a source-bound outcome. Raw hook capture, copied text, a shared session identifier, or a child event is input evidence rather than automatic semantic authority.

## Actions

Record `action-start` before a material action, `action-outcome` after the actual result, and `action-reconcile` when a partial or unknown effect requires later evidence. Do not retry an unknown external effect until it has been reconciled or the recovery route explicitly permits a bounded attempt.

At an aggregation boundary, preserve first fault, contributing conditions, propagation, cleanup or recovery failures, affected and unaffected consumers, and unobserved scope. A projection or output failure after commit does not erase the committed effect.

## Decision-Bearing Results

Consume each material result as `CONSUME`, `REFUTE`, `CORRECT`, `USER_DECISION`, or `WAIT`. Refresh the active objective view before and after the result, then return to the exact next source-bound step. Wait only where new evidence can change the decision.

In a source checkout, the optional executable runtime is `runtime/objective_ledger.py`, with commands and recovery behavior in `docs/objective-continuity.md` and `docs/runtime-reference.md`.

After a complete bootstrap installation, use `.oif/runtime/objective_ledger.py` and read `.oif/docs/objective-continuity.md` plus `.oif/docs/runtime-reference.md` from the selected destination. A minimal Skill installation intentionally has no runtime dependency. In that mode, apply the contract manually with project-owned files: preserve exact source events, keep a concise objective and open-outcome record, record material action starts and outcomes, and reconcile unknown effects before retry. Add the complete runtime later only when executable persistence is useful.
