# Objective Continuity

Objective continuity keeps one source-bound outcome intact while a task grows, pauses, resumes, compacts, or receives additional instructions.

## Default Ownership Model

One primary owner normally performs the work and maintains the objective state in the same conversation. This reduces handoff loss and keeps action results close to the source that authorized them.

Independent review remains separate. A reviewer may inspect the source, candidate, evidence, and consumer relations, but stays read-only toward the candidate it reviews and does not become a second action owner. A legacy split-task coordinator/executor mode is a compatibility path for an explicitly existing lease, not the default topology.

## Durable State

The runtime keeps three complementary views under an explicit project-local ledger root:

- `journal.jsonl`: append-only source, progress, action, reconciliation, and recovery events.
- `current.json`: machine-readable replayed state for the current head.
- `objective.txt`: concise human projection of the active objective, open outcomes, blockers, effects, and next eligible work.

The exact source event remains immutable. The human projection is a view, not a replacement source.

Repeated source identities are displayed once in a source dictionary and referenced by aliases. Every clause retains its locator; open outcomes are not truncated to meet a line budget. Current next-work references replace superseded current pointers, while source, outcome and action history stay recoverable. See [Knowledge Stewardship](knowledge-stewardship.md).

Create the ledger at the first effective user instruction, reuse it for the same logical conversation, and give forks separate identities. Re-read it on a user update, after a material result or completed item, on resume or compaction recovery, and before the final response. Automatic delivery depends on the host integration; a manual owner read remains the fallback.

## Classifying New Instructions

Every later source event receives an explicit disposition:

- `ADD`: introduces a compatible new deliverable or condition.
- `CLARIFY`: resolves meaning without replacing the existing objective.
- `CORRECT`: fixes a mistaken interpretation or record while preserving lineage.
- `REPLACE`: substitutes a source-authorized objective or condition.
- `WITHDRAW`: removes a source-authorized outcome from the active set while retaining history.

Raw prompt capture is not automatic semantic authority. The primary owner binds the event to the exact source, classifies its effect on current objectives, and preserves every unaffected open outcome.

## Progress and Action Effects

Progress records cannot silently close an objective. They state the actual evidence delta and the next source-bound step.

Material actions use a small lifecycle:

```text
action-start -> action-outcome -> action-reconcile, when needed
```

The ledger distinguishes planned work, confirmed no-effect, committed effect, partial effect, and unknown effect. A display or cleanup failure after a consumer action does not erase the earlier effect. A retry does not clear an unknown result; reconcile the prior action against current evidence first.

## Concurrency and Recovery

Append operations use expected-head comparison so stale writers fail before adding semantic state. Replay validates the journal before append. Partial tails, lock recovery, source-integrity failures, and capture gaps remain explicit instead of being normalized into success.

`status` remains available when a mutation-dependent semantic claim is held. It is semantically read-only: it cannot change the objective, source classification, progress, or action state. It may acquire the configured ledger lock, preserve a partial journal tail, and rewrite `current.json` and `objective.txt` from replayed state. Those bounded read-repair writes stay inside the explicit ledger root and grant no new action authority.

## Direct Runtime Entry

Advanced users can initialize a ledger directly:

```bash
python runtime/objective_ledger.py bootstrap --config <explicit-config> --session-id <id> --source-file <file>
```

That command is for a source checkout. In a complete installed destination, use `.oif/runtime/objective_ledger.py`. A minimal Skill installation intentionally omits the runtime; keep exact source events, the objective and open outcomes, action starts and results, and unknown-effect reconciliation in project-owned files until complete mode is useful.

Use an explicit configuration, ledger root, logical identifier, and source file. The portable runtime also exposes `classify-source`, `progress`, `action-start`, `action-outcome`, `action-reconcile`, `verify`, `status`, `preflight`, and `hook`. Exact arguments and event contracts are summarized in [Runtime Reference](runtime-reference.md).

## Consumer Check

For larger implementations, link one current [whole completion scope](whole-scope-work.md) from the objective state. Bounded jobs advance that scope instead of maintaining rival definitions of done. The `objective-input` command prepares owner updates against the exact native ledger version and head; it does not replace source classification or authorize an action.

Continuity is working when an ordinary add or clarification preserves progressed outcomes, a stale writer is rejected without append, an unknown action effect survives retry pressure, and the human projection leads back to the exact next objective-bound step. File presence alone is not that consumer result.
