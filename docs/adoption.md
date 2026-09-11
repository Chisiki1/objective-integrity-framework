# Adoption Guide

Start with the smallest mode that helps the work, then add only the controls whose omission would materially weaken an outcome.

## Try the Workflow in a Sandbox

The demonstration creates its state only under the explicit new or empty directory you provide:

```bash
python tools/demo.py --directory ../oif-demo
```

Use the generated files to follow source capture, owner-input progress, an action result, a learning candidate, and a later matching query. Sample outputs live in [examples/runtime-demo/](../examples/runtime-demo/) — see [demo-result.example.json](../examples/runtime-demo/demo-result.example.json) — for comparison. The `current_workflow` result adds a complete BUILD → SWEEP → REPAIR → SWEEP → ACCEPT example, typed allocation, and current/history retrieval. Inspect `current-workflow/first-sweep-observations.json` alongside the repaired observations: the first findings remain available. Review references in the demo are synthetic structural fixtures, not a real independent review. The demonstration does not dispatch agents, install project guidance, or change an agent's global configuration.

## Preview a Complete Project-Local Installation

The complete mode installs the generic runtime, tools, documentation, examples, and templates under `<destination>/.oif`. The selected adapter controls only the top-level project guidance.

Preview first:

```bash
python tools/bootstrap.py --destination ../sample-project --adapter generic --mode complete
```

The preview computes and prints a plan without changing the destination. Review the resolved destination, planned member set, replacements, new files, `backup-parent`, and `plan-sha256`.

Copy that hash into the apply command to bind the reviewed source bytes, destination pre-state, adapter, mode, and installer identity:

```bash
python tools/bootstrap.py --destination ../sample-project --adapter generic --mode complete --expect-plan <plan-sha256> --apply
```

If the current source or destination no longer matches, apply exits without changing destination files; preview again and review the new plan. Direct `--apply` without `--expect-plan` remains available as an explicit current-state route for callers that deliberately inspect the state at apply time. It does not claim to be bound to an earlier preview.

After installation, run the bundled demonstration from the destination:

```bash
python ../sample-project/.oif/tools/demo.py --directory ../oif-demo-installed
```

## Installation Behavior

- The destination is required; there is no implicit home or global-agent target.
- The adoption destination must not be this distribution checkout, its ancestor, or its descendant. This keeps source and installed state fully separate.
- Dry-run is the default. A write requires `--apply`.
- The recommended preview-to-apply route supplies the printed `plan-sha256` through `--expect-plan`.
- The complete package is installed below the destination's `.oif` directory.
- The adapter supplies the top-level integration appropriate to the selected runtime.
- Files that would be replaced are copied into an action-specific backup before the replacement is committed.
- Newly created files and replaced files are recorded so rollback can distinguish them.
- A partial or conflicting operation reports its first decision-bearing fault and retains recovery information.
- Installation does not make runtime behavior, host integration, or downstream results true by declaration; those are observed through their own consumer paths.

## Choose an Adoption Mode

### Read-only

Read [Core Invariants](../framework/core-invariants.md) and follow the [10-minute core loop](core-loop.md). Nothing is installed.

### Manual project-local

Copy only the records you need into a project-owned directory. A useful first set is:

- `templates/objective-contract.md`
- `templates/open-deliverable-ledger.md`
- `templates/evidence-map.md`
- `templates/audit-receipt.md`

Keep the copies under normal project version control when they describe project behavior.

### Project skill

Use `.agents/skills/objective-integrity/SKILL.md` when the runtime supports project-scoped skills. The skill keeps the entrypoint compact and routes standard, high-assurance, continuity, and learning detail through focused references.

### Complete runtime

Use `--mode complete` when durable objective state, material-transition checks, a learning queue, provenance-bound skill application, or other executable framework capabilities are needed. See [Runtime Reference](runtime-reference.md).

The complete package includes its shipped evaluation fixtures and runtime regressions. After installation, create an empty disposable parent outside the source and destination, then run the installed-only route:

