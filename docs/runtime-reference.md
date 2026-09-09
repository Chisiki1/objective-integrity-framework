# Runtime Reference

The runtime is a standard-library Python package organized around explicit files, explicit destinations, and identity-bound JSON inputs. It can be installed project-locally with the complete bootstrap mode or used directly from this repository.

Paths in this guide are source-checkout paths. After complete installation, prefix them with `.oif/` inside the selected destination—for example, use `.oif/runtime/objective_ledger.py` and `.oif/tools/check.py`. Minimal Skill installation does not include executable runtime files; use the Skill's manual continuity contract or adopt complete mode when persistence commands are needed.

## Requirements

- Python 3.10 or later.
- A project-owned writable directory for ledgers, indexes, queues, candidate material, or demonstrations.
- Explicit input files and roots; the portable core does not discover a user's live agent configuration.
- Python's bundled `sqlite3` module for the learning queue.

Native PowerShell preflight is optional and limited to the PowerShell adapter.

## Command Directory

Use `tools/oif.py` to discover and invoke the complete package from one stable entrypoint:

```bash
python tools/oif.py --help
python tools/oif.py <command> --help
```

| Command | Capability |
|---|---|
| `objective` | Durable objective ledger. |
| `objective-input` | Typed owner-input preparation and compare-and-swap progress against the native ledger. |
| `work`, `work-phase` | Source-bound work handoff and whole-scope phase selection. |
| `facts-build`, `facts-compile` | Provenance-bound source and action fact preparation. |
| `resolve`, `registry-check`, `skill-inventory`, `master-inventory` | Skill selection, registry structure, and bounded inventory. |
| `apply-skill` | Identity-bound selected-script preparation, execution, and effect. |
| `learning`, `index`, `lifecycle` | Candidate queue, source index, and effect transitions. |
| `operation` | Retain a native operation's status and raw streams, consume its result, or derive complete inactive candidate input. |
| `reconcile` | Inventory, propose and read back an owner-authored whole-current master reconciliation. |
| `artifact` | Exact manifest member reads, bounded diffs and JSON-pointer views without fallback to a guessed version. |
| `allocate`, `candidate`, `adopt`, `governance` | Stage allocation, isolated candidate/adoption, and condition governance. |
| `allocation-io` | Typed allocation state preparation and fresh allocator-result consumption. |
| `capabilities` | Pure bounded assessment of supplied host capability metadata. |
| `transition`, `scenarios`, `control` | Material transition, scenario/recomposition, and objective-control checks. |
| `legacy-status` | Explicit legacy split-task recovery. |
| `catalog` | Generate a public eight-skill distribution catalog to stdout or an explicit new file. |
| `demo`, `bootstrap` | Synthetic walkthrough and project-local installation. |
| `plugin` | Preview/build a portable skills-only package or validate its local structure; no host installation or submission. |

The dispatcher adds no implicit destinations or permissions. Each subcommand retains the underlying implementation's required explicit inputs and dry-run behavior.

`work` also supports result-derived next requests and per-actor safe-boundary
checks. `operation` is a runner, not a sandbox: the underlying argv still needs
the user's authority and an explicit effect scope. `reconcile` creates a proposal
and verifies readback; it does not itself publish a master. Read the linked
[learning contracts](governance-self-improvement.md) before adopting these routes.
The artifact helper is an independently usable generic Skill; it does not expand
the specialized eight-entry distribution catalog.

## Objective Ledger

Entrypoint: `runtime/objective_ledger.py`

| Command | Purpose |
|---|---|
| `bootstrap` | Create or reuse one logical objective ledger from an exact source event. |
| `classify-source` | Classify a captured source event against current objectives. |
| `dispose-source` | Apply an admitted source disposition while preserving lineage. |
| `resolve-capture-gap` | Reconcile a source capture that could not be safely classified earlier. |
| `progress` | Record actual progress, evidence delta, blockers, and the return step. |
| `action-start` | Bind a material planned action to the current objective head. |
| `action-outcome` | Record the observed action result without erasing partial or unknown effects. |
| `action-reconcile` | Resolve a prior partial or unknown effect from new evidence. |
| `verify` | Replay and verify journal, source, projection, and head relationships. |
| `recover-lock` | Recover only the expected abandoned lock identity. |
| `status` | Replay current state and refresh replaceable projections without changing objective semantics. |
| `preflight` | Project objective-state constraints onto an exact planned tool action. |
| `hook` | Consume a supported host event through an explicit adapter contract. |

Start with the demonstration unless you are integrating the API directly.

`status`, host start, and compact recovery are semantically read-only but may write bounded repair metadata inside the configured ledger root. They can acquire its lock, preserve a partial tail, and rebuild `current.json` and `objective.txt`; they do not classify source, change objective meaning, or authorize an action.

`objective-input` retains the native ledger's validation and expected-head checks; it is not another objective store. The human projection factors repeated source triples into aliases without dropping source clauses or outcomes. Current progress references can change while immutable outcome history remains available.

## Whole-Scope Work

`work-phase` consumes a hash-bound `source-wide-scope-v1` and `source-wide-state-v1` binding. The scope maps source requirements to every implementation item, required connection, and stage-appropriate check. The state records implementation, findings and BUILD/SWEEP/REPAIR/ACCEPT progress. The owner-control and v3 transition consumers use that same binding; a child result cannot reduce the owner's completion scope.

`work` prepares source-bound work units and proof-carrying handoffs. Its `{target, message}` output is a caller contract, not an executed dispatch. An integration maps `internal:<opaque-id>` (or a compatible `/root/...` target) to its host's actual internal-job API. It does not send a message into another user-visible task or create new authority.

