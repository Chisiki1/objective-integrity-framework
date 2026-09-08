#!/usr/bin/env python3
"""Read-only source-wide work selection; not permission or semantic outcome proof.

The owner maintains the complete scope and phase state. This evaluator checks
their declared, exact file identities and selects structurally eligible work. It
cannot infer omitted source requirements, prove independent judgment, attest
that a command is safe, or intercept callers which do not use it.

Construction feedback requires text decision/return_step/cost_disposition/
output_ownership, effect_class='owned-local', effects_disposition from
known-bounded|unknown|harmful|external, and applicability from
construction|accepted-product-regression|essential-design-failure. The latter
two fields are declarations, not classifications inferred from free prose.
Only known-bounded construction feedback is eligible on this route. Existing
useful shared caches are permitted when their ownership/effects are declared;
this interface does not require cache isolation or authorize build scripts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys


ACTIONS = frozenset({'IMPLEMENT', 'SCOPE_REVIEW', 'CONSTRUCTION_FEEDBACK',
    'FORMAL_CHECK', 'COLLECT_FINDINGS', 'REPAIR_FINDINGS', 'ACCEPT'})
PHASES = frozenset({'BUILD', 'SWEEP', 'REPAIR', 'ACCEPT'})
PROOF_CEILING = (
    'Structural selection against owner-declared source-wide scope and exact '
    'files only; no semantic completeness, independent judgment, safety, '
    'permission, command execution, host enforcement or source-outcome proof.')


def _object(value, required, optional=()):
    if type(value) is not dict or set(value) != set(required) | (set(value) & set(optional)):
        raise ValueError('object fields differ; required: ' + ', '.join(required))
    return value


def _text(value, label):
    if type(value) is not str or not value.strip():
        raise ValueError(label + ' must be nonempty text')
    return value


def _enum(value, choices, label):
    _text(value, label)
    if value not in choices:
        raise ValueError('unsupported ' + label)
    return value


def _hash(value):
    if type(value) is not str or not re.fullmatch(r'[0-9a-fA-F]{64}', value):
        raise ValueError('expected explicit SHA256')
    return value.upper()


def _json_value(value):
    """Validate in-memory bindings as strictly as decoded JSON, including 1e999."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError('input must contain only finite JSON values')


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate JSON field: ' + key)
            value[key] = item
        return value

    def constant(value):
        raise ValueError('non-finite JSON value: ' + value)

    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
            parse_constant=constant)
        _json_value(value)
        return value
    except (UnicodeError, RecursionError) as error:
        raise ValueError('invalid JSON encoding or nesting') from error


def _plain_path(value):
    value = _text(value, 'path')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('path must be absolute without parent traversal')
    # Device paths and alternate data streams do not identify ordinary files.
    if os.name == 'nt' and (value.startswith(('\\\\?\\', '\\\\.\\'))
            or ':' in value[len(path.drive):]):
        raise ValueError('device paths and alternate data streams are unsupported')
    for component in reversed((path, *path.parents)):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked/reparse path is unsupported')
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('expected a plain regular file')
    return path


def _stamp(info):
    # Windows Python 3.12 may expose creation time through path lstat().ctime
    # and change time through handle fstat().ctime for the SAME unchanged file.
    # Compare only common-meaning fields across APIs; hash checks below retain
    # exact byte binding. ctime remains comparable within each API separately.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_plain(value):
    path = _plain_path(value)
    before = path.lstat()
    # Opening is read-only. Recheck the path and file descriptor; this is a
    # bounded drift check, not a filesystem transaction or future-use lease.
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _stamp(opened) != _stamp(before):
            raise ValueError('file identity changed before read')
        raw = stream.read()
        finished = os.fstat(stream.fileno())
        if (_stamp(finished) != _stamp(opened)
                or finished.st_ctime_ns != opened.st_ctime_ns):
            raise ValueError('file changed during read')
    _plain_path(str(path))
    after = path.lstat()
    if _stamp(after) != _stamp(opened) or after.st_ctime_ns != before.st_ctime_ns:
        raise ValueError('file identity changed after read')
    ref = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest().upper()}
    return ref, raw, _stamp(opened)


