# Example: Bug Repair

Scenario: A project command fails after a configuration migration.

Apply the framework:

1. Preserve raw output, command, environment, and changed files.
2. Separate first fault from propagated errors.
3. Build a root-cause hypothesis only after checking neighboring negative evidence.
4. Generate blind scenarios from objective, state, dependency, order, recovery, and consumer lenses.
5. Authorize a correction only for the causal link that explains the fault and preserves normal success.
6. Treat test-runner or fixture faults as verification-model claims, not product claims.
