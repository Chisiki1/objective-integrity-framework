# Privacy

Objective Integrity Framework is designed for public, project-local adoption.

## Package Data Practices

OIF operates no hosted service, account system or automatic telemetry endpoint.
The skills-only plugin contains instructions and static resources; it does not
send task content to OIF maintainers or connect an external account. The host
processes conversation content under its own settings and policies, and its
tools retain their normal permissions.

Optional runtime tools write task source, objective history, operation streams,
learning records and recovery material only through the explicitly configured or
supplied paths. Those records may contain information from the user's task. Keep
them outside installed plugin/Skill files and public repositories. Capture only
what the task needs; never supply credentials, unrelated chat histories or
sensitive personal records for examples or diagnostics.

Local records remain until their owner deliberately removes them; OIF has no
remote copy or automatic retention service. The owner controls access, location,
export and deletion using their environment's controls. Preserve needed recovery
and unresolved effects before deleting working history. Uninstalling a plugin
does not imply deletion of separately owned task records. Sending a reproduction
to the public issue tracker is a separate voluntary disclosure: sanitize it first.

## What This Repository Excludes

- Private operational logs.
- Personal machine paths.
- Private usernames and account identifiers; credentials, tokens, cookies, and session material.
- Private repository names, private task identifiers, raw memory files, and private incident records.
- Product-specific financial, deployment, or runtime details from any private project.

Public contributor or sponsor credits use only information explicitly supplied for publication. They do not disclose private operational provenance or imply additional identities or relationships.

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
