#!/usr/bin/env python3
"""Explicit project-local adoption with preview, verified backup and recovery.

No network or implicit activation. A cooperative lock serializes this tool;
optimistic hashes detect changed files before replacement, not hostile writers.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import stat
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from path_identity import comparison_identity, within

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = ".objective-integrity-backups"
LOCK = ".objective-integrity-install.lock"
SCHEMA = "oif-install-v3"
OMIT = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def no_links(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Redirected path is not supported: {part}")


def absolute(path: str | Path) -> Path:
    value = Path(os.path.abspath(path))
    no_links(value)
    return value.resolve()


def member(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if (not relative or "\\" in relative or ":" in relative or rel.is_absolute()
            or any(p in {"..", ".", ""} for p in relative.split("/"))):
        raise ValueError(f"Invalid relative member: {relative!r}")
    target = root.joinpath(*rel.parts)
    no_links(target)
    target.resolve().relative_to(root.resolve())
    return target


def fingerprint(path: Path) -> str | None:
    no_links(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Expected a regular file: {path}")
    return digest(path.read_bytes())


def atomic_write(path: Path, data: bytes) -> None:
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".oif-tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        no_links(path)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def install_lock(destination: Path):
    path = member(destination, LOCK)
    token = ("OIF installation lock " + uuid.uuid4().hex).encode("ascii")
    try:
        with path.open("xb") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise ValueError("Installation lock exists. Reconcile its owner and partial backups before retrying.") from exc
    try:
        yield
    finally:
        if fingerprint(path) == digest(token):
            path.unlink()


def is_global_like(path: Path) -> bool:
    try:
        relative = comparison_identity(path).relative_to(comparison_identity(Path.home()))
    except ValueError:
        relative = None
    return bool({p.lower() for p in path.parts} & {".codex", ".config", ".agents"}) or (relative is not None and len(relative.parts) <= 1)


def planned_files(adapter: str, mode: str = "minimal") -> dict[str, Path]:
    if adapter == "generic":
        result = {"objective-integrity-adapter.md": ROOT / "adapters/generic/system-developer-adapter.md",
                  "objective-integrity/objective-contract.md": ROOT / "templates/objective-contract.md"}
    elif adapter == "codex":
        result = {"AGENTS.md": ROOT / "adapters/codex/AGENTS.md"}
        skill = ROOT / ".agents/skills/objective-integrity"
        result.update({".agents/skills/objective-integrity/" + p.relative_to(skill).as_posix(): p
                       for p in skill.rglob("*") if p.is_file() and not set(p.parts) & OMIT})
    elif adapter == "skill-book":
        result = {"objective-integrity/" + name: ROOT / "templates" / name for name in
                  ("skill-selection-receipt.md", "skill-lifecycle-record.md", "exact-action-preflight.md")}
        result["objective-integrity/skill-book.md"] = ROOT / "docs/skill-book.md"
    else:
        raise ValueError(f"Unknown adapter: {adapter}")
    if mode == "complete":
        for directory in ("runtime", "tools", "docs", "framework", "profiles", "templates", "schemas", "examples", "adapters", ".agents", "tests", "evals"):
            base = ROOT / directory
            if not base.is_dir():
                raise ValueError(f"Incomplete distribution: missing {directory}")
            for path in sorted(base.rglob("*")):
                if set(path.relative_to(ROOT).parts) & OMIT:
                    continue
                no_links(path)
                if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
                    result[".oif/" + path.relative_to(ROOT).as_posix()] = path
        for name in ("README.md", "LICENSE", "PRIVACY.md", "PROVENANCE.md", "CONTRIBUTING.md"):
            result[".oif/" + name] = ROOT / name
    return result


def save_manifest(backup: Path, manifest: dict) -> None:
    atomic_write(member(backup, "manifest.json"), (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))


def update_baseline(destination: Path) -> tuple[dict[str, str], dict]:
    """Resolve the latest active complete install, never infer ownership by location."""
    parent = member(destination, BACKUPS)
    if not parent.is_dir():
        raise ValueError("Update requires a prior complete installation and its retained backup manifest.")
    for backup in sorted(parent.iterdir(), reverse=True):
        no_links(backup)
        if not backup.is_dir():
            continue
        path = member(backup, "manifest.json")
        raw = path.read_bytes()
        view = json.loads(raw)
        if view.get("schema_version") not in {"oif-install-v2", SCHEMA}:
            raise ValueError("Unrecognized installation history; reconcile it before updating.")
        if comparison_identity(absolute(view["destination"])) != comparison_identity(destination):
            raise ValueError("Prior installation destination binding differs.")
        if view.get("mode") != "complete":
            continue
        if view.get("state") == "ROLLED_BACK":
            continue
        if view.get("state") != "APPLIED":
            raise ValueError("Prior complete installation has unresolved effects; reconcile its backup before updating.")
        owned = {}
        seen = set()
        for entry in view["entries"]:
            relative = entry["path"]
            member(destination, relative)
            if relative in seen:
                raise ValueError("Duplicate prior installation member.")
            seen.add(relative)
            if not relative.startswith(".oif/"):
                continue
            installed = entry["installed_sha256"]
            if installed is None and entry.get("operation") == "delete":
                continue
            if not isinstance(installed, str) or len(installed) != 64:
                raise ValueError("Invalid prior package member identity.")
            if fingerprint(member(destination, relative)) != installed:
                raise ValueError(f"Update conflict: {relative} changed or is missing. Preserve local edits and reconcile before updating.")
            owned[relative] = installed
        if not owned:
            raise ValueError("Prior complete installation contains no owned package members.")
        return owned, {"path": path.relative_to(destination).as_posix(), "sha256": digest(raw)}
    raise ValueError("No active complete installation is available for update.")


def rollback(backup: Path) -> int:
    backup = absolute(backup)
    if backup.parent.name != BACKUPS:
        raise ValueError("Rollback needs a generated project-local backup directory.")
    manifest = json.loads(member(backup, "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") not in {"oif-install-v2", SCHEMA}:
        raise ValueError("This backup lacks a supported integrity inventory. Review and restore legacy backups manually.")
    destination = absolute(manifest["destination"])
    if comparison_identity(destination) != comparison_identity(backup.parent.parent):
        raise ValueError("Backup destination binding differs from its physical project location.")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Backup inventory is empty or malformed.")
    if len({e["path"] for e in entries}) != len(entries):
        raise ValueError("Duplicate backup member.")
    with install_lock(destination):
        restore = []
        for entry in entries:
            target = member(destination, entry["path"])
            if target == destination / LOCK or BACKUPS in target.relative_to(destination).parts:
                raise ValueError("Backup member may not target installation control files.")
            before, installed = entry["before_sha256"], entry["installed_sha256"]
            deletion = (manifest["schema_version"] == SCHEMA and entry.get("operation") == "delete"
                        and entry["path"].startswith(".oif/") and isinstance(before, str) and len(before) == 64)
            if not deletion and (not isinstance(installed, str) or len(installed) != 64):
                raise ValueError("Invalid installed member identity.")
            if deletion and installed is not None:
                raise ValueError("Deleted member must have an absent installed identity.")
            data = None
            if before is not None:
                data = member(backup / "files", entry["path"]).read_bytes()
                if digest(data) != before:
                    raise ValueError(f"Backup integrity mismatch: {entry['path']}")
            current = fingerprint(target)
            if current not in (before, installed):
                raise ValueError(f"Rollback conflict: {entry['path']} changed after installation. No recovery files were changed.")
            restore.append((target, current, before, data))
        attempt = {"id": uuid.uuid4().hex, "state": "ROLLING_BACK", "restored": [], "already_restored": []}
        manifest.setdefault("rollback_attempts", []).append(attempt)
        manifest["state"] = "ROLLING_BACK"
        save_manifest(backup, manifest)
        try:
            for target, expected, before, data in reversed(restore):
                relative = target.relative_to(destination).as_posix()
                if fingerprint(target) != expected:
                    raise ValueError(f"Concurrent rollback change: {target}. Reconcile the retained backup.")
                if expected == before:
                    attempt["already_restored"].append(relative)
                    save_manifest(backup, manifest)
                    continue
                attempt["pending"] = relative
                save_manifest(backup, manifest)
                if data is None:
                    target.unlink()
                else:
                    atomic_write(target, data)
                if fingerprint(target) != before:
                    raise ValueError(f"Rollback readback mismatch: {target}")
                attempt["restored"].append(relative)
                attempt.pop("pending", None)
                save_manifest(backup, manifest)
            manifest["state"] = attempt["state"] = "ROLLED_BACK"
            save_manifest(backup, manifest)
        except BaseException as fault:
            manifest["state"] = attempt["state"] = "ROLLBACK_INTERRUPTED"
            attempt["first_fault"] = f"{type(fault).__name__}: {fault}"
            manifest.setdefault("rollback_first_fault", attempt["first_fault"])
            try:
                save_manifest(backup, manifest)
            except Exception as recovery_fault:
                # The last durable pending frontier is retained if storage itself fails.
                print(f"Rollback recovery-record error: {recovery_fault}; original fault: {fault}; backup: {backup}", file=sys.stderr)
            raise
    print(f"rollback: restored {backup}")
    return 0


def plan_identity(destination: Path, adapter: str, mode: str, entries: list[dict], update_from: dict | None = None) -> str:
    plan = {"schema_version": "oif-preview-v1", "installer_sha256": digest(Path(__file__).read_bytes()),
            "path_identity_sha256": digest(Path(__file__).with_name("path_identity.py").read_bytes()),
            "distribution": str(comparison_identity(ROOT)), "destination": str(comparison_identity(destination)),
            "adapter": adapter, "mode": mode, "update_from": update_from,
            "entries": sorted(entries, key=lambda item: item["path"])}
    return digest(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def install(destination: Path, adapter: str, mode: str, apply: bool, expect_plan: str | None = None, update: bool = False) -> int:
    destination = absolute(destination)
    if update and mode != "complete":
        raise ValueError("--update requires --mode complete; it preserves top-level project guidance and records.")
    owned, update_from = update_baseline(destination) if update else ({}, None)
    payloads, entries = {}, []
    for relative, source in planned_files(adapter, mode).items():
        if update and not relative.startswith(".oif/"):
            continue
        no_links(source)
        data = source.read_bytes()
        target = member(destination, relative)
        if update and relative not in owned and fingerprint(target) is not None:
            raise ValueError(f"Update conflict: new package member {relative} would replace an unowned file.")
        entries.append({"path": relative, "before_sha256": fingerprint(target), "installed_sha256": digest(data)})
        payloads[relative] = data
    for relative in sorted(set(owned) - set(payloads)):
        entries.append({"path": relative, "before_sha256": owned[relative], "installed_sha256": None, "operation": "delete"})
    plan_sha256 = plan_identity(destination, adapter, mode, entries, update_from)
    if expect_plan is not None and (not apply or expect_plan.lower() != plan_sha256):
        raise ValueError("Preview plan differs from the current source or destination. Preview again; no destination files were changed.")
    print(f"adapter: {adapter}\npackage: {mode}\ndestination: {destination}\nmode: {'apply' if apply else 'dry-run'}")
    print(f"plan-sha256: {plan_sha256}\nbackup-parent: {destination / BACKUPS}")
    if apply:
        print("plan binding: " + ("matched prior preview" if expect_plan else "explicit current-state apply; no prior preview supplied"))
    print(f"created: {[e['path'] for e in entries if e['before_sha256'] is None]}")
    print(f"replaced: {[e['path'] for e in entries if e['before_sha256'] is not None]}")
    if update:
        print(f"removed obsolete owned members: {[e['path'] for e in entries if e.get('operation') == 'delete']}")
        print("update scope: .oif package only; top-level guidance, objective state and user settings are preserved")
    if not apply:
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    with install_lock(destination):
        if update_from and fingerprint(member(destination, update_from["path"])) != update_from["sha256"]:
            raise ValueError("Prior installation manifest changed since preview.")
        for entry in entries:
            if fingerprint(member(destination, entry["path"])) != entry["before_sha256"]:
                raise ValueError(f"Destination changed since preview: {entry['path']}")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        backup = member(destination, BACKUPS + "/" + stamp)
        backup.mkdir(parents=True, exist_ok=False)
        manifest = {"schema_version": SCHEMA, "destination": str(destination), "adapter": adapter,
                    "mode": mode, "plan_sha256": plan_sha256, "prior_preview_matched": expect_plan is not None,
                    "state": "PREPARING", "entries": entries, "applied": [], "update_from": update_from}
        for entry in entries:
            if entry["before_sha256"] is not None:
                data = member(destination, entry["path"]).read_bytes()
                if digest(data) != entry["before_sha256"]:
                    raise ValueError(f"Source changed during backup: {entry['path']}")
                atomic_write(member(backup / "files", entry["path"]), data)
        manifest["state"] = "PREPARED"
        save_manifest(backup, manifest)
        print(f"backup: {backup}", flush=True)
        try:
            for entry in entries:
                target = member(destination, entry["path"])
                if fingerprint(target) != entry["before_sha256"]:
                    raise ValueError(f"Concurrent destination change: {entry['path']}")
                manifest["state"], manifest["pending"] = "APPLYING", entry["path"]
                save_manifest(backup, manifest)
                if entry.get("operation") == "delete":
                    target.unlink()
                else:
                    atomic_write(target, payloads[entry["path"]])
                if fingerprint(target) != entry["installed_sha256"]:
                    raise ValueError(f"Installed readback mismatch: {entry['path']}")
                manifest["applied"].append(entry["path"])
                manifest.pop("pending", None)
            manifest["state"] = "APPLIED"
            save_manifest(backup, manifest)
        except BaseException as fault:
            manifest["state"] = "INTERRUPTED"
            manifest["first_fault"] = f"{type(fault).__name__}: {fault}"
            try:
                save_manifest(backup, manifest)
            except Exception as recovery_fault:
                print(f"Recovery record error: {recovery_fault}; prior backup retained at {backup}", file=sys.stderr)
            raise
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview or install project-local Objective Integrity Framework files.")
    parser.add_argument("--destination", help="Explicit project destination.")
    parser.add_argument("--adapter", choices=["generic", "codex", "skill-book"], default="generic")
    parser.add_argument("--mode", choices=["minimal", "complete"], default="minimal", help="Complete includes runtime and documentation in .oif/.")
    parser.add_argument("--apply", action="store_true", help="Write files. Default preview writes nothing.")
    parser.add_argument("--update", action="store_true", help="Update a known complete .oif package only; preserve project guidance/settings and refuse locally modified package members.")
    parser.add_argument("--expect-plan", help="On apply, require the plan-sha256 printed by a prior preview.")
    parser.add_argument("--allow-global", action="store_true", help="Acknowledge a global-looking destination; unnecessary for ordinary project adoption.")
    parser.add_argument("--rollback", help="Recover from a generated v2/v3 backup; refuse later user edits.")
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            if args.destination or args.apply or args.expect_plan or args.update:
                parser.error("--rollback cannot be combined with --destination, --apply, --expect-plan or --update")
            return rollback(Path(args.rollback))
        if not args.destination:
            parser.error("--destination is required unless --rollback is used")
        destination = absolute(args.destination)
        if is_global_like(destination) and not args.allow_global:
            raise ValueError("Refusing global-looking destination without --allow-global.")
        if within(destination, ROOT) or within(ROOT, destination):
            raise ValueError("Destination must be separate from the framework distribution.")
        return install(destination, args.adapter, args.mode, args.apply, args.expect_plan, args.update)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
