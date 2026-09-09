# Lifecycle and effect contract v3

New effects use `mgskill-effect-v3`. It retains the v2 fields and adds `materiality` with explicit `material`, `effect_candidate_generated`, and `reason` values, plus an `equivalent_skill_fingerprints` SHA-256 array. A material event is rejected unless candidate generation is true. Equivalent fingerprints recommend one merge candidate. Existing `mgskill-effect-v2` remains accepted for lineage compatibility but cannot establish the v3 obligation. Mechanical generation never authorizes semantic activation.

Only positive v3 outcomes (`applied` or `advanced`) require `effect_observation`, with actual outcome evidence and an independently identified observer. Its `use_kind` is `instruction`, `script`, or `tool`: instruction requires `instruction-used`; script/tool require `script-executed`/`tool-executed` plus bound action ID and outcome SHA-256. A read-only episode cannot claim a positive effect. Non-positive outcomes, including `not_applied`, `not_observed`, `unavailable`, and an unavailable observer, remain recordable with their typed reason/evidence and are never promoted to positive effect. `COORDINATED-WORK` owners carry `task_id`, `lease_id`, `chat_id`, and `source_sha256`; legacy `WORK` remains lineage-compatible.

## Inactive candidate and isolated adoption

The existing `materialize_skill_candidate.py` and `isolated_registry_adoption.py` now share `scripts/skill_package.py`. Both consume the exact resource member set; the helper is a library, not another optional execution command. Materialization and staging do not author a Skill, decide its usefulness, execute its scripts, register it in an active root, or grant activation authority. The owner must first construct the actual useful instructions/resources, then pass that complete reviewed package through these existing callers.

`materialize_skill_candidate.py` accepts `mgskill-inactive-candidate-v1` and `mgskill-inactive-candidate-v2` for `CREATE`, `REVISE`, `MERGE`, `SUPERSEDE`, or `RETIRE`. These operations describe the inactive candidate's lifecycle intention; none mutates an older version or retires an active installation. Existing v1 remains instruction-only: it binds one source `SKILL.md` and copies only that file. Unrelated siblings at the v1 source are not claimed as packaged resources. The resulting candidate directory, including an old v1 candidate presented to adoption, must contain exactly its declared files. Old self-hashed v1 materialization receipts remain readable without the new optional package fields.

V2 supplies one complete, explicit file package. `SKILL.md` is required with that exact case; linked scripts, references, assets, templates and other needed resource files belong to the same identity. There is no extension or fixed resource-folder allowlist. File names must be safe portable relative POSIX paths: no absolute path, backslash, empty/dot/dot-dot component, control character, drive/stream syntax, reserved device name, trailing dot/space, duplicate/case alias, or file/directory-prefix collision. Every regular file below the source package root must appear once, with no extra or missing files. Empty plain directories have no file identity and are not reproduced. No file is silently excluded as a cache, hidden file, UI resource or test fixture; remove unneeded files from the prepared source or explicitly review/include them.

### V2 input and identity

Top-level fields are exactly `schema_version`, `operation`, `event_id`, `objective_id`, `source_claims`, `owner`, `candidate`, `evidence`, `rollback`, `independent_challenge`, and `proof_ceiling`. `candidate` keeps the v1 fields `candidate_id`, `artifact_path`, `artifact_sha256`, `prior_candidate_id`, and `equivalent_fingerprints`, adding exactly `package`. `package` has exactly `root`, `members`, and `manifest_sha256`. Each member has exactly `path` and `sha256`, matching the registry's `files` row shape; member size and permissions are not additional registry fields. The artifact path/hash must bind this package root's `SKILL.md`.

Canonicalize the member array by case-sensitive ascending `path`, with uppercase SHA-256 strings. Its package identity is SHA-256 of UTF-8 `json.dumps(members, sort_keys=True, separators=(",", ":"))`. `skill_package.package_manifest(members)` supplies this exact operation after validation. Input member order may vary; returned member sets and the adoption entry must use this canonical array. This hash is separate from the retained v1 candidate-root CAS: SHA-256 of UTF-8 newline-joined, path-sorted `relative/path<TAB>UPPERCASE_SHA256` rows, with no final newline. An empty root uses the SHA-256 of empty bytes.

