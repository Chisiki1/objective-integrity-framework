# Security Policy

Please report security or privacy issues through a private channel provided by the repository maintainers.

## Scope

Security issues include secret exposure, unsafe installer behavior, privacy leaks, unsafe defaults, and documentation that could cause users to modify live agent configuration without an explicit action.

## Safe Defaults

The included bootstrap tool defaults to dry-run, requires an explicit destination, detects existing files, and creates backups before replacement when `--apply` is used. It does not write to global agent configuration unless an explicit destination and global-write acknowledgement are both supplied.
