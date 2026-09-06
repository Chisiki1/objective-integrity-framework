# Privacy

Objective Integrity Framework is designed for public, project-local adoption.

## What This Repository Excludes

- Private operational logs.
- Personal machine paths.
- Usernames, account identifiers, credentials, tokens, cookies, and session material.
- Private repository names, private task identifiers, raw memory files, and private incident records.
- Product-specific financial, deployment, or runtime details from any private project.

## Safe Examples

Examples in this repository use neutral synthetic scenarios. They are intended to explain mechanisms, not to disclose private provenance.

Public configuration files contain placeholders or repository-relative paths. The repository does not ship live ledgers, source captures, learning databases, registry state, indexes, action receipts, caches, or generated runtime output.

## Adoption Safety

Cloning, reading, or running repository checks does not install agent configuration. The demonstration requires an explicit new or empty sandbox outside the distribution tree. Bootstrap defaults to dry-run, requires an explicit separate destination, rejects source/ancestor/descendant overlap, and creates hash-bound recovery material before replacement when `--apply` is used.

Complete mode installs under `<destination>/.oif`. The selected adapter controls only the destination's top-level guidance. Rollback detects files changed after installation and leaves them for explicit review instead of deleting later user-owned work.

Before publishing downstream changes based on this framework, run:

```bash
python tools/privacy_scan.py .
```

Also inspect generated public examples, archives, Git history, and configuration output before publication. A clean tracked-tree scan does not automatically cover those separate surfaces.
