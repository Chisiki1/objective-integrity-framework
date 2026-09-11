# Connected execution through the existing ledger

Use the current own source interpretation, required methods and action authority first. These commands do not grant permission or replace work_phase, independent review, transition admission or external-action requirements.

`owner-view` accepts an omitted head only for read-only retrieval. Use the returned head for mutation; never copy an old head into repeated retries. `progress`, `action-start` and `action-outcome` accept their existing runtime field objects and `--apply --event-id <unique-id>`; preparation and append/readback then share one call. `classify-source` remains an explicit legacy prepare route; use source-update for incremental own-source changes.

## Native operation

`run-operation --expected-head <observed-head> --fields <fields.json> --apply --event-id <unique-id>` retains common exact runtime/config/chat arguments. Fields are exactly:

```json
{"start":{"action_id":"NEW-ACTION","description":"actual bounded work","outcome_ids":["OWN-OUTCOME"],"source_clause_ids":["OWN-CLAUSE"]},"runner_path":"<absolute lifecycle/scripts/operation_io.py>","runner_sha256":"<reviewed SHA256>","spec_path":"<absolute reviewed work-operation-v1.json>","output_root":"<absent owned evidence directory>"}
```

Read lifecycle references/operations.md and the selected runner/resources. The existing spec owns exact argv, source path/hash, methods, owner, cwd, accepted exit codes, timeout, effect scope and next consumer. Its source must match an own action clause. Start input hash is derived from this exact spec; a conflicting supplied hash is rejected. The helper validates, appends/readbacks start, then calls the existing runner once. No process is started on rejected preparation/start/readback. Failure after append leaves the action pending with the committed head, and requires readback/reconciliation before any retry. Reused evidence directories are rejected; each actual retry is a new linked action and directory.

Consume output.operation (or saved output_root/result.json through operation_io consume): host CLI exit0 means the runner returned, not native success. Native exit code, status, first_fault, process_started, stdout/stderr and effect_state stay explicit. The helper never invents SUCCEEDED or clean effects. Record action-outcome --apply from actual observations; pending/unknown effects stay reachable. Owned-local is a declaration, not a sandbox. Shell/live/external operations retain their separately authorized routes.

## Response disposition

`response-check --fields <response.json>` is read-only and may omit expected-head. Required fields:

```json
{"request_outcome_ids":["OWN-OUTCOME"],"excluded_active":{},"source_clause_ids":["OWN-CLAUSE"],"purpose":"progress","next_action":{"eligible":true,"description":"actual next authorized operation","evidence_refs":["current source/decision reference"]},"boundary":null}
```

Every active outcome outside this request needs its ID and source-bound exclusion reason in excluded_active. Keep the actual source-wide request scope: exclusions cannot shrink it. purpose is progress or completion. next_action requires a real reason and evidence even when eligible=false. An optional boundary has exactly kind (user_pause/permission_required/blocked/host_limit), reason and evidence_refs. Only an explicit user pause can coexist with eligible work and still permit final. New pending sources require reconciliation; completion cannot ignore them or current-request pending actions.

CONTINUE_WORK means continue the actual eligible work, or resolve the local missing disposition; commentary is the progress channel. REPORT_COMPLETION requires every scoped outcome terminal, no scoped pending action/source frontier and no eligible next action. REPORT_BOUNDARY preserves incomplete outcomes; it does not certify block/permission facts or grant them. Owner-declared source/scope/evidence/eligibility remain semantic responsibilities. No host Stop interception, autonomous wakeup, arbitrary-tool enforcement or all-model compliance is claimed.
