# Contributing

Thank you for improving Objective Integrity Framework.

## Contribution Principles

- Preserve the primary objective of each change.
- Keep examples synthetic and public-safe.
- Prefer clearer adoption paths over longer rule lists.
- Add mechanisms only when they address a reusable failure mode.
- Separate structural design claims from empirical outcome claims.

## Development

Run the checks before opening a pull request:

```bash
python tools/privacy_scan.py .
python tools/no_drop_check.py .
python tools/validate_schemas.py .
python tools/eval_runner.py .
python -m unittest discover -s tests
```

## Pull Request Checklist

- The change has a source-bound objective and acceptance criteria.
- New terminology is introduced in `docs/terminology.md`.
- New records have schemas or explain why a schema is not useful.
- Privacy-sensitive examples use neutral synthetic data.
- Any new claim about effectiveness includes evidence or is framed as design intent.
