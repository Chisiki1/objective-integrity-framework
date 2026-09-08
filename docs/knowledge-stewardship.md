# Knowledge Stewardship

Make current guidance easier to use without making past knowledge disappear.

The framework separates the current decision view from immutable evidence. This is not automatic summarization by age or file size. The owner decides which sections are historical, preserves all source and unresolved obligations, and verifies that the next reader can still retrieve what it needs.

## Current and Historical Information

| Artifact | Current role | What stays durable |
|---|---|---|
| `objective.txt` | The current objective, conditions, unresolved outcomes/effects and next work. | Exact source files, full machine state and append-only journal. |
| Project master | Current project control and relevant active knowledge. | Raw episodes, failures, old dispositions and recovery evidence. |
| Global master | Sanitized reusable families and current applicability. | Linked provenance and prior versions, without copying private project details. |
| Learning queue | Each candidate's current owner, state, artifact and next-use trigger. | Immutable revision and transition history. |

Repeated source event/ref/hash triples in `objective.txt` appear once in a local alias dictionary. Every clause and locator remains present. Read the dictionary and clause list together; aliases are display keys, not replacement identities. `current.json` and `journal.jsonl` retain their full replay meaning.

## Read Complete Applicable History

Build an index only in an explicit project-owned cache:

```bash
python tools/oif.py index build --master <global-master> --master <project-master> --cache <cache-directory>
python tools/oif.py index query --index <index-file> --term <relevant-family>
```

Masters can link exact immutable backing through `master-history-v1`. The index follows those links, checks their hashes and rejects missing, changed or cyclic backing. Ordinary queries search current and historical text; `--controls` selects current live-root control sections. Follow returned cursors to finish a query. Searching only a compact master with a text tool does not establish complete historical coverage.

An index is a retrieval projection. It does not decide that two rules mean the same thing, supersede a condition, change a master or certify that a relevant lesson was applied.

## Organize at a Useful Decision

Use the existing owner record to identify obsolete current sections and their exact source dependencies. Then inspect the proposal command:

```bash
python tools/oif.py index propose-organization --help
```

It requires the master, planned history path, selected section IDs and expected master hash. The result contains complete old backing and proposed current text; the command writes neither. Review the selected sections and every preserved obligation, independently challenge the change when material, publish backing first, and replace the current view only if its expected hash still matches. Keep the old bytes and rollback route.

Replace an affected family's current entry and link its earlier evidence. Do not keep adding competing current instructions. Do not edit another task's current control or rewrite a frozen backup just to attach a later result; update the owning current pointer instead.

## Correct Queue Metadata Without Pretending Progress

A reason or next-use trigger can become stale while a candidate's lifecycle state remains correct. A queue event with `change_kind: metadata` can update those references at the same state under revision comparison, while retaining immutable history and prior evidence. An ordinary same-state transition is still invalid. A correction to a display is not evaluation, activation or measured improvement.

When the candidate becomes eligible under existing authority, perform its next real preparation, evaluation, bounded adoption or use. If its value is not established for current work, retain the actual reason and return trigger. This keeps learning actionable without turning every result into a new Skill or a larger record.

## Keep Conditions Stable Unless Their Meaning Changes

Use the current-condition resolver for an explicitly indexed document and body hashes. A method implementation can reuse that unchanged document; it need not append another conditions chapter. An exact user-approved semantic change consolidates the current condition set and retains old/new/source/approval/recovery lineage. Pending approval affects only that amendment, not independent improvements within standing authority.

See [Governance and Self-Improvement](governance-self-improvement.md) for the full learning and amendment route.
