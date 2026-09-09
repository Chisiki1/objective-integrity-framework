# Releases and downloads

Use the [latest release](https://github.com/Chisiki1/objective-integrity-framework/releases/latest)
for a fixed, inspectable version. The current version is **0.1.1**. The framework
and skills-only plugin share that version; `main` can contain later development.

## Choose one download

| Asset | Use it for | Requirements |
|---|---|---|
| `objective-integrity-framework-0.1.1.zip` | Full source, documentation, examples and optional executable runtime | Reading needs no runtime; Python 3.10+ with standard-library `sqlite3` for the tools |
| `objective-integrity-0.1.1.zip` | Self-contained Objective Integrity skill, references and practice cases | A compatible plugin host; no Python, API key, server or OIF account |
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
Get-FileHash .\objective-integrity-framework-0.1.1.zip -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

On Linux, place the two ZIPs, `release.json` and `SHA256SUMS.txt` in one directory:

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

OIF uses `MAJOR.MINOR.PATCH` versions and `v`-prefixed release tags. The initial
tag is **v0.1.1**, following the earlier untagged 0.1.0 plugin package. This release
corrects packaging/license metadata; it does not migrate task records or change
runtime schemas. The [changelog](../CHANGELOG.md) distinguishes additions, fixes
and strengthened existing behavior.

During 0.x development, minor versions may change interfaces; patch versions are
intended for compatible corrections. Record schemas retain their own explicit
versions. Read migration notes before adopting an incompatible change. Supported
CI covers Windows and Linux on Python 3.10 and the latest stable Python available
to CI. Other hosts can use the portable instructions, but check their own tooling
compatibility. Maintainers prioritize the latest release; no paid support or
fixed response deadline is implied. See [support](support.md) and [security](../SECURITY.md).

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
`plugin-build/` is retained local build material; the four named top-level assets
are the distribution. Repeated builds with the same Git/Python/compression
environment produce the same bytes. If a build fails, inspect its retained output
before selecting a new destination. Existing output is never deleted or reused.
