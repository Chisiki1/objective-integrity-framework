# Runtime demo

Run the demonstration from the framework distribution and give it a new or empty sibling directory:

```text
python -B tools/demo.py --directory ../oif-demo
```

The command creates only the directory you name. It rejects live configuration roots, redirected paths, the framework distribution itself, and destinations that already contain files.

The demonstration uses synthetic English source text. It calls the shipped objective ledger, inactive-candidate materializer, and learning queue as subprocesses. The resulting `demo-result.json` points to:

- the complete BUILD → SWEEP → REPAIR → ACCEPT `current_workflow` example (its artifacts land under `current-workflow/`);
- two work artifacts whose outcomes remain visible across an additive source update;
- the durable objective projection and command records;
- an actual owned draft whose independent challenge is truthfully still pending;
- the materializer's non-writing `INACTIVE_CANDIDATE_HOLD_INDEPENDENT_CHALLENGE` preview receipt; and
- the exact queue query that finds the draft for independent review before a later matching use.

The pending materializer preview intentionally exits with status `3`; the demo records that expected hold without treating it as a failed primary outcome. It verifies that the preview reports no writes and that `inactive-candidates/` remains empty. The owned draft remains at `inputs/candidate-source/SKILL.md`, and both requested synthetic work outcomes remain `SATISFIED` before the improvement path is considered.

Every subprocess keeps separate stdout and stderr files under `records/`. If an unexpected command failure occurs, the directory is retained so the first failing step and any earlier effects can be inspected. The demo does not fabricate an independent reviewer, materialize or install the draft, edit a live project, or claim that one synthetic run proves broader improvement.

When the framework was installed with `tools/bootstrap.py --mode complete`, run the installed copy from `<project>/.oif/tools/demo.py` and choose a directory outside `<project>/.oif`.
