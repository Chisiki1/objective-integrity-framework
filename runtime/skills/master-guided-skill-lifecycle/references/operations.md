# Preserve the ordinary operation before display loses its result

Use `scripts/operation_io.py` for a recurring, material, owner-authorized local
native operation when its actual result changes the next operation. Replace the
corresponding direct invocation; do not run both. A trivial known-file read,
adequate existing caller or one-off task does not need this wrapper. Shell,
external and live operations retain their existing exact-action route.

The runner executes one argv without a shell and retains stdout/stderr, actual
exit status, first fault and declared source/method identities. Empty output is
not success; accepted nonzero exit codes are explicit. Timeout and incomplete
capture preserve unknown effects. There is no retry, daemon or descendant-cleanup
claim. Scope is asserted, not an OS sandbox. Keep secrets out of argv/evidence.

An existing caller may construct this ordinary input with the Python API; do not
hand-copy source/result metadata into subsequent stages:

```python
spec = {
    "schema": "work-operation-v1", "operation_id": "required-artifact-read",
    "owner_chat_id": owner_chat_id, "source": source_ref,
    "purpose": "Read the exact artifact needed by the current review",
    "argv": [python_executable, "-B", script_path, *actual_arguments],
    "cwd": owned_root, "timeout_seconds": 30, "accepted_exit_codes": [0],
    "methods": [{"id": "artifact-access-script", **script_ref}],
    "next_consumer": actual_consumer_id, "effect_scope": "read-only"
}
```

`methods` binds required instructions/scripts/resources, not personal reading or
semantic use. References have absolute `path` and uppercase `sha256`.
`owned-local` permits only already-authorized bounded artifacts; its partial
effects still need owner reconciliation. The evidence root must be absent with
a plain existing parent. No input or existing result is overwritten.

```text
python -B <skill>/scripts/operation_io.py run --input <spec.json> --output-root <new-owned-directory>
python -B <skill>/scripts/operation_io.py consume --result <directory/result.json>
```

CLI exit 0 means capture succeeded, not that the operation/objective succeeded.
Read structured `status`, `exit_code`, raw streams and effect state. Missing or
invalid results retain a raw reference and scoped reconciliation need. Historical
results remain readable after method changes; stale references stay explicit.
Never repeat an uncertain operation merely to obtain a cleaner result.

The operation, materializer and isolated-adoption entrypoints load their sibling
resources from exact source through `scripts/source_module.py`. They do not read
or write sibling bytecode caches, even when a CLI caller omits `-B`; no global
interpreter setting is changed. Keep the complete resource set together. For
external Python API loaders, suppress their own import-cache writes or use source
loading too: this does not control arbitrary third-party imports or operations.
The registry still rejects extra/missing/changed resources, including unexpected
cache files. Preserve and diagnose an existing extra file before scoped cleanup;
never add it to the manifest or ignore it just to make selection succeed.

`tests/test_bytecode_isolation.py` exercises default-Python cold/repeated calls,
real run/consume/candidate input and bytecode-substitution counterexamples in
temporary copies, not active discovery roots. Running tests only under `-B` does
not establish cache-free behavior of ordinary callers.

For the next necessary bounded internal job, use `work_io.py build-next-operation
--spec <actual-next-spec.json> --result <directory/result.json> --meaning
<owner-meaning.json> --output-root <new-owned-directory>`. The meaning schema is
the existing [in-work route](in-work.md). Raw result and owner decision become
next-request inputs. Persist/verify the returned prepared request and actually
dispatch that payload through the normal route. Preparation is not authority for
dependent replay. Independent read-only investigation may continue while effects
remain unresolved; its report must preserve that frontier.

When the actual result justifies creation/revision, author the complete necessary
package with skill-creator. `operation_io.py candidate-input --result <result.json>
--package-root <complete-package> --candidate-id <id> --operation
<CREATE|REVISE|MERGE|SUPERSEDE|RETIRE> --output <new-input.json>` derives the existing
full-resource materializer input. Non-create operations require
`--prior-candidate-id`; add `--challenge <actual-review>` only after independent
review. This is inactive input, not readiness/approval. Use the existing
materializer, transition, scoped activation and recovery requirements.

Generic task Skills use native discovery; not every Skill needs the custom
workflow registry. Replace affected current instructions/callers, preserve
history and unresolved users, and remove retired versions from active discovery.
Measure the actual necessary consumer, recurrence and false holds. Retain,
revise, merge, narrow or retire by those results, not folder/read/hook counts.
Broad autonomous adoption needs actual ordinary-task observation.
