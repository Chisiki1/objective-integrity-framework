# Ledger contract and CLI

Use the reviewed `objective_ledger.py` candidate or its exact installed copy. The script uses only the Python standard library. Its journal is authoritative; `current.json` and `objective.txt` are replaceable projections.

The human projection is a bounded Objective Card. Full source bindings, normative fields and outcome/action history remain in same-ledger `EVIDENCE-INDEX.json`, `current.json` and `journal.jsonl`. While a source is unclassified or a dependent source-recovery frontier remains, the card leads with reconciliation, labels the old objective as last reconciled, and withholds old next-work advice; overflow and compact context preserve this priority. The owner reviews the latest actual user message and all pending source references, classifies or explicitly disposes each, and rereads the updated card before new material work. A raw capture is not semantic authority. Do not directly edit the generated card, drop machine history, or treat shorter display as retirement. Hash-pinned owner callers must explicitly rebind a reviewed changed runtime; never edit historical evidence helpers or another task's caller on their behalf.

## Identity and trust

The config schema is `chat-objective-continuity-config-v1` and requires `namespace`, `implicit_session_ledgers: true`, `session_bindings`, and `data_root`. The common config is not fixed to one chat. For an unseen host session the exact `session_id` becomes the logical-chat ID when the first `UserPromptSubmit` arrives. The directory key is SHA-256 over namespace and logical-chat ID. Titles, models, workspaces, repository paths, and prompt words never enter this key.

An explicit binding can route a replacement host session to an existing logical-chat ID after a source-backed same-chat decision, or exclude a separately identifiable session with `role: subagent`. Host adapters must document which stable identity they supply and whether child events are distinguishable; ordinary hook events may expose only the parent session ID, so they cannot by themselves prove that the actor is the root owner. The runtime therefore uses session identity only for routing, never semantic authority.

For manual bootstrap, obtain the stable session and logical-chat identifiers through the reviewed host adapter, or provide explicit neutral project identifiers. Never substitute a title, working directory, model name, repository name, or inferred label. Equality between adapter metadata and a trusted host event remains unproven until that event is observed at the adapter boundary.

An unseen fork/session is a distinct new chat by default and remains absent until its first prompt. Reuse an existing ledger only after the owner adds an explicit binding because the source says it is the same logical objective. A separately identified child can be bound as `subagent`; a child that shares the parent session can see the same projection but cannot create a second ledger.

## Source and contract lifecycle

Bootstrap stores exact UTF-8 source bytes at `sources/<SHA256>.txt`, appends one deterministic unclassified source event, and creates the projection if the ledger was absent. The trusted `(session_id, delivery_id)` pair is the idempotency key: exact repeat is idempotent and different bytes under the same key are a collision. Without a trusted delivery ID, equal content is retained as distinct occurrences.

`classify-source` consumes a JSON object containing:

- exact `source_event_id`;
- disposition `INITIAL`, `ADD`, `CLARIFY`, `CORRECT`, `REPLACE`, or `WITHDRAW`;
- a new contract ID and immutable clause references (`clause_id`, `source_event_id`, `source_ref`, `source_sha256`, optional locator);
- the primary objective and open outcome IDs for an active contract;
- exact predecessor linkage for every non-initial change.

The runtime checks lineage and reference identity, not whether the root's interpretation is semantically correct. Independent source review remains necessary where the workflow requires it.

Supported normative fields are `primary_objective`, `mandatory_acceptance`, `acceptance_criteria`, `authority`, `scope`, `constraints`, `permissions`, `prohibited_substitutes`, `preservation_contracts`, `evidence_requirements`, `stopping_conditions`, `independent_deliverables`, `methods`, `preferences`, and `proof_ceiling`. Other contract fields are rejected rather than silently dropped. On `ADD / CLARIFY / CORRECT`, omitted supported fields are copied from the parent. Every supplied changed normative field requires one `normative_transitions` item whose `field`, matching operation, and source-clause IDs bind the change to the current source. `REPLACE / WITHDRAW` do not inherit parent policy and cannot use incremental normative transitions.

`ADD`, `CLARIFY`, and `CORRECT` preserve progressed status/evidence from current replay rather than trusting a stale contract template. Outcome `WITHDRAW / DEFER / SUPERSEDE / REOPEN` transitions require their exact prior status and clause IDs bound to the current source. A faithful correction gives the corrected meaning a new outcome ID and explicitly supersedes the prior ID; an outcome ID never changes description.

`progress` requires the exact `contract_id` and accepts only existing outcome updates, evidence references, blocker/return information, proof ceiling, and next work. It can mark `OPEN` or `SATISFIED` and merges prior evidence. It cannot source-authoritatively retire, defer, supersede or reopen an outcome. Every source-classified contract transition clears the prior progress/next-work record; a new one must bind the new current contract.

`dispose-source` consumes a root-reviewed unclassified capture with one of `INTERNAL_CONTINUATION`, `SUBAGENT`, `IRRELEVANT`, `NON_USER`, or `ALREADY_INCORPORATED_DUPLICATE`. It retains raw bytes and appends no authority or contract change. The already-incorporated form also requires the exact current contract and clause IDs it was reviewed against.

