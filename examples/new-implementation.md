# Example: New Implementation

Scenario: Build a queue-backed report generator.

Key records:

- Objective contract: produce a report for accepted jobs.
- Workload progress: admitted jobs eventually reach a terminal result or recoverable failure.
- Boundary-state continuity: queued payload, worker state, and report identity survive retry and restart.
- Consumer oracle: the requester can retrieve the correct report exactly once.

The test plan can use unit checks and integration checks, but completion needs a recomposition witness that those checks imply the final consumer result.
