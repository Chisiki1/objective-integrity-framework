#!/usr/bin/env python3
"""Measure OIF master-query CLI response bytes on fixed synthetic inputs.

Standard library only. Run a reviewed OIF runtime script; it is read-only.
Synthetic inputs and raw evidence stay in the explicit scratch directory;
the separate JSON report contains no filesystem paths. This measures response bytes, not
tokens, latency, task completion, or general productivity.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import traceback


TERM = "benchmark-needle"
BUDGET = 12000
SCHEMA = "oif-master-query-benchmark-v1"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest().upper()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def write_new(path: Path, raw: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(raw)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii")


def plain_absolute(path: Path, *, existing: bool) -> Path:
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                    "symlink or reparse path refused")
    if existing:
        require(path.is_file(), "required regular file is absent")
    return path


class InvocationError(RuntimeError):
    pass


class Runner:
    def __init__(self, script: Path, root: Path) -> None:
        self.script = script
        self.root = root
        self.calls = 0
        self.environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        self.python = str(Path(sys.executable).resolve(strict=True))
        (root / "calls").mkdir()

    def call(self, args: list[str], *, expected: int = 0) -> tuple[dict, bytes, int]:
        self.calls += 1
        prefix = self.root / "calls" / f"{self.calls:03d}"
        command = [self.python, "-B", str(self.script), *args]
        try:
            result = subprocess.run(command, capture_output=True, env=self.environment, timeout=30, check=False)
        except subprocess.TimeoutExpired as exc:
            write_new(prefix.with_suffix(".stdout"), exc.stdout or b"")
            write_new(prefix.with_suffix(".stderr"), exc.stderr or b"")
            write_new(prefix.with_suffix(".json"), json_bytes({"status": "timeout", "exit_code": None}))
            raise InvocationError("bounded CLI invocation timed out; no retry performed") from exc
        except OSError as exc:
            write_new(prefix.with_suffix(".json"), json_bytes({"status": "launch-failed", "error_class": type(exc).__name__}))
            raise InvocationError("CLI launch failed; no retry performed") from exc
        write_new(prefix.with_suffix(".stdout"), result.stdout)
        write_new(prefix.with_suffix(".stderr"), result.stderr)
        write_new(prefix.with_suffix(".json"), json_bytes({"status": "returned", "exit_code": result.returncode}))
        require(result.returncode == expected, "unexpected CLI exit code; original streams retained")
        parsed = json.loads(result.stdout)
        require(isinstance(parsed, dict), "CLI response must be a JSON object")
        return parsed, result.stdout, len(result.stderr)

    def pages(self, index: Path, term: str, *, legacy: bool = False, group: str | None = None) -> list[tuple[dict, bytes, int]]:
        pages = []
        cursor = None
        seen = set()
        while True:
            args = ["query", "--index", str(index), "--term", term, "--page-chars", str(BUDGET)]
            if legacy:
                args.append("--legacy-output")
            if group is not None:
                args += ["--origins", group]
            if cursor is not None:
                require(cursor not in seen, "cursor repeated without progress")
                seen.add(cursor)
                args += ["--cursor", cursor]
            page = self.call(args)
            if not legacy:
                require(len(page[1]) <= BUDGET, "compact serialized output exceeded its declared byte budget")
            pages.append(page)
            cursor = page[0]["next_cursor"]
            if cursor is None:
                return pages


def payload(tag: str) -> bytes:
    # Tags are equal-width: the no-duplicate control changes content, not size.
    text = "## Reusable procedure\n" + TERM + " fixture-" + tag + "\n"
    text += ("Read the source, retain evidence, apply the procedure, and verify the requested outcome.\n" * 9)
    return text.encode("ascii")


def fixture(root: Path, mode: str) -> dict:
    source = root / "src"
    source.mkdir()
    nodes: dict[str, dict] = {}
    edges: set[tuple[str, str]] = set()

    def add(name: str, body: bytes, role: str, child: str | None = None) -> str:
        path = source / name
        raw = b"# Synthetic master\n\n" + body + b"\n## History links\n"
        if child is not None:
            old = nodes[child]
            marker = {"path": child, "bytes": len(old["raw"]), "sha256": sha(old["raw"])}
            raw += b"<!-- master-history-v1 " + json.dumps(marker, sort_keys=True, separators=(",", ":")).encode("ascii") + b" -->\n"
            edges.add((str(path), child))
        write_new(path, raw)
        # Section scan includes the separator before the next heading.
        section = body + b"\n"
        start = raw.index(section)
        require(raw.find(section, start + 1) == -1, "fixture section must occur exactly once per source")
        nodes[str(path)] = {"raw": raw, "section": section, "role": role, "start": start, "end": start + len(section)}
        return str(path)

    if mode in {"duplicates", "distinct"}:
        previous = None
        for number in range(8):
            tag = "000" if mode == "duplicates" else f"{number:03d}"
            previous = add(f"h{number:02d}.md", payload(tag), "history", previous)
        roots = []
        for number, name in [(8, "global.md"), (9, "project.md")]:
            tag = "000" if mode == "duplicates" else f"{number:03d}"
            roots.append(add(name, payload(tag), "live", previous))
    else:
        bodies = [
            b"## Permission\nbenchmark-needle: allow action only with source authority.\n",
            b"## Permission\nbenchmark-needle: deny action without source authority.\n",
            b"## Permission\r\nbenchmark-needle: allow action only with source authority.\r\n",
        ]
        history = add("history.md", bodies[0], "history")
        roots = [add("global.md", bodies[1], "live", history), add("project.md", bodies[2], "live", history)]
    return {"nodes": nodes, "edges": edges, "roots": roots}


def content_from_pages(pages: list[tuple[dict, bytes, int]], *, legacy: bool) -> dict:
    rebuilt: dict[object, str] = {}
    for page, _, _ in pages:
        for item in page["items"]:
            key = (item["source"], item["section_id"]) if legacy else item["group_id"]
            old = rebuilt.get(key, "")
            require(item["character_offset"] == len(old), "content fragment is missing, duplicated, or out of order")
            rebuilt[key] = old + item["text"]
    require(pages[-1][0]["complete_for_explicit_query"], "content traversal is incomplete")
    return rebuilt


def verify_provenance(document: dict, group_body: bytes, model: dict) -> set[str]:
    sources = {item["source"]: item for item in document["sources"]}
    require(len(sources) == len(document["sources"]), "duplicate source identity in provenance")
    for item in sources.values():
        node = model["nodes"][item["path"]]
        require(item["sha256"] == sha(node["raw"]) and item["bytes"] == len(node["raw"]) and item["role"] == node["role"],
                "provenance source identity or role changed")
    occurrences = set()
    for origin in document["origins"]:
        path = sources[origin["source"]]["path"]
        node = model["nodes"][path]
        require(origin["matched_by_query"] is True, "matching occurrence was relabeled")
        require((origin["byte_start"], origin["byte_end"]) == (node["start"], node["end"]), "origin byte span changed")
        require(origin["line"] == node["raw"][:node["start"]].count(b"\n") + 1, "origin line changed")
        require(node["raw"][origin["byte_start"]:origin["byte_end"]] == group_body, "origin no longer identifies exact content")
        require(path not in occurrences, "duplicate origin")
        occurrences.add(path)
    expected = {path for path, node in model["nodes"].items() if node["section"] == group_body}
    require(occurrences == expected, "origin was omitted or added")
    ancestors = set(occurrences)
    while True:
        expanded = ancestors | {parent for parent, child in model["edges"] if child in ancestors}
        if expanded == ancestors:
            break
        ancestors = expanded
    require({item["path"] for item in sources.values()} == ancestors, "provenance ancestor source graph changed")
    actual_edges = {(sources[e["parent"]]["path"], sources[e["source"]]["path"]) for e in document["history_edges"]}
    expected_edges = {(a, b) for a, b in model["edges"] if a in ancestors and b in ancestors}
    require(actual_edges == expected_edges and len(actual_edges) == len(document["history_edges"]), "history-edge provenance changed")
    for edge in document["history_edges"]:
        parent = model["nodes"][sources[edge["parent"]]["path"]]["raw"]
        # The source contract binds the full marker line, including its newline.
        # Preserve/check that span; remove only line endings for JSON syntax.
        marker_line = parent[edge["marker_start"]:edge["marker_end"]]
        require(marker_line == parent.splitlines(keepends=True)[edge["marker_line"] - 1], "history marker full-line span changed")
        marker = marker_line.rstrip(b"\r\n")
        require(marker.startswith(b"<!-- master-history-v1 ") and marker.endswith(b"-->"), "history marker span changed")
        reference = json.loads(marker[len(b"<!-- master-history-v1 "):-len(b"-->")])
        target = sources[edge["source"]]
        require(reference == {key: target[key] for key in ("path", "sha256", "bytes")}, "history marker identity changed")
        require(edge["marker_line"] == parent[:edge["marker_start"]].count(b"\n") + 1, "history marker line changed")
    return occurrences


def reduction(baseline: int, candidate: int) -> float | None:
    return round((baseline - candidate) * 100 / baseline, 2) if baseline else None


def measure(runner: Runner, model: dict, index: Path, term: str) -> dict:
    legacy = runner.pages(index, term, legacy=True)
    compact = runner.pages(index, term)
    old = content_from_pages(legacy, legacy=True)
    new = content_from_pages(compact, legacy=False)
    expected = model["nodes"] if term == TERM else {}
    for page, _, _ in legacy:
        for item in page["items"]:
            node = model["nodes"][item["source"]]
            require(item["source_sha256"] == sha(node["raw"]) and item["source_role"] == node["role"], "legacy provenance identity changed")
            incoming = {(edge["parent_path"], edge["path"]) for edge in item["parent_refs"]}
            require(incoming == {(a, b) for a, b in model["edges"] if b == item["source"]}, "legacy incoming history lineage changed")
    require(Counter((path, text.encode("utf-8")) for (path, _), text in old.items()) ==
            Counter((path, node["section"]) for path, node in expected.items()), "legacy content differs from independent fixture")
    wanted_groups = {sha(node["section"]): node["section"] for node in expected.values()}
    require({group: text.encode("utf-8") for group, text in new.items()} == wanted_groups,
            "compact content differs from independent exact-byte groups")
    origin_bytes = origin_pages = origin_stderr = 0
    seen_origins = set()
    for group, content in new.items():
        pages = runner.pages(index, term, group=group)
        document_text = ""
        for page, raw, stderr in pages:
            require(page["character_offset"] == len(document_text), "provenance fragment order changed")
            document_text += page["origin_json"]
            origin_bytes += len(raw)
            origin_stderr += stderr
            origin_pages += 1
        require(pages[-1][0]["origins_complete"], "provenance traversal is incomplete")
        document = json.loads(document_text)
        require(document["group_id"] == group, "wrong provenance group")
        seen_origins.update(verify_provenance(document, content.encode("utf-8"), model))
    require(seen_origins == set(expected), "full matching provenance is incomplete")
    old_bytes = sum(len(raw) for _, raw, _ in legacy)
    new_bytes = sum(len(raw) for _, raw, _ in compact)
    return {
        "status": "verified",
        "source_occurrences": len(expected),
        "unique_exact_sections": len(wanted_groups),
        "matching_text_bytes_with_repetition": sum(len(node["section"]) for node in expected.values()),
        "unique_text_bytes": sum(len(raw) for raw in wanted_groups.values()),
        "legacy_first_response_bytes": len(legacy[0][1]),
        "compact_first_response_bytes": len(compact[0][1]),
        "first_response_reduction_percent": reduction(len(legacy[0][1]), len(compact[0][1])),
        "legacy_first_content_complete": legacy[0][0]["complete_for_explicit_query"],
        "compact_first_content_complete": compact[0][0]["complete_for_explicit_query"],
        "legacy_content_and_provenance_bytes": old_bytes,
        "compact_all_content_bytes": new_bytes,
        "compact_separate_provenance_bytes": origin_bytes,
        "compact_content_and_provenance_bytes": new_bytes + origin_bytes,
        "full_retrieval_reduction_percent": reduction(old_bytes, new_bytes + origin_bytes),
        "legacy_response_count": len(legacy),
        "compact_content_response_count": len(compact),
        "compact_provenance_response_count": origin_pages,
        "stderr_bytes": sum(stderr for _, _, stderr in legacy + compact) + origin_stderr,
        "exact_content_preserved": True,
        "all_matching_origins_and_ancestor_edges_preserved": True,
        "root_path_characters": len(str(runner.root)),
        "source_path_characters": sorted({len(path) for path in model["nodes"]}),
    }


def run_case(script: Path, scratch: Path, name: str, mode: str, *, term: str = TERM, stale: bool = False) -> dict:
    root = scratch / name
    root.mkdir()
    runner = Runner(script, root)
    model = fixture(root, mode)
    args = ["build", "--cache", str(root / "cache")]
    for path in model["roots"]:
        args += ["--master", path]
    built, _, _ = runner.call(args)
    index = Path(built["index"])
    if stale:
        changed = Path(model["roots"][0])
        original = changed.read_bytes()
        modified = original.replace(b"fixture-000", b"fixture-999", 1)
        require(modified != original and len(modified) == len(original), "stale control must change same-length bytes")
        changed.write_bytes(modified)
        checks = {}
        for legacy in (True, False):
            args = ["query", "--index", str(index), "--term", TERM, "--page-chars", str(BUDGET)]
            if legacy:
                args.append("--legacy-output")
            page, raw, stderr = runner.call(args, expected=2)
            require(page.get("status") == "ERROR" and "STALE_SOURCE" in json.dumps(page), "stale source was not explicitly rejected")
            checks["legacy" if legacy else "compact"] = {"exit_code": 2, "stale_source_rejected": True, "response_bytes": len(raw), "stderr_bytes": stderr}
        return {"status": "verified-negative-control", "same_length_source_mutation": True, "checks": checks}
    return measure(runner, model, index, term)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True, type=Path, help="Exact OIF master_index.py to compare")
    parser.add_argument("--scratch", required=True, type=Path, help="Fresh absent directory under an existing owned parent; raw evidence remains here")
    parser.add_argument("--output", required=True, type=Path, help="Fresh absent JSON file under an existing owned parent")
    args = parser.parse_args()
    script = plain_absolute(args.script, existing=True)
    scratch = plain_absolute(args.scratch, existing=False)
    output = plain_absolute(args.output, existing=False)
    require(scratch.parent.is_dir() and output.parent.is_dir(), "explicit output parents must already exist")
    require(not scratch.exists() and not output.exists(), "scratch and output must be absent; existing evidence is never replaced")
    require(not script.is_relative_to(scratch) and not scratch.is_relative_to(script.parent), "synthetic scratch must be separate from runtime sources")
    require(not output.is_relative_to(script.parent), "report must be separate from runtime sources")
    before = sha(script.read_bytes())
    scratch.mkdir()
    results = {
        "schema": SCHEMA,
        "status": "verified",
        "method": {
            "metric": "actual CLI stdout bytes, including JSON metadata, escaping, and newline",
            "budget_argument": BUDGET,
            "budget_semantics": {"legacy": "selected text characters", "compact": "serialized response bytes"},
            "same_input_per_comparison": True,
            "grouping": "exact section bytes only; no fuzzy or semantic merging",
            "fixed_fixture_design": "8 history nodes and 2 live roots, 9 edges; equal-width tags toggle exact duplication",
            "provenance_accounting": "legacy inline on every content page; compact all origin pages fetched separately for every returned group",
            "negative_reduction_means": "candidate response overhead, not an improvement",
            "path_dependence": "raw byte totals vary with absolute synthetic source-path length; no path normalization or redaction before counting",
            "excluded_metrics": ["tokens", "elapsed time", "task completion", "quality", "rework", "general productivity"],
            "raw_evidence": "all actual subprocess stdout, stderr and exit metadata retained in explicitly owned scratch",
        },
        "runtime": {"python": platform.python_version(), "platform": sys.platform, "master_index_sha256": before},
        "cases": {},
        "first_fault": None,
    }
    definitions = [
        ("duplicate-rich", "duplicates", TERM, False),
        ("no-duplicates", "distinct", TERM, False),
        ("near-contradictions", "near", TERM, False),
        ("no-match", "duplicates", "absent-needle", False),
        ("stale-source", "duplicates", TERM, True),
    ]
    for name, mode, term, stale in definitions:
        try:
            results["cases"][name] = run_case(script, scratch, name, mode, term=term, stale=stale)
        except Exception as exc:
            write_new(scratch / name / "verification-error.txt", traceback.format_exc().encode("utf-8"))
            fault = {"case": name, "error_class": type(exc).__name__, "details": "original CLI streams are retained in scratch; no automatic retry"}
            results["cases"][name] = {"status": "failed", "fault": fault}
            if results["first_fault"] is None:
                results["first_fault"] = fault
            results["status"] = "failed"
    after = sha(script.read_bytes())
    results["runtime"]["master_index_unchanged"] = before == after
    if before != after:
        results["status"] = "failed"
        results["first_fault"] = results["first_fault"] or {"case": "source-identity", "error_class": "SourceChanged"}
    write_new(output, json_bytes(results))
    print(json.dumps({"status": results["status"], "cases": {name: row["status"] for name, row in results["cases"].items()}}, sort_keys=True))
    return 0 if results["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
