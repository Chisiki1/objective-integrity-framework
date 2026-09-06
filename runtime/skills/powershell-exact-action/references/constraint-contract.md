# Constraint contract

Each constraint has a stable ID and repeat family, a typed applicability predicate, an exact discriminator over the finalized representation, safe and unsafe examples, a blocked-member result, and a proof ceiling.

Current constraints:

- `PS-FOREACH-PIPE-001` / `POWERSHELL::FOREACH-PIPE-PARSER`: applies to every PowerShell member. It blocks only when token structure contains a `foreach` statement whose closing block brace is followed directly by a pipeline token.
- `PS-LITERAL-DOLLAR-001` / `POWERSHELL::LITERAL-DOLLAR-ARG-EXPANSION`: applies only when the member explicitly declares `literal_dollar_required=true`. It blocks double-quoted command text that contains the required literal token in an interpolation-capable context; caller-provided `literal_tokens` bind the expected strings.

The script records the exact input SHA-256 and per-member applicable constraint set. A post-execution rejection is containment, not prevention. `PASS` never means the PowerShell is correct, safe, authorized, or semantically equivalent.
