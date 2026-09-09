"""Owner-authored whole-current reconciliation proposal; never a live writer.

Retain every previous byte in the existing master-history graph. Require an
explicit disposition for every section, including old current controls, before
offering replacement bytes to an independently reviewed CAS publisher. This is
a structural check, not semantic approval or an authorization mechanism.
"""
from __future__ import annotations
import argparse
import base64
import json
from pathlib import Path
import runpy

_load = runpy.run_path(str(Path(__file__).with_name('source_module.py')))['load_source']
mi = _load(Path(__file__).with_name('master_index.py'))
SCHEMA = 'master-reconciliation-plan-v1'
CEILING = ('Exact whole-section disposition, source references and lossless history proposal only; '
           'semantic faithfulness, permission, publication and task benefit are not established.')


def exact(ref):
    if type(ref) is not dict or set(ref) != {'path', 'sha256'}:
        raise ValueError('exact path/sha256 reference required')
    raw = mi.read_plain(ref['path'])
    if mi.sha(raw) != mi._hash(ref['sha256']):
        raise ValueError('STALE_SOURCE: referenced bytes differ: ' + ref['path'])
    return raw


def inventory(master):
    raw = mi.read_plain(master)
    sections, _ = mi.scan(raw)
    return {'master_ref': {'path': str(mi.plain_path(master)), 'sha256': mi.sha(raw)},
            'sections': sections, 'bytes': len(raw), 'semantic_review_required': True}


def text(value, name):
    if type(value) is not str or not value.strip():
        raise ValueError('nonempty ' + name + ' required')
    return value


def occurrence_keys(sections):
    """Exact section occurrence with its actual ancestry and control role.

    Ordinals distinguish otherwise identical siblings; one surviving occurrence
    cannot stand in for two old occurrences. Intentional relocation is a sourced
    replace/archive, not keep. A changed identical-sibling order may consequently
    need explicit reconciliation instead of guessed semantic equivalence.
    """
    stack, counts, keys = [], {}, {}
    for section in sections:
        while stack and stack[-1][0] >= section['level']:
            stack.pop()
        parent = stack[-1][1] if stack else ()
        identity = (section['level'], section['sha256'], section['current_control_region'])
        family = parent + (identity,)
        ordinal = counts.get(family, 0)
        counts[family] = ordinal + 1
        key = parent + ((identity, ordinal),)
        keys[section['section_id']] = key
        stack.append((section['level'], key))
    return keys


