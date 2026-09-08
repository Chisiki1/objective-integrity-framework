#!/usr/bin/env python3
"""Prepare bounded internal work and consume actual artifacts; never dispatch or write.

The owner supplies scope, source meaning, allocation and permission. This adapter
only binds files and produces an internal target/message payload, a result
handoff, or structural destination readback. The caller maps a prepared payload
to its host's internal-job API; the adapter never sends it. None completes a
source objective or proves that an opaque target is an authorized internal job.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
from pathlib import Path
import re
import sys
import types
from typing import Any

# Reuse the inspected sibling's pure JSON/path primitives without creating a
# bytecode cache as a side effect of the dependency import.
_io_path = Path(__file__).with_name('allocation_io.py')
io = types.ModuleType('work_io_primitives'); io.__file__ = str(_io_path)
exec(compile(_io_path.read_bytes(), str(_io_path), 'exec'), io.__dict__)


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value)-set(required)-set(optional):
        raise ValueError('object fields differ: required='+','.join(required))
    return value


def _text(value):
    return io._text(value, 'owner field')


def _texts(value):
    if not isinstance(value, list):
        raise ValueError('expected a list')
    return [_text(item) for item in value]


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9A-Fa-f]{64}', value):
        raise ValueError('expected explicit SHA256')
    return value.upper()


def _path(value):
    path = Path(_text(value))
    if not path.is_absolute():
        raise ValueError('paths must be explicit absolute paths')
    return str(Path(os.path.normpath(path)))


def _file(path):
    path = io._plain_path(_path(str(path)), existing=True)
    raw = path.read_bytes()
    return {'path': str(path), 'sha256': io._sha(raw)}, raw


def _ref(value, *, extra=(), optional_input=False):
    required = ['path', 'sha256', *extra]
    _object(value, required, ['required'] if optional_input else [])
    result = dict(value); result['path'] = _path(value['path'])
    if optional_input:
        result.setdefault('required', True)
        if type(result['required']) is not bool:
            raise ValueError('required must be boolean')
    if result['sha256'] is None and optional_input and not result['required']:
        pass  # Explicit absence has no invented digest.
    else:
        result['sha256'] = _hash(result['sha256'])
    for field in extra: _text(result[field])
    return result


def _spec(value, *, validate_live_phase=True):
    v2 = isinstance(value, dict) and value.get('schema') == 'work-spec-v2'
    _object(value, ['schema', 'unit_id', 'owner_chat_id', 'target', 'objective', 'instructions',
        'source', 'inputs', 'methods', 'masters', 'scope', 'prohibitions', 'outputs', 'result_path', 'consumer']
        + (['objective_id', 'work_phase'] if v2 else []))
    spec = copy.deepcopy(value)
    if spec['schema'] not in {'work-spec-v1', 'work-spec-v2'}: raise ValueError('unsupported work spec')
    if v2: _text(spec['objective_id'])
    for key in ['unit_id', 'owner_chat_id', 'objective', 'instructions', 'scope']: _text(spec[key])
    if not isinstance(spec['target'], str) or not re.fullmatch(
            r'(?:internal:[A-Za-z0-9][A-Za-z0-9_.-]*|/root/(?:[a-z0-9_]+/)*[a-z0-9_]+)', spec['target']):
        raise ValueError('target must be an explicit internal: identifier or supported canonical internal-agent path, not another chat')
    spec['source'] = _ref(spec['source'])
    for key, extra in [('inputs', ['id']), ('methods', ['id', 'apply']), ('masters', ['role', 'read', 'apply'])]:
        if not isinstance(spec[key], list): raise ValueError(key+' must be a list')
        spec[key] = [_ref(item, extra=extra, optional_input=(key == 'inputs')) for item in spec[key]]
        names = [item[extra[0]] for item in spec[key]]
        if len(names) != len(set(names)): raise ValueError('duplicate '+key+' identity')
    if {item['role'] for item in spec['masters']} != {'global', 'project'}:
        raise ValueError('both explicit Global and Project READ/APPLY bindings are required')
    spec['prohibitions'] = _texts(spec['prohibitions'])
    spec['result_path'] = _path(spec['result_path'])
    if not isinstance(spec['outputs'], list) or not spec['outputs']: raise ValueError('declare real outputs')
    for item in spec['outputs']:
        _object(item, ['id', 'path', 'before_sha256', 'acceptance'])
        _text(item['id']); _text(item['acceptance']); item['path'] = _path(item['path'])
        if item['before_sha256'] is not None: item['before_sha256'] = _hash(item['before_sha256'])
    output_ids = [item['id'] for item in spec['outputs']]
    paths = [item['path'] for item in spec['outputs']]+[spec['result_path']]
    if len(output_ids) != len(set(output_ids)) or len(paths) != len({os.path.normcase(p) for p in paths}):
        raise ValueError('output/result identities must be distinct')
    readonly = [spec['source'], *spec['inputs'], *spec['methods'], *spec['masters']]
    if v2 and validate_live_phase:
        readonly += _phase(spec)['input_refs']
    for path in paths:
        for ref in readonly:
            if os.path.normcase(path) == os.path.normcase(ref['path']) or (
                Path(path).exists() and Path(ref['path']).exists() and os.path.samefile(path, ref['path'])):
                raise ValueError('declared write aliases a read-only binding')
    consumer = _object(spec['consumer'], ['id', 'description', 'acceptance', 'destinations'])
    for key in ['id', 'description', 'acceptance']: _text(consumer[key])
    if not isinstance(consumer['destinations'], list): raise ValueError('consumer destinations must be a list')
    for item in consumer['destinations']:
        _object(item, ['output_id', 'path']); _text(item['output_id']); item['path'] = _path(item['path'])
    if sorted(item['output_id'] for item in consumer['destinations']) != sorted(output_ids):
        raise ValueError('bind each output to exactly one real consumer destination')
    return spec


def _phase(spec):
    """The same immutable owner scope selects both direct and child work."""
    if spec['schema'] == 'work-spec-v1':
        return {'admitted': True, 'source_wide_evaluated': False,
            'proof_ceiling': 'legacy work spec; no source-wide phase protection evaluated'}
    path = Path(__file__).with_name('work_phase.py')
    module = types.ModuleType('work_phase_consumer'); module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module.evaluate(spec['work_phase'], objective_id=spec['objective_id'],
        source_sha256=spec['source']['sha256'], owner_chat_id=spec['owner_chat_id'])


def _fresh(core, *, output_baseline):
    required, optional = [], []
    spec = core['spec']
    refs = [core['spec_ref'], *core['helper_refs'], spec['source'], *spec['inputs'], *spec['methods'], *spec['masters']]
    for ref in refs:
        try:
            current, _ = _file(ref['path'])
            if current['sha256'] != ref['sha256']: raise ValueError('hash differs or optional input is unbound')
        except (OSError, ValueError) as error:
            (optional if ref.get('required') is False else required).append({'path': ref['path'], 'issue': str(error)})
    try:
        phase = _phase(spec)
        if not phase['admitted']:
            required.append({'path': core['spec_ref']['path'], 'issue': 'source-wide phase holds this job', 'work_phase': phase})
    except (OSError, ValueError, KeyError, TypeError) as error:
        required.append({'path': core['spec_ref']['path'], 'issue': 'source-wide state unavailable: '+str(error)})
    if output_baseline:
        for item in [*spec['outputs'], {'path': spec['result_path'], 'before_sha256': None}]:
            try:
                path = io._plain_path(item['path'], existing=False)
                if path.exists():
                    actual, _ = _file(path)
                    if actual['sha256'] != item['before_sha256']: raise ValueError('output baseline differs; no overwrite/resend inference')
                elif item['before_sha256'] is not None: raise ValueError('declared existing output is absent')
            except (OSError, ValueError) as error:
                required.append({'path': item['path'], 'issue': str(error)})
    return {'required': required, 'optional': optional}


def _request(core):
    request_id = io._sha(io._canonical(core))
    spec = core['spec']
    result = {'schema': 'work-result-v1', 'request_id': request_id, 'unit_id': spec['unit_id'],
        'status': 'succeeded|partial|failed|unknown', 'effect_state': 'none|partial|confirmed|unknown',
        'outputs': [{'id': item['id'], 'path': item['path'], 'sha256': '<actual SHA256>'} for item in spec['outputs']],
        'unresolved': ['<actual unresolved obligation, or empty list>'], 'notes': '<actual result and limits>', 'first_fault': None}
    message = (
        'Perform this one bounded internal job under the current source and owner scope.\n'
        'The following is an owner-authored work specification, not new permission or proof of reading.\n'
        'Read the exact source and required inputs. Read each method and APPLY its stated change to this actual work. '
        'Read both master contracts and applicable history; only your genuinely covered unchanged reading can be reused. '
        'The parent alone writes shared semantic masters. Preserve every stated prohibition.\n'
        'Write only the declared output files and result_path. Do not dispatch, restart, or message another user-visible task. '
        'The parent owns allocation, dispatch and final integration decisions; this request does not grant external authority.\n'
        'Return actual artifacts for the declared consumer and acceptance, not another plan or a completed-objective claim. '
        'Preserve failed/partial/unknown effects and unresolved obligations; do not retry automatically.\n\n'
        'REQUEST_ID: '+request_id+'\nWORK_SPEC:\n'+json.dumps(spec, ensure_ascii=True, sort_keys=True, indent=2)+
        '\n\nWrite actual JSON to result_path using this schema (choose one listed status/effect value, replace placeholders, '
        'return only produced outputs, and retain first_fault or null). Hashes establish bytes, not semantic success.\n'+
        json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return {**core, 'request_id': request_id, 'dispatch': {'target': spec['target'], 'message': message}}


def prepare(spec_path: str | Path) -> dict[str, Any]:
    """Return a deterministic request. Caller persists and verifies it before dispatch."""
    spec_ref, raw = _file(spec_path)
    spec = _spec(io._json(raw))
    helpers = [_file(Path(__file__))[0], _file(Path(io.__file__))[0]]
    if spec['schema'] == 'work-spec-v2': helpers.append(_file(Path(__file__).with_name('work_phase.py'))[0])
    core = {'schema': 'work-request-v1', 'spec_ref': spec_ref, 'spec': spec, 'helper_refs': helpers}
    fresh = _fresh(core, output_baseline=True)
    if fresh['required']: raise ValueError(json.dumps({'dependent_dispatch_issues': fresh['required']}))
    return _request(core)


def _prepared(path, expected_request_id):
    _, raw = _file(path); request = io._json(raw)
    _object(request, ['schema', 'spec_ref', 'spec', 'helper_refs', 'request_id', 'dispatch'])
    if request['schema'] != 'work-request-v1': raise ValueError('unsupported prepared request')
    core = {key: request[key] for key in ['schema', 'spec_ref', 'spec', 'helper_refs']}
    if _request(core) != request or request['request_id'] != _hash(expected_request_id):
        raise ValueError('prepared request differs from the explicit dispatched identity')
    # This is the immutable request actually dispatched. A later missing/stale
    # owner state must not prevent parsing the historical result and effects.
    # prepare/verify still evaluate live phase and alias ownership before send;
    # consume separately reports those current freshness/eligibility issues.
    _spec(core['spec'], validate_live_phase=False)
    return request, core


def verify(prepared_path: str | Path, *, expected_request_id: str) -> dict[str, Any]:
    """Recheck immediately before the parent sends dispatch unchanged; never sends it."""
    request, core = _prepared(prepared_path, expected_request_id)
    # A new dispatch must recheck current owner-input/output aliases, including
    # equal-byte hardlinks introduced after prepare. Historical consume is not
    # this eligibility decision and must still preserve returned effects.
    _spec(core['spec'])
    fresh = _fresh(core, output_baseline=True)
    if fresh['required']: raise ValueError(json.dumps({'dependent_dispatch_issues': fresh['required']}))
    return {'request_id': request['request_id'], 'dispatch': request['dispatch'], 'optional_input_issues': fresh['optional'],
        'permission_granted': False, 'dispatch_performed': False, 'work_phase': _phase(core['spec'])}


def _raw(path):
    observation = {'path': str(path), 'sha256': None, 'bytes': None, 'base64': None}
    try:
        ref, raw = _file(path)
        observation.update(ref, bytes=len(raw), base64=base64.b64encode(raw).decode('ascii'))
        return observation, raw
    except (OSError, ValueError) as error:
        observation['read_error'] = str(error)
        return observation, None


def consume(prepared_path: str | Path, result_path: str | Path, *, expected_request_id: str) -> dict[str, Any]:
    """Preserve raw result first; stale source or invalid reports never erase effects."""
    raw_ref, raw = _raw(result_path)
    handoff = {'schema': 'work-handoff-v1', 'request_id': expected_request_id, 'raw_result': raw_ref,
        'reported_result': None, 'result_identity_matches': False, 'artifacts': [], 'validation_issues': [],
        'freshness_issues': {'required': [], 'optional': []}, 'unresolved': [],
        'effect_requires_reconciliation': False,
        'source_outcome_completed': False, 'permission_granted': False}
    issues = handoff['validation_issues']
    try:
        request, core = _prepared(prepared_path, expected_request_id)
        spec = core['spec']; handoff['consumer'] = spec['consumer']; handoff['unit_id'] = spec['unit_id']
        handoff['freshness_issues'] = _fresh(core, output_baseline=False)
        # Phase drift must not discard an already executed child's raw effects.
        try: handoff['work_phase'] = _phase(spec)
        except (OSError, ValueError, KeyError, TypeError) as error:
            handoff['work_phase'] = {'admitted': False, 'error': str(error), 'source_wide_evaluated': False}
        if raw is None: raise ValueError('result bytes unavailable; effects remain unobserved')
        result = io._json(raw); handoff['reported_result'] = result
        _object(result, ['schema', 'request_id', 'unit_id', 'status', 'effect_state', 'outputs', 'unresolved', 'notes'], ['first_fault'])
        if result['schema'] != 'work-result-v1' or result['request_id'] != request['request_id'] or result['unit_id'] != spec['unit_id']:
            raise ValueError('wrong result schema/request/unit identity')
        if _path(str(result_path)) != spec['result_path']: raise ValueError('result was not read from declared result_path')
        handoff['result_identity_matches'] = True
        # Incomplete/unknown reports retain their artifact value, but exact bytes
        # cannot resolve their effect frontier. A known failed/no-effect report
        # is different: failure alone is not an invented unknown external effect.
        handoff['effect_requires_reconciliation'] = (
            result['status'] in {'partial', 'unknown'} or result['effect_state'] in {'partial', 'unknown'})
        if result['status'] not in {'succeeded', 'partial', 'failed', 'unknown'} or result['effect_state'] not in {'none', 'partial', 'confirmed', 'unknown'}:
            issues.append('unsupported reported status/effect; raw values retained')
        if result['status'] == 'succeeded' and result['effect_state'] != 'confirmed':
            issues.append('success report does not confirm its effect')
        handoff['unresolved'] = _texts(result['unresolved']); _text(result['notes'])
        if not isinstance(result['outputs'], list): raise ValueError('outputs must be a list')
        declared = {item['id']: item for item in spec['outputs']}; seen = set()
        for item in result['outputs']:
            try:
                _object(item, ['id', 'path', 'sha256'])
                if item['id'] not in declared or item['id'] in seen: raise ValueError('unexpected/duplicate output identity')
                seen.add(item['id']); expected = declared[item['id']]
                if _path(item['path']) != expected['path']: raise ValueError('unexpected output path')
                ref, _ = _file(item['path'])
                if ref['sha256'] != _hash(item['sha256']): raise ValueError('output hash mismatch')
                handoff['artifacts'].append({**ref, 'id': item['id'], 'acceptance': expected['acceptance']})
            except (OSError, ValueError, TypeError) as error:
                issues.append(str(error))
        available = {item['id'] for item in handoff['artifacts']}
        for identity in sorted(set(declared)-available):
            handoff['unresolved'].append('Output '+identity+' not verified: '+declared[identity]['acceptance'])
        if result['status'] == 'succeeded' and available != set(declared): issues.append('success report has missing/unverified outputs')
    except (OSError, ValueError, KeyError, TypeError) as error:
        issues.append(str(error))
    handoff['unresolved'].append('Parent must judge semantic acceptance and actual integration; source objective remains separate.')
    handoff['requires_reconciliation'] = bool(issues or handoff['freshness_issues']['required'] or handoff['effect_requires_reconciliation'])
    return handoff


def integrate(prepared_path: str | Path, result_path: str | Path, observation_path: str | Path, *, expected_request_id: str) -> dict[str, Any]:
    """Read explicit parent observation and actual destination bytes; never copy/apply."""
    handoff = consume(prepared_path, result_path, expected_request_id=expected_request_id)
    ref, raw = _raw(observation_path)
    handoff['raw_integration_observation'] = ref; handoff['integration_readback'] = []
    expected_ids = {item['output_id'] for item in handoff.get('consumer', {}).get('destinations', [])}
    try:
        if raw is None: raise ValueError('integration observation unavailable')
        observation = io._json(raw)
        _object(observation, ['schema', 'request_id', 'unit_id', 'consumer_id', 'parent_observation', 'outputs'])
        if not handoff['result_identity_matches']: raise ValueError('result identity is unresolved')
        if observation['schema'] != 'work-integration-v1' or observation['request_id'] != expected_request_id or observation['unit_id'] != handoff['unit_id'] or observation['consumer_id'] != handoff['consumer']['id']:
            raise ValueError('integration identity differs')
        _text(observation['parent_observation'])
        destinations = {item['output_id']:item['path'] for item in handoff['consumer']['destinations']}
        artifacts = {item['id']:item for item in handoff['artifacts']}; seen = set()
        if not isinstance(observation['outputs'], list): raise ValueError('integration outputs must be a list')
        for item in observation['outputs']:
            _object(item, ['output_id', 'destination', 'sha256']); identity = item['output_id']
            if identity not in artifacts or identity in seen: raise ValueError('unverified/duplicate integration output')
            seen.add(identity)
            if _path(item['destination']) != destinations[identity]: raise ValueError('undeclared consumer destination')
            current, _ = _file(item['destination'])
            if current['sha256'] != _hash(item['sha256']) or current['sha256'] != artifacts[identity]['sha256']:
                raise ValueError('consumer destination differs from returned artifact')
            handoff['integration_readback'].append({'output_id':identity, 'destination':current['path'],
                'sha256':current['sha256'], 'parent_observation':observation['parent_observation'], 'semantic_acceptance': 'not-established-by-file-identity'})
    except (OSError, ValueError, KeyError, TypeError) as error:
        handoff['validation_issues'].append(str(error)); handoff['requires_reconciliation'] = True
    observed_ids = {item['output_id'] for item in handoff['integration_readback']}
    handoff['missing_integration_output_ids'] = sorted(expected_ids-observed_ids)
    handoff['structural_integration_complete'] = bool(expected_ids and handoff['result_identity_matches']
        and not handoff['missing_integration_output_ids'] and not handoff['validation_issues'])
    for identity in handoff['missing_integration_output_ids']:
        handoff['unresolved'].append('Integration output '+identity+' has no verified destination readback.')
    handoff['requires_reconciliation'] = bool(handoff['requires_reconciliation'] or handoff['missing_integration_output_ids'])
    return handoff


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('prepare').add_argument('--spec', required=True)
    for name in ['verify', 'consume', 'integrate']:
        command = sub.add_parser(name); command.add_argument('--prepared', required=True); command.add_argument('--expected-request-id', required=True)
        if name in {'consume', 'integrate'}: command.add_argument('--result', required=True)
        if name == 'integrate': command.add_argument('--observation', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare': result = prepare(args.spec)
        elif args.command == 'verify': result = verify(args.prepared, expected_request_id=args.expected_request_id)
        elif args.command == 'consume': result = consume(args.prepared, args.result, expected_request_id=args.expected_request_id)
        else: result = integrate(args.prepared, args.result, args.observation, expected_request_id=args.expected_request_id)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
        return 0 if not result.get('validation_issues') else 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'dependent_request_rejected': str(error), 'dispatch_performed': False, 'permission_granted': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
