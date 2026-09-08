#!/usr/bin/env python3
"""Inventory real master headings/current-control roles without claiming semantic completeness."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import types
from pathlib import Path
from typing import Any


HEADING = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
STABLE_ID = re.compile(
    r'(?<![A-Z0-9])(?:GPM|PM|G|P|GK|GC|PK|U|KCM|POLICY|SKILL|MGSKILL)[A-Z0-9]*(?:-[A-Z0-9]+)+'
)
STATUS_WORDS = ("STALE", "REDUNDANT", "CONFLICT", "ORPHAN", "SUPERSEDED", "INVALIDATED", "UNPROVEN", "PENDING")
WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s`]+")


def source_loader(path=None, expected_sha256=None):
    """Select the inspected lifecycle sibling by exact path/bytes, never search.

    Optional caller hash binding supports frozen bundles; the default reports
    the actual installed sibling identity and does not claim signed provenance.
    exec avoids creating bytecode as a side effect of dependency import.
    """
    selected = Path(path) if path is not None else (
        Path(__file__).parents[2] / 'master-guided-skill-lifecycle' / 'scripts' / 'master_index.py')
    if not selected.is_absolute():
        raise ValueError('source loader requires an exact absolute path')
    for component in (selected, *selected.parents):
        info = component.lstat()
        if component.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked/reparse source loader is unsupported')
    raw = selected.read_bytes()
    digest = sha256_bytes(raw)
    if expected_sha256 is not None and digest != expected_sha256.upper():
        raise ValueError('source loader hash differs')
    module = types.ModuleType('master_inventory_source_loader')
    module.__file__ = str(selected)
    exec(compile(raw, str(selected), 'exec'), module.__dict__)
    return module, {'path': str(selected), 'sha256': digest}


def project_links(body):
    expressions = re.findall(r'PROJECT_EVENT_LINK\s*[:=]\s*([^;\n]+)', body.upper())
    links = []
    for expression in expressions:
        # Preserve expressions (including shorthand) separately; do not invent
        # missing range IDs or claim a prose/path reference is verified truth.
        match = re.match(r'(P-[A-Z0-9-]+(?:\s*,\s*P-[A-Z0-9-]+)*)', expression)
        if match:
            links.extend(re.findall(r'P-[A-Z0-9-]+', match[1]))
    return sorted(set(links)), expressions


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def normalized_body(value: str) -> str:
    value = STABLE_ID.sub("<ID>", value.upper())
    value = WINDOWS_PATH.sub("<PATH>", value)
    return " ".join(value.split())


def section_inventory(lines: list[str], headings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for index, heading in enumerate(headings):
        start = heading["line"] - 1
        end = headings[index + 1]["line"] - 1 if index + 1 < len(headings) else len(lines)
        body = "\n".join(lines[start:end])
        normalized = normalized_body(body)
        tags = [tag for tag in ("RAW_EPISODE", "SANITIZED_FAMILY", "PROJECT_EVENT_LINK") if tag in body.upper()]
        links, expressions = project_links(body)
        sections.append({
            "line": heading["line"],
            "title": heading["title"],
            "role": heading["role"],
            "tags": tags,
            "project_event_links": links,
            "project_event_link_expressions": expressions,
            "normalized_body_sha256": sha256_bytes(normalized.encode("utf-8")),
            "normalized_body_length": len(normalized),
        })
    return sections


def inventory(path: Path, *, source_loader_path=None, source_loader_sha256=None) -> dict[str, Any]:
    loader, loader_ref = source_loader(source_loader_path, source_loader_sha256)
    graph = loader.load_sources([path])
    root = graph['sources'][0]
    raw = root['raw']
    lines = raw.decode('utf8').splitlines()
    headings: list[dict[str, Any]] = []
    current_lines: list[str] = []
    status_hits: dict[str, list[dict[str, Any]]] = {word: [] for word in STATUS_WORDS}
    sections, all_occurrences, current_heading_ids, source_summaries = [], [], [], []
    for source in graph['sources']:
        source_lines = source['raw'].decode('utf8').splitlines()
        source_summaries.append({key: source[key] for key in ('path', 'sha256', 'bytes', 'role', 'parent_refs')})
        for section in source['sections']:
            current = source['role'] == 'live' and section['current_control_region']
            body = source['raw'][section['start']:section['end']].decode('utf8')
            if current:
                current_lines.extend(body.splitlines())
            if section['level'] == 0:
                continue
            ids = sorted(set(STABLE_ID.findall(section['title'].upper())))
            role = 'current-control' if current else 'archived-history' if source['role'] == 'history' else 'append-history'
            heading = {'line': section['line'], 'level': section['level'], 'title': section['title'],
                'ids': ids, 'role': role, 'source': source['path'], 'source_sha256': source['sha256'],
                'source_role': source['role'], 'section_id': section['section_id'],
                'start': section['start'], 'end': section['end']}
            headings.append(heading)
            if current:
                current_heading_ids.extend(ids)
            links, expressions = project_links(body)
            normalized = normalized_body(body)
            sections.append({**heading, 'tags': [tag for tag in ('RAW_EPISODE', 'SANITIZED_FAMILY',
                'PROJECT_EVENT_LINK') if tag in body.upper()], 'project_event_links': links,
                'project_event_link_expressions': expressions,
                'normalized_body_sha256': sha256_bytes(normalized.encode('utf8')),
                'normalized_body_length': len(normalized)})
        all_occurrences.extend({**occurrence, 'source': source['path'], 'source_role': source['role']}
            for occurrence in source['id_occurrences'])
        for number, line in enumerate(source_lines, 1):
            upper = line.upper()
            for word in STATUS_WORDS:
                if word in upper:
                    status_hits[word].append({'line': number, 'ids': sorted(set(STABLE_ID.findall(upper))),
                        'source': source['path'], 'source_role': source['role']})
    heading_ids = [identifier for item in headings for identifier in item["ids"]]
    duplicate_heading_ids = sorted({identifier for identifier in heading_ids if heading_ids.count(identifier) > 1})
    control_ids = sorted(set(current_heading_ids) | set(re.findall(
        r'CURRENT_CONTROL_ID\s*[:=]\s*([A-Za-z0-9_.:/-]+)', '\n'.join(current_lines), re.IGNORECASE)))
    superseded_controls = sorted(set(re.findall(
        r'SUPERSEDES_CURRENT_CONTROL(?:\s*[:=]\s*|\s+)([A-Za-z0-9_.:/-]+)',
        '\n'.join(lines), re.IGNORECASE)))
    return {
        "path": str(path.resolve()),
        "sha256": sha256_bytes(raw),
        "line_count": len(lines),
        "headings": headings,
        "heading_ids": sorted(set(heading_ids)),
        "heading_id_occurrences": len(heading_ids),
        "duplicate_heading_ids": duplicate_heading_ids,
        "sections": sections,
        "all_ids": sorted({occurrence['id'] for occurrence in all_occurrences}),
        "id_occurrences": all_occurrences,
        "sources": source_summaries,
        "history_edges": graph['history_edges'],
        "source_loader_ref": loader_ref,
        "current_control_ids": control_ids,
        "later_superseded_control_ids": superseded_controls,
        "stale_current_control_candidates": sorted(set(control_ids) & set(superseded_controls)),
        "current_control_sha256": sha256_bytes("\n".join(current_lines).encode("utf-8")),
        "status_hits": status_hits,
        "proof_ceiling": "heading/current-control/status-token identity and no-drop comparison only; semantic completeness, KCM comparability and conflict correctness unproven",
    }


def cross_layer(global_master: dict[str, Any], project_master: dict[str, Any]) -> dict[str, Any]:
    """Judge live layout, retaining immutable old layout as sourced diagnostics.

    Legacy inventories had only live-root sections (including append-history),
    so absent source_role keeps that established behavior. Explicit history is
    never a current blocker; unknown explicit roles fail rather than disappear.
    Project references may legitimately resolve into its complete history.
    """
    def role(item):
        value = item.get('source_role', 'history' if item.get('role') == 'archived-history' else 'live')
        if value not in {'live', 'history'}:
            raise ValueError('unknown section source role')
        return value

    def identity(item, master):
        source_role = role(item)
        return {'source': item.get('source', master.get('path') if source_role == 'live' else None),
            'source_sha256': item.get('source_sha256', master.get('sha256') if source_role == 'live' else None),
            'source_role': source_role, 'section_id': item.get('section_id'),
            'line': item['line'], 'title': item.get('title'), 'ids': item.get('ids', [])}

    global_all, project_all = global_master['sections'], project_master['sections']
    global_sections = [item for item in global_all if role(item) == 'live']
    project_sections = [item for item in project_all if role(item) == 'live']
    global_history = [item for item in global_all if role(item) == 'history']
    project_history = [item for item in project_all if role(item) == 'history']
    project_event_ids = {identifier for identifier in project_master['heading_ids'] if identifier.startswith('P-')}

    def diagnostics(global_items, project_items):
        return {
            'global_sanitized_without_project_link_sections': [identity(item, global_master)
                for item in global_items if 'SANITIZED_FAMILY' in item['tags']
                and 'PROJECT_EVENT_LINK' not in item['tags']],
            'global_raw_episode_sections': [identity(item, global_master)
                for item in global_items if 'RAW_EPISODE' in item['tags']],
            'invalid_project_event_links': [{**identity(item, global_master), 'link': link}
                for item in global_items for link in item['project_event_links'] if link not in project_event_ids],
            'project_contains_sanitized_family_sections': [identity(item, project_master)
                for item in project_items if 'SANITIZED_FAMILY' in item['tags']],
            'legacy_untagged_sections': {
                'global': [identity(item, global_master) for item in global_items if not item['tags']],
                'project': [identity(item, project_master) for item in project_items if not item['tags']]},
        }

    current = diagnostics(global_sections, project_sections)
    historical = diagnostics(global_history, project_history)
    # Do not let a later historical occurrence overwrite a live fingerprint's
    # evidence. Preserve every source occurrence in the matching role groups.
    global_by_fp, project_by_fp = {}, {}
    for item in global_all:
        global_by_fp.setdefault(item['normalized_body_sha256'], []).append(item)
    for item in project_all:
        project_by_fp.setdefault(item['normalized_body_sha256'], []).append(item)
    duplicate_groups, historical_groups = [], []
    for fingerprint in sorted(set(global_by_fp) & set(project_by_fp)):
        global_matches, project_matches = global_by_fp[fingerprint], project_by_fp[fingerprint]
        for global_role, project_role in (('live', 'live'), ('live', 'history'), ('history', 'live'), ('history', 'history')):
            globals_for_role = [item for item in global_matches if role(item) == global_role]
            projects_for_role = [item for item in project_matches if role(item) == project_role]
            if not globals_for_role or not projects_for_role:
                continue
            tagged = (any('SANITIZED_FAMILY' in item['tags'] for item in globals_for_role)
                or any('RAW_EPISODE' in item['tags'] for item in projects_for_role))
            group = {'normalized_body_sha256': fingerprint, 'tagged': tagged,
                'global_sections': [identity(item, global_master) for item in globals_for_role],
                'project_sections': [identity(item, project_master) for item in projects_for_role]}
            (duplicate_groups if global_role == project_role == 'live' else historical_groups).append(group)
    current['normalized_cross_layer_duplicate_groups'] = duplicate_groups
    historical['normalized_cross_layer_duplicate_groups'] = historical_groups
    historical['affects_current_write_decision'] = False
    duplicates = [group['normalized_body_sha256'] for group in duplicate_groups]
    tagged_duplicates = [group['normalized_body_sha256'] for group in duplicate_groups if group['tagged']]
    global_tag_errors = [item['line'] for item in current['global_sanitized_without_project_link_sections']]
    global_raw_errors = [item['line'] for item in current['global_raw_episode_sections']]
    invalid_project_links = current['invalid_project_event_links']
    project_tag_errors = [item['line'] for item in current['project_contains_sanitized_family_sections']]
    blockers = bool(tagged_duplicates or global_tag_errors or global_raw_errors or invalid_project_links or project_tag_errors)
    return {
        "decision": "BLOCK_NEW_WRITE" if blockers else "ELIGIBLE_WITH_SEMANTIC_AUDIT",
        "normalized_cross_layer_duplicates": duplicates,
        "tagged_duplicate_blockers": tagged_duplicates,
        "global_sanitized_without_project_link_lines": global_tag_errors,
        "global_raw_episode_lines": global_raw_errors,
        "invalid_project_event_links": [{'line': item['line'], 'link': item['link']} for item in invalid_project_links],
        "project_contains_sanitized_family_lines": project_tag_errors,
        "legacy_untagged_sections": {
            "global": [item["line"] for item in global_sections if not item["tags"]],
            "project": [item["line"] for item in project_sections if not item["tags"]],
        },
        'current_diagnostics': current,
        'historical_diagnostics': historical,
        'project_reference_scope': 'complete live and reachable historical heading IDs',
        "proof_ceiling": "live-root normalized exact-body/tag/link structural checks only; historical layout retained as nonblocking source-identified diagnostics; semantic equivalence, sanitization quality and historical backing remain independently audited",
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_ids = set(before["heading_ids"])
    after_ids = set(after["heading_ids"])
    return {
        "before_sha256": before["sha256"],
        "after_sha256": after["sha256"],
        "preserved_heading_ids": sorted(before_ids & after_ids),
        "missing_heading_ids": sorted(before_ids - after_ids),
        "added_heading_ids": sorted(after_ids - before_ids),
        "before_duplicate_heading_ids": before["duplicate_heading_ids"],
        "after_duplicate_heading_ids": after["duplicate_heading_ids"],
        "no_drop": not (before_ids - after_ids),
        "proof_ceiling": "stable heading-ID set comparison only; raw semantic no-drop remains separately audited",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", action="append", required=True)
    parser.add_argument("--compare-before", action="append")
    parser.add_argument("--output")
    parser.add_argument('--source-loader')
    parser.add_argument('--source-loader-sha256')
    args = parser.parse_args()
    masters = [inventory(Path(item).absolute(), source_loader_path=args.source_loader,
        source_loader_sha256=args.source_loader_sha256) for item in args.master]
    result: dict[str, Any] = {
        "schema_version": "master-live-role-inventory-v2",
        "masters": masters,
        "proof_ceiling": "real-master heading/current-control/status identity; historical KCM and semantic completeness unproven",
    }
    if len(masters) == 2:
        global_candidates = [item for item in masters if Path(item["path"]).name.upper() == "GLOBAL_PROJECT_MASTER.MD"]
        project_candidates = [item for item in masters if Path(item["path"]).name.upper() == "PROJECT_MASTER.MD"]
        if len(global_candidates) == 1 and len(project_candidates) == 1:
            result["cross_layer"] = cross_layer(global_candidates[0], project_candidates[0])
    if args.compare_before:
        if len(args.compare_before) != len(masters):
            raise SystemExit("compare-before count must equal master count")
        before_values = []
        for before in args.compare_before:
            loaded = json.loads(Path(before).read_text(encoding="utf-8"))
            if loaded.get("schema_version") == "master-live-role-inventory-v2":
                if len(loaded.get("masters", [])) != 1:
                    raise SystemExit("each compare-before envelope must contain exactly one master")
                loaded = loaded["masters"][0]
            before_values.append(loaded)
        result["comparisons"] = [compare(before, after) for before, after in zip(before_values, masters)]
    text = json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