## Action and recovery lifecycle

`action-start` records a unique action ID, description, affected outcome IDs, contract/source links, and intended evidence. It rejects new starts while source reconciliation is pending; exact old contract IDs alone do not prove latest-input coverage. Source read/classify/dispose/recovery and existing action outcomes/reconciliation remain available. This normalizer check does not reinterpret old journal events or intercept tools outside this caller. `action-outcome` binds it once to `SUCCEEDED`, `FAILED`, `CANCELLED`, or `UNKNOWN_EFFECT` plus an explicit matching effect state. It cannot overwrite start identity fields or a prior terminal result. A retry uses a new ID plus `retry_of`; its success never clears the earlier unknown effect. `action-reconcile` adds evidence-linked reconciliation attempts. `REMAINS_UNKNOWN` can be followed only by an event linked to that exact prior reconciliation; a terminal reconciliation cannot be overwritten. The original first fault stays on the action.

Use `preflight --tool-name <exact-name> --action-class <read_only|mutating|mixed|unknown> --depends-on-action <id>` immediately before a dependent action. Exact configured classifications can enforce the bounded hold. Mixed/unknown classification has no prevention claim. Read and repair commands stay available when mutation is held.

Every proposed event is normalized and fully replayed against current state under the same identity-bound lock before any journal byte is appended. Rejection evidence is separate and leaves the semantic head unchanged. Lock owner publication uses complete fsynced bytes and atomic creation. Once append begins, the lock carries exact event/head/effect identity. A release failure after commit yields an executed/uncertain receipt even before the normal result assignment; if append/projection already failed, that initiating fault remains primary and cleanup is separate. `recover-lock` requires the exact lock hash and conservative dead-owner result, preserves the lock bytes, refuses live/unknown owners, then replays and repairs.

Identity-bound capture-gap sidecars are hash/identity checked and joined into replayed state. They remain unresolved in SessionStart/status/projection and hold only this chat's dependent configured mutation; independent read-only work remains available. `resolve-capture-gap` accepts `SOURCE_RECOVERED` only when the selected immutable source's verified SHA-256 and byte count equal the sidecar's exact observed identity. Replay reconstructs and rechecks the same relation, so an older/different source, a later-modified source, or a gap without exact source identity cannot remain exact-recovered. Before any resolution exists, the owner may instead record `OWNER_DISPOSITION` with an explicit ground. Each gap has one immutable resolution: if a prior exact recovery is later invalidated, a second resolution or owner-disposition overwrite is rejected. Restore the exact originally bound source bytes to recover that relation; if those bytes are irretrievable, leave the gap unresolved and continue only independent work. Neither resolution creates semantic authority or changes the objective. Before ledger creation, compact context identifies the existing `capture-gaps` directory containing the complete unresolved frontier rather than naming a nonexistent projection. Global unbound gaps are never inferred into a chat.

Every dependent classification/progress/action-start reopens required source files and verifies their hashes. `STRUCTURE_PASS_SOURCE_UNPROVEN` is not PASS for dependent semantics. Unrelated read-only work remains allowed with that proof ceiling. A journal append/projection/output failure after append begins is reported as committed/uncertain and requires replay; it is never reported `executed:false`.

The Stop hook performs structural readback but never blocks or continues the turn. Some hosts convert a blocking stop reason into a new input turn; manufacturing that input would violate authority and can loop. On failure the adapter lets stopping proceed normally, surfaces a warning, and leaves the invalid ledger explicitly unresolved.

## Hook adapter

`hook --event <name>` reads one bounded JSON object from stdin. Supported names are `UserPromptSubmit`, `SessionStart`, `PreCompact`, `PostCompact`, `PreToolUse`, and `Stop`.

- `UserPromptSubmit` creates/reuses the session-routed ledger, stores exact source bytes with an unverified-host origin, and marks them unclassified. It never extracts intent from keywords.
- `SessionStart` and compact events read/repair an existing ledger; they append no semantic/journal event and do not create a ledger before the first prompt. They may acquire a lock, preserve a partial tail, and rewrite projections, so they do not claim zero filesystem writes. SessionStart returns recovery-target-first context bounded to 600 bytes: an existing `capture-gaps` directory before ledger creation, otherwise the current `objective.txt`. Current Pre/PostCompact outputs cannot add developer context, so they return only normal continuation while the subsequent `SessionStart(source=compact)` is the context-restoration route.
- `PreToolUse` applies only explicit tool classification and dependency fields. It does not parse arbitrary shell text into semantic authority.
- `Stop` checks structure without requesting automatic continuation.

Host event availability, field shape, command trust, disabled/opted-out paths, and actual startup/resume/compact delivery require live evidence. The template alone proves none of them.

## Command examples

Pass file paths as separate process arguments. Do not compose variable-heavy inline PowerShell or Python source.

