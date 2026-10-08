# Changelog

## 0.3.0-beta.5 — 2026-10-09

- A known Web response-size limit no longer holds an entire task as an unknown operation. Recovery authenticates the retained bytes and keeps the original failure without replaying its request.
- Oversized public responses retain a searchable prefix, explicitly marked incomplete. Independent requested pages can still be collected.
- Large JavaScript reads can request up to 8 MB per response without changing saved settings. Saved source can be searched for up to eight literal terms in one bounded read; static source inspection does not execute scripts or establish runtime behavior.

## 0.3.0-beta.4 — 2026-10-09

- Web evidence now includes observed links, form fields, script URLs and page metadata. Saved raw HTML and JavaScript can be searched or read in bounded ranges without refetching.
- Large pages receive separate context excerpts; successful pages remain available when another requested URL returns an HTTP error.
- Explicit URL lists are accepted for practical research. A single tool missing its purpose field can use the explicit message in the same response; original responses, arguments and permission checks are preserved.
- The Windows launcher preserves an exited service record and starts normally when Windows has reused its process ID. It never stops the unrelated process.

## 0.3.0-beta.3 — 2026-09-28

- Restore new-task sending after the local service restarts by refreshing expired session information.
- Add an explicit desktop connection recovery action that preserves the current document, draft and attachments.
- Look up the original submission during connection recovery and reuse its ID on an explicit retry; keep transport failures from silently replaying work.
- Show validation failures and disconnected status directly instead of reporting every failure as an unknown receipt.

## 0.3.0-beta.2 — 2026-09-28

- Replace the English and Japanese desktop showcase with matching content and order: shared learning, harness improvement, and actual-use records.
- Show the real cross-chat lesson-use and assessment records in both language editions.
- Align current documentation and distribution archives with the six replacement images; remove obsolete showcase artwork from the current source.
- Add official release notifications and optional in-app desktop updates with package verification, preserved user data and rollback on replacement failure.
- Reload changed application files through an idle-only service restart. Reopening the desktop no longer keeps reusing an outdated idle service.
- Keep active tasks running while an update is available, and provide a service restart action in the update panel.

## 0.3.0-beta.1 — 2026-09-28

- Add the OIF Windows desktop harness with a dedicated window, white OIF icon and local service recovery.
- Preserve instruction-by-instruction progress and results, important/full progress views, bounded record retrieval and artifact preview.
- Add shared lesson creation, selection, actual-use assessment, refinement, consolidation and retirement across chats.
- Retrieve lessons from both task goals and acceptance criteria, including work described only in the requirements.
- Include proportional execution policy, automatic context compression, model configuration and explicit access modes.
- Include a controlled harness-code update path with verification, activation, next-use records and rollback.
- Ship a complete Windows x64 package with pinned Python dependencies, English/Japanese guides and labelled example images.
- Keep the framework and skills-only plugin distributions available at the same beta version.


## 0.2.1 — 2026-09-11

A small follow-up that fixes stale descriptions left behind by the 0.2.0 card
change and makes the core concepts clear from the documents themselves.

### Fixed

- Stale "the human view keeps every clause" wording replaced with the bounded
  card + `EVIDENCE-INDEX.json` description across the framework reference, the
  packaged agent guidance and the knowledge-stewardship guide.
- The skill lifecycle schema now accepts the current `mgskill-effect-v2`/`v3`
  records, and the lifecycle contract's effect-candidate section matches the v3
  header (including `materiality` and `equivalent_skill_fingerprints`).
- Adapter lists and samples aligned: the optional Hermes adapter is listed, the
  runtime-demo sample matches current output, and the ledger path example is
  corrected.

### Documentation

- A concise "How It Works" overview (English and Japanese) and terminology entries
  for the objective card and the evidence index, so the core concepts read clearly
  without requiring prior framework knowledge.

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
