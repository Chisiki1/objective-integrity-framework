# Whole-Scope Work

Keep the requested finish line fixed while making the work easier to complete.

A long task can look busy without converging: implement one small part, run formal checks, patch one finding, repeat, and leave the connections between parts unfinished. Whole-scope execution changes the unit of completion. Internal jobs may stay small; the owner's requested outcome and formal verification scope stay whole.

## One Scope, Several Useful Jobs

Start with the exact user source. Map every required outcome to implementation items, including its real inputs, consumer connections, retained state and recovery when applicable. Map each requirement to the checks that can observe it. Keep an immutable completion-scope file and one current state reference under the owner's control.

The objective ledger points to those references and names remaining work. It does not copy the entire matrix into every progress entry. A child receives a bounded write scope but cannot replace the parent's completion scope or turn its own success into the whole result.

## The Execution Cycle

| Phase | Do next | What it prevents |
|---|---|---|
| BUILD | Implement all requested parts and required consumer connections. | A passing component becoming the new finish line. |
| SWEEP | Freeze the whole candidate and collect safe current-stage findings. | Editing after the first failure before the rest of the affected scope is understood. |
| REPAIR | Group mandatory findings by shared cause and repair all affected consumers. | Repeated symptom patches and duplicate checking of unchanged evidence. |
| ACCEPT | Close all required implementation, findings and evidence against the original outcome. | File counts, test flags or a partial result being called completion. |

Cheap parse, type, compile or isolated semantic feedback is available during BUILD when it is necessary to choose the next implementation. State the decision it informs, its bounded effects and the return step. It is construction feedback, not a miniature formal acceptance cycle. An essential design failure, accepted-product regression, uncontrolled effect or external action still follows its own diagnosis and authority route.

During SWEEP, a failed dependent check retains its first fault and leaves unobserved scope explicit. Other safe partitions continue. Evidence stages reflect what can actually be observed; later operational evidence remains required without blocking repair of already collected current-stage findings. Optional enhancements do not silently become mandatory findings.

## Connected Runtime Paths

Run the synthetic walkthrough first:

```bash
python tools/demo.py --directory ../oif-demo
```

The advanced entrypoints use the same binding rather than separate completion truths:

```bash
python tools/oif.py work-phase --help
python tools/oif.py work --help
python tools/oif.py control --help
python tools/oif.py transition --help
```

- `work_phase.py` reads the exact scope/state and selects eligible next actions.
- `work_io.py` v2 checks that phase when preparing or verifying a bounded request, and retains the real result even if the source or phase later changes.
- Owner control v2 uses the same decision for `ACT`; `CORRECT` requires grouped repair.
- Transition admission v3 checks the phase before a lightweight route and retains independent action-authority requirements.

The [data contract](../runtime/skills/master-guided-skill-lifecycle/references/source-wide-execution.md) defines each field, status, check, finding and evidence reference. The [work entrypoint](../runtime/skills/master-guided-skill-lifecycle/references/work-entrypoint.md) explains prepare, verify, actual host dispatch, consume and destination readback. Prepared payloads and file hashes support those relations; the owner and independent reviewer still judge whether the delivered result satisfies the source.

## Example: A Migration

For a migration that must preserve behavior, a useful scope includes input handling, shared state, the real consumer, failure recovery and the agreed normal-operation outcome. Separate implementers can build those parts concurrently. Their artifacts enter the one integrated candidate before formal whole-candidate verification. Findings about the same state owner are repaired across every affected reader and writer together.

The scope does not expand to every conceivable enhancement, and the task does not end at a checklist or package build when the user asked for normal operation. The same principle applies to other implementation work. Standalone research, diagnosis and short documentation tasks keep their own appropriate direct routes rather than being forced through product phases.

## Learn From the Next Result

If a control keeps adding cost without improving integration, compare keeping it, a smaller repair, a different work unit or entrypoint, and removal or consolidation. Carry a worthwhile within-authority change into an actual next action. Measure the consumer result, quality, elapsed/integration cost, rework and false holds where available. A smaller check count alone is not success; neither is a larger policy.
