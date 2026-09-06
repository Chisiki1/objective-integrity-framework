---
name: durable-supervisor-status
description: Recover an explicitly existing legacy split-task coordinator/executor lease through executor-local status, hash-chain history, and a later bound coordinator message. Do not use for the default same-conversation workflow or as external-action authority.
---

# Durable Supervisor Status

Use only for an explicitly existing legacy split-task lease whose observation cannot rely on a user-interface projection. New work uses `chat-objective-continuity` in one conversation.

1. Read [contract.md](references/contract.md). Keep workflow authority and action gates unchanged.
2. Shape bounded projection evidence with [projection-evidence-schema.json](references/projection-evidence-schema.json), then run `python -B scripts/durable_status.py select --evidence <evidence.json>`. `SELECT` is a mechanical fallback choice, not app-state or action authority.
3. At a selected decision-bearing event, prepare a v2 write request under [status-schema.json](references/status-schema.json). Record stage, action or wait owner, last evidence delta, next decision condition, held transition, expected value, metric availability, decision window, wait value, and `no_push=true`. Run `preflight` and then `write`; both validate the complete request. Use [legacy-status-schema-v1.json](references/legacy-status-schema-v1.json) only to read an existing v1 lease.
4. Write `READY` or `WORK_NEEDS_ATTENTION`, then stop locally. At a decision window, the coordinator reads the exact durable status once and chooses `WAIT`, `CORRECT`, `REPLACE`, or `USER_DECISION` without narration polling or duplicate review.
5. Resume the same dependent lease only when a later host-delivered message names the previous status ID, record hash, and transition. Preserve message identifiers and prompt digest without copying prompt text into status.
6. A self-authored status, journal, message envelope, skill, registry receipt, or claimed role is never coordinator authority. A mismatch holds only the named dependent transition.
7. Use `read` for the validated tail and projection; use `verify` for read-only consistency. Neither repairs. A later eligible write may rebuild a missing projection from a valid journal before append.
8. Invoke the canonical script directly with `python -B`. After the final execution in a transition, re-read the exact resolver member set and hashes and do not execute it again in that transition.

The tool writes only below the explicitly supplied executor-local root, recommended `.oif-supervision-status`. It does not inspect, execute, authorize, or prevent product or external actions. Static validity proves only local record identity and chain structure.
