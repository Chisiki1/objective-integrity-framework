# OIF Desktop guide

Version **0.4.0** · [Quick start](../README.md) · [Japanese](ja.md)

## A task from request to result

Create a chat and give the desired result, constraints and files. Each new instruction retains its own progress and result. A pinned instruction summary shows whether it has been received and applied. Earlier turns collapse automatically; you can expand them without losing the current turn.

Completed earlier requests remain in the record. The next answer focuses on the latest instruction and unfinished work, with source-bound criteria for added or clarified requests. Creating files, running programs and observing pages use their actual tool results as completion evidence.

Important progress shows user-facing milestones, decisions, failures and results. All progress also includes the detailed model/tool events. The chat follows new content when you are following the latest work; scrolling back lets you inspect earlier content. The full timeline is newest first. Expand a progress row for a readable account, or inspect its original evidence. Explanation is available on demand.

Artifacts are listed with their latest version first. Clicking a supported file opens a preview; Download saves the original. Large records are paged rather than loading the whole history into the page. Task details contain operations, unfinished work, improvement ideas and skills. Right-click a task to rename, pin, archive, group or copy its local link. A local chat link opens this installation; it is not a public sharing service.

## Models and access

### Use a local model on this PC

OIF can connect to OpenAI-compatible servers such as Ollama, LM Studio and llama.cpp. Start the model server and select **Compatible API** in connection settings. For Ollama, use `http://127.0.0.1:11434/v1` and the exact installed model name. Leave the API key empty for a local server without authentication, or supply that server's own key when authentication is enabled.

Save your current cloud connection under a name first so you can switch back. Changing to a local endpoint without entering a key clears the previous connection's active key; its saved profile remains available. HTTP is allowed only for `127.0.0.1`, `localhost` and `[::1]`. Web research has separate settings: selecting local inference does not make requested Web research offline.

Match OIF's context and output limits to the actual model server. Changing OIF's context field does not enlarge Ollama's server context. If needed, create a Modelfile with `FROM model-name` and `PARAMETER num_ctx 65536`, then run `ollama create oif-local -f Modelfile` to add a dedicated model name using existing weights. [Ollama context configuration](https://docs.ollama.com/api/openai-compatibility#setting-the-local-context-size)

### Permissions and context management

Settings holds provider endpoints, model names and named roles/profiles. Use an API key when the OpenAI-compatible server requires authentication. The OpenRouter sign-in flow is an optional provider-specific route. OAuth is not available for every provider. Context limits and output limits must match the model you select. Context compression keeps bounded working memory and original records; it cannot make an incorrect summary authoritative.

Access is explicit in the composer. Workspace mode confines ordinary file tools to the task folder. Confirmation mode asks before covered changes and communication. Read-only restricts mutations. Full access allows broader native file and command operations after its confirmation; choose it only for work you intend to authorize. Changing a selection applies to subsequent actions, not to already-issued effects.

Keys are protected for the current Windows user. Model requests send the information needed for the task to the configured provider. Public research sends queries to the configured research route. Do not place credentials in instructions or lesson text. See the repository's privacy documentation for the separate framework distribution.

## Web research

Choose **Public URLs** for direct public HTTPS pages. Keyword search uses Brave or Tavily with the corresponding API key in Settings. The page reader returns text plus observed links, form fields, script URLs and page metadata. It follows model-selected links under the same public-network checks; collecting a link does not execute it or submit a form.

Large pages receive separate excerpts so one sitemap cannot hide the other results. The agent can search or page through the saved text, structured details and original HTML/JavaScript with `history_read`; reading a saved response makes no new network request. A definite HTTP error leaves successful sources available and permits the other requested pages to be retrieved. Unknown effects and credential checks keep their existing recovery boundaries.

For a large JavaScript file, the agent can request a per-response limit with <code>web_fetch(max_bytes=8000000)</code>, up to 8 MB, while the saved default stays unchanged. Reaching a known byte limit preserves a searchable prefix marked incomplete and permits independent pages to continue. <code>history_read(view=web_response, query=["fetch(", "sendBeacon", "localStorage"])</code> searches several literal terms together. Missing matches in a prefix do not establish their absence in the whole file. Recovery of an older size-limit hold verifies the retained bytes without replaying that request.

For dynamic pages, `browser_inspect` launches a fresh isolated browser, runs the page JavaScript, and records rendered text, console errors, network attempts, cookies, localStorage and sessionStorage. The agent can also inspect an HTML file it created in the task workspace. Windows reuses installed Edge or Chrome; other platforms need Chromium installed with `python -m playwright install chromium` after installing OIF dependencies. Browser observation does not need Docker.

This is passive anonymous observation. Public HTTPS GET/HEAD requests pass through the same address validation and pinned transport as the page reader. POST, WebSockets, service workers, media bodies and downloads are outside this tool's current coverage; blocked attempts are labelled in the result. Top-level HTTP redirects are resolved before opening their final URL. Cookies set along that redirect chain are not imported; redirects from scripts and other subresources are recorded but blocked. Local task HTML cannot contact external sites. The browser never opens your existing profile, signs in or submits a form. Results are bounded by time, request count and response size, with partial coverage stated. Static source reads and browser observations remain separate evidence; neither establishes the absence of vulnerabilities or all authenticated behavior.

## How learning carries forward

A useful experience may become a provisional lesson with an evidence reference, conditions of use, a concrete procedure, limitations and a next trigger. The shared library makes general procedures eligible in later chats. Task-specific source material remains attached to its task; a lesson does not become user authority.

