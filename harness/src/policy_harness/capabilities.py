"""Content-addressed recipes; policy approval belongs to the trusted Engine.

The worker never receives this registry's directory or an unrestricted command
runner. Registration is an effect and must follow the ordinary policy gate.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from .models import PolicyError, StrictModel, now


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


@contextmanager
def process_lock(path: Path):
    """Nonblocking OS lease, released on process death (no timed polling)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise PolicyError('WORKSPACE_BUSY: another controller operation owns this workspace') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def relative_path(value: str, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or '\x00' in value:
        raise PolicyError('PATH_INVALID: a task-relative path is required')
    value = value.replace('\\', '/')
    if allow_root and value in ('', '.'):
        return '.'
    if not value or PureWindowsPath(value).drive or PurePosixPath(value).is_absolute():
        raise PolicyError('PATH_ESCAPE: absolute paths are forbidden')
    parts = value.split('/')
    reserved = re.compile(r'^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)', re.I)
    if any(p in ('', '.', '..') or ':' in p or p.endswith((' ', '.'))
           or reserved.match(p) or any(ord(c) < 32 for c in p) for p in parts):
        raise PolicyError('PATH_INVALID: traversal, aliases and device paths are forbidden')
    return '/'.join(parts)


def check_node(path: Path) -> None:
    """lstat, never follow a symlink/reparse point supplied by a worker."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise PolicyError(f'PATH_LINK: {path.name}')
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise PolicyError(f'PATH_SPECIAL: {path.name}')
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise PolicyError(f'PATH_HARDLINK: {path.name}')


def confined(workspace: Path, value: str, *, allow_root: bool = False,
             allow_missing: bool = False) -> Path:
    rel = relative_path(value, allow_root=allow_root)
    check_node(workspace)
    current = workspace
    for part in (() if rel == '.' else rel.split('/')):
        current = current / part
        try:
            check_node(current)
        except FileNotFoundError:
            if not allow_missing:
                raise
    # Lexical confinement plus every existing ancestor checked above. resolve()
    # alone would conceal the fact that an in-workspace symlink was traversed.
    current.resolve().relative_to(workspace.resolve())
    return current


def atomic_json(path: Path, value: object, *, exclusive: bool = False) -> None:
    data = canonical(value)
    if exclusive:
        with path.open('xb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        return
    temporary = path.with_name('.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Resources(StrictModel):
    timeout_seconds: float = Field(gt=0)
    cpus: float = Field(gt=0)
    memory_mb: int = Field(ge=32)
    pids_limit: int = Field(ge=8)
    output_bytes: int = Field(ge=1024)
    tmp_mb: int = Field(ge=1)
    rationale: str = Field(min_length=1)

    @field_validator('timeout_seconds', 'cpus')
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError('resource limits must be finite')
        return value


class Recipe(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    recipe: Literal['python', 'pytest']
    entrypoint: str | None = None
    files: dict[str, str] = Field(min_length=1)
    arguments: list[str] = Field(default_factory=list)
    declared_effects: list[str] = Field(min_length=1)
    resources: Resources
    candidate_sha256: str | None = None


def describe(workspace: Path, args: dict, environment: dict) -> dict:
    try:
        recipe = Recipe.model_validate(args)
    except Exception as error:
        raise PolicyError(f'CAPABILITY_SCHEMA: {error}') from error
    normalized_files: dict[str, str] = {}
    sources: list[dict] = []
    for name, expected in recipe.files.items():
        rel = relative_path(name)
        if not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
            raise PolicyError('SOURCE_HASH_REQUIRED: each declared file needs SHA256')
        path = confined(workspace, rel)
        if not path.is_file():
            raise PolicyError('SOURCE_NOT_FILE: ' + rel)
        data = path.read_bytes()
        actual = digest(data)
        if actual != expected.lower():
            raise PolicyError('SOURCE_CHANGED: ' + rel)
        normalized_files[rel] = actual
        try:
            source = data.decode('utf-8-sig')
        except UnicodeDecodeError as error:
            raise PolicyError('SOURCE_NOT_UTF8: ' + rel) from error
        sources.append({'path': rel, 'sha256': actual, 'bytes': len(data), 'text': source})
    if any(not isinstance(arg, str) or '\x00' in arg for arg in recipe.arguments):
        raise PolicyError('ARGV_INVALID')
    if recipe.recipe == 'python':
        entrypoint = relative_path(recipe.entrypoint or '')
        if entrypoint not in normalized_files or not entrypoint.endswith('.py'):
            raise PolicyError('ENTRYPOINT_UNBOUND: a declared Python file is required')
        argv = ['-I', '-B', '/workspace/' + entrypoint, *recipe.arguments]
    else:
        if recipe.entrypoint is not None:
            raise PolicyError('PYTEST_ENTRYPOINT: use arguments and files')
        if not environment.get('pytest'):
            raise PolicyError('PREPARATION_REQUIRED: prepare_environment(include_pytest=True)')
        # Options are reviewed as part of the exact recipe; python's entrypoint
        # remains fixed. Test/config/plugin source must be in the bound manifest.
        argv = ['-I', '-B', '-m', 'pytest', '-p', 'no:cacheprovider', *recipe.arguments]
    if not environment.get('image_id'):
        raise PolicyError('PREPARATION_REQUIRED: prepare the isolated environment')
    candidate = recipe.model_dump(exclude={'candidate_sha256'})
    candidate['files'] = normalized_files
    candidate['workspace'] = str(workspace)
    candidate['image_id'] = environment['image_id']
    candidate['executable'] = '/usr/local/bin/python'
    candidate['argv'] = argv
    candidate_hash = digest(canonical(candidate))
    if recipe.candidate_sha256 and recipe.candidate_sha256.lower() != candidate_hash:
        raise PolicyError('CANDIDATE_CHANGED: prepare and review the current candidate')
    return {'candidate': candidate, 'candidate_sha256': candidate_hash,
            'sources': sources, 'source_complete': True,
            'proof_ceiling': 'source identity and bounded recipe; semantic approval is Engine-owned'}


class CapabilityRegistry:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        check_node(directory)

    def register(self, description: dict, operation_id: str) -> dict:
        candidate = description['candidate']
        identity = digest(canonical(candidate))
        record = {'id': identity, 'candidate': candidate,
                  'registration_operation_id': operation_id, 'registered_at': now()}
        path = self.directory / (identity + '.json')
        try:
            atomic_json(path, record, exclusive=True)
        except FileExistsError:
            existing = self.get(identity)
            if existing['candidate'] != candidate:
                raise PolicyError('CAPABILITY_ID_COLLISION')
            return existing
        return record

    def get(self, identity: str) -> dict:
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
            raise PolicyError('PREPARATION_REQUIRED: unknown capability ID')
        path = self.directory / (identity + '.json')
        try:
            check_node(path)
            record = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError as error:
            raise PolicyError('PREPARATION_REQUIRED: register the requested capability') from error
        if record.get('id') != identity or digest(canonical(record.get('candidate'))) != identity:
            raise PolicyError('CAPABILITY_CORRUPT')
        return record
