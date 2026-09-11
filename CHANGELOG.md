# Changelog

## 0.2.0 — 2026-09-11

The framework and plugin share version 0.2.0. Existing task records continue to
work; no migration step is required, and record schemas keep their own versions.

### Added

- Bounded objective card v2 with a same-ledger `EVIDENCE-INDEX.json`: the human
  projection keeps counts, pointers and continuity frontiers; full authority,
  source bindings, outcome catalog and action history stay machine-readable.
- `action-method-supplement`: align a late method record with an exact, hash-bound
  update without rewriting history or hiding the original omission.
- Explicit host configuration: `projection_filename` and `host_binding`
  (`session_env`, `bindings_dir`) express host wiring as configuration instead of
  a fork; `adapters/hermes/` documents one optional binding end to end.
- Skill updates across the distributed packages, including the chat
  execution-route reference and condition-amendment / write-request templates.

### Changed

- Public positioning states the framework's goal explicitly: quality compounds
  with every pass, and a fast model that follows the framework can out-iterate
  heavier, costlier models. See README and `docs/philosophy.md`.

## 0.1.1 — 2026-09-09

### Added

- First versioned release: full-framework and skills-only archives, SHA-256
  checksums and a source-bound release manifest. The local builder previews an
  exact clean commit, protects existing destinations and does not publish.
- Download/verification, version compatibility and support guides; issue and
  pull-request templates; current complete contributor check commands.
- A reproducible query-response byte benchmark with duplicate-rich, distinct,
  contradiction, empty-result and stale-source controls. Its scoped results
  measure the preceding query improvement, not general agent productivity.

### Fixed

- Include the complete Apache License 2.0 and preserve contributor attribution
  in `NOTICE`. Correct inconsistent MIT metadata and plugin documentation;
  reject incomplete license payloads before packaging. The project's intended
  Apache-2.0 license is unchanged.
- Complete installations retain version, attribution, security, code-of-conduct
  and changelog files alongside the existing runtime and documentation.

The framework and plugin share version 0.1.1. No runtime schema or task-record
migration is required. Earlier changes below predate this release hardening.

## 2026-09-09

### Added

- A self-contained, skills-only plugin package builder with preview, source-bound approval, deterministic ZIP output, readback, and protected existing destinations. It does not install a plugin or submit it to a directory.
- Eight known plugin scenarios covering active requirements, corrections, failure recovery, resume, learning, quoted data, cancellation, and inappropriate activation. These are reproducible inputs and expected outcomes, not model evaluation results.
- `operation`, `reconcile`, `artifact`, and `plugin` command routes; a plugin guide and aligned Japanese onboarding.

### Strengthened

- In-work learning now connects original operation results to the next request or a complete inactive Skill package, including required scripts and resources. Continuing actors can reconcile their own method coverage at a safe boundary.
- Skill packaging and adoption use source-only loading on the affected import paths, preserving generated-cache isolation and version identity.
- Master stewardship can inventory and reconcile all current sections while retaining complete history. Search returns bounded unique-content groups with separately paged provenance; `--legacy-output` preserves the earlier CLI shape.
- Complete installations include the new runtime resources and plugin build sources. Privacy and support guidance distinguish task-owned records, installed files, and host data processing.

### Preserved

- Existing objective continuity, source-wide completion, condition governance, explicit adoption and rollback, branding, sponsor acknowledgments, and binary-safe Git-history scanning.

### Fixed

- Strict operation tests now select the real interpreter path on systems with linked Python launchers. Runtime path guards remain unchanged; a linked-executable counterexample protects that boundary.
- Default-Python regression failures retain both output streams and exit status, including structured errors emitted on stdout.

## Earlier foundation

### Added

- Durable same-conversation objective continuity with immutable source events, explicit source dispositions, open outcomes, pending-effect reconciliation, append-only history, and an English `objective.txt` projection.
- Provenance-bound source and finalized-action fact compilation, exact/near/rejected skill resolution, stale-snapshot checks, and an identity-bound application/result/effect bridge.
- Actionable learning queue, source-hash-bound master index, lifecycle disposition, dynamic stage allocation, isolated candidate adoption, and source-bound condition governance.
- Executable structural scenario/recomposition, material-transition admission, and mid-work objective-control checks.
- Project-local complete bootstrap mode and a runnable synthetic demonstration.
- Public runtime reference, objective-continuity guide, seven-condition maintainer guide, native PowerShell adapter guide, and expanded progressive skill guidance.
- Public `SKILL.md` entrypoints for all eight generic runtime skill families.

### Changed

- Same-conversation primary ownership is now the default; independent review stays read-only, while split-task status is explicitly a legacy recovery adapter.
- Public capability coverage now maps established normative families and current runtime mechanisms to their shipped paths.
- Bootstrap recovery uses a hash-bound manifest and conflict-aware rollback; source and adoption trees must remain fully separate.
- README and adoption guidance lead with objective achievement, a runnable demonstration, safe preview, complete project-local installation, and measured learning.

The release candidate, verification results, and publication status are reported separately from this change description.
