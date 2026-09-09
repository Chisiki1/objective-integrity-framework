#!/usr/bin/env python3
"""Preview or build versioned release assets from an exact clean Git checkout.

No tagging, upload, installation, credentials or network access. Retain partial
output on failure; the explicit destination is never overwritten or removed.
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path
import re
import subprocess
import sys
import zipfile

sys.dont_write_bytecode = True
import plugin
from bootstrap import absolute, no_links, within

ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        raise ValueError(f"git {args[0]} exited {result.returncode}: "
                         + result.stderr.decode("utf8", errors="replace"))
    return result.stdout


def source_bundle(root):
    """Read committed bytes; reject local drift and non-regular tracked entries."""
    root = absolute(root)
    if git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("Use a clean committed checkout; no local or untracked changes")
    version = (root / "VERSION").read_text(encoding="utf8").strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
        raise ValueError("VERSION must contain one stable numeric semantic version")
    manifest, _ = plugin.payload(root)
    if manifest["version"] != version:
        raise ValueError("VERSION and plugin version must agree")
    commit = git(root, "rev-parse", "HEAD").decode().strip()
    tree = git(root, "rev-parse", "HEAD^{tree}").decode().strip()
    names = []
    for entry in git(root, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        header, path = entry.split(b"\t", 1)
        mode, kind, _ = header.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("Only ordinary tracked files are releasable")
        name = path.decode("utf8")
        plugin.relative("./" + name)
        no_links(root / name)
        names.append(name)
    prefix = "objective-integrity-framework-" + version + "/"
    archive = git(root, "archive", "--format=zip", "--prefix=" + prefix, commit)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        actual = [p for p in z.namelist() if not p.endswith("/")]
        if len(actual) != len(set(actual)) or set(actual) != {prefix + n for n in names}:
            raise ValueError("Git archive does not contain the exact tracked file set")
        for name in names:
            if z.read(prefix + name) != (root / name).read_bytes():
                raise ValueError("Working bytes differ from committed source: " + name)
    return dict(version=version, tag="v" + version, source_commit=commit,
                source_tree=tree, source_members=len(names)), archive


def plan(destination, root=ROOT):
    root, dest = absolute(root), absolute(destination)
    if within(dest, root) or within(root, dest):
        raise ValueError("Release destination must be separate from the checkout")
    if dest.exists() or not dest.parent.is_dir():
        raise ValueError("Destination must be absent with an existing parent")
    identity, archive = source_bundle(root)
    manifest, members = plugin.payload(root)
    view = dict(schema="oif-release-plan-v1", destination=str(dest), **identity,
                source_archive_sha256=plugin.digest(archive),
                plugin_members=[dict(path=k, sha256=plugin.digest(v)) for k, v in sorted(members.items())],
                helpers=[dict(path="tools/" + n, sha256=plugin.digest((root / "tools" / n).read_bytes()))
                         for n in ("release.py", "plugin.py", "bootstrap.py", "path_identity.py")])
    return {**view, "plan_sha256": plugin.digest(plugin.encoded(view))}, archive


def build(destination, expect_plan=None, apply=False, root=ROOT):
    view, source = plan(destination, root)
    if not apply:
        return {**view, "written": False}
    if not expect_plan or expect_plan != view["plan_sha256"]:
        raise ValueError("Apply requires --expect-plan from the unchanged preview")
    dest = absolute(destination)
    dest.mkdir()
    # Reuse the plugin's complete, source-bound build and readback route.
    plugin_dest = dest / "plugin-build"
    preview = plugin.build(plugin_dest, root=root)
    expected_members = {m["path"]: m["sha256"] for m in view["plugin_members"]}
    if {m["path"]: m["sha256"] for m in preview["members"]} != expected_members:
        raise ValueError("Plugin source changed during the release build; partial output retained")
    made = plugin.build(plugin_dest, preview["plan_sha256"], True, root)
    if {m["path"]: m["sha256"] for m in made["members"]} != expected_members or made["version"] != view["version"]:
        raise ValueError("Built plugin differs from the reviewed release")
    files = {
        "objective-integrity-framework-" + view["version"] + ".zip": source,
        Path(made["archive"]).name: Path(made["archive"]).read_bytes(),
    }
    release = dict(schema="oif-release-v1", version=view["version"], tag=view["tag"],
                   source_commit=view["source_commit"], source_tree=view["source_tree"],
                   source_members=view["source_members"], license="Apache-2.0",
                   artifacts=[dict(name=n, bytes=len(b), sha256=plugin.digest(b))
                              for n, b in sorted(files.items())],
                   helpers=view["helpers"])
    files["release.json"] = plugin.encoded(release)
    files["SHA256SUMS.txt"] = "".join(plugin.digest(b) + "  " + n + "\n"
                                     for n, b in sorted(files.items())).encode("ascii")
    for name, raw in files.items():
        with (dest / name).open("xb") as f:
            f.write(raw)
        if (dest / name).read_bytes() != raw:
            raise ValueError("Release asset readback mismatch: " + name)
    return dict(written=True, destination=str(dest), **release,
                published=False, host_installed=False, assets=sorted(files))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--expect-plan")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(plugin.encoded(build(args.destination, args.expect_plan, args.apply)).decode(), end="")
        return 0
    except (OSError, ValueError, KeyError, UnicodeError, zipfile.BadZipFile) as exc:
        print(plugin.encoded(dict(status="error", message=str(exc),
                                  recovery="Inspect retained output before choosing a new destination; nothing is deleted or published.")).decode(), file=sys.stderr, end="")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
