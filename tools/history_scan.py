#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


PATTERNS = {
    "private_path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
    "uuid": re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    "long_hex": re.compile(r"\b[0-9a-fA-F]{40,}\b"),
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

    commits = git(root, "rev-list", "--all")
    if commits.returncode != 0:
        print("history_scan: FAIL")
        print(commits.stderr.strip() or "git rev-list failed")
        return 1
    for commit in [line for line in commits.stdout.splitlines() if line]:
        files = git(root, "ls-tree", "-r", "--name-only", commit)
        if files.returncode != 0:
            findings.append(f"{commit}: tree cannot be listed")
            continue
        for rel in [line for line in files.stdout.splitlines() if line]:
            show = git(root, "show", f"{commit}:{rel}")
            if show.returncode != 0:
                findings.append(f"{commit}:{rel}: tracked file cannot be read")
                continue
            for label, pattern in PATTERNS.items():
                for match in pattern.finditer(show.stdout):
                    findings.append(f"{commit}:{rel}: {label}: {match.group(0)[:80]}")

    if findings:
        print("\n".join(findings))
        return 1
    print("history_scan: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
