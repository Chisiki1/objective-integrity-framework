# Skill Book Minimal Example

This example runs the resolver against one tiny project-local skill and one rejected candidate.

```bash
python tools/skill_resolver.py --registry examples/skill-book-minimal/registry.json --input examples/skill-book-minimal/input.json --root project=examples/skill-book-minimal/skills
```

Expected result:

- `parser-preflight` is selected because the current work facts match a mechanical command-preflight job.
- `deployment-release` is rejected because the current work facts do not include deployment.
- `fallback_normal_workflow` is `false` because one bounded helper matched.

The selected helper does not prove the product is correct. It only proves the resolver produced a deterministic structural receipt for this snapshot.
