# Evaluation

The included fixtures are small representative cases for testing whether an adoption preserves important mechanisms.

Run the coordinated static checks:

```bash
python tools/check.py
```

Runtime partitions are opt-in and require an existing disposable, non-redirected parent outside the distribution and live configuration. The checker creates and reports a unique tool-owned child beneath it:

```bash
mkdir ../oif-check-work
python tools/check.py --runtime --temp-root ../oif-check-work
```

For a complete installed copy, use the nonrecursive consumer route:

```bash
mkdir ../oif-installed-check-work
python ../sample-project/.oif/tools/check.py --installed-runtime --temp-root ../oif-installed-check-work
```

Installed-runtime mode exercises the evaluation fixtures and runtime regressions shipped in `.oif` while leaving development-only public-consumer and installer partitions out of that installed check. A setup rejection is evidence about the check route, not a product failure.

For a human walkthrough of the primary path, use a separate new or empty sandbox:

```bash
python tools/demo.py --directory ../oif-demo
```

Fixtures cover:

- Objective drift.
- Test-fix loops.
- Symptom patching.
- Unnecessary stop guards.
- Lost semantic recomposition.
- Missing impact analysis.
- Audit-result non-consumption.
- Repeated failure without new evidence.
- Inefficient delegation.
- Misselected reusable skills.
- Stale resolver snapshots and retargeted paths.
- Blind contamination from skill metadata.
- Mechanical preflight overclaiming semantic proof.
- Parallel skill registry writer conflict.

These fixtures do not prove empirical superiority. They provide a starting set for measuring whether a team can detect and handle known workflow failure families.

The runtime suite should exercise the eight shipped generic capability families and their relations: objective continuity; fact compilation and resolution; selected-skill application; learning queue, index, lifecycle, allocation and condition governance; transition/scenario/objective control; native PowerShell preflight; legacy recovery compatibility; and project-local adoption. Keep each partition's first fault, skipped scope, and consumer oracle distinct.

Useful operational metrics include avoided rework, missed-defect rate, false holds, elapsed time, token and tool cost, review overhead, rollback success, and final consumer outcome.
