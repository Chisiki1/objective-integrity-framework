# Releases and downloads

Use the [desktop beta release](https://github.com/Chisiki1/objective-integrity-framework/releases/tag/v0.3.0-beta.2)
for a fixed, inspectable version. The current version is **0.3.0-beta.2**. The framework
and skills-only plugin share that version; `main` can contain later development.

## Choose one download

- **OIF-Desktop-0.3.0-beta.2-windows-x64.zip** — the ready-to-open Windows app. Extract all files and open OIF.exe. [Guide](https://github.com/Chisiki1/objective-integrity-framework/blob/v0.3.0-beta.2/harness/README.md).
- **OIF-Showcase-0.3.0-beta.2.zip** — labelled example images and their captions.


| Asset | Use it for | Requirements |
|---|---|---|
| `objective-integrity-framework-0.3.0-beta.2.zip` | Full source, documentation, examples and optional executable runtime | Reading needs no runtime; Python 3.10+ with standard-library `sqlite3` for the tools |
| `objective-integrity-0.3.0-beta.2.zip` | Self-contained Objective Integrity skill, references and practice cases | A compatible plugin host; no Python, API key, server or OIF account |
| `SHA256SUMS.txt` | Check downloaded ZIPs and `release.json` | A SHA-256 utility |
| `release.json` | Exact source commit/tree, artifact hashes, sizes and builder identities | Any text or JSON reader |

Download only the mode you need, plus the checksums. Extract into a new directory.
The ZIPs contain a named top-level folder and do not run installation scripts.
GitHub's automatic **Source code** downloads are alternatives for source browsing;
the named assets above are the files covered by the accompanying checksums.

## Verify before use

Download `SHA256SUMS.txt` from the **same version's release** as your ZIP. On
PowerShell, print the file's hash and compare it with the matching filename's row:

```powershell
Get-FileHash .\objective-integrity-framework-0.3.0-beta.2.zip -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

On Linux, place all downloaded ZIPs, `release.json` and `SHA256SUMS.txt` in one directory:

```bash
sha256sum -c SHA256SUMS.txt
```

On macOS, use `shasum -a 256 -c SHA256SUMS.txt`. If you downloaded only one ZIP,
compute its SHA-256 individually and compare its row. Stop on a mismatch and
download again from the matching release. Checksums identify bytes; use the
project's HTTPS release page and tagged commit to establish their source.

## Start, update and recover

For the full archive, open its top-level folder and follow the [quick start](../README.md#quick-start)
or [adoption guide](adoption.md). The demo, installation and test temporary roots
must be separate from the extracted distribution. No package manager install is
required. Git is needed only for repository/history checks or building a release.

For the smaller ZIP, follow [plugin installation and recovery](plugin.md).
Downloading it is not an installation or an official-directory listing.

Keep the previous distribution and your backup manifests. For a full installation,
preview the new version against the same explicitly chosen project using
`tools/bootstrap.py --update`; apply the reviewed preview as described in the
adoption guide. This preserves user-owned records and detects conflicts. Keep
task data outside the plugin folder. Use the retained version and the documented
rollback route if recovery is needed; do not overwrite edited files by hand.

## Version compatibility and support

OIF uses `MAJOR.MINOR.PATCH` versions and `v`-prefixed release tags. The current
tag is **v0.3.0-beta.2**; the first versioned tag was **v0.1.1**, following the earlier
untagged 0.1.0 plugin package. This beta adds the Windows desktop harness.
Framework records keep their existing schemas. Desktop data uses its own local
store and the documented [backup/update/restore procedure](https://github.com/Chisiki1/objective-integrity-framework/blob/v0.3.0-beta.2/harness/docs/guide.md#back-up-update-and-restore).

During 0.x development, minor versions may change interfaces; patch versions are
intended for compatible corrections. The framework CI covers Windows and Linux
on Python 3.10 and the current Python release. The desktop job uses Windows and
Python 3.12 with Node for UI regressions. See [support](support.md) and [security](../SECURITY.md).

## Reproduce the assets

From a clean checkout of the desired tag, with Git and Python 3.10+ available:

```bash
python -B tools/release.py --destination ../oif-release
python -B tools/release.py --destination ../oif-release --expect-plan <plan-sha256> --apply
```

The first command is a read-only preview. The second requires its exact hash,
an absent destination and an existing separate parent. The builder uses committed
source, rejects local changes/redirected members, reuses the plugin builder, and
reads back every asset. It never tags, uploads, installs or changes configuration.
`plugin-build/` is retained local build material. These commands build the two core archives and their checksums. Repeated builds with the same Git/Python/compression
environment produce the same bytes. If a build fails, inspect its retained output
before selecting a new destination. Existing output is never deleted or reused.


For a desktop beta, first build the Windows folder with
`harness/scripts/Build-Windows.ps1` in a separate directory, then run:

```bash
python -B tools/desktop_release.py --base ../oif-release --desktop ../OIF-build --destination ../oif-beta-assets
```

The additional builder checks desktop source bytes against the same committed
checkout, excludes runtime data and build caches, and creates the desktop and
showcase ZIPs plus a combined manifest and checksums. It refuses an existing output
directory. None of these commands publishes the result.
