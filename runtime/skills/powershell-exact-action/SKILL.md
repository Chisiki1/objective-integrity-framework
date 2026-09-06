---
name: powershell-exact-action
description: Screen an exact finalized PowerShell command or batch member for registered mechanical recurrence families after wrapping, defaults, JSON construction, member expansion, and quoting are final. Use only for PowerShell actions; it is mechanical preflight, not semantic or runtime proof.
---

# PowerShell Exact-Action Preflight

Use this skill only on the exact finalized PowerShell representation that would otherwise be submitted.

1. For one command, run `powershell -NoProfile -File scripts/Test-PowerShellExactAction.ps1 -CommandText '<exact text>' -IncludeSource` through a transport that preserves literal content. For a batch, provide `-InputJsonPath` with every independently executable member.
2. Exit `2` with `decision=BLOCK` means the preserved candidate is not eligible for submission. Change the representation and screen the changed exact action once.
3. Exit `0` means only that registered mechanical families did not block that exact input. Continue every source, authority, semantic, recovery, and consumer-evidence gate.
4. Exit `1` or `decision=ERROR` means preflight failed. Preserve the error and use a constrained representation or hold only the dependent member.

The direct-pipeline family covers parser-invalid `foreach (...) { ... } | Command`. An intermediate array or `ForEach-Object` can preserve intended semantics. Strings, comments, here-strings, subexpressions, and later separate pipelines are counterexamples and must not false-block.

The literal-dollar family applies only when an argument explicitly requires a literal `$name` token. Mark that requirement in the input, use a non-expanding transport, and read back the consumer file.

Read [constraint-contract.md](references/constraint-contract.md) when adding a mechanical family. Require an exact recurrence discriminator, safe counterexamples, member-level applicability, bounded proof ceiling, and source lineage.
