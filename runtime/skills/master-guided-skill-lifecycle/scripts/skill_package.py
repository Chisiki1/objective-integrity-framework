#!/usr/bin/env python3
"""Shared, explicit-path Skill package I/O. No discovery, activation or execution.

Callers preflight first, capture exact source bytes, copy into an owned empty
stage, recheck sources, and publish without replacing an existing destination.
Path checks are not an OS sandbox against a concurrent hostile namespace owner.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HEX = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class PackageError(ValueError):
    """A rejected identity, namespace or package contract."""


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha_value(value: Any) -> str:
    return sha_bytes(canonical(value))


def require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise PackageError(f"{label} must be a SHA-256 hex string")
    return value.upper()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PackageError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(value: bytes | str) -> Any:
    def reject_constant(item: str) -> Any:
        raise PackageError(f"non-finite JSON constant: {item}")
    return json.loads(value, object_pairs_hook=_object, parse_constant=reject_constant)


def relative_name(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise PackageError("member path must be a nonempty relative POSIX path")
    for part in value.split("/"):
        if (not part or part in {".", ".."} or part[-1:] in {".", " "}
                or any(ord(char) < 32 or char in ':*?"<>|' for char in part)
                or RESERVED.match(part)):
            raise PackageError(f"unsafe or nonportable member path: {value!r}")
    return value


def normalize_members(value: Any, *, require_skill: bool = True) -> list[dict[str, str]]:
    if not isinstance(value, list) or (require_skill and not value):
        raise PackageError("members must be an explicit nonempty array")
    members: list[dict[str, str]] = []
    folded: set[str] = set()
    spellings: dict[str, str] = {}
    for row in value:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise PackageError("each member has exactly path and sha256")
        name = relative_name(row["path"])
        if name.casefold() in folded:
            raise PackageError(f"duplicate/case-alias member: {name}")
        folded.add(name.casefold())
        parts = name.split("/")
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            key = prefix.casefold()
            if key in spellings and spellings[key] != prefix:
                raise PackageError(f"case-alias path component: {prefix}")
            spellings[key] = prefix
        members.append({"path": name, "sha256": require_hash(row["sha256"], name)})
    for name in folded:
        if any("/".join(name.split("/")[:end]) in folded for end in range(1, len(name.split("/")))):
            raise PackageError(f"member is both file and directory: {name}")
    if require_skill and "SKILL.md" not in {row["path"] for row in members}:
        raise PackageError("package requires exactly named SKILL.md")
    return sorted(members, key=lambda row: row["path"])


def package_manifest(members: Any) -> str:
    return sha_value(normalize_members(members))


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def intersects(left: Path, right: Path) -> bool:
    return is_under(left, right) or is_under(right, left)


def identity(info: os.stat_result) -> tuple[int, int]:
    if info.st_ino == 0:
        raise PackageError("filesystem object identity unavailable; cannot assert ownership")
    return info.st_dev, info.st_ino


def _check_stat(path: Path, info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE:
        raise PackageError(f"symlink/reparse component rejected: {path}")
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise PackageError(f"hardlinked file rejected: {path}")
    elif not stat.S_ISDIR(info.st_mode):
        raise PackageError(f"non-regular filesystem object rejected: {path}")


def checked_path(value: str | Path, *, kind: str, absent: bool = False) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\0" in str(value):
        raise PackageError("an explicit absolute path is required")
    raw = str(value)
    path = Path(raw)
    if not path.is_absolute() or any(part in {".", ".."} for part in re.split(r"[\\/]", raw)):
        raise PackageError(f"absolute path without dot traversal required: {raw!r}")
    # Reject device/extended namespaces and alternate data streams before resolve.
    if raw.startswith(("\\\\?\\", "\\\\.\\")) or any(":" in part for part in path.parts[1:]):
        raise PackageError(f"device namespace or stream path rejected: {raw!r}")
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if item == path and absent:
                break
            raise PackageError(f"path component missing: {item}") from None
        _check_stat(item, info)
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise PackageError(f"non-directory ancestor: {item}")
    if absent:
        if os.path.lexists(path):
            raise PackageError(f"destination already exists: {path}")
        # Also reject portable case aliases on a case-sensitive host.
        if any(entry.name.casefold() == path.name.casefold() for entry in path.parent.iterdir()):
            raise PackageError(f"case-alias destination exists: {path}")
    else:
        info = path.lstat()
        if kind == "dir" and not stat.S_ISDIR(info.st_mode):
            raise PackageError(f"directory required: {path}")
        if kind == "file" and not stat.S_ISREG(info.st_mode):
            raise PackageError(f"regular file required: {path}")
    return path.resolve(strict=not absent)


@dataclass(frozen=True)
class FileBytes:
    path: Path
    data: bytes
    object_id: tuple[int, int]
    mode: int

    @property
    def sha256(self) -> str:
        return sha_bytes(self.data)


def read_file(value: str | Path) -> FileBytes:
    path = checked_path(value, kind="file")
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _check_stat(path, opened)
        if identity(opened) != identity(before):
            raise PackageError(f"file changed while opening: {path}")
        data = stream.read()
        after = os.fstat(stream.fileno())
        if ((opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns, identity(opened))
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, identity(after))):
            raise PackageError(f"file changed while reading: {path}")
    checked_path(path, kind="file")
    if identity(path.lstat()) != identity(opened) or len(data) != opened.st_size:
        raise PackageError(f"file identity changed after reading: {path}")
    return FileBytes(path, data, identity(opened), stat.S_IMODE(opened.st_mode) & 0o777)


def assert_file_unchanged(source: FileBytes) -> None:
    current = read_file(source.path)
    if current.object_id != source.object_id or current.data != source.data:
        raise PackageError(f"source-byte/identity CAS mismatch: {source.path}")


def read_json_file(path: str | Path) -> tuple[Any, FileBytes]:
    source = read_file(path)
    return strict_json(source.data), source


def walk_files(root: Path, *, exclude: Path | None = None) -> dict[str, Path]:
    root = checked_path(root, kind="dir")
    files: dict[str, Path] = {}
    seen: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        checked_path(directory, kind="dir")
        for item in directory.iterdir():
            info = item.lstat()
            _check_stat(item, info)
            name = relative_name(item.relative_to(root).as_posix())
            if name.casefold() in seen:
                raise PackageError(f"case-alias tree entry: {name}")
            seen.add(name.casefold())
            if item == exclude:
                if not stat.S_ISDIR(info.st_mode):
                    raise PackageError("excluded staging object is not a plain directory")
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append(item)
            else:
                files[name] = item
    return files


def tree_members(root: Path, *, exclude: Path | None = None) -> list[dict[str, str]]:
    paths = walk_files(root, exclude=exclude)
    members = [{"path": name, "sha256": read_file(path).sha256} for name, path in paths.items()]
    if set(walk_files(root, exclude=exclude)) != set(paths):
        raise PackageError(f"tree member set changed during read: {root}")
    return normalize_members(members, require_skill=False)


def root_manifest_from_members(members: list[dict[str, str]]) -> str:
    # Preserve v1 candidate-root CAS serialization, distinct from package identity.
    return sha_bytes("\n".join(f"{row['path']}\t{row['sha256']}" for row in sorted(members, key=lambda row: row["path"])).encode("utf-8"))


def root_manifest(root: Path, *, exclude: Path | None = None) -> str:
    return root_manifest_from_members(tree_members(root, exclude=exclude))


@dataclass(frozen=True)
class PackageBytes:
    root: Path
    members: list[dict[str, str]]
    files: dict[str, FileBytes]
    exact_tree: bool

    @property
    def manifest_sha256(self) -> str:
        return package_manifest(self.members)


def capture_package(root: str | Path, members: Any, *, expected_manifest: str | None = None,
                    legacy_artifact: str | Path | None = None) -> PackageBytes:
    root = checked_path(root, kind="dir")
    normalized = normalize_members(members)
    if expected_manifest is not None and package_manifest(normalized) != require_hash(expected_manifest, "package manifest"):
        raise PackageError("package manifest CAS mismatch")
    exact_tree = legacy_artifact is None
    expected = {row["path"] for row in normalized}
    if exact_tree:
        paths = walk_files(root)
        if set(paths) != expected:
            raise PackageError(f"package member set mismatch; missing={sorted(expected-set(paths))}, extra={sorted(set(paths)-expected)}")
    else:
        source = checked_path(legacy_artifact, kind="file")
        if source.parent != root or source.name.lower() != "skill.md" or expected != {"SKILL.md"}:
            raise PackageError("legacy source must be a single SKILL.md artifact")
        paths = {"SKILL.md": source}
    files = {name: read_file(path) for name, path in paths.items()}
    for row in normalized:
        if files[row["path"]].sha256 != row["sha256"]:
            raise PackageError(f"source-byte CAS mismatch: {row['path']}")
    if exact_tree and set(walk_files(root)) != expected:
        raise PackageError("package member set changed while reading")
    return PackageBytes(root, normalized, files, exact_tree)


def assert_package_unchanged(source: PackageBytes) -> None:
    if source.exact_tree and set(walk_files(source.root)) != set(source.files):
        raise PackageError("source package extra/missing member before publication")
    for item in source.files.values():
        assert_file_unchanged(item)


def verify_tree(root: Path, members: Any, *, require_skill: bool = True) -> list[dict[str, str]]:
    expected = normalize_members(members, require_skill=require_skill)
    observed = tree_members(root)
    if observed != expected:
        raise PackageError(f"staged/committed exact member readback mismatch: {root}")
    return observed


def write_exclusive(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    checked_path(path, kind="file", absent=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        opened = os.fstat(stream.fileno())
        _check_stat(path, opened)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        os.chmod(path, mode & 0o777, follow_symlinks=False)
    current = read_file(path)
    if current.object_id != identity(opened) or current.data != data:
        raise PackageError(f"exclusive write readback mismatch: {path}")


def copy_package(source: PackageBytes, destination: Path, writes: list[str]) -> None:
    checked_path(destination, kind="dir")
    if any(destination.iterdir()):
        raise PackageError("package staging directory must be empty")
    for row in source.members:
        target = destination / row["path"]
        parent = destination
        for part in Path(row["path"]).parts[:-1]:
            parent = parent / part
            if not os.path.lexists(parent):
                parent.mkdir()
                writes.append(str(parent))
            checked_path(parent, kind="dir")
        # Append attempted target before a possibly partial write, not afterwards.
        writes.append(str(target))
        item = source.files[row["path"]]
        write_exclusive(target, item.data, mode=item.mode)
    verify_tree(destination, source.members)


def publish_capability() -> str:
    """Non-mutating availability check; no unsafe replace fallback."""
    if os.name == "nt":
        return "windows-rename-no-replace"
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        return "linux-renameat2-noreplace"
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        return "darwin-rename-excl"
    raise PackageError("atomic no-replace directory publication unavailable on this host; no writes performed")


def publish_directory(stage: Path, destination: Path) -> None:
    method = publish_capability()
    checked_path(stage, kind="dir")
    checked_path(destination, kind="dir", absent=True)
    if stage.parent != destination.parent:
        raise PackageError("stage and destination must have the same parent/filesystem")
    if method == "windows-rename-no-replace":
        os.rename(stage, destination)  # Windows fails if destination already exists.
        return
    library = ctypes.CDLL(None, use_errno=True)
    if method == "linux-renameat2-noreplace":
        action = library.renameat2
        action.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        action.restype = ctypes.c_int
        result = action(-100, os.fsencode(stage), -100, os.fsencode(destination), 1)
    else:
        action = library.renamex_np
        action.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        action.restype = ctypes.c_int
        result = action(os.fsencode(stage), os.fsencode(destination), 4)
    if result:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code), str(destination))


def same_object(path: Path, object_id: tuple[int, int] | None) -> bool:
    try:
        checked_path(path, kind="dir")
        return object_id is not None and identity(path.lstat()) == object_id
    except (OSError, ValueError):
        return False


def destination_readback(destination: Path | None) -> dict[str, Any]:
    if destination is None:
        return {"destination_exists": None, "manifest_sha256": None, "status": "not-observed"}
    try:
        exists = os.path.lexists(destination)
        return {"destination_exists": exists, "manifest_sha256": root_manifest(destination) if exists else None}
    except (OSError, ValueError) as exc:
        return {"destination_exists": os.path.lexists(destination), "manifest_sha256": None, "readback_error": str(exc)}


def failure_record(primary: str, state: str, stage: Path | None, destination: Path | None,
                   writes: list[str], *, stage_id: tuple[int, int] | None = None) -> dict[str, Any]:
    cleanup: list[str] = []
    rollback = "not-needed"
    if state == "COMMITTING" and destination is not None:
        if same_object(destination, stage_id):
            state = "COMMITTED"
        elif stage is not None and not os.path.lexists(stage):
            state = "COMMIT_UNCERTAIN"
    committed = state in {"COMMITTED", "VERIFIED", "COMMIT_UNCERTAIN"}
    if committed:
        rollback = "committed-destination-preserved-pending-explicit-recovery"
    elif stage is not None and os.path.lexists(stage):
        try:
            if not same_object(stage, stage_id):
                raise PackageError("staging ownership changed; cleanup withheld")
            walk_files(stage)  # Never recursively clean a linked/unowned tree.
            shutil.rmtree(stage)
            rollback = "precommit-staging-removed"
        except (OSError, ValueError) as exc:
            rollback = f"precommit-cleanup-failed:{exc}"
            cleanup.append(rollback)
    return {"transaction_state": state, "primary_fault": primary, "cleanup_failures": cleanup,
            "rollback_result": rollback, "effect_state": "unknown" if committed or cleanup else ("partial" if writes else "none"),
            "readback": destination_readback(destination), "writes_performed": writes,
            "stage_path": str(stage) if stage is not None else None,
            "stage_exists": os.path.lexists(stage) if stage is not None else False}
