# Knowledge Stewardship

Make current guidance easier to use without making past knowledge disappear.

The framework separates the current decision view from immutable evidence. This is not automatic summarization by age or file size. The owner decides which sections are historical, preserves all source and unresolved obligations, and verifies that the next reader can still retrieve what it needs.

## Current and Historical Information

| Artifact | Current role | What stays durable |
|---|---|---|
| `objective.txt` (the objective card) | The current objective, conditions, unresolved outcomes/effects and next work, as a short human-readable summary. | Exact source files, full machine state, `EVIDENCE-INDEX.json` and the append-only journal. |
| Project master | Current project control and relevant active knowledge. | Raw episodes, failures, old dispositions and recovery evidence. |
| Global master | Sanitized reusable families and current applicability. | Linked provenance and prior versions, without copying private project details. |
| Learning queue | Each candidate's current owner, state, artifact and next-use trigger. | Immutable revision and transition history. |

`objective.txt` is a bounded card: counts, pointers and continuity frontiers only. Full source bindings, every clause locator, and the outcome/action catalog live beside it in `EVIDENCE-INDEX.json`, a machine-readable index in the same ledger. Read the card first and open the index for completeness; `current.json` and `journal.jsonl` retain their full replay meaning.

## Read Complete Applicable History

Build an index only in an explicit project-owned cache:

```bash
python tools/oif.py index build --master <global-master> --master <project-master> --cache <cache-directory>
python tools/oif.py index query --index <index-file> --term <relevant-family>
```

Masters can link exact immutable backing through `master-history-v1`. The index follows those links, checks their hashes and rejects missing, changed or cyclic backing. Ordinary queries search current and historical text; `--controls` includes current live-root control sections, and any supplied terms also include their matching sections. Follow returned cursors to finish a query. Searching only a compact master with a text tool does not establish complete historical coverage.

The default query returns each exact text group once within the complete response
budget. Changed wording remains a separate group. To retrieve all origins of a
group, repeat the same terms and `--controls` setting and add
`--origins <group-id>`; follow that mode's own cursor. Text coverage and provenance
coverage are separate. Existing programmatic consumers and `--legacy-output`
retain the earlier response shape.

An index is a retrieval projection. It does not decide that two rules mean the same thing, supersede a condition, change a master or certify that a relevant lesson was applied.

## Organize at a Useful Decision

Use the existing owner record to identify every affected current location, not
only the newest event. Include control headers, indexes, related families and
cross-links. Reuse existing objective/work/registry sources for volatile state.
Then inspect the whole-view proposal command:

```bash
python tools/oif.py reconcile --help
```

Inventory the exact master, then supply an owner-authored plan assigning every
section once to `keep`, `replace` or `archive`. Keep means preserving its actual
occurrence, ancestry and current/history role, not finding the same sentence
elsewhere. The proposal contains full old backing and one reviewed replacement;
it writes neither. Publish backing first, then compare-and-swap the current view
and read back actual transitive history. Preserve unknown effects and rollback. The distribution ships proposal validation and readback tooling but no live master writer: a deployment's scoped publisher performs the staged backing install, the compare-and-swap and the readback, so any host can implement the same contract.
The [reconciliation contract](../runtime/skills/master-guided-skill-lifecycle/references/master-reconciliation.md)
defines the exact inputs. The earlier `index propose-organization` route remains
available for its narrower selected-section use; it is not whole-current review.

Replace an affected family's current entry and link its earlier evidence. Do not keep adding competing current instructions. Do not edit another task's current control or rewrite a frozen backup just to attach a later result; update the owning current pointer instead.

## Correct Queue Metadata Without Pretending Progress

A reason or next-use trigger can become stale while a candidate's lifecycle state remains correct. A queue event with `change_kind: metadata` can update those references at the same state under revision comparison, while retaining immutable history and prior evidence. An ordinary same-state transition is still invalid. A correction to a display is not evaluation, activation or measured improvement.

When the candidate becomes eligible under existing authority, perform its next real preparation, evaluation, bounded adoption or use. If its value is not established for current work, retain the actual reason and return trigger. This keeps learning actionable without turning every result into a new Skill or a larger record.

## Keep Conditions Stable Unless Their Meaning Changes

Use the current-condition resolver for an explicitly indexed document and body hashes. A method implementation can reuse that unchanged document; it need not append another conditions chapter. An exact user-approved semantic change consolidates the current condition set and retains old/new/source/approval/recovery lineage. Pending approval affects only that amendment, not independent improvements within standing authority.

See [Governance and Self-Improvement](governance-self-improvement.md) for the full learning and amendment route.
