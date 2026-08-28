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

## Adoption Safety

Cloning, reading, testing, or running validators from this repository should not modify a user's live agent configuration. The bootstrap tool defaults to dry-run, requires an explicit destination, and creates backups before replacement when `--apply` is used.

Before publishing downstream changes based on this framework, run:

```bash
python tools/privacy_scan.py .
```
