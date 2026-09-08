#!/usr/bin/env python3
"""Run static package checks or observable runtime regressions independently."""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from path_identity import comparison_identity, within

ROOT = Path(__file__).resolve().parents[1]


def prepare_temporary_parent(value: str | None) -> Path:
    if not value:
        raise ValueError("Runtime checks require --temp-root with an explicit existing temporary parent.")
    path = Path(os.path.abspath(value))
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Runtime temporary parent may not follow a redirected path.")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Runtime temporary parent must be a directory.")
    if within(path, ROOT) or within(ROOT, path):
        raise ValueError("Runtime temporary parent must be separate from the distribution.")
    if {p.lower() for p in path.parts} & {".codex", ".agents", ".config"} or comparison_identity(path) == comparison_identity(Path.home()):
        raise ValueError("Runtime temporary parent may not be a global instruction or configuration location.")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="store_true", help="Run behavioral tests after freezing/reviewing the candidate.")
    parser.add_argument("--installed-runtime", action="store_true", help="Run shipped runtime regressions and fixtures, without recursive development/install tests.")
    parser.add_argument("--temp-root", help="Explicit existing parent; runtime checks create a unique owned temporary child here.")
    args = parser.parse_args()
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    if args.runtime and args.installed_runtime:
        parser.error("Select --runtime or --installed-runtime, not both")
    if args.runtime or args.installed_runtime:
        try:
            parent = prepare_temporary_parent(args.temp_root)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        temporary = Path(tempfile.mkdtemp(prefix="c-", dir=parent))
        print(f"Owned temporary directory: {temporary} (retained for inspection)", flush=True)
        env.update({key: str(temporary) for key in ("TMP", "TEMP", "TMPDIR")})
    failures = []
    commands = []
    if args.runtime or args.installed_runtime:
        if args.runtime:
            commands.append(("public consumer tests", ["-m", "unittest", "discover", "-s", "tests", "-v"]))
        else:
            print("Installed-runtime scope: development Git-history and installer integration tests are excluded; shipped runtime tests and evaluation fixtures follow.")
        commands.append(("evaluation fixtures", ["tools/eval_runner.py", "."]))
        runtime_tests = sorted((ROOT / "runtime").rglob("test_*.py"))
        if not runtime_tests:
            failures.append("runtime regression inventory is missing")
        for path in runtime_tests:
            argv = [str(path)]
            if path.relative_to(ROOT).as_posix() == "runtime/skills/chat-objective-continuity/scripts/test_ledger_input.py":
                runtime = ROOT / "runtime/objective_ledger.py"
                argv.extend(["--runtime", str(runtime), "--runtime-sha256", hashlib.sha256(runtime.read_bytes()).hexdigest().upper(),
                             "--temp-root", str(temporary)])
            elif path.relative_to(ROOT).as_posix() == "runtime/skills/master-guided-skill-lifecycle/scripts/test_work_io.py":
                argv.extend(["--temp-root", str(temporary)])
            commands.append((path.relative_to(ROOT).as_posix(), argv))
    else:
        for base in (ROOT / "tools", ROOT / "runtime", ROOT / "tests"):
            for path in sorted(base.rglob("*.py")):
                try:
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                except (SyntaxError, UnicodeError) as exc:
                    failures.append(path.relative_to(ROOT).as_posix())
                    print(f"Syntax failure: {exc}", file=sys.stderr)
        commands = [(name, ["tools/" + name + ".py", "."]) for name in
                    ("privacy_scan", "no_drop_check", "link_check")]
        commands.append(("schema documents", ["tools/validate_schemas.py"]))
        if (ROOT / ".git").exists():
            commands.append(("reachable Git history", ["tools/history_scan.py", "."]))
        else:
            print("Git history: not applicable to a source archive without repository metadata.")
    for name, argv in commands:
        print(f"\nChecking {name}", flush=True)
        result = subprocess.run([sys.executable, "-B", *argv], cwd=ROOT, env=env, check=False)
        if result.returncode:
            failures.append(f"{name} (exit {result.returncode})")
    if failures:
        print("\nFailed partitions (independent partitions were retained):", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("\nRequested checks completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
