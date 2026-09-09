---
name: artifact-access
description: Locate and read exact files across generated or versioned artifact roots, with stale-input detection and bounded output. Use when guessed paths, partial overlays, or oversized evidence repeatedly interrupt work; simple known-file reads can use ordinary tools.
---

# Artifact access

Use the existing manifest first. When its paths are already exact and sufficient,
do not create another inventory. For a generated tree without a usable manifest,
`scripts/artifact_access.py inventory --root <explicit-task-root> --output <new-json>`
creates a disposable path/hash view. It reads only the chosen tree, never searches
the home directory for a similar version and never modifies the source.

Read a member with:

```text
python -B <skill>/scripts/artifact_access.py read --inventory <json> --path <exact/relative/file> --start 1 --lines 80
```

For an existing file manifest, pass `--manifest <json> --root <explicit-root>`
instead of `--inventory`. Supported manifests contain `files` or `members` rows
with relative `path` and `sha256`. The explicit root determines their meaning;
an installation's `live` path is not silently substituted for a candidate path.

The result includes exact source identity, selected lines, `next_line` and whether
that view covers the whole file. Follow `next_line` to EOF when a required Skill,
policy or source must be read completely. A bounded preview is not a full read.
Missing, duplicate, linked or changed members remain explicit errors; do not
silently fall back from an overlay to another release. Choose the actual baseline
or overlay from its existing manifest when both are required.

Use `diff --left <file> --right <file> --lines 120` for a bounded ordinary unified
diff with both full-file hashes. The original files remain the evidence; a
truncated diff cannot certify complete change coverage.

For a large single-line JSON artifact, use `json --file <exact-file> --pointer
<JSON-pointer> --keys` to list one object's keys, or omit `--keys` for its bounded
value. An empty pointer selects the root. Oversized selections fail explicitly;
choose a narrower pointer rather than printing the entire file. This selected
projection never counts as reading a mandatory full source.

For repeated material operations whose first fault/next use matters, the existing
local `master-guided-skill-lifecycle` operation runner can retain this script's
actual exit status and full stdout/stderr. This Skill also runs independently;
it does not require a workflow registry, create learning authority, or certify
that the inspected product works. Its exact input and result—not a read count—
are the useful learning evidence.