For example, the following is an input template, not executable evidence. Replace every angle-bracket value with the current explicit path, exact source identity or computed hash. The three declared files must be the whole prepared source tree; do not copy these illustrative hashes literally.

```json
{
  "schema_version": "mgskill-inactive-candidate-v2",
  "operation": "CREATE",
  "event_id": "EVT-EXAMPLE",
  "objective_id": "OBJ-EXAMPLE",
  "source_claims": ["SRC-EXAMPLE"],
  "owner": {
    "lane": "COORDINATED-WORK",
    "task_id": "<actual owner task>",
    "chat_id": "<actual logical chat>",
    "lease_id": "<actual bounded lease>",
    "source_sha256": "<exact source SHA256>"
  },
  "candidate": {
    "candidate_id": "example-report",
    "artifact_path": "<absolute prepared package root>/SKILL.md",
    "artifact_sha256": "<SKILL.md SHA256>",
    "prior_candidate_id": null,
    "equivalent_fingerprints": [],
    "package": {
      "root": "<absolute prepared package root>",
      "members": [
        {"path": "SKILL.md", "sha256": "<SKILL.md SHA256>"},
        {"path": "references/format.json", "sha256": "<format.json SHA256>"},
        {"path": "scripts/report.py", "sha256": "<report.py SHA256>"}
      ],
      "manifest_sha256": "<canonical member array SHA256>"
    }
  },
  "evidence": {"source": "<actual result and package review evidence>"},
  "rollback": {"owner": "<recovery owner>", "method": "remove only the new inactive candidate after an explicit recovery decision"},
  "independent_challenge": {"status": "completed", "result": "<actual independent review>"},
  "proof_ceiling": "exact inactive package only; use and effect remain separate"
}
```

`candidate_id` remains one lowercase alphanumeric/hyphen basename, starting with an alphanumeric character and at most 63 characters. Owner lanes remain `WORK` or `COORDINATED-WORK`; the latter requires nonempty `task_id`, `lease_id`, `chat_id`, and `source_sha256`. Source claims, event/objective IDs, evidence and rollback must be supplied. The owner and challenge fields are supplied assertions with structural checks, not a trusted actor identity, independent-observation proof or authority grant.

### Existing commands and receipts

Materialization without `--apply` only validates/returns a proposal. A pending independent challenge returns `INACTIVE_CANDIDATE_HOLD_INDEPENDENT_CHALLENGE` (exit 3); it does not create files. `--apply` requires a ready proposal, an existing inactive candidate root, its exact pre-write root manifest, and every applicable active discovery root. The destination `<candidate-root>/<candidate_id>` must be absent and disjoint from the prepared package. The candidate root must be disjoint, in both ancestry directions, from every supplied discovery root. Existing unrelated candidate-root files are preserved under root CAS; a different new candidate can coexist with them.

```text
python -B scripts/materialize_skill_candidate.py --input <absolute-candidate-json> --candidate-root <absolute-inactive-root> --expected-root-manifest-sha256 <root-hash> --discovery-root <absolute-active-root> --apply
```

The caller captures stdout as the materialization receipt. A successful v2 receipt uses `mgskill-inactive-candidate-proposal-v2`, `decision=INACTIVE_CANDIDATE_MATERIALIZED`, and `support_mode=resource-package`. Alongside the original candidate/source/owner fields it includes `candidate_root`, `candidate_path` (the copied `SKILL.md`), `candidate_package_root`, `candidate_artifact_sha256`, `candidate_member_set`, `candidate_package_manifest_sha256`, pre/post root manifests, exact readback, transaction state, rollback and actual/attempted write paths. `materialization_receipt_sha256` remains canonical SHA-256 over the receipt without that self-hash field. V1 keeps its proposal schema and `support_mode=instruction-only`, even when the implementation emits the new optional evidence fields.

Adoption uses the same existing command and the entire returned member set:

