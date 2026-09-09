#!/usr/bin/env python3
"""Pure result-to-next-work projection. Never dispatch, activate, or write a master.

The work receiver calls this for real results, including results without a
selected Skill. An owner chooses a useful disposition; code cannot invent the
cause, user authority, a child's reading, or the value of creating a new Skill.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat

DISPOSITIONS = frozenset({'reuse', 'revise', 'create', 'merge', 'narrow', 'retire', 'no-change', 'defer'})
CAUSES = frozenset({'selection-gap', 'application-gap', 'stale-method', 'implementation-fault',
    'applicability-mismatch', 'environment-fault', 'reuse-opportunity', 'unclassified'})


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest().upper()


def read_ref(ref):
    if not isinstance(ref, dict) or set(ref) != {'path', 'sha256'}:
        raise ValueError('exact path and sha256 reference required')
    p = Path(ref['path'])
    if not p.is_absolute() or '..' in p.parts or not re.fullmatch('[0-9A-Fa-f]{64}', str(ref['sha256'])):
        raise ValueError('invalid plain absolute file reference')
    for part in [*reversed(p.parents), p]:
        s = part.lstat()
        if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked/reparse reference is unsupported')
    s = p.stat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError('plain single-link file required')
    raw = p.read_bytes()
    if sha(raw) != ref['sha256'].upper():
        raise ValueError('reference changed')
    return raw


def result_context(handoff, methods):
    """Extract facts without treating an unsuccessful result as a known cause."""
    reported = handoff.get('reported_result')
    reported = reported if isinstance(reported, dict) else {}
    uses = reported.get('method_applications', [])
    uses = uses if isinstance(uses, list) else []
    facts = {
        'schema': 'in-work-result-v1',
        'request_id': handoff.get('request_id'),
        'raw_result_ref': {k: handoff.get('raw_result', {}).get(k) for k in ('path', 'sha256')},
        'status': reported.get('status', 'unobserved'),
        'effect_state': reported.get('effect_state', 'unknown'),
        'first_fault': reported.get('first_fault'),
        'validation_issues': handoff.get('validation_issues', []),
        'required_methods': [{'id': m['id'], 'path': m['path'], 'sha256': m['sha256']} for m in methods],
        'reported_applications': uses,
        'next_consumer': handoff.get('consumer'),
        'signals': [],
        'owner_decision_required': True,
        'proof_ceiling': 'source/result projection and reported uses, not root cause, reading, permission or benefit',
    }
    if facts['status'] in {'failed', 'partial', 'unknown', 'unobserved'} or facts['validation_issues']:
        facts['signals'].append('inspect-first-fault-and-effects-before-dependent-retry')
    observed = {u.get('id') for u in uses if isinstance(u, dict) and u.get('state') in {'instruction-used', 'script-executed'}}
    excluded = {u.get('id') for u in uses if isinstance(u, dict) and u.get('state') == 'not-applicable'}
    facts['connection_opportunities'] = [
        {'method_id': m['id'], 'method_ref': {'path': m['path'], 'sha256': m['sha256']},
         'raw_result_ref': facts['raw_result_ref'], 'next_consumer': facts['next_consumer'],
         'status': 'required-method-use-unobserved',
         'question': 'Is this a missing caller connection, an omitted observation, or a justified nonapplication?'}
        for m in methods if m['id'] not in observed | excluded]
    if any(m['id'] not in observed for m in methods):
        facts['signals'].append('method-application-not-observed-not-assumed-unused')
    if not methods:
        facts['signals'].append('no-selected-skill-does-not-exclude-a-useful-new-operation')
    facts['signals'].append('compare-unchanged-reuse-small-correction-and-structural-change-if-worthwhile')
    facts['context_sha256'] = sha(canonical(facts))
    return facts


def build_decision(context, next_spec, meaning):
    """Generate repeated source/result/evidence identities from actual inputs.

    Only the non-mechanical meaning remains an owner input. This avoids another
    handwritten fact packet without turning automatic generation into approval.
    """
    required = {'disposition', 'cause', 'reason', 'expected_change', 'alternatives',
        'effect_reconciliation', 'return_trigger'}
    if not isinstance(meaning, dict) or set(meaning) != required:
        raise ValueError('explicit owner meaning fields differ')
    result = {'schema': 'in-work-decision-v1', 'owner_chat_id': next_spec['owner_chat_id'],
        'result_context_sha256': context['context_sha256'], 'source_sha256': next_spec['source']['sha256'],
        'next_consumer': next_spec['consumer']['id'], 'evidence_refs': [context['raw_result_ref']], **meaning}
    validate_decision(result, context=context, owner_chat_id=next_spec['owner_chat_id'],
        next_source_sha256=next_spec['source']['sha256'])
    return result


def validate_decision(decision, *, context, owner_chat_id, next_source_sha256):
    """Bind a semantic owner decision to a real raw result and next consumer."""
    required = {'schema', 'owner_chat_id', 'result_context_sha256', 'source_sha256', 'disposition',
        'cause', 'reason', 'next_consumer', 'expected_change', 'alternatives', 'evidence_refs',
        'effect_reconciliation', 'return_trigger'}
    if not isinstance(decision, dict) or set(decision) != required or decision['schema'] != 'in-work-decision-v1':
        raise ValueError('in-work decision fields differ')
    if decision['owner_chat_id'] != owner_chat_id or decision['source_sha256'] != next_source_sha256:
        raise ValueError('decision owner/source does not match next work')
    if decision['result_context_sha256'] != context['context_sha256']:
        raise ValueError('decision does not bind actual result context')
    if decision['disposition'] not in DISPOSITIONS or decision['cause'] not in CAUSES:
        raise ValueError('unsupported disposition/cause')
    for field in ('reason', 'next_consumer', 'expected_change', 'effect_reconciliation'):
        if not isinstance(decision[field], str) or not decision[field].strip():
            raise ValueError(field + ' is required')
    if decision['disposition'] == 'defer' and not decision['return_trigger']:
        raise ValueError('deferred work needs a concrete return trigger')
    if decision['disposition'] in {'create', 'revise', 'merge', 'narrow', 'retire'}:
        if not isinstance(decision['alternatives'], list) or not decision['alternatives']:
            raise ValueError('material improvement needs a same-context reuse/smaller/structural comparison')
    if not isinstance(decision['evidence_refs'], list) or not decision['evidence_refs']:
        raise ValueError('decision needs existing evidence')
    for ref in decision['evidence_refs']:
        read_ref(ref)
    return {'decision_sha256': sha(canonical(decision)), 'disposition': decision['disposition'],
        'next_consumer': decision['next_consumer'], 'authority_granted': False,
        'activation_ready': False, 'proof_ceiling': 'owner-authored meaning and bound evidence only'}


def relation_gaps(episodes, procedures):
    """Project exact family relations from existing facts, not a second graph DB.

    Same family is a hypothesis to inspect. Exclusions and missing application
    are distinct; this function neither selects a Skill nor merges its meaning.
    """
    result = []
    for episode in episodes:
        if not isinstance(episode, dict) or not {'id', 'family_keys', 'evidence_ref', 'next_consumer', 'applied_ids', 'excluded_ids'} <= set(episode):
            raise ValueError('episode facts incomplete')
        read_ref(episode['evidence_ref'])
        if not episode['next_consumer']:
            continue
        for procedure in procedures:
            if not isinstance(procedure, dict) or not {'id', 'family_keys', 'evidence_ref'} <= set(procedure):
                raise ValueError('procedure facts incomplete')
            common = sorted(set(episode['family_keys']) & set(procedure['family_keys']))
            if not common or procedure['id'] in episode['applied_ids'] or procedure['id'] in episode['excluded_ids']:
                continue
            read_ref(procedure['evidence_ref'])
            result.append({'episode_id': episode['id'], 'procedure_id': procedure['id'],
                'common_families': common, 'next_consumer': episode['next_consumer'],
                'evidence_refs': [episode['evidence_ref'], procedure['evidence_ref']],
                'status': 'unconfirmed-connection-opportunity',
                'alternatives': ['unchanged-reuse', 'connect-existing-operation', 'revise', 'merge-or-remove', 'new-operation-if-needed'],
                'permission_granted': False})
    return result
