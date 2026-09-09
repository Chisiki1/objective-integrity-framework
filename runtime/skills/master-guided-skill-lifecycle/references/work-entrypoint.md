# Completion work entrypoint

Use `scripts/work_io.py` when a needed assignment or handoff benefits from one bound request and result-consumption route. It generates an internal target/message payload and reads returned artifacts/destinations; the caller maps that payload to its actual host API. V2 retains source-wide phase selection. The in-work `build-next` and `build-next-operation` operations each write only two new generated inputs in an explicit absent owned directory, replacing repeated manual result/decision assembly. It never allocates, dispatches, copies delivered artifacts, writes masters, grants permission or judges semantic completion.

## Select the useful unit

Name the required artifact, its actual next consumer, and the acceptance that connects it to the user's outcome. Compare this route with direct work, reuse of an existing artifact, and an ordinary handoff. Use it when repeated assembly, identity mistakes, or lost handoffs justify the preparation and integration cost. Skip it for a simple task where direct work is cheaper, work without a real consumer, or an unavailable internal recipient. This is optional routing, not a new ticket for every job or a reason to create an agent.

The parent supplies the current source, scope, prohibitions, model/reasoning allocation, and integration decision under existing authority. Retain all open outcomes and action-specific restrictions. Each reader must satisfy its own required source, Skill, and master reading; another reader's receipt is not reading evidence. The parent remains the sole shared semantic writer.

## Prepare and verify the exact request

Use inventoried absolute paths and actual file hashes. Inputs must be regular files without link/reparse components. The helper also binds its own bytes and the sibling `allocation_io.py` and `in_work_learning.py`. Source-wide implementation uses `work-spec-v2`: all v1 fields below plus `objective_id` and `work_phase`, the same binding used by the parent, as defined in [source-wide-execution.md](source-wide-execution.md). The v2 helper also binds `work_phase.py`; it rejects a job whose exact current state does not permit that action. A child's `scope` describes its write boundary, never a replacement completion scope. The v1 example remains usable for ordinary non-product work and historical handoffs but explicitly reports that source-wide selection was not evaluated. Do not claim a legacy input exercised the new protection. Replace all example facts and full SHA256 placeholders before use.

```json
{
  "schema": "work-spec-v1",
  "unit_id": "DOC-01",
  "owner_chat_id": "<current owner chat id>",
  "target": "internal:document-worker",
  "objective": "Deliver the reference needed by the current candidate.",
  "instructions": "Read the actual API; write the declared reference and result JSON. Return limits and unresolved work.",
  "source": {"path": "C:/work/source.txt", "sha256": "<SOURCE_SHA256>"},
  "inputs": [
    {"id": "api", "path": "C:/work/api.py", "sha256": "<API_SHA256>", "required": true}
  ],
  "methods": [
    {"id": "consumer-first", "path": "C:/work/convergence.md", "sha256": "<METHOD_SHA256>", "apply": "Write instructions the declared consumer can actually use."}
  ],
  "masters": [
    {"role": "global", "path": "C:/work/GLOBAL_PROJECT_MASTER.md", "sha256": "<GLOBAL_SHA256>", "read": "Current control and applicable history.", "apply": "Preserve source scope and authority."},
    {"role": "project", "path": "C:/work/PROJECT_MASTER.md", "sha256": "<PROJECT_SHA256>", "read": "Current control and applicable history.", "apply": "Deliver the required reference; parent alone writes shared semantics."}
  ],
  "scope": "Only the declared draft and result file.",
  "prohibitions": ["No shared, product, or external writes; no other-task messages or automatic retry."],
  "outputs": [
    {"id": "reference", "path": "C:/work/returned/reference.md", "before_sha256": null, "acceptance": "Accurate, usable instructions for the actual API."}
  ],
  "result_path": "C:/work/returned/result.json",
  "consumer": {
    "id": "candidate-reference",
    "description": "The reference used by the candidate Skill.",
    "acceptance": "Parent accepts and incorporates the artifact; the next reviewer reads it.",
    "destinations": [{"output_id": "reference", "path": "C:/work/candidate/reference.md"}]
  }
}
```

