# Adoption Guide

Start with the lightest adoption mode that protects the work you actually do.

## Read-Only Adoption

Read `framework/core-invariants.md` and use the templates as thinking aids. This mode has no installer and no configuration changes.

## Project-Local Adoption

Copy templates into a project-owned directory such as `docs/objective-integrity/` or `.agent-workflow/`. Keep them under normal version control if they describe project behavior.

Recommended first files:

- `templates/objective-contract.md`
- `templates/open-deliverable-ledger.md`
- `templates/evidence-map.md`
- `templates/audit-receipt.md`

## Skill Adoption

Use `.agents/skills/objective-integrity/SKILL.md` when your agent runtime supports project-scoped skills. The skill is intentionally concise and routes deeper procedures through references.

## Bootstrap Tool

The bootstrap tool is non-polluting by default:

- It performs a dry-run unless `--apply` is present.
- It requires an explicit destination.
- It refuses global-looking destinations unless `--allow-global` is also present.
- It backs up replaced files before writing.
- It supports rollback from the generated backup directory.

## Uninstall and Rollback

For manual adoption, remove the copied templates or adapter files from the project.

For bootstrap adoption, keep the backup path printed by the tool and run:

```bash
python tools/bootstrap.py --rollback <backup-directory>
```

Rollback restores backed-up files and removes files that were newly created by the bootstrap action.