def propose(plan):
    """Validate a complete owner disposition, without writing any path.

    groups partition exact section IDs, not a fuzzy age/size filter. Each group
    declares keep/replace/archive, reason, and exact supporting source refs.
    Replaced control sections must map to an explicit current heading. Archived
    control sections must map to an explicit historical frontier heading, so
    old unresolved work cannot disappear behind a generic compactness claim.
    """
    if type(plan) is not dict or set(plan) != {
            'schema', 'owner', 'master_ref', 'replacement_ref', 'history_path',
            'source_refs', 'groups'} or plan['schema'] != SCHEMA:
        raise ValueError('master reconciliation plan schema differs')
    text(plan['owner'], 'owner')
    old = exact(plan['master_ref'])
    replacement = exact(plan['replacement_ref'])
    refs = plan['source_refs']
    if type(refs) is not list or not refs:
        raise ValueError('owner source references required')
    for ref in refs:
        exact(ref)
    graph = mi.load_sources([plan['master_ref']['path']])
    if graph['sources'][0]['sha256'] != mi.sha(old):
        raise ValueError('STALE_SOURCE: graph changed during preparation')
    history = mi.plain_path(plan['history_path'], existing=False)
    mi._separate_output(history, graph['sources'])
    candidate_path = mi.plain_path(plan['replacement_ref']['path'])
    mi._separate_output(candidate_path, graph['sources'])
    if candidate_path == history:
        raise ValueError('replacement and retained preimage must be separate')
    if history.exists() and mi.read_plain(history) != old:
        raise ValueError('history destination contains different bytes')
    sections, _ = mi.scan(old)
    after, _ = mi.scan(replacement)
    after_titles = {s['title']: [] for s in after}
    for section in after:
        after_titles[section['title']].append(section)
    by_id = {s['section_id']: s for s in sections}
    seen = set()
    decisions = {}
    groups = plan['groups']
    if type(groups) is not list or not groups:
        raise ValueError('explicit whole-section groups required')
    for group in groups:
        if type(group) is not dict or set(group) != {
                'section_ids', 'disposition', 'reason', 'target_heading', 'evidence_refs'}:
            raise ValueError('invalid disposition group')
        ids = group['section_ids']
        if (type(ids) is not list or not ids or any(type(i) is not int for i in ids)
                or len(ids) != len(set(ids)) or not set(ids) <= by_id.keys()
                or seen.intersection(ids)):
            raise ValueError('unknown, duplicate or empty section disposition')
        seen.update(ids)
        decision = group['disposition']
        if decision not in {'keep', 'replace', 'archive'}:
            raise ValueError('unsupported disposition')
        text(group['reason'], 'source-bound reason')
        if type(group['evidence_refs']) is not list:
            raise ValueError('evidence reference list required')
        for ref in group['evidence_refs']:
            exact(ref)
        target = group['target_heading']
        if target is not None:
            if type(target) is not str or len(after_titles.get(target, [])) != 1:
                raise ValueError('target heading must identify one replacement section')
        for identity in ids:
            section = by_id[identity]
            body = old[section['start']:section['end']]
            if decision == 'keep' and body not in replacement:
                raise ValueError('kept section bytes missing')
            if decision == 'replace' and (target is None or not group['evidence_refs']):
                raise ValueError('replacement needs its source evidence and target heading')
            if section['current_control_region'] and decision != 'keep':
                if target is None or not group['evidence_refs']:
                    raise ValueError('every changed current control needs explicit disposition')
                if decision == 'replace' and not after_titles[target][0]['current_control_region']:
                    raise ValueError('replacement current control must remain current')
            decisions[identity] = decision
    if seen != by_id.keys():
        raise ValueError('whole current/knowledge scope has undisposed sections')
    # Keeping children under a changed heading can change meaning despite exact
    # child bytes. Require retaining the original ancestry or explicit replacing
    # of each moved child, with source references and reviewed destination.
    for section in sections:
        if decisions[section['section_id']] == 'keep':
            continue
        for child in sections[section['section_id'] + 1:]:
            if child['level'] <= section['level']:
                break
            if decisions[child['section_id']] == 'keep':
                raise ValueError('changed parent requires explicit child reconciliation')
    before_keys = occurrence_keys(sections)
    after_keys = set(occurrence_keys(after).values())
    for identity, decision in decisions.items():
        if decision == 'keep' and before_keys[identity] not in after_keys:
            raise ValueError('kept section occurrence, ancestry or current role changed; explicit reconciliation required')
    # One direct link to the complete old cut already preserves its entire
    # transitive history. Do not copy every old parent edge into the new view.
    history_ref = {'path': str(history), 'sha256': mi.sha(old), 'bytes': len(old)}
    if mi.history_markers(replacement):
        raise ValueError('replacement body must not duplicate history pointers; proposal adds the complete preimage')
    nl = b'\r\n' if b'\r\n' in replacement else b'\n'
    output = replacement + (b'' if replacement.endswith((b'\n', b'\r')) else nl)
    output += b'<!-- master-history-v1 ' + mi.canonical(history_ref) + b' -->' + nl
    # Recheck every planning input before emitting a proposal. Publication still
    # needs its own precondition, readiness/review and exact readback.
    for ref in [plan['master_ref'], plan['replacement_ref'], *refs,
                *(ref for group in groups for ref in group['evidence_refs'])]:
        exact(ref)
    return {'schema': 'master-reconciliation-proposal-v1', 'owner': plan['owner'],
            'plan_sha256': mi.sha(mi.canonical(plan)), 'master_ref': plan['master_ref'],
            'history_ref': history_ref, 'replacement_sha256': mi.sha(output),
            'replacement_bytes': len(output),
            'replacement_base64': base64.b64encode(output).decode('ascii'),
            'backing_base64': base64.b64encode(old).decode('ascii'),
            'section_count': len(sections), 'changed_current_sections': sum(
                s['current_control_region'] and decisions[s['section_id']] != 'keep' for s in sections),
            'history_sources_preserved': [{k: node[k] for k in ('path', 'sha256', 'bytes')}
                                         for node in graph['sources']],
            'publication_observed': False, 'writes_performed': False,
            'permission_granted': False, 'proof_ceiling': CEILING}


def readback(proposal):
    """Read actual installed sources; not a union of predicted ID sets."""
    master = proposal['master_ref']['path']
    current = mi.read_plain(master)
    if mi.sha(current) != proposal['replacement_sha256']:
        raise ValueError('installed replacement differs')
    old = mi.read_plain(proposal['history_ref']['path'])
    if mi.sha(old) != proposal['master_ref']['sha256']:
        raise ValueError('retained preimage differs')
    graph = mi.load_sources([master])
    reached = {(node['path'], node['sha256'], node['bytes']) for node in graph['sources']}
    for prior in proposal['history_sources_preserved']:
        if prior['path'] == master:
            wanted = proposal['history_ref']
        else:
            wanted = prior
        if (wanted['path'], wanted['sha256'], wanted['bytes']) not in reached:
            raise ValueError('prior source not reachable from installed master')
    return {'schema': 'master-reconciliation-readback-v1', 'master': master,
            'replacement_sha256': mi.sha(current), 'prior_sources_verified': len(proposal['history_sources_preserved']),
            'history_bytes_verified': len(old), 'exact_publication_observed': True,
            'semantic_review_required': True, 'proof_ceiling': CEILING}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inventory', 'propose', 'readback'])
    parser.add_argument('--input', required=True, help='Absolute master path for inventory; exact plan/proposal JSON otherwise')
    args = parser.parse_args()
    try:
        result = inventory(args.input) if args.action == 'inventory' else globals()[args.action](mi.strict_json(mi.read_plain(args.input)))
        print(json.dumps(result, ensure_ascii=True))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({'error': str(error), 'writes_performed': False, 'permission_granted': False}))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
