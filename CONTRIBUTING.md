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

Use Python 3.10+ with standard-library `sqlite3` and Git. No third-party Python
packages are required. Run the static checks before opening a pull request:

```bash
python -B tools/check.py
```

For runtime changes, run the complete regression route with an **existing**
temporary directory outside the checkout. Use a short writable path on Windows:

```bash
python -B tools/check.py --runtime --temp-root <existing-separate-temp-directory>
```

The same commands run in CI on Windows/Linux and Python 3.10/latest. A focused
test is useful while building; disclose any omitted full checks or host-dependent
skips in the PR. Preserve the first failure and fix shared causes together.

Use [issue templates](https://github.com/Chisiki1/objective-integrity-framework/issues/new/choose)
for bugs or questions, [support](docs/support.md) for reporting context and
[SECURITY.md](SECURITY.md) for sensitive issues. All participation follows the
[code of conduct](CODE_OF_CONDUCT.md). Contributions are provided under the
project's [Apache-2.0 license](LICENSE); retain applicable attribution notices.

For a public-facing change, update [CHANGELOG.md](CHANGELOG.md). Release downloads
and local asset reproduction are documented in [releases](docs/releases.md).

## Pull Request Checklist

- The change has a source-bound objective and acceptance criteria.
- New terminology is introduced in `docs/terminology.md`.
- New records have schemas or explain why a schema is not useful.
- Every affected capability row in `docs/coverage-map.md` points to the public artifact that carries it.
- Privacy-sensitive examples use neutral synthetic data.
- Any new claim about effectiveness includes evidence or is framed as design intent.
- Installation and generated-output changes preserve explicit destinations, preview, backup, conflict-aware rollback, and distribution/adoption separation.
- User-condition changes retain exact source and old/new/rollback lineage; delegated-method changes preserve source meaning and the normal path.