```text
python -B scripts/isolated_registry_adoption.py --proposal <absolute-materialization-receipt> --candidate-root <same-absolute-inactive-root> --isolated-adoption-root <absolute-new-isolated-root> --expected-destination-manifest-sha256 ABSENT --registry <absolute-source-registry> --expected-registry-sha256 <source-registry-hash> --registry-entry <exact-entry-JSON> --discovery-root <absolute-active-root> --apply
```

Repeat `--discovery-root` for every relevant active root. Omitting a root is not proof of isolation. The isolated destination's parent must already exist; the destination must be absent and disjoint from the candidate root and discovery roots in both ancestry directions. The registry entry retains required `skill_id`, `name`, `version`, `origin`, `relative_path`, `status`, `match_clauses`, and `files`; `name` and `relative_path` equal the candidate ID, `status` is exactly `candidate`, and `files` equals the complete canonical `candidate_member_set`. Existing metadata fields are preserved. Duplicate skill ID/name, incomplete member lists, active-status promotion and output-name collisions are rejected. This adapter is not a replacement for full semantic registry validation.

The adapter checks the materialization receipt's self-hash, candidate-root/name/path, owner/source and current package bytes. V2 additionally binds original and materialized full member identities. It reads the source registry once for initial SHA-256/JSON/backup, retains those same bytes, then rechecks the source before commit. It stages the complete candidate directory, `<source-registry-name>`, and `<source-registry-name>.backup` under one isolated destination. The source registry and source candidate are not edited. `ISOLATED_ADOPTION_STAGED` includes the retained `skill_path`/`skill_sha256`, plus `package_root`, `package_member_set`, `package_manifest_sha256`, `support_mode`, registry/backup hashes and whole-destination readback. Its `receipt_sha256` binds the result without that self-hash field.

### Filesystem and transaction boundaries

Both callers validate lexical absolute paths before resolution, inspect every existing ancestor and source-tree member, and reject symlinks, Windows reparse objects, non-regular files, hardlinked files, traversal and aliases. Resource/control files are read through regular-file handles with identity/change checks. Exact source bytes are captured and copied into new exclusive files, then source package/control CAS and staged members are rechecked before publication. V1 receives these protections without gaining implicit resource support. File bytes/path membership are the identity: extended attributes, ACLs, alternate streams, empty directories and shell/runtime dependencies are not packaged equivalence claims. Ordinary POSIX file mode bits are copied without special privilege bits; no package code is automatically run.

All preflight rejections occur before the first write and return `writes_performed=[]`. Once staging starts, errors retain the first fault, attempted write paths, stage path/existence, cleanup failures, effect state and destination readback. Only an owned, still-plain precommit stage may be removed; an ownership/link mismatch leaves it for explicit recovery. Cleanup failure is not reported as no effect. Materialization emits `MATERIALIZATION_OUTCOME_UNKNOWN` on a transaction failure; adoption emits `ADOPTION_FAILED` before commit and `OUTCOME_UNKNOWN` at or after commit. Neither automatically retries.

Transactions preserve `PREPARED`, `STAGED`, `COMMITTING`, `COMMITTED`, and `VERIFIED`. Publication must not replace a racing existing destination: Windows uses non-replacing rename, Linux uses available `renameat2(RENAME_NOREPLACE)`, and macOS uses available `renamex_np(RENAME_EXCL)`. There is no replace-existing fallback. A host without the required primitive is rejected before writes; a filesystem-level publication failure after staging retains its actual partial/cleanup evidence. An exception after a successful rename is recognized from the owned directory identity where observable. `COMMIT_UNCERTAIN` retains uncertainty when the outcome cannot be established. Every fallible readback or receipt-assembly operation after rename, including after `VERIFIED`, preserves the committed destination and returns unknown instead of asserting no effect. An output-channel failure may also prevent delivery of that failure JSON; retain the host's raw fault and inspect the declared destination before a dependent recovery or retry.

These checks assume the caller controls the declared staging namespace during the operation. Rechecks and no-replace publication are not an OS sandbox or a guarantee against an adversarial actor replacing writable ancestors between system calls. Self-hashes do not authenticate the receipt writer or confer authority. A verified receipt proves the observed bounded file state, not host discovery, later byte freshness, execution, useful behavior, activation, rollback success or power-loss durability. The next consumer still revalidates the relevant current package and invokes only an authorized exact action.