```text
python -B objective_ledger.py bootstrap --config config.json --session-id <root-session-id> --source-file <exact-source> --delivery-id <stable-delivery-id>
python -B objective_ledger.py status --config config.json --logical-chat-id <logical-chat-id>
python -B objective_ledger.py classify-source --config config.json --logical-chat-id <logical-chat-id> --input classification.json --event-id <id> --expected-head <hash>
python -B objective_ledger.py dispose-source --config config.json --logical-chat-id <logical-chat-id> --input - --event-id <id> --expected-head <hash>
python -B objective_ledger.py resolve-capture-gap --config config.json --logical-chat-id <logical-chat-id> --input - --event-id <id> --expected-head <hash>
python -B objective_ledger.py progress --config config.json --logical-chat-id <logical-chat-id> --input progress.json --event-id <id> --expected-head <hash>
python -B objective_ledger.py action-start --config config.json --logical-chat-id <logical-chat-id> --input action.json --event-id <id> --expected-head <hash>
python -B objective_ledger.py action-outcome --config config.json --logical-chat-id <logical-chat-id> --input outcome.json --event-id <id> --expected-head <hash>
python -B objective_ledger.py action-reconcile --config config.json --logical-chat-id <logical-chat-id> --input - --event-id <id> --expected-head <hash>
python -B objective_ledger.py verify --config config.json --logical-chat-id <logical-chat-id>
python -B objective_ledger.py recover-lock --config config.json --logical-chat-id <logical-chat-id> --expected-lock-sha256 <hash>
```

Every `--input` accepts a JSON file or `-` for explicit UTF-8 stdin. Stdout JSON is ASCII-escaped so non-CP932 characters remain lossless across a Windows console; exact source files remain UTF-8. Oversize records contain hashes, sizes and an explicit continuity-false state, not raw oversized input.

## Proof ceiling

Local execution can prove byte identity, source references, chain/replay/CAS behavior, partial-tail preservation, atomic projection replacement, and tested scope. It cannot prove semantic interpretation, complete hook interception, host identity truth, same-user security, model obedience, external action outcome, population benefit, or long-run efficiency.

## Owner view and incremental update caller

`ledger_input.py owner-view` uses the exact config, logical-chat ID and optional read-only expected head to return canonical own paths, inherited host routing status, every current normative field, active outcome, pending action/effect and source clauses with one dictionary. Other task objectives remain references, not cwd-owned copies. Full immutable history is unchanged and separately retrievable. This view does not replace mandatory policy/Skill/history reading.

The source-update fields object contains `source_event_id`, `contract_id`, `clause_id`, `disposition` (INITIAL/ADD/CLARIFY/CORRECT), optional `changes` (supported normative fields only), `new_outcomes`, explicit `outcome_transitions`, `locator` and `classification_note`. It derives source paths/hashes and prior clauses from this exact ledger, and validates through the existing runtime normalizer and whole replay. Foreign source IDs, stale heads and attempts to inject a copied contract are rejected before append. Existing outcome statuses/evidence are inherited; new_outcomes cannot rewrite an existing ID. Questions with unchanged primary text still retain their unanswered outcome. REPLACE/WITHDRAW and explicit non-user dispositions continue to use their existing full runtime commands.

```text
python -B scripts/ledger_input.py owner-view --runtime <exact-runtime> --runtime-sha256 <sha> --config <config> --logical-chat-id <host-bound-chat>
python -B scripts/ledger_input.py source-update --runtime <exact-runtime> --runtime-sha256 <sha> --config <config> --logical-chat-id <host-bound-chat> --expected-head <head> --fields <reviewed-delta.json> --apply --event-id <unique-id>
```

Without --apply the second command only prepares input. Progress/action-start/action-outcome also support validated --apply with an explicit observed head and unique event ID. See [execution-route.md](execution-route.md) for the connected native operation and response decision; no write automatically refreshes a stale head. Apply additionally requires an inherited host session binding to resolve through the existing config to the selected ledger (config `host_binding`: `session_env` and `bindings_dir`; see the host adapter notes); explicit reviewed session migrations remain supported. The conventional provisioning is one JSON file per session under `bindings_dir`: the filename is the lowercase SHA-256 hex of the host session ID plus `.json`, and the body carries `role` (`root` or `subagent`) and `session_id`; a relative `bindings_dir` resolves against the configuration file's location. The host integration writes these files; reference reads and preparation remain available without one. These environment values can be altered by a same-user process and may be shared by children, so they establish accidental-target checking only, not parent authentication or universal prevention. The primary-owner restriction and source authority remain binding. An absent/foreign/child binding cannot use this convenience apply route; reference reads, preparation and existing runtime recovery remain available.

After append, the helper compares actual current.json and the own generated card with the committed result. A failure after apply begins is reported uncertain and requires replay; do not infer executed:false or automatically retry. The receipt reports actual event/head and readback, not model understanding or fulfillment of the user's outcome. A Global write reminder summarizes standing scope/CAS/history/backup requirements and never grants new permission. Actual filesystem errors must be preserved rather than inferred or suppressed.
