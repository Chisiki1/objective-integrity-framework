#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from public_identifiers import PublicIdentifiers


DEFAULT_EXCLUDES = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "work"}
# A narrow language exception, not an exclusion from privacy or secret checks.
JAPANESE_EXPLANATIONS = {"docs/ja/README.md"}
GENERIC_PATTERNS = {
    "windows_home_path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
    "drive_root_path": re.compile(r"(?<![A-Za-z])[A-Za-z]:\\(?!\\)"),
    "long_hex_identifier": re.compile(r"\b[0-9a-fA-F]{40,}\b"),
    "uuid_identifier": re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "openai_token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "japanese_text": re.compile(r"[\u3040-\u30ff\u3400-\u9fff]"),
}


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDES]
        for name in filenames:
            yield Path(dirpath) / name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a public candidate for private or non-public residue.")
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--extra-literal", action="append", default=[], help="Additional literal string to reject.")
    parser.add_argument("--allow-email", action="store_true", help="Allow email-shaped text for projects that intentionally publish contact addresses.")
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    try:
        public = PublicIdentifiers(root)
    except (OSError, ValueError) as error:
        print(f"privacy_scan: FAIL: {error}")
        return 1
    findings: list[str] = []
    patterns = dict(GENERIC_PATTERNS)
    if args.allow_email:
        patterns.pop("email", None)

    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        for label, pattern in patterns.items():
            for match in pattern.finditer(rel):
                findings.append(f"{rel}: path_{label}: {match.group(0)[:80]}")
        for literal in args.extra_literal:
            if literal and literal in rel:
                findings.append(f"{rel}: path_extra_literal: {literal}")
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in patterns.items():
            if label == "japanese_text" and rel in JAPANESE_EXPLANATIONS:
                continue
            for match in pattern.finditer(text):
                if public.permits(rel, raw, label, match.group(0)):
                    continue
                findings.append(f"{rel}: {label}: {match.group(0)[:80]}")
        for literal in args.extra_literal:
            if literal and literal in text:
                findings.append(f"{rel}: extra_literal: {literal}")

    if findings:
        print("\n".join(findings))
        return 1
    print("privacy_scan: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