Keep IDs unique within each list and bind every output to exactly one consumer destination. Both `global` and `project` master contracts are required. `inputs` and `methods` may be empty when genuinely inapplicable. An optional input can use `required: false`; an explicitly absent optional input uses `sha256: null`. Optional-input issues remain visible in `verify` and later handoffs and must not conceal a required dependency. Required source, method, and master bindings cannot be made optional.

`before_sha256: null` requires a new output path; an existing output requires its observed baseline hash. The result path must be absent. Output/result paths must be distinct and must not alias read-only bindings. Naming a consumer destination does not authorize the child to write there: the child writes only its declared outputs and result path, and the parent handles integration within its own scope.

```text
python -B "<skill>/scripts/work_io.py" prepare --spec "C:/work/spec.json"
python -B "<skill>/scripts/work_io.py" verify --prepared "C:/work/prepared.json" --expected-request-id "<REQUEST_ID>"
```

`prepare` emits a `work-request-v1` JSON object on stdout. Persist that exact object as `prepared.json` through the authorized writing route and retain its `request_id` in the existing work record. It is derived from the spec, file bindings, and helper identities. Do not hand-edit the generated message or take a later replacement file's ID as evidence of the earlier dispatch.

Run `verify` immediately before sending. It rechecks the prepared request, required hashes, and output/result baselines, and returns `dispatch.target`, `dispatch.message`, and `optional_input_issues`. It always reports `permission_granted: false` and `dispatch_performed: false`. A structural rejection holds the affected send, not independent work. Resolve changed source meaning or dependencies before preparing an updated request; simply refreshing hashes is not reconciliation.

## Parent dispatch and actual result

For an ongoing next unit, use [in-work.md](in-work.md) and `work_io build-next` to
generate the source/result/decision bindings from the actual prior result. The
ordinary `consume` path emits the learning input even with no selected Skill.
Use `boundary` for relevant continuing-actor updates; it is not an automatic
restart or a certificate of another person's reading. The original v1 result and
prepared-request lineage remain consumable.

After the existing allocation and authority checks, the parent sends the returned `dispatch` object unchanged through a compatible internal-job API, or preserves an explicit host mapping of its target/message fields. Portable targets use `internal:document-worker`; compatible `/root/...` canonical internal-agent targets remain accepted. The actual recipient must already exist and be authorized; an identifier is not authority. This route neither creates agents nor addresses another user-visible task. Preserve the actual host response in the existing work evidence. The helper has no background process, host interception, retry queue, or dispatch history.

The generated message includes the spec, exact request ID, reading/APPLY boundaries, and result shape. The worker writes only produced outputs and an actual result file, for example:

```json
{
  "schema": "work-result-v1",
  "request_id": "<REQUEST_ID>",
  "unit_id": "DOC-01",
  "status": "succeeded",
  "effect_state": "confirmed",
  "outputs": [{"id": "reference", "path": "C:/work/returned/reference.md", "sha256": "<ACTUAL_OUTPUT_SHA256>"}],
  "unresolved": [],
  "notes": "Draft created and read back. Parent acceptance and integration remain separate.",
  "first_fault": null
}
```

Choose one `status`: `succeeded`, `partial`, `failed`, or `unknown`; choose one `effect_state`: `none`, `partial`, `confirmed`, or `unknown`. `succeeded` requires `confirmed` and every declared output to verify. Report a partial result with only the artifacts actually produced, retain unresolved obligations, and preserve the first fault in `first_fault` when present. A failed job with known no effect is different from an unknown effect. Do not infer effect success from a process exit, file hash, or successful component test.

```text
python -B "<skill>/scripts/work_io.py" consume --prepared "C:/work/prepared.json" --result "C:/work/returned/result.json" --expected-request-id "<REQUEST_ID>"
```

Persist and inspect the emitted `work-handoff-v1`; `consume` itself writes nothing. `raw_result` includes the result bytes as base64, their hash, or a read error before report validation. `reported_result`, `artifacts`, `validation_issues`, `freshness_issues`, `unresolved`, and `effect_requires_reconciliation` retain distinct evidence. A verified artifact is eligible for semantic review, not automatically accepted. The raw return is retained even when its identity, schema, output, or current dependencies cannot be validated.