class _References:
    def __init__(self):
        self.reads = {}

    def read(self, value):
        _object(value, ('path', 'sha256'))
        expected = _hash(value['sha256'])
        ref, raw, stamp = _read_plain(value['path'])
        if ref['sha256'] != expected:
            raise ValueError('stale file hash: ' + ref['path'])
        key = os.path.normcase(ref['path'])
        prior = self.reads.get(key)
        if prior is not None and prior != (ref, stamp):
            raise ValueError('conflicting file identity')
        self.reads[key] = (ref, stamp)
        return ref, raw

    def optional(self, value):
        return None if value is None else self.read(value)[0]

    def fresh(self):
        for ref, stamp in self.reads.values():
            current, _, observed = _read_plain(ref['path'])
            if current != ref or observed != stamp:
                raise ValueError('reference changed during evaluation')


def _distinct(refs):
    refs = [ref for ref in refs if ref is not None]
    for index, ref in enumerate(refs):
        for other in refs[:index]:
            if os.path.samefile(ref['path'], other['path']):
                raise ValueError('distinct structural roles alias the same file')


def _same_ref(left, right):
    return (_hash(left['sha256']) == _hash(right['sha256'])
        and os.path.normcase(left['path']) == os.path.normcase(right['path']))


def _ids(value, label, allowed=None, *, nonempty=False):
    if type(value) is not list or (nonempty and not value):
        raise ValueError(label + ' must be a list' + (' with selected IDs' if nonempty else ''))
    for item in value:
        _text(item, label)
    if len(value) != len(set(value)):
        raise ValueError('duplicate ' + label)
    if allowed is not None and not set(value) <= set(allowed):
        raise ValueError('unknown ' + label)
    return value


def _records(value, label, *, nonempty=False):
    if type(value) is not list or (nonempty and not value):
        raise ValueError(label + ' must be a list' + (' with records' if nonempty else ''))
    result = {}
    for item in value:
        if type(item) is not dict:
            raise ValueError(label + ' records must be objects')
        identity = _text(item.get('id'), label + ' ID')
        if identity in result:
            raise ValueError('duplicate ' + label + ' ID')
        result[identity] = item
    return result


def _scope(scope, refs):
    _object(scope, ('schema', 'objective_id', 'owner_chat_id', 'source_ref',
        'requirements', 'implementation_items', 'checks', 'coverage_review'))
    _enum(scope['schema'], {'source-wide-scope-v1'}, 'scope schema')
    for field in ('objective_id', 'owner_chat_id'):
        _text(scope[field], field)
    source_ref, _ = refs.read(scope['source_ref'])
    requirements = _records(scope['requirements'], 'requirements', nonempty=True)
    items = _records(scope['implementation_items'], 'implementation items', nonempty=True)
    checks = _records(scope['checks'], 'scope checks', nonempty=True)
    for record in requirements.values():
        _object(record, ('id', 'source_clause', 'description'))
        _text(record['source_clause'], 'source clause')
        _text(record['description'], 'requirement description')
    for label, records, fields in (
            ('implementation', items, ('id', 'requirement_ids', 'description')),
            ('check', checks, ('id', 'requirement_ids', 'stage', 'description'))):
        covered = set()
        for record in records.values():
            _object(record, fields)
            covered.update(_ids(record['requirement_ids'], 'requirement IDs', requirements, nonempty=True))
            _text(record['description'], label + ' description')
            if label == 'check':
                _text(record['stage'], 'check stage')
        if covered != set(requirements):
            raise ValueError('each requirement needs ' + label + ' coverage')
    review = _object(scope['coverage_review'], ('owner_ref', 'independent_ref'))
    owner = refs.optional(review['owner_ref'])
    independent = refs.optional(review['independent_ref'])
    return items, checks, source_ref, owner, independent


