# OIF Desktop · 0.3.0-beta.2

**An agent workspace that keeps the goal in view and carries useful experience into the next task.**

OIF combines a Windows desktop app, a persistent task engine and a shared learning library. Follow the work, preview its output, and keep corrections connected to the original request. English and Japanese interfaces are included.

## Start in three steps

1. Download **OIF-Desktop-0.3.0-beta.2-windows-x64.zip** from the [beta release](https://github.com/Chisiki1/objective-integrity-framework/releases/tag/v0.3.0-beta.2).
2. Extract the complete folder into a writable location. Open **OIF.exe**. Keep its `python`, `desktop`, `src`, `policy` and `scripts` folders beside it.
3. Open **Settings**, choose an OpenAI-compatible provider and model, enter its API key, then start a task. OpenRouter also has a browser sign-in route. Availability and model capabilities depend on the provider.

Windows 10/11 x64 and the [Microsoft Edge WebView2 Evergreen Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) are required. The Windows package includes Python and its dependencies. A separate browser window or Python installation is unnecessary. This beta is not code-signed; Windows may ask you to confirm the publisher before launching it. Verify the downloaded archive against the release checksums.

## What you can do

- **Keep a long task coherent.** Instructions, completion criteria, operations and unfinished effects are stored with the task. Context is compacted as it grows; the original records stay available through bounded retrieval.
- **See the result for each instruction.** Earlier progress can collapse while the current work stays open. Important progress is the default, with the full timeline available. Preview text, Markdown, images and PDFs; download the original file separately.
- **Reuse learning across tasks.** A useful lesson contains evidence, applicability, steps, limits and a next-use trigger. Applicable shared lessons reach new chats. Usage and outcome records help refine, consolidate or retire them. A saved lesson is not automatically a proven improvement.
- **Improve the harness under control.** The agent can read non-secret settings and allowlisted source, prepare a code change, verify it, and request activation through the controlled update path. Verification, activation, actual next use and rollback remain distinct stages.
- **Choose the access scope.** Workspace access is the ordinary default; confirmation and read-only modes are available. Full access has a separate confirmation and allows operations outside the task workspace. It is still subject to explicit user scope.
- **Keep working locally.** Multiple chats can run concurrently. Per-chat recovery preserves recorded results. Closing the window leaves running work available when you reopen it.

Ordinary file work does **not** require Docker. Isolated shell execution and controlled harness-code verification use Docker Desktop with a working Linux container backend. Install that optional environment before using those paths. The beta has no claim of universal model compatibility or benchmark superiority over other agent products.

## Try it

Start with: **“Create answer.txt containing exactly hello, without a newline, then read it back.”**

Next, ask for a small research report with public sources, or a change to a file you attach. Paste files into the composer, or use Attach. Review the result and its latest artifact. To explore learning, ask the agent to explain the shared lessons it actually used and the evidence for keeping or changing them.

The [desktop guide](docs/guide.md) covers settings, recovery, long tasks, learning and updates. [Japanese guide](docs/ja.md).

## Data and updates

Task records, workspace files and encrypted provider credentials live in `.runtime` beside the app. Credentials use Windows user-bound protection. Back up this folder while the service is stopped. Closing the window alone does not stop the service; use `scripts/Stop-Harness.ps1` before a backup or update. Keep the same Windows account and original installation path when restoring.

Open **Updates** to check official OIF releases. The Windows package offers **Update and restart** when a newer compatible release is available. Installation starts only when you choose it and all tasks are idle. OIF verifies the download and package inventory, keeps the same app path, preserves `.runtime` and unknown user files, and backs up changed application files. Failed replacement restores those files before reopening. Locally modified package files are preserved and prevent automatic replacement. Beta installations see beta and stable releases; stable installations see stable releases.

For a manual update or a source checkout, keep a full copy of the old app, extract the new version separately, stop both versions, move the old folder aside, put the new folder at the **same original path**, and copy the old `.runtime` into it before launch. Keep the old copy for rollback. Never merge two databases or replace data while work is running. See the guide for the complete procedure.

## Build from source

Install [uv](https://docs.astral.sh/uv/), then from this folder:

```powershell
uv sync --frozen --group dev
.\scripts\Build-Desktop.ps1
.\OIF.exe
```

Build a self-contained Windows folder outside the checkout:

```powershell
.\scripts\Build-Windows.ps1 -Destination ..\OIF-build
```

The builder pins Python, Python package hashes and the WebView2 SDK. It changes no global Python installation. Native compilation uses the Windows .NET Framework compiler. See [third-party notices](THIRD-PARTY-NOTICES.md).

## Verification

`python scripts/check.py` exercises the supported PracticalEngine and shared service, permission, model, UI, learning and recovery paths. Tests use synthetic workspaces and scripted providers unless explicitly identified otherwise. Legacy Engine research fixtures are retained as source references; they are not a promise of a supported second runtime.

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
