# PowerShell Adapter

This optional adapter supplies native PowerShell parser and token evidence for exact-action preflight. The portable core does not require PowerShell.

The implementation lives under `runtime/skills/powershell-exact-action/`. It accepts one exact command or an input file containing independently executable batch members, preserves the source hash, and returns a member-level `PASS`, `BLOCK`, or `ERROR` result.

Use native preflight after command wrapping, defaults, interpolation, JSON construction, and batch expansion are final. A changed command is a new candidate and must be screened in its final form.

The adapter's evidence boundary is mechanical:

- `PASS` means the registered PowerShell action families did not block that exact representation.
- `BLOCK` means the preserved candidate matched a registered mechanical constraint before submission.
- `ERROR` means parsing or preflight itself failed; it is not a pass or target execution.

Continue to apply the framework's normal source, authority, semantic, recovery, and consumer-evidence rules. Use `tools/exact_action_check.py` when only a portable conservative screen is available, and report its narrower evidence accordingly.
