# Durable Supervisor status contract v2

## Purpose and authority

This protocol restores a pullable Worker-local observation and correction surface when a host task-body projection is empty, contradictory, or stale. It is not an external-action gate. Existing workflow policy remains the authority for objectives, action eligibility, external writes, releases, deployment, and completion.

The Worker owns one status root at `<worker-root>/.oif-supervision-status`. `journal/event-<sequence>-<status-id>.json` is the append-only authority. `status.json` is an atomic full-record projection of the journal tail. The lock is an OS-released serialization mechanism, not authority.

## Decision-bearing writes

Write only when objective evidence, active step, pending decision, proposed transition, effect boundary, open-deliverable state, or a later Supervisor-message binding materially changes. Each full status contains:

- objective ID, source/hash, and bound Supervisor task when known;
- Worker task, turn, and same-lease identity when available;
- monotonic sequence and previous record hash;
- candidate and evidence identities;
- active step and bounded, caller-redacted actual evidence delta;
- pending decision and exact next transition/effect;
- allowed and prohibited effects;
- no-drop deliverable lineage;
- `CONTINUE`, `READY`, or `WORK_NEEDS_ATTENTION`;
- timestamp, transition gate, optional later-message binding, and proof ceiling.
- stage, action/wait owner, last decision delta, next decision condition, held transition, expected value, elapsed/metric availability, Decision Window/Wait Value, and `no_push=true`.

The read-only `select` command accepts bounded runtime/action projection evidence and returns `SELECT` only when an empty, contradictory, or stale projection intersects a decision-bearing boundary. Caller evidence remains untrusted input; selection does not prove app truth, semantic need, or action authority. Existing v1 records and requests remain supported for lineage preservation; new callers use v2.

The tool appends a canonical full record to the journal, fsyncs it, then atomically replaces `status.json`. If a crash leaves a valid journal event ahead of the current projection, the next `write` reconstructs current from the journal before applying the caller's exact status CAS. `verify` never repairs.

The supported CLI is direct and bytecode-disabled: `python -B scripts/durable_status.py <command> ...`. `preflight` resolves paths with `create=False`, loads the request, reads the journal/current projection without repair, and performs the complete request, lineage, CAS, sequence/previous-hash, message-binding, and transition validation without creating, locking, repairing, or writing. `read` returns the validated journal tail and projection state without repair; `verify` returns the compact consistency summary without repair.

Before creating the status root, journal, or lock, `write` performs the same read-only preflight. A request rejected in this admission phase leaves no status artifact. Only an eligible request may create/acquire the local lock. Under that lock the tool re-reads the journal and repeats the complete validation before repairing current or writing any record; the locked result, not the preflight record, is committed. If another eligible writer wins between preflight and lock acquisition, the losing request writes no status, current projection, or journal event, although the already-created empty status/journal directories and lock may remain. The tool does not delete them because it cannot prove exclusive ownership against the winning writer.

Each JSON input or record is limited to 1 MiB, effect lists to 128 entries, open deliverables to 256 entries, and the append-only journal to 4,096 full records. Reaching the journal limit rejects the next write without deleting or rotating history; a later retention/migration action requires separate authority and design.

## Supervisor correction binding

`READY` and `WORK_NEEDS_ATTENTION` always name one pending decision and held transition. The Worker then ends its turn locally; it never writes to the Supervisor conversation. The Supervisor pulls by known task ID and durable path.

A later `send_message_to_thread` prompt is the source-authorized correction or transition request. For the dependent resume, the current Worker turn supplies the exact host-delivered prompt facts to the tool. The tool requires:

- bound Supervisor source task;
- exact prior status ID and record hash;
- exact held transition ID;
- exact destination Worker task and current later turn;
- the same Worker lease;
- a distinct later turn from the prior status.

The next status stores only message ID, source task, destination turn, prompt SHA-256, exact prior status binding, transition ID, and received timestamp. The message text is not persisted.

An input JSON file is not authority and may be forged. The Worker must derive message facts from the actual current host-delivered prompt/turn. The tool validates consistency but cannot prove host provenance. No ACK file is created or consumed.

Without an exact later binding, `DEPENDENT_TRANSITION` is rejected without a new record. `STATUS_ONLY` or `READ_ONLY_UPDATE` may append evidence while held, but must preserve the pending decision, transition, and effect authority. Thus the hold is transition-scoped, not a blanket stop.

## Chain and recovery invariants

- Sequence starts at one and increments exactly once.
- Every event's `previous_record_sha256` equals the prior record body hash.
- Canonical UTF-8 journal bytes, filename identity, record hash, and current projection must agree.
- A missing/corrupt current projection may be reconstructed only from a valid journal tail.
- A current projection ahead of or conflicting with the journal is not authority and blocks recovery.
- Concurrent writers serialize on one Worker-root lock and must CAS the same latest status ID/hash.
- Admission-invalid requests create no status infrastructure; a race-lost request after successful admission may leave only empty infrastructure and never deletes another writer's artifacts.
- Existing deliverable IDs cannot disappear; a satisfied deliverable cannot revert.
- Objective, Worker task, and established Worker lease cannot change within one chain.
- The tool never deletes journal events or the superseded v4 lineage.

Filesystem same-user tampering, junction/reparse behavior beyond resolved-root containment, host message provenance, app task projection, and backup durability remain environmental proof limits.

## Installed-skill identity boundary

Supported workflow, audit, and test paths execute the canonical script directly with `python -B`. They do not import the installed script as a module and do not run `py_compile` or `compileall` against the installed skill. After the last canonical execution in a material transition, the caller re-runs the exact registered resolver/file-set/hash check before consuming current identity and performs no later canonical skill execution in that transition. This rule addresses supported paths only. It does not claim that `-B` prevents explicit compilation or that the skill resists arbitrary unsupported same-user imports.

## Proof ceiling

Schema, source, hashes, and static syntax can establish only a staged local record format and intended chain transitions. They do not establish active installation, app visibility, Supervisor prompt authenticity, normal-path behavior, crash recovery in practice, action interception/prevention, external safety, or empirical improvement.