## Integrate and read the actual destination

The parent reads the verified artifact, judges it against the original source and consumer acceptance, and performs the separately authorized integration with existing tools. Incorporate the exact returned bytes into the declared destination. If a correction is needed, preserve the original result and represent the revised work explicitly; do not relabel modified bytes as the same returned artifact. Independent review and any adoption requirements remain separate.

After actual integration, the parent writes its factual observation using the existing record/file route:

```json
{
  "schema": "work-integration-v1",
  "request_id": "<REQUEST_ID>",
  "unit_id": "DOC-01",
  "consumer_id": "candidate-reference",
  "parent_observation": "Read and accepted the draft against the source; incorporated these exact bytes. Next reviewer consumption remains pending.",
  "outputs": [{"output_id": "reference", "destination": "C:/work/candidate/reference.md", "sha256": "<ACTUAL_OUTPUT_SHA256>"}]
}
```

```text
python -B "<skill>/scripts/work_io.py" integrate --prepared "C:/work/prepared.json" --result "C:/work/returned/result.json" --observation "C:/work/integration.json" --expected-request-id "<REQUEST_ID>"
```

`integrate` consumes the actual result again and reads the observation and destination bytes; it never copies or applies them. Inspect `integration_readback`, `missing_integration_output_ids`, all validation/freshness issues, and `requires_reconciliation`. `structural_integration_complete` establishes only the declared artifact/destination byte relation; it can coexist with unresolved freshness or partial-effect concerns. The parent's observation text is an assertion, not independently verified semantic acceptance. Verify the next consumer's actual use under the existing review route. `source_outcome_completed` remains false; unit success does not complete the user's broader objective.

## Preserve incomplete and changed work

- Missing or invalid results: retain the raw return/read error and first fault. Determine what actually happened before any dependent retry. Do not invent a no-effect result.
- Partial or unknown results: retain verified artifacts, missing outputs, and unresolved effects. The parent reconciles the affected obligations and decides whether a safe portion can be used. Independent work may continue.
- Changed source, input, method, master, or helper: `verify` rejects an affected new send; `consume` still retains the old result and reports required freshness issues. Preserve that history and reconcile applicability before dependent integration. Do not discard old effects or stop unrelated work.
- Changed whole-scope state or an out-of-phase returned job: v2 reports phase/freshness issues without discarding raw result bytes, produced artifacts or partial/unknown effects. Reconcile the already performed work; a new hold is not evidence that the old action never ran.
- Repeated sends or reads: the deterministic request ID is not an execution lock. An unchanged baseline does not prove that an earlier send never ran. Inspect actual dispatch/worker/effect evidence before deciding to send again. Repeated `consume` calls are read-only and do not prove exactly-once execution or once-only business effects.
- CLI outcomes: all commands emit JSON and perform no dispatch. Only `build-next` and `build-next-operation` write their two generated inputs under a new owned output root; the other commands are read-only. A rejection exits 2; `consume`/`integrate` also exit 2 for validation issues. Exit 0 alone does not clear `requires_reconciliation`, required freshness issues, partial effects, or pending semantic acceptance. Inspect the returned fields.

## Evaluate the real use

Use the already-needed artifact and its actual consumer as the observation unit. Record available preparation, required reading, dispatch, integration, rework, false holds, and maintenance cost in existing work/learning records. Compare with ordinary manual assembly or artifact reuse where a comparable baseline exists; otherwise report observations and unavailable metrics without a speedup claim. Request generation, helper tests, records written, and hashes are not outcome benefit.

At a real recurrence or useful result, compare the smaller repair with changes to the work unit, execution entrypoint, representation, ownership, or removal/consolidation. Carry the selected method to its next actual consumer, preserving source conditions and authority. Reuse, revise, narrow, merge, or retire this route when its measured contribution warrants it; do not add a new semantic database, idea quota, approval loop, or background task.
