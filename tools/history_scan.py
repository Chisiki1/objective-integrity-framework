#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


PRIVATE_TERMS = [
    "PC" + "_" + "User",
    "auto" + "dice",
    "Parallax" + "Quant",
    "Win" + "VM",
    "M" + "T5",
    "codex-workflow" + "-emergency-backup",
]

PATTERNS = {
    "private_path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
    "uuid": re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    "long_hex": re.compile(r"\b[0-9a-fA-F]{40,}\b"),
    "private_terms": re.compile(r"\b(?:" + "|".join(re.escape(term) for term in PRIVATE_TERMS) + r")\b"),
    "token": re.compile(r"\b(?:ghp|gho|github_pat|sk-)[A-Za-z0-9_-]{20,}\b"),
}


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan Git history metadata and tracked blobs for public-safety residue.")
    parser.add_argument("path", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.path).resolve()

    if not (root / ".git").exists():
        print("history_scan: SKIP (not a Git repository)")
        return 0

    head = git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        print("history_scan: FAIL")
        print("git-history: missing HEAD; commit the release candidate before using history_scan as a release check")
        return 1

    findings: list[str] = []
    log = git(root, "log", "--all", "--format=%an%n%ae%n%cn%n%ce%n%s%n%b")
    if log.returncode != 0:
        print("history_scan: FAIL")
        print(log.stderr.strip() or "git log failed")
        return 1
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(log.stdout):
            findings.append(f"git-log: {label}: {match.group(0)[:80]}")

    files = git(root, "ls-files")
    if files.returncode != 0:
        print("history_scan: FAIL")
        print(files.stderr.strip() or "git ls-files failed")
        return 1
    for rel in [line for line in files.stdout.splitlines() if line]:
        show = git(root, "show", f"HEAD:{rel}")
        if show.returncode != 0:
            findings.append(f"{rel}: tracked file cannot be read from HEAD")
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(show.stdout):
                findings.append(f"{rel}: {label}: {match.group(0)[:80]}")

    if findings:
        print("\n".join(findings))
        return 1
    print("history_scan: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
