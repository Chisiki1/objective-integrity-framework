---
name: powershell-exact-action
description: Screen an exact finalized PowerShell command or batch member before execution for confirmed mechanical recurrence families, currently the invalid direct pipeline from a foreach statement and unsafe literal-dollar interpolation in metadata arguments. Use for Windows PowerShell actions after wrappers, defaults, JSON construction, member expansion, and quoting are final. It is a mechanical preflight, not semantic safety or runtime outcome proof.
---

# PowerShell Exact-Action Preflight

Use this skill only on the exact finalized PowerShell representation that would otherwise be submitted.

1. For one command, run `powershell -NoProfile -File scripts/Test-PowerShellExactAction.ps1 -CommandText '<exact text>' -IncludeSource` using a transport that preserves the literal command. For a batch, supply `-InputJsonPath` containing `{ "members": [{"id":"...","command":"..."}] }` and screen every independently executable member.
2. Exit `2` and `decision=BLOCK` means the matching candidate was not eligible for submission. Preserve its SHA-256 and source in the preflight evidence, change the representation, and screen the changed exact action once.
3. Exit `0` means only that the currently registered mechanical families did not block the input. Continue all workflow, authority, scope, semantic, and evidence gates.
4. Exit `1` or `decision=ERROR` means the preflight route itself failed. Preserve it and use a constrained representation or block only the dependent PowerShell member.

The known foreach family is the parser-invalid form `foreach (...) { ... } | Command`. Use an intermediate array or `ForEach-Object` when semantics are preserved. Strings, comments, here-strings, subexpressions, and a later separate pipeline must not be falsely classified as this family.

The literal-dollar family applies when a command argument must contain a literal `$skill-name` or similar token. Use a non-expanding literal transport or exact post-generation patch, then read back the consumer file. Do not infer the caller's intention from a dollar sign alone; the input JSON must mark `literal_dollar_required=true` for this constraint to apply.

Read [constraint-contract.md](references/constraint-contract.md) when adding a confirmed mechanical family. A new family requires an exact recurrence discriminator, safe counterexamples, member-level applicability, bounded proof ceiling, and master lineage.