```bash
mkdir ../oif-check-work
python ../sample-project/.oif/tools/check.py --installed-runtime --temp-root ../oif-check-work
```

`mkdir` is a one-time setup for the explicit parent; choose a fresh location if that name already contains unrelated work. `--temp-root` names that existing, non-redirected parent. The checker creates and reports one unique tool-owned child beneath it. Installed-only mode exercises shipped runtime and evaluation inputs without recursively running development installer tests.

### Optional adapters

The generic adapter is the default portable integration. Other adapters are opt-in. The Codex, native PowerShell and Hermes adapters do not define the framework and are not required by the portable core; see [Platform adapters](platform-adapters.md).

## Skill Book Adoption

For teams with multiple reusable procedures, add the [Skill Book](skill-book.md) gradually:

1. Compile facts from the actual source clauses and finalized job or action payload.
2. Resolve selected, near, rejected, and no-match entries from the current registry snapshot.
3. Revalidate the snapshot before first decision-bearing use.
4. Bind the exact selected bytes to the actual action and result.
5. Record the later consumer effect separately from selection and execution.
6. Revise, narrow, merge, supersede, or retire guidance from observed effects.

For a small task where this chain would not change a decision, use the core loop directly.

## Update an Existing Complete Installation

Run the new distribution's installer from its separate checkout. Retain the destination's `.objective-integrity-backups` directory: its active hash-bound installation manifest identifies exactly which package files the installer owns. Both the previous `oif-install-v2` and current `oif-install-v3` manifests are accepted.

Preview the update:

```bash
python tools/bootstrap.py --destination ../sample-project --adapter generic --mode complete --update
```

Review the printed package members, any obsolete owned members to remove, and the new `plan-sha256`. The plan also binds the selected retained installation manifest, so a changed manifest invalidates the preview. Then apply that exact plan:

```bash
python tools/bootstrap.py --destination ../sample-project --adapter generic --mode complete --update --expect-plan <plan-sha256> --apply
```

`--update` changes only recorded package members under `.oif/`. It preserves top-level `AGENTS.md`, project Skills, objective records, and user settings. Choose the adapter already used by the project; updating the package is not an adapter migration. New documentation does not silently replace customized project guidance—review any desired guidance changes separately.

The installer refuses an edited owned package member, a new package path occupied by an unowned file, a changed preview, or an unresolved prior installation. Unowned files are not cleanup targets. Obsolete files are removed only when the retained manifest proves ownership and their current bytes still match that installation; each removal has a backup for rollback. If package files were customized, preserve and reconcile those edits explicitly before retrying. Without a valid complete-install manifest, use a fresh separate destination and migrate deliberately instead of guessing ownership.

Run the installed demonstration or the installed-only checks above to inspect the updated package. Save the new backup path if you may need to restore the previous package.

## Rollback and Uninstall

Keep the backup path printed by the apply operation. To restore that action:

```bash
python tools/bootstrap.py --rollback <backup-directory>
```

Rollback restores files replaced or removed by the matching hash-bound action and removes files that action created. It supports both v2 installation manifests and v3 installation/update manifests. If a tracked destination file changed after the action, rollback reports a conflict and preserves the later content for explicit recovery. Each rollback attempt records its pending member and restored frontier. An interrupted attempt retains `ROLLBACK_INTERRUPTED` and its first fault so a later retry can distinguish completed work while rechecking the full destination before another mutation. Older backup directories without a supported hash-bound manifest require manual review rather than automatic restoration.

Manual adoption has no bootstrap manifest. Remove or revert only the files you deliberately copied.

## Moving From Evaluation to Real Work

Use a new project-local ledger for each logical objective tree. Keep source events immutable, classify later user messages explicitly, preserve pending or unknown action effects, and take one read-only independent review key when the risk justifies it. Avoid copying demonstration state into a real task: the demo is synthetic evidence, not an authority source for your project.
