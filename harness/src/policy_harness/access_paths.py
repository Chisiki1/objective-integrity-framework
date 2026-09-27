"""Filesystem paths selected by the controller's current access mode."""
import os
from pathlib import Path

from .capabilities import check_node, confined, relative_path
from .models import PolicyError


def operation_path(workspace: Path, value: str, *, full_access=False,
                   allow_root=False, allow_missing=False) -> Path:
    if not full_access:
        return confined(workspace, value, allow_root=allow_root, allow_missing=allow_missing)
    if not isinstance(value, str) or not value or '\x00' in value:
        raise PolicyError('PATH_INVALID: specify a file or directory')
    # Normal files only. Reject Windows device/stream aliases even with full access.
    supplied = Path(value)
    if supplied.drive and not supplied.is_absolute():
        raise PolicyError('PATH_INVALID: use a complete absolute path')
    candidate = Path(os.path.abspath(workspace / supplied))
    if os.name == 'nt':
        if str(candidate).startswith(('\\\\?\\', '\\\\.\\')):
            raise PolicyError('PATH_INVALID: device paths are not files')
        for part in candidate.parts[1:]:
            relative_path(part)
    for node in [*reversed(candidate.parents), candidate]:
        try:
            check_node(node)
        except FileNotFoundError:
            if not allow_missing:
                raise
    return candidate


def display_path(workspace: Path, path: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.as_posix()


def artifact_path(workspace: Path, value: str, *, full_access=False) -> str:
    path = Path(value)
    if path.is_absolute():
        try:
            return path.relative_to(workspace).as_posix()
        except ValueError:
            if full_access:
                return path.as_posix()
            raise
    return value