The engine selects matching lessons, records actual use and evaluates the outcome. It can revise a lesson when new evidence changes it, merge overlapping lessons, retire an ineffective one and preserve the history of that decision. Retirement removes a lesson from active selection; it does not erase its past use. A counter in the UI is an inventory, not a quality score. A routine file write may correctly create zero new lessons.

The practical policy preserves the goal, all mandatory outcomes, permissions, first failures, recovery and learning. It uses research for current or uncertain external facts and requested research; it does not research the web for every deterministic local file operation. Optional learning shares the ordinary decision loop and does not hold a completed deliverable indefinitely. The public policy is in `policy/complete-policy-v3.json` with its execution profile in `policy/practical-runtime-v1.json`.

## Improving the harness itself

The agent can inspect non-secret runtime settings and allowlisted source. A source improvement follows proposal, isolated verification, review, activation, subsequent ordinary use and possible rollback. It cannot turn arbitrary source writes into an update by calling them learning. Changing explicit user conditions still requires the corresponding user authority.

In workspace mode, Docker Desktop with a working Linux container backend is needed for isolated Python execution and controlled harness-code verification. In full access, Python and commands execute natively with the current user permissions and network access. Missing Docker does not make Web research offline or prevent file creation and browser observation. Ordinary workspace file reading/writing, task management, artifact preview and the desktop UI do not require Docker. Model quality, provider reliability and a working verification environment still affect the result. Successful tests establish their tested scope, not unlimited autonomous growth.

## Sending prompts

Enter sends a new task or follow-up instruction; Shift+Enter inserts a newline. In **Connection settings → Send key**, choose **Ctrl/Cmd+Enter to send** to make plain Enter insert a newline. This preference applies immediately and is saved on this device. Confirming Japanese or other IME text does not send the prompt.

## Stop, recover and work in parallel

Chats have independent task state and can run concurrently. Stop affects the selected task. Recorded results remain available, and Resume continues from the saved boundary. If a tool's effect is unknown, inspect that action before replaying it. A chat's display can be reloaded without restarting other chats. Provider-key recovery also preserves task records.

Closing the desktop window keeps the local service and running tasks alive. To shut the service down, finish or stop active tasks and run `scripts/Stop-Harness.ps1` from the app folder. It validates process ownership and requests graceful shutdown. A failed shutdown is not permission to overwrite a live database. Logs are retained under `.runtime`; support reports should omit credentials and private task contents.

## Back up, update and restore

If a new task cannot be sent, the notice explains whether the local service is disconnected or the request was rejected. **Restore connection** keeps the current input and attachments, reconnects the desktop service if needed, and checks the original submission ID. It does not resend the request. If no receipt exists, choose **Start work** to send it. An expired session refreshes automatically.

The application must stay in a writable folder. The complete Windows ZIP contains the app and its private Python environment. It is not a single-file executable.

### Update from the app

Open **Updates** at the top of the window. OIF checks the official GitHub releases at startup and caches the result for six hours; **Check for updates** refreshes it. Beta installations include beta releases, while stable installations stay on stable releases. A failed network check leaves ordinary tasks available.

Choose **Update and restart** to download, verify and install the selected Windows package. This is optional. Active tasks must finish before installation starts. The app closes and reopens at the same path, preserving pins, `.runtime`, credentials, lessons and user files. Changed application files are backed up under `.runtime/product-updates`. The download is checked against the official release asset digest and every packaged file against its inventory. This verifies the files obtained from the official repository; it is not a separate publisher signature.

If a package-owned file has been changed locally, the updater preserves it and asks for manual integration. A failed replacement restores the old application files before reopening. If the helper was interrupted during replacement, the next normal launch recovers the saved preimages first. This recovery does not rewind task data or replay tasks. A full folder backup remains the way to restore an older version and its data together.

If the running service still holds older application files, choose **Restart service** in this panel. It waits for idle work, retains task records and reconnects to the new process. Reopening the desktop also replaces a stale idle service. A held chat can then be resumed explicitly. An ordinary page refresh only refreshes the display.

### Manual update and full backup

Before updating or restarting, send or clear unsent text and attachments in every chat and the new-task composer. OIF keeps the window open while these drafts or a submission are pending.

1. Finish or stop active work. Close the OIF window. Run `scripts/Stop-Harness.ps1` and wait for confirmed shutdown.
2. Copy the entire application folder to a backup location. This retains `.runtime`, including task files, settings, protected credentials, learning and recovery records. Keep the same Windows user; credentials are not transferable to another account.
3. Download the new ZIP and verify its checksum from that release. Extract it separately. Do not start it yet.
4. Move the stopped old application folder aside. Move the extracted new application to the **same original absolute path**. Copy the old `.runtime` into the new folder before opening it. Do not overwrite it with a second live database.
5. Open OIF.exe. Confirm your task list and settings. Review any policy/version reconciliation before resuming old work. Keep the backup until normal work is confirmed.

To restore, stop and preserve the current version and its data, then restore the complete backup at the same original path. This restores the backup's point in time; retain newer work separately. Desktop preferences are in the current user's local OIF profile, keyed by installation path. These steps describe versioned public desktop packages. Private development workspaces and unrelated framework/plugin installations are not automatically migrated.

## Supported and tested scope

This release supports Windows x64 and the PracticalEngine. Automated tests use synthetic providers for repeatable runtime assertions. Illustrative screenshots are labelled examples. Live provider behavior, OS trust prompts and third-party service changes can differ across installations. Report a reproducible problem with the version, the affected operation and redacted logs. Keep the original first error and any partial result.
