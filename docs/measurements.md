# Measured query-response size

The September 9 update introduced exact-content grouping with separately paged
source provenance. Here is a reproducible example of that existing mechanism,
measured for the 0.1.1 release. It is not a new query algorithm in 0.1.1.

Both modes returned all matching content. The **full retrieval** column also
counts every source-origin response and its ancestor metadata, verified against
independently generated input bytes. Values are actual CLI stdout bytes, not
estimated tokens or reserialized objects.

| Synthetic input | Legacy first / full | Compact first | Compact full | Full-retrieval change |
|---|---:|---:|---:|---:|
| 10 sources containing one identical section | 19,004 B | 1,646 B | 7,979 B | 58.01% smaller |
| 10 sources containing 10 distinct sections | 18,976 B | 11,437 B | 42,785 B | 125.47% larger |
| Similar allow/deny statements and LF/CRLF differences | 3,360 B | 1,451 B | 6,117 B | 82.05% larger |
| No matching content | 543 B | 557 B | 557 B | 2.58% larger |

The duplicate-rich first response is **91.34% smaller**. All 10 origins and the
exact section survive. Different wording and line endings remain separate; both
modes also reject a same-length stale-source mutation with exit code 2.

## What to do with this result

Compact output is useful for selecting content without repeatedly returning the
same text. Retrieve the origins needed for the current decision. If the consumer
needs all distinct sections and every origin inline, `--legacy-output` can be
cheaper: the distinct-content case above required 11 compact responses versus
one legacy response, repeating some shared ancestry metadata.

These figures measure response data, **not latency, tokens, model quality, task
completion or total work cost**. Setup/index construction is excluded from the
response-size metric and no timing was recorded. A smaller first response alone
does not establish cheaper complete retrieval. See [evaluation](evaluation.md)
when measuring end-to-end outcomes.

## Reproduce

From the full distribution, with Python 3.10+ and existing separate output parents:

```bash
python -B tools/benchmark_query.py --script runtime/skills/master-guided-skill-lifecycle/scripts/master_index.py --scratch ../query-benchmark --output ../query-results.json
```

Both output names must be absent. The source remains read-only; the new scratch
holds synthetic files, an index and original stdout/stderr/exit records. The JSON
summary has no machine paths. Keep raw scratch data local because it contains
absolute synthetic paths. Nothing is installed globally or published.

The main fixture has 8 history nodes, 2 current masters and 9 history edges. Each
matched section is 853 ASCII bytes; equal-width tags toggle exact duplication.
Both modes use identical files, index, query and a `--page-chars 12000` argument.
That historical option bounds selected text characters in legacy mode but total
serialized bytes in compact mode. The harness follows all pages and verifies
content and complete provenance rather than assuming equal budgets imply equal
coverage. It then runs contradiction, empty-result and stale-source controls.

The recorded run used Python 3.12.2 on Windows; paths in the duplicate-rich case
were 151, 154 or 155 characters. Path lengths affect metadata sizes, so other
roots or operating systems can produce different exact totals. No path redaction
or normalization was applied before counting. The percentage formula is
`100 * (legacy_bytes - compact_bytes) / legacy_bytes`, rounded to two decimals;
a negative reduction means overhead, not an improvement.

[Machine-readable observations](benchmarks/query-response-0.1.1.json) retain
all cases, environment, source hash and denominators. The source runtime is
unchanged. The shipped harness preserves the measured fixture and byte-counting
algorithm; its portable path screening supports Python 3.10 on Windows as well
as newer Python. A first harness run exposed an oracle assumption about the newline in a
history-marker span; the full span check was corrected before these observations.
That was a benchmark correction, not an OIF runtime defect or a discarded slow run.
