# Evaluation

The included fixtures are small representative cases for testing whether an adoption preserves important mechanisms.

Run:

```bash
python tools/eval_runner.py .
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

These fixtures do not prove empirical superiority. They provide a starting set for measuring whether a team can detect and handle known workflow failure families.

Useful operational metrics include avoided rework, missed-defect rate, false holds, elapsed time, token and tool cost, review overhead, rollback success, and final consumer outcome.