def _state(state, scope, scope_ref, items, checks, refs):
    _object(state, ('schema', 'objective_id', 'owner_chat_id', 'source_sha256',
        'completion_scope_ref', 'phase', 'current_stage', 'candidate_ref',
        'implementation', 'checks', 'findings', 'findings_closed'))
    _enum(state['schema'], {'source-wide-state-v1'}, 'state schema')
    phase = _enum(state['phase'], PHASES, 'phase')
    for field in ('objective_id', 'owner_chat_id'):
        if state[field] != scope[field]:
            raise ValueError('state/scope ' + field + ' differs')
    if _hash(state['source_sha256']) != _hash(scope['source_ref']['sha256']):
        raise ValueError('state/scope source differs')
    bound_scope, _ = refs.read(state['completion_scope_ref'])
    if not _same_ref(bound_scope, scope_ref):
        raise ValueError('state/binding completion scope differs')
    stage = _text(state['current_stage'], 'current stage')
    if stage not in {check['stage'] for check in checks.values()}:
        raise ValueError('current stage must exist in the whole scope')
    if type(state['findings_closed']) is not bool:
        raise ValueError('findings_closed must be boolean')
    candidate_ref, _ = refs.read(state['candidate_ref'])
    implementation = _records(state['implementation'], 'state implementation')
    observed = _records(state['checks'], 'state checks')
    if set(implementation) != set(items) or set(observed) != set(checks):
        raise ValueError('state must retain the complete scope item/check sets')
    for item in implementation.values():
        _object(item, ('id', 'status', 'evidence_ref'))
        _enum(item['status'], {'pending', 'complete'}, 'implementation status')
        evidence = refs.optional(item['evidence_ref'])
        if item['status'] == 'complete' and evidence is None:
            raise ValueError('complete implementation needs evidence')
    for check in observed.values():
        _object(check, ('id', 'status', 'evidence_ref', 'reason'))
        _enum(check['status'], {'pending', 'pass', 'fail', 'blocked'}, 'check status')
        evidence = refs.optional(check['evidence_ref'])
        if type(check['reason']) is not str:
            raise ValueError('check reason must be text')
        if check['status'] != 'pending' and evidence is None:
            raise ValueError('observed or blocked checks need evidence')
        if check['status'] == 'blocked':
            _text(check['reason'], 'first-fault/dependency reason')
    findings = _records(state['findings'], 'findings')
    for finding in findings.values():
        _object(finding, ('id', 'check_id', 'classification', 'basis', 'root_cause',
            'consumer_item_ids', 'status', 'evidence_ref'))
        _text(finding['check_id'], 'finding check ID')
        if finding['check_id'] not in checks:
            raise ValueError('finding references an unknown check')
        # A resolved finding retains its original evidence when the owner marks
        # an affected check pending for the next sweep. Do not erase history or
        # force the former result to masquerade as the new check's result.
        _enum(finding['classification'], {'mandatory', 'optional'}, 'finding classification')
        _enum(finding['status'], {'open', 'resolved'}, 'finding status')
        _text(finding['basis'], 'finding basis')
        _text(finding['root_cause'], 'finding root cause')
        _ids(finding['consumer_item_ids'], 'finding consumer IDs', items, nonempty=True)
        refs.read(finding['evidence_ref'])
    if state['findings_closed']:
        bound_checks = {finding['check_id'] for finding in findings.values()}
        if any(check['status'] == 'fail' and identity not in bound_checks
                for identity, check in observed.items()):
            raise ValueError('closed collection omits a failed check finding')
    if phase != 'BUILD' and any(scope['coverage_review'][key] is None
            for key in ('owner_ref', 'independent_ref')):
        raise ValueError('formal phases require both source coverage review references')
    return implementation, observed, findings, candidate_ref


