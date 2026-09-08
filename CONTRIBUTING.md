# Contributing

Thank you for improving Objective Integrity Framework.

## Contribution Principles

- Preserve the primary objective of each change.
- Keep examples synthetic and public-safe.
- Prefer clearer adoption paths over longer rule lists.
- Add mechanisms only when they address a reusable failure mode.
- Keep generic behavior in the core and host-specific behavior in optional adapters.
- Preserve same-conversation ownership and read-only independent review as the default topology.
- Separate structural, behavioral, runtime, and external-consumer claims.

## Development

Run the checks before opening a pull request:

```bash
python tools/privacy_scan.py .
python tools/no_drop_check.py .
python tools/validate_schemas.py
python tools/eval_runner.py .
python -m unittest discover -s tests
```

## Pull Request Checklist

- The change has a source-bound objective and acceptance criteria.
- New terminology is introduced in `docs/terminology.md`.
- New records have schemas or explain why a schema is not useful.
- Every affected capability row in `docs/coverage-map.md` points to the public artifact that carries it.
- Privacy-sensitive examples use neutral synthetic data.
- Any new claim about effectiveness includes evidence or is framed as design intent.
- Installation and generated-output changes preserve explicit destinations, preview, backup, conflict-aware rollback, and distribution/adoption separation.
- User-condition changes retain exact source and old/new/rollback lineage; delegated-method changes preserve source meaning and the normal path.
