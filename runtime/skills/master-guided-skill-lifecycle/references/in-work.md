# Learn through the next necessary operation

Use this route during decision-bearing work, not only at task end. The existing
`work_io consume` produces `in_work_learning` from an internal job's original
result, including when no Skill was selected. For recurring ordinary native
operations use [operations.md](operations.md) at the actual caller, before display
loses its exit status. Preserve the original result and first fault. A signal is
an investigation lead, not a known cause.

## Change the operation, not just its reminder

Identify whether the useful correction belongs in selection, the actual caller,
method freshness, script implementation, applicability, or environment diagnosis.
Unknown cause remains unclassified. Compare unchanged same-context reuse, a small
correction and a structural alternative when worthwhile. Success/reuse and a
missing useful connection can justify action without waiting for another failure.

Prefer an existing typed generator for repeated file/argument/identity assembly.
When it cannot express a useful next operation, create the needed Skill package
using skill-creator and the full-resource materializer route. Keep instructions,
required scripts/resources, examples and actual caller one reviewed version.
Instruction-only and no-Skill work remain valid. No birth/update quotas apply.
Independent challenge, proportional normal cases, authority, no-drop and rollback
remain required before bounded adoption. Do not stop at a draft/registration when
the already-authorized next actual use is eligible.

## Ordinary receiver and next request

`work_io consume` retains raw bytes even for invalid/stale results, and returns a
source/result/method/next-consumer projection. It does not infer nonuse from a
missing application report. Old work-result-v1 remains readable. New requests
include optional `method_applications`: each names the declared method, actual
reader, exact version, observed state, action reference and limits. Positive
states require an actual reference; neither that declaration nor its hash proves
semantic benefit.

For continuing work, use `work_io build-next` instead of copying old result and
source identities into another handwritten packet:

```text
python -B <skill>/scripts/work_io.py build-next --spec <next-owner-spec.json> --prepared <prior-prepared.json> --result <actual-result.json> --meaning <owner-meaning.json> --expected-request-id <prior-id> --output-root <new-owned-directory>
```

`owner-meaning.json` contains exactly `disposition` (reuse/revise/create/merge/
narrow/retire/no-change/defer), `cause` (selection-gap/application-gap/stale-method/
implementation-fault/applicability-mismatch/environment-fault/reuse-opportunity/
unclassified), `reason`, `expected_change`, `alternatives` (array),
`effect_reconciliation` and `return_trigger`. Defer needs a concrete trigger;
material changes need an alternatives comparison. The helper generates the exact
decision and next spec from the real result, binds them as inputs, and returns
`prepared`. Persist it, verify its request identity and send its returned dispatch
unchanged through the existing work route. It neither dispatches nor activates.
The output root must be absent with an existing plain parent; prior partial
generation is retained and reconciled before any retry.

The new owner spec still states actual scope, source, roles, output ownership,
consumer and applicable whole-source phase. Code must not invent these. Changed
source authority requires owner reconciliation. Unknown action effects are not a
license to replay. Ongoing learning may improve construction immediately but does
not replace whole-source BUILD → SWEEP → grouped REPAIR → ACCEPT.

## Continuing parent and child

At a relevant change or compaction/resume, before the next safe dependent material
action, call `work_io boundary` using the exact prepared request, current actor,
that actor's own read references and effect frontier. Supported actor values are
the declared internal target or `COORDINATED-WORK:<owner_chat_id>`.

```text
python -B <skill>/scripts/work_io.py boundary --prepared <prepared.json> --expected-request-id <id> --actor <actor> --read-refs <own-read-refs.json> --effect-state <none|known-bounded|in-flight|unknown>
```

Read references are an array of `{path,sha256}` for actual personal reads, not a
parent-issued certificate. Include every required script/resource in the work's
method bindings. The result distinguishes changed source/method/master bytes,
missing current reads and unavailable dependencies. Read affected instructions
and decide relevance; unrelated master growth is not an instruction to restart
or reread all Skills. A read hash is not proof of reading. Carry the accepted
change into the actual next operation and its application result. Keep in-flight
old versions and unresolved effects; never hot-replace or automatically rerun.

## Find and consolidate useful connections

`in_work_learning.relation_gaps(episodes, procedures)` derives a disposable view
from existing evidence. Each episode supplies id/family_keys/evidence_ref/
next_consumer/applied_ids/excluded_ids; each procedure supplies id/family_keys/
evidence_ref. Exact shared family relations without observed use or deliberate
exclusion become hypotheses. Inspect the source and applicability before using
one; similarity is not causation. Use this when a relation changes the next
decision, not as a new graph database or mandatory sweep on every result.

Carry useful discoveries into the existing learning queue/Project event and a
sanitized Global family link. The same raw evidence owns the lineage. Consolidate
common procedure and instance parameters; replace current guidance rather than
append a second rule. Merge/narrow/retire ineffective paths with exceptions and
history preserved. Measure the real next consumer, total integration/rework and
false holds against equal-context ordinary work. Unavailable metrics are unknown,
not zero; fixture/script success is not general ordinary-task benefit.

## Host connection and limits

The separately reviewed `in_work_hooks.py` adapter connects supported PreToolUse,
PostToolUse, SessionStart and SubagentStart events. Root compaction context uses
SessionStart with source=compact; PostCompact is not a context-output route.
Continuing-child compaction still requires that actor's own safe-boundary method;
automatic child delivery is not established. PreToolUse denies
only the existing exact PowerShell direct-foreach-pipeline discriminator on known
PowerShell input; no arbitrary command rewriting, general safety gate, dollar-
intent inference or actor impersonation occurs. Other inputs follow normal work.
PostToolUse never blocks/replaces the original result. It gives bounded context
on structured failures, actual operation-result envelopes or patch destination
requests under active Skill roots. A path mention is neither use nor an edit;
a patch request is not successful-change evidence. An empty display string cannot
establish exit status. The ordinary caller owns that evidence.

Observations contain event identities and input/output hashes, not payload bodies
or secrets. A disposable per-session current pointer refers to immutable history
under the configured observational root, not another objective or learning master.
Session IDs shared by children do not prove actor authority. Hosted/uncovered
tools, opaque nested work, continuing write_stdin inputs and absent child-event
delivery remain outside verified coverage. Configuration, exact normal trust,
actual event delivery, next use and benefit are separate observations. Context
cannot force semantic learning or re-infer an already-issued action.

On hook failure normal feedback is preserved; inspect the affected path under
existing instructions. No Stop hook, background continuation or autonomous task
creation is added. Recovery restores only the exact owned hook definition and
runtime preimage; preserve unrelated concurrent changes and observation history.