`tests/test_skill_package.py` supplies complete CLI normal/adverse paths and actually runs a synthetic staged script that reads its staged reference, template and asset. It also covers v1 historical receipt compatibility, missing/extra/changed members, malformed identities, traversal/case aliases, links, preflight no-write, source/root/registry CAS changes during staging, racing publication, cleanup failure and committed-unknown outcomes. Symlink/hardlink fixtures may explicitly skip when the host cannot create them; skipped paths are not PASS. The synthetic reparse-attribute check is not an actual junction-creation test. These fixtures remain isolated execution evidence, not autonomous ordinary-task creation or measured user benefit. Preserve raw suite faults and combine this lane with the root's complete candidate review before adoption.

## Effect candidate

`schema_version` is `mgskill-effect-v2`. Required fields are `event_id`, `objective_id`, `source_claims`, `skill_id`, `skill_version`, `registry_sha256`, `selection_snapshot_sha256`, `planned_objective_delta`, `actual_objective_delta`, `planned_evidence_delta`, `actual_evidence_delta`, `owner`, `trigger`, `candidate_identity`, `independent_challenge`, `retirement_trigger`, `unknown_case_disposition`, `outcome`, `consumer_result`, `side_effects`, `rollback_result`, `rework`, `proof_ceiling`, and `metrics`.

`outcome` is `advanced`, `applied`, `no_effect`, `recurred`, `false_block`, `misselected`, `prevented`, `outcome_unknown`, `not_observed`, `not_applied`, `unavailable`, or `not_applicable`. `prevented` additionally requires `pre_submission_block_observed=true`, a 64-hex `rejected_candidate_sha256`, `candidate_preserved=true`, `applicable_set_equality=true`. It is a mechanical action result, not automatically inferred consumer benefit. `not_applied` means applicable knowledge or a selected constraint existed but did not screen the final action before submission; `unavailable` and `not_applicable` must not be encoded as zero-value benefit.

`metrics` has exactly these fields: `objective_evidence`, `mandatory_quality`, `elapsed_ms`, `token_usage`, `tool_calls`, `delegation`, `integration`, `rework`, `consumer_outcome`, `overhead`, `false_positive`, `late_miss`, and `recurrence`. Each is an object with `status` equal to `observed`, `unavailable`, or `not_applicable`. Observed metrics require `value`; unavailable/not-applicable metrics require `reason` and must not contain a placeholder value.

Accepted effects return a candidate-only recommended disposition. `recurred`, `misselected`, and `not_applied` recommend revise-or-merge; `false_block` recommends narrow-or-retire; `no_effect` recommends revise-or-retire; unknown/unobserved effects hold measurement. These recommendations never authorize semantic activation or a master write.

`independent_challenge.status` is `pending` or `completed`; completed requires a result. Pending challenge forces `hold-independent-challenge` regardless of the outcome recommendation. The active source-bound primary Worker owns event-driven candidate generation at the existing PENDING-EVENT/Stage Effect transition; no fixed polling or background daemon is implied.

## Transition candidate

`schema_version` remains `mgskill-transition-v1`. Required fields are `event_id`, `objective_id`, `source_claims`, `skill_id`, `from_state`, `to_state`, `reason`, `baseline`, `normal_path_counterexamples`, `independent_challenge`, `rollback`, `identity_snapshot`, `proof_ceiling`, and `ambiguity`. `identity_snapshot` contains exact registry, selection and skill content SHA-256 values.

Legal transitions remain `raw-event -> solution -> candidate -> shadow -> audited -> active-bounded -> measured`, followed by revise, merge, supersede, or retire. Direct `active-bounded -> retire` is allowed only with exact harm, containment and recovery owner. Material ambiguity returns `USER-DECISION`. `identity_snapshot` and candidate materialization do not create those transition proofs.

The validator proves schema and lifecycle/effect invariant closure only. Semantic merit, actual consumer outcome, empirical improvement and rollback success remain outside its proof ceiling.
