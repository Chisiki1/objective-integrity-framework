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

Use the project skill only when your runtime supports project-scoped skills. In this repository, `.agents/skills/objective-integrity/SKILL.md` is one portable packaging format: it is intentionally concise and routes deeper procedures through references. The optional Codex adapter also uses that shape, but the Skill Book concept is generic.

For teams with multiple reusable skills, add the [Skill Book Plane](skill-book.md) gradually. Start with a read-only registry and selection receipt. Add lifecycle and exact-action preflight records only for skills or mechanical constraints that repeatedly affect outcomes.

Do not install skills into a global agent directory by cloning this repository. Copy project-local files deliberately, keep backups before replacement, and prefer dry-run tooling until your destination is explicit.

## Skill Book in 5 Minutes

1. Identify one repeated workflow decision where reusable guidance changes correctness, recurrence prevention, privacy, or external-action safety.
2. Write a small registry entry with exact work facts, status, expected delta, rollback, and proof ceiling.
3. Run or manually fill a [Skill Selection Receipt](../templates/skill-selection-receipt.md) that records selected and rejected candidates.
4. If the skill is used, record the effect with [Skill Lifecycle Record](../templates/skill-lifecycle-record.md).
5. If the decision is only mechanical, use [Exact-Action Preflight](../templates/exact-action-preflight.md) and do not report semantic safety from a mechanical pass.

Skip this layer for small work where direct use of the core invariants is cheaper and no reusable skill or exact-action constraint would materially change the outcome.

A minimal runnable example is available in [Skill Book Minimal](../examples/skill-book-minimal/README.md).

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
