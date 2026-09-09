#!/usr/bin/env python3
"""Preview and build a self-contained skills-only plugin; never install it.

An exact preview binds the builder, explicit absent destination and every source
member. Build creates a new folder and deterministic ZIP; it never changes host
configuration, contacts a portal, overwrites a release or enables hooks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import unicodedata
from urllib.parse import urlsplit
import zipfile

# Process-local only: normal builder invocation must not create sibling caches.
sys.dont_write_bytecode = True
import bootstrap
import path_identity
from bootstrap import absolute, no_links, within

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def text(value, limit, *, multiline=False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Missing or oversized metadata text")
    if any(unicodedata.category(c).startswith("C") and not (multiline and c == "\n")
           or c in "\u2028\u2029" for c in value):
        raise ValueError("Unsupported metadata characters")
    return value


def relative(value):
    if not isinstance(value, str) or not value.startswith("./"):
        raise ValueError("Package reference must start with ./")
    rel = value[2:]
    if not rel or "\\" in rel or ":" in rel or any(p in {"", ".", ".."} for p in rel.split("/")):
        raise ValueError("Unsafe package reference")
    return rel


def validate_metadata(manifest):
    if manifest.get("$schema") != SCHEMA:
        raise ValueError("Expected portable Agent Plugins schema")
    name = text(manifest.get("name"), 64)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError("Use a stable lowercase kebab-case plugin name")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", text(manifest.get("version"), 64)):
        raise ValueError("A semantic version is required")
    text(manifest.get("description"), 1024, multiline=True)
    overlay = manifest.get("extensions", {}).get("com.openai", {})
    if any(k in manifest or k in overlay for k in ("mcpServers", "apps", "hooks")):
        raise ValueError("This package deliberately contains only skills and resources")
    interface = overlay.get("interface", {})
    for key, limit in (("displayName", 30), ("shortDescription", 30), ("developerName", 80)):
        text(interface.get(key), limit)
    text(interface.get("longDescription"), 4000, multiline=True)
    if manifest.get("author", {}).get("name") != interface["developerName"]:
        raise ValueError("Author and displayed developer must agree")
    if interface.get("category") != "Productivity" or "screenshots" in interface:
        raise ValueError("Expected Productivity skills-only metadata without UI screenshots")
    prompts = interface.get("defaultPrompt", [])
    if not isinstance(prompts, list) or len(prompts) > 3:
        raise ValueError("At most three starter prompts")
    seen = set()
    for prompt in prompts:
        text(prompt, 128)
        normalized = " ".join(unicodedata.normalize("NFKC", prompt).split()).casefold()
        if "@" in prompt or normalized in seen:
            raise ValueError("Duplicate or tool-mention starter prompt")
        seen.add(normalized)
    capabilities = interface.get("capabilities", [])
    if not isinstance(capabilities, list) or len(capabilities) > 20:
        raise ValueError("Invalid capability list")
    for capability in capabilities:
        text(capability, 120)
    urls = [manifest.get(k) for k in ("homepage", "repository")]
    urls += [v for k, v in interface.items() if k.endswith("URL")]
    for url in urls:
        parsed = urlsplit(text(url, 1024))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Public metadata requires credential-free HTTPS URLs")
    for key in ("logo", "composerIcon"):
        relative(interface.get(key))
    return interface


def compatibility(manifest):
    return {**{k: v for k, v in manifest.items() if k not in {"$schema", "extensions"}},
            "skills": "./skills/", **manifest["extensions"]["com.openai"]}


def payload(root=ROOT):
    root = absolute(root)
    sources = {}
    def add(relative_path, source):
        no_links(source)
        if not source.is_file():
            raise ValueError("Required package source missing: " + str(source))
        sources[relative_path] = source.read_bytes()
    add("plugin.json", root / "packaging/plugin.json")
    manifest = read_json(sources["plugin.json"])
    validate_metadata(manifest)
    sources[".codex-plugin/plugin.json"] = encoded(compatibility(manifest))
    for name in ("LICENSE", "PRIVACY.md"):
        add(name, root / name)
    add("README.md", root / "packaging/README.md")
    add("assets/icon.png", root / "docs/assets/oif-icon.png")
    add("examples/scenarios.json", root / "evals/plugin-scenarios.json")
    skill = root / ".agents/skills/objective-integrity"
    no_links(skill)
    for member in sorted(skill.rglob("*")):
        no_links(member)
        if member.is_file():
            if "__pycache__" in member.parts or member.suffix in {".pyc", ".pyo"}:
                raise ValueError("Generated cache inside the skill; retain and reconcile it before packaging")
            add("skills/objective-integrity/" + member.relative_to(skill).as_posix(), member)
    if "skills/objective-integrity/SKILL.md" not in sources:
        raise ValueError("Complete skill entrypoint required")
    validate_members(sources, manifest)
    return manifest, sources


def validate_members(members, manifest):
    interface = validate_metadata(manifest)
    if len(members) > 5000 or sum(map(len, members.values())) > 512 * 1024 * 1024:
        raise ValueError("Package exceeds supported archive limits")
    names = set()
    for name in members:
        if relative("./" + name) != name or name.casefold() in names:
            raise ValueError("Unsafe or duplicate package member")
        names.add(name.casefold())
        if any(p in {".git", "__pycache__", "hooks"} for p in PurePosixPath(name).parts):
            raise ValueError("Undeclared generated or executable integration content")
        if PurePosixPath(name).name in {"mcp.json", ".mcp.json", ".app.json"}:
            raise ValueError("Server configuration is outside this skills-only package")
    for key in ("logo", "composerIcon"):
        path = relative(interface[key])
        raw = members.get(path, b"")
        if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) < 24 or len(raw) > 5 * 1024 * 1024:
            raise ValueError("A package-local PNG icon of at most 5 MiB is required")
        width, height = struct.unpack(">II", raw[16:24])
        if width != height or not 48 <= width <= 4096:
            raise ValueError("Icon must be square and between 48 and 4096 pixels")
    for name, raw in members.items():
        if not name.startswith("skills/") or not name.endswith(".md"):
            continue
        content = raw.decode("utf8")
        for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", content):
            if "://" in link or link.startswith("#"):
                continue
            local = link.split("#")[0]
            if local and (PurePosixPath(name).parent / local).as_posix() not in members:
                raise ValueError("Missing bundled skill reference: " + link)


def plan(destination, root=ROOT):
    root = absolute(root)
    dest = absolute(destination)
    if within(dest, root) or within(root, dest):
        raise ValueError("Use a separate destination outside the distribution tree")
    if dest.exists() or not dest.parent.is_dir():
        raise ValueError("Destination must be absent and its parent must already exist")
    manifest, members = payload(root)
    dependencies = []
    for module_path in (__file__, bootstrap.__file__, path_identity.__file__):
        source = Path(module_path)
        no_links(source)
        dependencies.append(dict(path="tools/" + source.name, sha256=digest(source.read_bytes())))
    view = dict(schema="oif-plugin-build-v1", destination=str(dest), name=manifest["name"],
                version=manifest["version"], builder_sha256=digest(Path(__file__).read_bytes()),
                builder_dependencies=dependencies,
                members=[dict(path=k, sha256=digest(v), bytes=len(v)) for k, v in sorted(members.items())])
    return {**view, "plan_sha256": digest(encoded(view))}, members


def build(destination, expect_plan=None, apply=False, root=ROOT):
    view, members = plan(destination, root)
    if not apply:
        return {**view, "mode": "preview", "written": False}
    if not expect_plan or expect_plan != view["plan_sha256"]:
        raise ValueError("Apply requires the unchanged preview's --expect-plan")
    dest = Path(view["destination"])
    no_links(dest)
    dest.mkdir()  # Exclusive creation; retain any partial output on later failure.
    package = dest / view["name"]
    package.mkdir()
    for name, raw in sorted(members.items()):
        target = package.joinpath(*PurePosixPath(name).parts)
        no_links(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
    archive = dest / (view["name"] + "-" + view["version"] + ".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, raw in sorted(members.items()):
            info = zipfile.ZipInfo(view["name"] + "/" + name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(info, raw)
    if archive.stat().st_size > 100_000_000:
        raise ValueError("Archive exceeds 100 MB; partial output retained")
    readback = validate_directory(package)
    with zipfile.ZipFile(archive) as zipped:
        expected = {view["name"] + "/" + k: v for k, v in members.items()}
        if set(zipped.namelist()) != set(expected) or any(zipped.read(k) != v for k, v in expected.items()):
            raise ValueError("Archive readback mismatch")
    with (dest / "build.json").open("xb") as stream:
        stream.write(encoded({**view, "archive_sha256": digest(archive.read_bytes())}))
    return {**view, "mode": "built", "written": True, "package": str(package), "archive": str(archive),
            "archive_sha256": digest(archive.read_bytes()), "readback": readback,
            "host_installed": False, "directory_submitted": False}


def validate_directory(directory):
    directory = absolute(directory)
    if not directory.is_dir():
        raise ValueError("Package directory missing")
    members = {}
    for path in sorted(directory.rglob("*")):
        no_links(path)
        if path.is_file():
            members[path.relative_to(directory).as_posix()] = path.read_bytes()
    manifest = read_json(members.get("plugin.json", b"{}"))
    validate_members(members, manifest)
    if directory.name != manifest["name"]:
        raise ValueError("Package directory and plugin name differ")
    if read_json(members.get(".codex-plugin/plugin.json", b"{}")) != compatibility(manifest):
        raise ValueError("Compatibility metadata differs from the portable manifest")
    if "skills/objective-integrity/SKILL.md" not in members:
        raise ValueError("Skill entrypoint missing")
    return dict(valid=True, members=len(members), proof_ceiling="Local package structure and resources only; host use and directory review are separate.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build")
    make.add_argument("--destination", required=True)
    make.add_argument("--expect-plan")
    make.add_argument("--apply", action="store_true")
    check = commands.add_parser("validate")
    check.add_argument("--directory", required=True)
    args = parser.parse_args()
    try:
        result = build(args.destination, args.expect_plan, args.apply) if args.command == "build" else validate_directory(args.directory)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "error", "message": str(exc), "partial_output": "Inspect an existing destination before any retry; it is never automatically removed."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
