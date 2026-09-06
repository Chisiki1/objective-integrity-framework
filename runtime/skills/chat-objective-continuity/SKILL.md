---
name: chat-objective-continuity
description: Preserve one source-bound primary objective and unfinished outcomes in the same logical conversation across later prompts, resume, compaction, and bounded child work. Use when durable continuity or uncertain action effects matter; do not use it to infer intent, authorize external actions, or replace semantic review.
---

# Chat Objective Continuity

Use one explicitly configured project-local ledger to keep the exact source, objective contract, open outcomes, pending or unknown effects, and next eligible work available without creating a second user-visible task.

## Required Behavior

1. Bind the ledger to an explicit namespace and logical conversation ID. A reviewed host adapter may use its stable session ID for a new conversation. Never derive identity from a title, model, working directory, repository name, or prompt text.
2. Create the ledger and `objective.txt` on the first effective user source. Resume or compaction before that source creates nothing. Reuse the journal-derived state for the same logical ID, and read the human projection before choosing a material next action after resume.
3. Capture a host event as immutable input with unclassified actor and meaning unless the adapter proves otherwise. The primary owner classifies an actual source as `INITIAL`, `ADD`, `CLARIFY`, `CORRECT`, `REPLACE`, or `WITHDRAW`. Internal, child, irrelevant, and duplicate captures retain evidence but do not change the objective.
4. Change objective semantics only through `classify-source`. Bind every clause to the exact source event and hash. `ADD`, `CLARIFY`, and `CORRECT` preserve every unaffected supported field. `REPLACE` and `WITHDRAW` require explicit source lineage. Progress may update outcome state, evidence, blockers, and next work, but cannot introduce or retire objective semantics.
5. Record a material action before execution and one immutable `SUCCEEDED`, `FAILED`, `CANCELLED`, or `UNKNOWN_EFFECT` outcome afterward. A retry gets a new linked action ID and never clears the earlier unknown effect. Reconcile uncertainty only through a distinct evidence-linked event.
6. Apply preflight only to configured tool classes and explicit action dependencies. Hold a configured mutation for a known unclassified source or named pending effect while allowing independent read-only work. An unknown tool is outside this control's protection, not automatically blocked.
7. The primary owner alone writes semantics, progress, and action state. Host status, start, and compact routes append no semantic event, but they may perform bounded read-repair: lock the configured ledger, preserve a partial tail, and rewrite replaceable projections. Stop and child routes do not gain semantic write authority unless a separately reviewed actor binding proves ownership.
8. Keep the journal authoritative. Under one identity-bound lock, replay and validate the complete proposed transition before append. A rejected transition leaves the semantic head unchanged. Publish projection bytes atomically, preserve append and cleanup faults separately, and recover a lock only by exact identity with conservative evidence that its owner is gone.
9. Reopen every dependent source identity before consumption. Missing or changed source bytes hold only dependent mutation. Preserve partial tails and capture gaps until exact source recovery or explicit owner disposition.
10. Keep `objective.txt` concise: active objective, conditions, prohibitions, open outcomes and effects, blockers, return step, and current-contract next work. Follow its pointers to `current.json` and `journal.jsonl` for complete machine and history state.

Read [ledger-contract.md](references/ledger-contract.md) before bootstrap, host integration, source classification, action recording, or recovery. Use `runtime/objective_ledger.py` rather than recreating persistence in a shell command.

## Effect Boundary

Record whether continuity changed the next action, avoided objective loss or duplicate effects, added delay or a false hold, and reached the final consumer. Keep observed, unavailable, and not applicable distinct. If the ledger is unavailable, continue safe independent work and repair only the minimum state needed for the dependent action; ledger perfection is not the objective.
