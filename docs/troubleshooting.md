# Troubleshooting

Start with the first decision-bearing error and keep later symptoms separate. Correct only the failed route; preserve independent read-only work and return to the objective as soon as the minimum unblock is complete.

## Demo Directory Is Not Empty

The demonstration expects an explicit new or empty directory outside the distribution checkout. Choose a clean sibling path:

```bash
python tools/demo.py --directory ../oif-demo
```

Do not point the demo at a real project or reuse generated demo state as authority for real work.

## Bootstrap Rejects the Destination

The distribution checkout, its parent, and its descendants are not adoption destinations. This separation keeps source, generated state, and installed state from contaminating one another. Choose a separate sibling directory and preview again:

```bash
python tools/bootstrap.py --destination ../sample-project --adapter generic --mode complete
```

## Preview Looked Right but Nothing Was Written

That is the default behavior. Bootstrap writes only with `--apply`. Review the complete plan first, then repeat the same destination, adapter, and mode with `--apply`.

## Rollback Reports a Conflict

A destination file changed after installation. The current rollback preserves that later content and reports the conflict instead of deleting user-owned work. Compare the file with the matching backup manifest and choose an explicit merge or restoration. Do not rerun rollback as a blind retry.

A legacy backup without the current hash-bound manifest is not restored automatically. Review its inventory and contents manually.

## Windows Nested Path Errors

Use a short checkout path and a short, separate disposable parent for demonstrations and runtime checks. Hash-addressed source, rejection, capture-gap, and recovery records need room for atomic temporary filenames. If a deeply nested sandbox produces a file-creation error, preserve its first result and retry in a fresh short sandbox. Keep the source distribution and live configuration untouched; no Windows path-policy change is required.

## Objective Ledger Reports a Stale Head

Another valid event changed the ledger after the caller read it. Re-read `objective.txt` and `current.json`, preserve the rejected request as non-applied evidence, rebuild the intended transition against the new head, and retry only if the transition still advances the current source-bound outcome.

## Source Integrity Cannot Be Reopened

The exact source file is missing or its bytes changed. Restore the recorded source identity or resolve the capture gap through an explicit owner event. Hold only mutations whose meaning depends on that source; read-only status and independent evidence collection can continue.

## An Earlier Action Has Unknown Effect

Do not treat a retry as proof that the earlier action did nothing. Inspect the actual consumer or recovery boundary, then record `action-reconcile` with the new evidence. Retry only when the reconciliation or bounded recovery plan makes duplicate effects safe.

## Resolver Returns Near Match, Conflict, or Fallback

- A near match lacks at least one exact compiled fact; inspect the mismatch instead of selecting it by similarity.
- A conflict needs explicit precedence or user decision; registry order is not precedence.
- A stale snapshot requires recompilation and resolution against the current source, payload, registry, paths, and members.
- A fallback continues the normal workflow for unrelated work; it does not bypass an independently applicable exact-action constraint.

## PowerShell Preflight Returns Error

`ERROR` means parsing or preflight failed. It is neither a pass nor evidence that the target command executed. Preserve the exact input and parser evidence, then use a constrained representation or hold only that PowerShell member. `BLOCK` is distinct: it means a registered mechanical family rejected the candidate before submission.

## A Structural Check Passes but the Outcome Is Still Open

That is expected when the claim crosses a behavioral, runtime, or external-consumer boundary. Use the evidence map to identify the final consumer oracle and the smallest route that can observe it. Do not repeat unchanged structural checks when they cannot change the decision.
