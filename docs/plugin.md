# Plugin Packaging and Use

Use the Objective Integrity skill as a self-contained plugin when a compatible
host should carry its instructions and references together. Use the complete OIF
runtime when your project also needs executable ledgers and workflow helpers.

| Mode | Includes | Needs |
|---|---|---|
| Skill or plugin | Objective continuity, whole-scope work and in-work learning instructions | A compatible agent host; task files only when authorized and available |
| Complete runtime | The same guidance plus executable objective, learning, operation, Skill-package and reconciliation tools | Python 3.10+, standard-library `sqlite3`, explicit writable project paths |
| Optional integration | Host-specific instructions and hook resources | A supported host and its separate configuration/trust process |

The plugin does not start a service, add an account connection, retrain the model,
install other Skills or enable hooks. Its portable manifest and a generated Codex
compatibility manifest describe the same package. The compatibility copy is
derived from the portable metadata, not maintained as a second configuration.

## Build a package

Prefer the ready-to-download ZIP on the [release page](https://github.com/Chisiki1/objective-integrity-framework/releases/latest).
The [release guide](releases.md) explains archive choice, checksum verification
and version compatibility. Building locally is optional.

Run these commands from the OIF repository with Python 3.10 or later. The output
directory must not exist; its parent must exist and remain separate from OIF.

```bash
python tools/plugin.py build --destination ../oif-plugin
```

This prints the exact members and `plan_sha256`, without writing the destination.
Review them, then use that hash:

```bash
python tools/plugin.py build --destination ../oif-plugin --expect-plan <plan-sha256> --apply
python tools/plugin.py validate --directory ../oif-plugin/objective-integrity
```

The output contains:

```text
oif-plugin/
  objective-integrity/
    plugin.json
    .codex-plugin/plugin.json
    skills/objective-integrity/SKILL.md
    skills/objective-integrity/references/
    assets/icon.png
    examples/scenarios.json
    README.md
    PRIVACY.md
    LICENSE
    NOTICE
  objective-integrity-0.2.0.zip
  build.json
```

The build checks metadata, package-local references, complete Skill members and
the icon; it reads back both the folder and ZIP. Identical source bytes produce
identical ZIP bytes. A changed source, occupied destination or mismatched preview
stops the build before writing that destination. A later failure retains any
partial output for inspection instead of silently deleting or retrying it.

## Load it in a compatible host

Use the host's supported plugin flow. For a local Codex/ChatGPT desktop
marketplace, the official documentation describes a repository catalog at
`.agents/plugins/marketplace.json` and a `source.path` relative to that catalog's
workspace root. For this generated layout, an explicitly chosen catalog root
would be `oif-plugin`, and the source path would be `./objective-integrity`.

The [official packaging guide](https://developers.openai.com/plugins/build/plugins)
documents the current registration, installation and refresh steps. Follow the
applicable host version and workspace policy. Building a ZIP does not install it
or make it available in a public directory. OIF's builder never edits a personal
marketplace, agent configuration or trust record.

After loading, try one of these tasks with your own relevant source:

- "Help me finish this long task while keeping every active requirement."
- "Resume this project and identify what still needs to be completed."
- "Review this result and apply a useful improvement to the next matching action."

The result should be the requested work, not a workflow report. Use an existing
task-owned checkpoint where available. If the host cannot persist files, keep
the checkpoint in the conversation and carry it explicitly on resume. Do not
claim cross-chat or post-compaction persistence from instructions alone.

## Check behavior, not just the archive

The package contains five positive and three negative synthetic scenarios in
`examples/scenarios.json`; their source is
[evals/plugin-scenarios.json](../evals/plugin-scenarios.json). They cover additions,
corrections, independent work after a failed dependency, supplied-checkpoint
replay, a reusable CSV correction, simple requests, quoted instructions and
cancellation.

These files are known test specifications, not recorded model successes. Replay
the turns in a clean, authorized host context and compare actual output with both
`required` and `forbidden`. Record which Skill was read, the host/model version,
tools, first fault, task completion, persistence mode and elapsed time. Check
selection and actual use separately. A supplied checkpoint tests replay, not
native compaction; a supplied lesson alone does not test autonomous Skill creation.

Keep existing host guidance unchanged in a comparison, retain failures, and use
unseen cases separately when estimating broader benefit. Follow the
[evaluation guide](evaluation.md) for evidence boundaries. No fixed model-trial
count is required to build or use OIF.

## Update or recover

Keep the previous generated folder and archive. Build a reviewed new version
into another absent output directory; inspect its diff and hashes before using
the host's normal update flow. If that update needs to be reversed, select the
retained previous package through the same host flow. OIF does not overwrite an
installed plugin or migrate host settings itself.

Store task checkpoints and learning records outside plugin directories so a
package update does not replace them. For complete project-local runtime
installations, use the settings-preserving update and hash-bound rollback in the
[adoption guide](adoption.md). Package reproduction, runtime update/restore and
actual host installation are separate observations.

## Support and data

Use the [issue tracker](https://github.com/Chisiki1/objective-integrity-framework/issues)
for general questions and bugs, and [SECURITY.md](../SECURITY.md) for sensitive
reports. Include a minimal synthetic reproduction, not task ledgers, secrets or
private source history. Read [PRIVACY.md](../PRIVACY.md) and the [Apache 2.0 license](../LICENSE)
for data handling and software terms.
