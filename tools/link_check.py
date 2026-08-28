#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
EXCLUDES = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def iter_markdown(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDES]
        for name in filenames:
            if name.endswith(".md"):
                yield Path(dirpath) / name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check local Markdown links.")
    parser.add_argument("path", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.path).resolve()
    failures: list[str] = []

    for path in iter_markdown(root):
        text = path.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            href = match.group(1).split("#", 1)[0]
            if not href or href.startswith(("http://", "https://", "mailto:")):
                continue
            target = (path.parent / href).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                failures.append(f"{path.relative_to(root)}: link escapes repository: {match.group(1)}")
                continue
            if not target.exists():
                failures.append(f"{path.relative_to(root)}: missing target: {match.group(1)}")

    if failures:
        print("link_check: FAIL")
        print("\n".join(failures))
        return 1
    print("link_check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