Use [Whole-Scope Work](whole-scope-work.md) and the runtime [work entrypoint contract](../runtime/skills/master-guided-skill-lifecycle/references/work-entrypoint.md) for exact record shapes and manual fallback. Inspect command help before preparing advanced JSON inputs.

## Learning Queue and Index

Entrypoints:

- `runtime/skills/master-guided-skill-lifecycle/scripts/learning_queue.py`
- `runtime/skills/master-guided-skill-lifecycle/scripts/master_index.py`

The queue supports `init`, `upsert`, `status`, `history`, and `due`. It stores candidate identity, state, graph references, artifact references, immutable event history, and typed metrics. Missing measurements remain unavailable instead of becoming numeric zero.

The index supports `build`, `query`, and `propose-organization`. A build binds source hashes and complete declared archive members; a query can filter terms or current controls and uses bounded cursors. Organization proposals do not mutate masters. Retained backing, no-drop verification, compare-and-swap replacement and independent review belong to the adopting owner. The index is a retrieval projection, not a replacement for its source files. See [Knowledge Stewardship](knowledge-stewardship.md) for the archive protocol and current/history separation.

Queue metadata such as next-use details may be corrected without inventing an effect or advancing lifecycle state. Preserve the correction in the immutable event history.

## Lifecycle, Allocation, and Condition Governance

The lifecycle package also provides:

- `skill_lifecycle.py`: validate effect records and lifecycle transitions.
- `stage_allocation.py`: choose a capability-satisfying execution configuration from a typed stage view.
- `allocation_io.py`: build typed state and retain the fresh allocator result before the caller acts on it.
- `capability_snapshot.py`: assess explicit supplied capability metadata without reading live settings, browsing, dispatching, or activating hooks.
- `materialize_skill_candidate.py`: preview or create an isolated candidate from explicit roots and manifests.
- `isolated_registry_adoption.py`: preview or apply a destination-bounded registry adoption.
- `workflow_governance.py`: validate condition proposals, evaluations, approvals, rollback, and source lineage.

Candidate materialization and isolated adoption are dry-run by default. Model and reasoning allocation are computed from work shape and required capability, not from domain labels or a fixed tier ladder. The record distinguishes requested, accepted, effective, estimated, and unavailable values.

Condition governance accepts an explicit `workflow-condition-index-v1` inventory whose complete numbered source bodies, IDs and hashes are checked. There is no fixed condition count. Reuse a verified unchanged inventory for method-only work; source-authorized amendments remain a separate route, not a prerequisite for every improvement.

## Provenance-Bound Skill Resolution

Entrypoints under `runtime/skills/master-guided-skill-resolver/scripts/`:

- `build_skill_fact_input.py`
- `compile_skill_facts.py`
- `resolve_skills.py`
- `validate_registry.py`
- `inventory_skillbook.py`
- `inventory_live_masters_v2.py`

The builder and compiler require a disposition for every source clause, finalized action and tool-schema state when applicable, and a negative-selection challenge. The resolver reports exact matches, near matches, rejected candidates, and no-match outcomes while binding registry, lexical path, physical path, member hashes, reparse state, and the selection snapshot.

Revalidate a snapshot before first decision-bearing use if its source facts, registry status, selected files, roots, or resource claims may have changed.

## Skill Application Bridge

Entrypoint: `runtime/skills/master-guided-skill-resolver/scripts/skill_application_bridge.py`

Commands:

- `prepare`: open and hash-verify selected non-UI files and bind source, owner, lease, candidate member set, finalized action, schema, and selected script in one graph.
- `run-script`: execute one receipt-selected Python script with exact arguments and no shell.
- `run-powershell`: use the optional native PowerShell path when the bundle selects it.
- `effect`: bind later independent effect observation to the same application graph.

Selection, delivered bytes, execution, action result, and observed effect are separate states. The bridge is deliberately not a general-purpose command runner.

## Transition, Scenario, and Objective-Control Checks

Entrypoints:

- `runtime/skills/workflow-transition-admission/scripts/transition_admission.py`
- `runtime/skills/scenario-interaction-closure/scripts/validate_scenario_closure.py`
- `runtime/skills/objective-supervisor-control/scripts/supervisor_control.py`

Each accepts one explicit JSON input. They check structural identity, references, relation sets, lineage, allocation, and bounded dispositions. Their outputs support a decision; they do not self-certify semantic completeness, root cause, consumer safety, or action authority.

## Native PowerShell Preflight

The optional native implementation lives under `runtime/skills/powershell-exact-action/`. It checks exact finalized PowerShell text or batch members with the PowerShell parser and preserves `PASS`, `BLOCK`, and `ERROR` as distinct results. The cross-platform Python checker remains useful as a conservative portable screen; its pass is not described as native parser success.

## Legacy Split-Task Compatibility

`runtime/skills/durable-supervisor-status/` supports recovery of an explicitly existing split-task lease. It is not part of the default same-conversation workflow. Its state is written only below the explicitly supplied worker-local root, and its read/verify path does not repair state.

## Exit Codes and Evidence

Consult each command's `--help` for its exact required fields and exit codes. Preserve raw stdout, stderr, exit status, input identity, and output identity at an aggregation boundary. A structural `PASS` supports only the relation checked by that command; final consumer evidence remains attached to the claim that needs it.

## Static and Runtime Checks

`tools/check.py` provides the repository's coordinated check entrypoint. Its default mode is static. Source-tree behavioral checks use `--runtime`; complete installed copies use `--installed-runtime` to exercise shipped evaluation fixtures and runtime regressions without recursively invoking development installer tests. Both modes require `--temp-root <existing-disposable-parent>`. The parent must be non-redirected and separate from the distribution and live configuration; the runner creates and reports a unique tool-owned child below it. The check runner reports each partition independently so a first fault does not turn unexecuted partitions into passes.