def _feedback(value):
    _object(value, ('decision', 'return_step', 'cost_disposition', 'effects_disposition',
        'effect_class', 'output_ownership', 'applicability'))
    for field in ('decision', 'return_step', 'cost_disposition', 'effect_class', 'output_ownership'):
        _text(value[field], field)
    _enum(value['effects_disposition'], {'known-bounded', 'unknown', 'harmful', 'external'},
        'feedback effects disposition')
    _enum(value['applicability'], {'construction', 'accepted-product-regression',
        'essential-design-failure'}, 'feedback applicability')
    return (value['effects_disposition'] == 'known-bounded'
        and value['effect_class'] == 'owned-local' and value['applicability'] == 'construction')


def _evaluate(binding, *, objective_id, source_sha256, owner_chat_id):
    _json_value(binding)
    _object(binding, ('state_ref', 'completion_scope_ref', 'action', 'item_ids',
        'check_ids', 'finding_ids', 'feedback'))
    action = _enum(binding['action'], ACTIONS, 'action')
    refs = _References()
    state_ref, state_raw = refs.read(binding['state_ref'])
    scope_ref, scope_raw = refs.read(binding['completion_scope_ref'])
    scope, state = _json(scope_raw), _json(state_raw)
    items, checks, source_ref, owner, independent = _scope(scope, refs)
    for field, expected in (('objective_id', objective_id), ('owner_chat_id', owner_chat_id)):
        if expected is not None and _text(expected, 'expected ' + field) != scope[field]:
            raise ValueError('expected ' + field + ' differs')
    if source_sha256 is not None and _hash(source_sha256) != source_ref['sha256']:
        raise ValueError('expected source differs')
    implementation, observed, findings, candidate_ref = _state(
        state, scope, scope_ref, items, checks, refs)
    _distinct((state_ref, scope_ref, source_ref, candidate_ref, owner, independent))
    item_ids = _ids(binding['item_ids'], 'selected item IDs', items,
        nonempty=action in {'IMPLEMENT', 'REPAIR_FINDINGS'})
    check_ids = _ids(binding['check_ids'], 'selected check IDs', checks,
        nonempty=action == 'FORMAL_CHECK')
    finding_ids = _ids(binding['finding_ids'], 'selected finding IDs', findings,
        nonempty=action == 'REPAIR_FINDINGS')
    if action == 'IMPLEMENT' and (check_ids or finding_ids):
        raise ValueError('implementation cannot select formal checks or findings')
    if action == 'FORMAL_CHECK' and (item_ids or finding_ids):
        raise ValueError('formal check selection must contain only check IDs')
    if action == 'REPAIR_FINDINGS' and check_ids:
        raise ValueError('repair does not select formal checks')
    if action in {'SCOPE_REVIEW', 'COLLECT_FINDINGS', 'ACCEPT'} and (item_ids or check_ids or finding_ids):
        raise ValueError('whole-scope action cannot be narrowed by selected IDs')
    if action == 'CONSTRUCTION_FEEDBACK' and (check_ids or finding_ids):
        raise ValueError('construction feedback is not a formal check/finding action')
    feedback_safe = _feedback(binding['feedback']) if action == 'CONSTRUCTION_FEEDBACK' else None
    if action != 'CONSTRUCTION_FEEDBACK' and binding['feedback'] is not None:
        raise ValueError('feedback belongs only to construction feedback action')

    phase = state['phase']
    current = [identity for identity, check in checks.items() if check['stage'] == state['current_stage']]
    pending_items = [identity for identity, item in implementation.items() if item['status'] == 'pending']
    pending_checks = [identity for identity in current if observed[identity]['status'] == 'pending']
    incomplete_current = [identity for identity in current if observed[identity]['status'] != 'pass']
    deferred = [identity for identity, check in checks.items()
        if check['stage'] != state['current_stage'] and observed[identity]['status'] != 'pass']
    mandatory = [identity for identity, finding in findings.items()
        if finding['classification'] == 'mandatory' and finding['status'] == 'open']
    optional = [identity for identity, finding in findings.items()
        if finding['classification'] == 'optional' and finding['status'] == 'open']
    collected = not pending_checks and state['findings_closed']
    all_pass = all(check['status'] == 'pass' for check in observed.values())
    reviews_ready = owner is not None and independent is not None
    holds = []

    def hold(condition, reason):
        if condition and reason not in holds:
            holds.append(reason)

    allowed = {
        'BUILD': {'IMPLEMENT', 'SCOPE_REVIEW', 'CONSTRUCTION_FEEDBACK'},
        'SWEEP': {'FORMAL_CHECK', 'COLLECT_FINDINGS'},
        'REPAIR': {'REPAIR_FINDINGS', 'CONSTRUCTION_FEEDBACK'},
        'ACCEPT': {'ACCEPT'},
    }
    hold(action not in allowed[phase], 'ACTION_NOT_IN_PHASE')
    if phase != 'BUILD':
        hold(bool(pending_items), 'SOURCE_WIDE_IMPLEMENTATION_INCOMPLETE')
    if phase == 'REPAIR':
        hold(not collected, 'CURRENT_STAGE_COLLECTION_INCOMPLETE')
    if action == 'IMPLEMENT':
        hold(any(identity not in pending_items for identity in item_ids), 'IMPLEMENT_SELECTS_NONPENDING_ITEM')
    if action == 'CONSTRUCTION_FEEDBACK':
        hold(not feedback_safe, 'EXISTING_DEPENDENT_DIAGNOSTIC_OR_ACTION_ROUTE_REQUIRED')
    if action == 'FORMAL_CHECK':
        hold(bool(pending_items), 'SOURCE_WIDE_IMPLEMENTATION_INCOMPLETE')
        hold(not reviews_ready, 'SOURCE_COVERAGE_REVIEW_INCOMPLETE')
        hold(any(identity not in pending_checks for identity in check_ids), 'CHECK_NOT_CURRENT_STAGE_PENDING')
    if action == 'REPAIR_FINDINGS':
        hold(not collected, 'CURRENT_STAGE_COLLECTION_INCOMPLETE')
        causes = {findings[identity]['root_cause'] for identity in finding_ids}
        required = {identity for identity in mandatory if findings[identity]['root_cause'] in causes}
        consumers = {item for identity in required for item in findings[identity]['consumer_item_ids']}
        hold(set(finding_ids) != required or not required, 'REPAIR_MUST_INCLUDE_ALL_OPEN_MANDATORY_CAUSE_FINDINGS')
        hold(set(item_ids) != consumers, 'REPAIR_MUST_INCLUDE_ALL_CAUSE_CONSUMERS')
    if action == 'ACCEPT':
        hold(bool(pending_items), 'SOURCE_WIDE_IMPLEMENTATION_INCOMPLETE')
        hold(not reviews_ready, 'SOURCE_COVERAGE_REVIEW_INCOMPLETE')
        hold(bool(mandatory), 'MANDATORY_FINDINGS_OPEN')
        hold(not all_pass, 'REQUIRED_CHECKS_INCOMPLETE')
        hold(not state['findings_closed'], 'FINDINGS_COLLECTION_NOT_CLOSED')

    # Recommendations describe the next owner transition, not an automatic
    # mutation. A later evidence stage never shrinks the source completion set.
    next_phase = phase
    owner_transition = None
    failed_current = [identity for identity in current if observed[identity]['status'] == 'fail']
    blocked = [identity for identity, check in observed.items() if check['status'] == 'blocked']
    if pending_items:
        next_actions = ['IMPLEMENT']
        next_phase = 'BUILD'
    elif not reviews_ready:
        next_actions = ['SCOPE_REVIEW']
        next_phase = 'BUILD'
    elif pending_checks:
        next_actions = ['FORMAL_CHECK']
        next_phase = 'SWEEP'
    elif not state['findings_closed']:
        next_actions = ['COLLECT_FINDINGS']
        next_phase = 'SWEEP'
    elif mandatory:
        next_actions = ['REPAIR_FINDINGS']
        next_phase = 'REPAIR'
    elif all_pass:
        next_actions = ['ACCEPT']
        next_phase = 'ACCEPT'
    elif failed_current:
        next_actions = ['FORMAL_CHECK']
        next_phase = 'SWEEP'
        owner_transition = (
            'Preserve former failed-check evidence and resolved findings. The owner must '
            'bind the changed candidate and affected recheck set, retain unaffected '
            'evidence, then mark eligible affected checks pending before a new sweep. '
            'This recommendation neither executes nor certifies the recheck.')
    elif deferred and not incomplete_current:
        available_later = [identity for identity in deferred if observed[identity]['status'] in {'pending', 'fail'}]
        next_actions = ['FORMAL_CHECK'] if available_later else []
        next_phase = 'SWEEP'
        owner_transition = (
            'The owner must select the next available evidence stage without shrinking '
            'completion scope. Preserve prior results; failed checks require an affected '
            'recheck transition, and blocked checks require changed dependency evidence '
            'before becoming eligible. Pending does not itself prove action safety.')
    else:
        # A blocked/failed check does not become safe or passed by relabeling it.
        # The owner must resolve its dependency or mark the affected recheck
        # pending with preserved history, using existing authority/evidence.
        next_actions = []
        owner_transition = (
            'Retain blocked first-fault/dependency evidence. Resolve the actual dependent '
            'condition under existing authority, bind new evidence, then let the owner '
            'mark only eligible affected checks pending; do not relabel blocked as pass.')

    dependent_route = action == 'CONSTRUCTION_FEEDBACK' and not feedback_safe
    if dependent_route:
        next_actions = []
    refs.fresh()
    return {
        'admitted': not holds, 'next_actions': next_actions, 'holds': holds,
        'phase': phase, 'next_phase': next_phase,
        'completion_scope_ref': scope_ref, 'state_ref': state_ref,
        'source_ref': source_ref, 'candidate_ref': candidate_ref,
        'pending_implementation': pending_items, 'pending_checks': pending_checks,
        'deferred_checks': deferred, 'incomplete_current_checks': incomplete_current,
        'affected_recheck_ids': failed_current, 'blocked_check_ids': blocked,
        'blocked_dependencies': [{'check_id': identity, 'reason': observed[identity]['reason'],
            'evidence_ref': observed[identity]['evidence_ref']} for identity in blocked],
        'owner_state_transition_required': owner_transition,
        'mandatory_findings': mandatory, 'optional_findings': optional,
        'current_stage_collected': collected, 'declared_feedback': binding['feedback'],
        'dependent_route_required': dependent_route,
        'source_wide_evaluated': True, 'permission_granted': False,
        'input_refs': [ref for ref, _ in refs.reads.values()],
        'source_outcome_completed': False, 'proof_ceiling': PROOF_CEILING,
    }


def evaluate(binding, *, objective_id=None, source_sha256=None, owner_chat_id=None):
    """Evaluate one exact binding without writing, executing or granting authority.

    Malformed, stale, missing or contradictory identities raise ValueError;
    well-formed but premature work returns admitted=False and dependent holds.
    The caller must preserve already-observed effects even on either outcome.
    """
    try:
        return _evaluate(binding, objective_id=objective_id,
            source_sha256=source_sha256, owner_chat_id=owner_chat_id)
    except (OSError, UnicodeError, KeyError, TypeError, RecursionError, OverflowError) as error:
        raise ValueError('invalid or unavailable source-wide input: ' + str(error)) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='absolute plain JSON binding file')
    args = parser.parse_args(argv)
    try:
        _, raw, _ = _read_plain(args.input)
        result = evaluate(_json(raw))
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2))
        return 0 if result['admitted'] else 2
    except (OSError, ValueError) as error:
        print(json.dumps({'admitted': False, 'error': str(error),
            'permission_granted': False, 'source_outcome_completed': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
