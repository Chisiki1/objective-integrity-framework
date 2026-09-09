# Security Policy

## Reporting a vulnerability

Use **Report a vulnerability** on the repository's
[Security Advisories page](https://github.com/Chisiki1/objective-integrity-framework/security/advisories).
If that option is unavailable, open a minimal
[contact request](https://github.com/Chisiki1/objective-integrity-framework/issues/new)
asking for a private reporting route. Do **not** include the vulnerability,
exploit details, secrets or private task data in that public request. Wait for a
private channel before sending sensitive material.

Include the affected version/commit, a synthetic reproduction, impact, first
error and any observed file changes. Preserve original evidence locally and
redact account data, prompts, ledgers and credentials. General bugs and usage
questions belong in the [public issue tracker](https://github.com/Chisiki1/objective-integrity-framework/issues).

Maintainers prioritize the latest release and coordinate fixes and disclosure
with the reporter. Please allow time to investigate; this community project does
not promise a fixed response deadline or operate a bounty program.

## Scope

Security issues include secret exposure, unsafe installer behavior, privacy leaks, unsafe defaults, and documentation that could cause users to modify live agent configuration without an explicit action.

## Safe Defaults

The included bootstrap tool defaults to dry-run, requires an explicit destination, detects existing files, and creates backups before replacement when `--apply` is used. It does not write to global agent configuration unless an explicit destination and global-write acknowledgement are both supplied.

Release and plugin builders use explicit separate destinations, verify their
inputs, retain partial failures and do not publish or install anything. Verify
downloaded bytes using the matching release's checksums before running tools.
