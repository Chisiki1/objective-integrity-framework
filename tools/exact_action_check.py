#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


FOREACH_PIPE = re.compile(r"(?is)(^|[;\n\r])\s*foreach\s*\([^)]*\)\s*\{[^{}]*\}\s*\|")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def strip_simple_strings_and_comments(command: str) -> str:
    result: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == quote:
                quote = None
            result.append(" ")
        elif char in {"'", '"'}:
            quote = char
            result.append(" ")
        elif char == "#":
            while index < len(command) and command[index] not in "\r\n":
                result.append(" ")
                index += 1
            continue
        else:
            result.append(char)
        index += 1
    return "".join(result)


def check_member(member: dict[str, Any]) -> dict[str, Any]:
    command = str(member.get("command", ""))
    scrubbed = strip_simple_strings_and_comments(command)
    findings: list[dict[str, str]] = []
    applicable = ["mechanical::powershell-foreach-pipe"]
    if FOREACH_PIPE.search(scrubbed):
        findings.append(
            {
                "constraint": "mechanical::powershell-foreach-pipe",
                "message": "Statement-form foreach output is piped directly; use an intermediate value or ForEach-Object when semantics are preserved.",
            }
        )
    if member.get("literal_dollar_required") is True:
        applicable.append("mechanical::literal-dollar-transport")
        if "$" in command and "`$" not in command and "'" not in command:
            findings.append(
                {
                    "constraint": "mechanical::literal-dollar-transport",
                    "message": "Literal dollar transport was requested but the command appears to allow expansion.",
                }
            )
    return {
        "id": member.get("id", "member-1"),
        "representation_sha256": sha256_text(command),
        "applicable_constraints": applicable,
        "decision": "block" if findings else "pass",
        "findings": findings,
        "proof_ceiling": "known mechanical exact-action discriminators only",
    }


def load_members(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.input_json:
        data = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
        return list(data.get("members", []))
    return [{"id": "member-1", "command": args.command or "", "literal_dollar_required": args.literal_dollar_required}]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screen exact action members for bounded mechanical recurrence families.")
    parser.add_argument("--command")
    parser.add_argument("--input-json")
    parser.add_argument("--literal-dollar-required", action="store_true")
    args = parser.parse_args(argv)
    members = load_members(args)
    results = [check_member(member) for member in members]
    decision = "block" if any(result["decision"] == "block" for result in results) else "pass"
    output = {
        "schema_version": "exact-action-preflight-v1",
        "decision": decision,
        "members": results,
        "proof_ceiling": "member-level mechanical screening; authorization, semantics, safety, and consumer outcome remain unproven",
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 2 if decision == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
