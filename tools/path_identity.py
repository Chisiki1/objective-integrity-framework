"""Physical comparison identities, separate from the spelling used for file I/O."""
from __future__ import annotations

import os
from pathlib import Path


def _io_path(path: Path) -> Path:
    """Use native extended I/O internally without changing caller-owned paths."""
    text = str(path)
    if os.name != "nt":
        return path
    if text.startswith("\\\\.\\"):
        raise ValueError("Device namespace paths are not supported project destinations.")
    if text.startswith("\\\\?\\"):
        return path
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def _comparison_spelling(path: Path) -> Path:
    text = str(path)
    if os.name == "nt":
        if text[:8].casefold() == "\\\\?\\unc\\":
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
            if len(text) < 3 or not text[0].isalpha() or text[1:3] != ":\\":
                raise ValueError("Only ordinary drive and UNC project paths are supported.")
    return Path(os.path.normcase(os.path.normpath(text)))


def comparison_identity(path: Path) -> Path:
    """Resolve an existing ancestor physically, then append a normalized suffix.

    Callers keep their original path for I/O and perform their existing reparse
    checks separately. This identity is not a lock against concurrent retargeting.
    """
    probe = _io_path(Path(os.path.abspath(path)))
    suffix = []
    while not probe.exists():
        if probe.parent == probe:
            raise ValueError("No existing ancestor for project path.")
        suffix.append(probe.name)
        probe = probe.parent
    native = probe.resolve(strict=True)
    canonical = _comparison_spelling(native)
    if not os.path.samefile(probe, _io_path(canonical)):
        raise ValueError("Physical path identity changed during comparison.")
    return _comparison_spelling(canonical.joinpath(*reversed(suffix)))


def within(path: Path, root: Path) -> bool:
    return comparison_identity(path).is_relative_to(comparison_identity(root))
