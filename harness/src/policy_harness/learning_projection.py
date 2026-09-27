"""Current Learning decisions, with immutable originals kept out of recursion.

This input version changes neither historical response schemas nor old captures.
Only byte-exact candidate meaning is grouped; differing judgments remain choices.
"""
from copy import deepcopy
import json

from .models import Idea, PolicyError
from .store import digest


VERSION = 'learning-current-v1'


def active(payload):
    return payload.get('learning_projection') == VERSION


def groups(ideas):
    """One current slot per complete meaning, retaining every original identity."""
    result = []; by_meaning = {}; identities = {}
    for index, item in enumerate(ideas):
        idea = Idea.model_validate(item).model_dump()
        identity = idea.pop('id')
        if identity in identities and identities[identity] != idea:
            raise PolicyError('Learning candidate identity has conflicting current meanings')
        identities[identity] = idea
        key = digest(idea)
        if key not in by_meaning:
            by_meaning[key] = len(result)
            result.append({'idea': dict(idea, id=identity), 'members': [], 'source_indexes': []})
        group = result[by_meaning[key]]
        if identity not in group['members']:group['members'].append(identity)
        group['source_indexes'].append(index)
    return result


def candidates(payload):
    """Only candidates explicitly supplied to this output owner are reusable."""
    source = [*payload.get('required_ideas', []), *payload.get('learning', {}).get('ideas', [])]
    # A current Learning decision may legitimately change the same identity's
    # disposition. It wins over that identity's earlier required observation.
    by_id = {item['id']: item for item in source}
    return groups(list(by_id.values()))


def merge_ideas(existing, observed):
    """Merge a repeated identity only when the complete decision is unchanged."""
    result = deepcopy(existing); owned = {i['id']: i for i in result}
    for item in observed:
        if item['id'] in owned:
            if item != owned[item['id']]:
                raise PolicyError('Assessment reused an Idea identity for a different decision')
        else:
            owned[item['id']] = item; result.append(deepcopy(item))
    return result


def candidate_view(ideas):
    return [dict(group['idea'], original_ids=group['members']) for group in groups(ideas)]


def learning_view(value):
    output = deepcopy(value)
    if isinstance(output, dict) and isinstance(output.get('ideas'), list):
        output['ideas'] = candidate_view(output['ideas'])
    return output


def current_input(payload, *, identity=None, editable_learning=True):
    """A distinct request; never relabel a prior captured request as this view."""
    output = deepcopy(payload)
    output['learning_projection'] = VERSION
    operation = payload.get('operation', {}); result = payload.get('result', {})
    scope = {'editable': ['current Learning proposal and its candidate dispositions'],
        'unchanged_operation': {'id': operation.get('id'), 'sha256': digest(operation)},
        'unchanged_result': {'operation_id': result.get('operation_id'), 'sha256': digest(result)},
        'instruction': 'A Learning revision changes only the uncommitted Learning proposal. It cannot rewrite an acquisition, operation identity, selection, committed result, collection snapshot or earlier opinion. Evaluate criticism against these actual code-owned facts; reject unsupported objections with substantive reasons, or hold unresolved ones. A real future task/method change belongs in a reasoned Idea with an actual next_operation and its ordinary governed execution. Existing committed-result corrections use the pending judgment_correction and reviewed judgment_resolve route. No prose is an executed patch, proof of correction, or permission to replay an effect.'}
    if payload.get('parent_operation'):
        scope['unchanged_parent'] = {'id': payload['parent_operation']['id'], 'sha256': digest(payload['parent_operation'])}
    if not editable_learning:
        scope['editable'] = []
        scope['instruction'] = (
            'This judgment concerns a captured acquisition, not an editable Learning proposal. '
            'All supplied acquisition, operation, selection, result and earlier judgment facts are immutable. '
            'Consider each current target and every independent opinion against its actual stage. '
            'Choose an unchanged supplied candidate with a substantive current consideration, or propose a '
            'genuinely new/changed Idea. A necessary operative correction uses the existing governed '
            'prepared-request or held-result route; prose cannot modify the captured facts or replay an effect.')
    if identity is not None:
        output['identity_and_stage'] = deepcopy(identity)
    output['learning_correction_scope'] = scope
    return output


def _archive(store, task_id, operation_id, value):
    record = {'task_id': task_id, 'operation_id': operation_id, 'value': deepcopy(value)}
    record['id'] = digest(record)
    saved = store.record_get('learning_revision_source', record['id'])
    if saved is not None and saved != record:
        raise PolicyError('Learning revision original changed')
    if saved is None:store.record('learning_revision_source', record['id'], record)
    return {'kind': 'learning_revision_source', 'id': record['id'], 'sha256': digest(record)}


def _historical_revision_feedback(store, task_id, operation_id, attempts, *, observations=True):
    """Factored semantic facts; never a prior request nested inside a new one.

    Original attempts remain addressable byte-for-byte. The current model sees
    every distinct operative value/opinion/failure, once, with its source paths.
    Input containers are not judgments: their duplicated context is already in
    the current input and their exact original bytes remain in the source refs.
    """
    sources = []; facts = []; lookup = {}; added_observations = False
    def add(field, value, source, path):
        if isinstance(value, dict) and {'id', 'target', 'proposal', 'disposition', 'rationale'} <= set(value):
            original_id = value['id']; value = {k:v for k,v in value.items() if k != 'id'}
        else:original_id = value.get('id') if isinstance(value, dict) else None
        key = digest({'field': field, 'value': value})
        if key not in lookup:
            lookup[key] = len(facts); facts.append({'field': field, 'value': deepcopy(value), 'origins': []})
        origin = {'source': source, 'path': path}
        if original_id is not None:origin['original_id'] = original_id
        if origin not in facts[lookup[key]]['origins']:facts[lookup[key]]['origins'].append(origin)
    def values(field, value, source, path):
        if isinstance(value, list):
            if not value:add(field, [], source, path)
            for i, item in enumerate(value):values(field, item, source, path+'/'+str(i))
        elif isinstance(value, dict) and field.endswith('learning'):
            for k,v in sorted(value.items()):values('learning.'+k, v, source, path+'/'+k)
        elif isinstance(value, dict) and ('assessment' in value):
            # Keep each target's observed assessment and its candidate origins.
            for k,v in sorted(value['assessment'].items()):
                values('assessment.'+k, v, source, path+'/assessment/'+k)
            add('assessment.target', value['target_id'], source, path+'/target_id')
        else:add(field, value, source, path)
    for attempt in attempts:
        if not attempt:continue
        ref = _archive(store, task_id, operation_id, attempt)
        if ref in sources:continue
        source = len(sources); sources.append(ref)
        for field in ('learning', 'assessments', 'choice_assessments', 'review', 'disposition',
                      'application_failure', 'proposal_failure', 'actual_application_failure'):
            if field not in attempt:continue
            value = attempt[field]
            # Ordinary post review is a reviewed bundle with its own Web input.
            # Consume its actual opinions/dispositions, not its captured prompt.
            if field == 'review' and isinstance(value, dict) and 'review' in value:
                values('review', value['review'], source, '/review/review')
                values('disposition', value['disposition'], source, '/review/disposition')
            elif field.endswith('failure') and isinstance(value, dict):
                for k,v in sorted(value.items()):
                    if k != 'original':values(field+'.'+k,v,source,'/'+field+'/'+k)
            else:values(field,value,source,'/'+field)
        if observations:
            # Controller envelopes remain referenced originals. Additional
            # observations are semantic input, including future unknown keys;
            # they must not disappear just because they are not a named stage.
            containers={'id','task_id','operation_id','source_hash','policy_hash','created_at','status',
                'result_sha256','assessment_context','learning_input','choice_context','choices','learning_choices',
                'proposal_receipt','learning_composition','review_input','review_input_sha256','disposition_input',
                'disposition_input_sha256','current_choices','projection_version','binding','revision_consumed','governed_correction',
                'operation','result','pre','web','learning_projection','learning_correction_scope','instruction',
                'actual_revision_feedback','previous_post_draft','post_observation'}
            stages={'learning','assessments','choice_assessments','review','disposition',
                'application_failure','proposal_failure','actual_application_failure'}
            for field in sorted(set(attempt)-containers-stages):
                values('observation.'+field,attempt[field],source,'/'+field.replace('~','~0').replace('/','~1'))
                added_observations=True
    if not sources:return None
    return {'version': VERSION, 'sources': sources, 'facts': facts,
        **({'observation_projection':'post-observations-v1'} if added_observations else {}),
        'instruction': 'These are immutable prior proposals, observations and actual opinions, not an instruction to accept them or repeat every proposal. Every distinct semantic fact is retained with original lineage. Decide the current proposal against the exact current result and editable scope. Do not rewrite the referenced original, treat this view as a new success receipt, or repeat completed effects.'}


def _review_pair(attempt):
    review = attempt.get('review', {})
    if isinstance(review, dict) and 'review' in review:
        return review.get('review', {}), review.get('disposition', {}), '/review/review', '/review/disposition'
    return review, attempt.get('disposition', {}), '/review', '/disposition'


def _frontier_v1_revision_feedback(store, task_id, operation_id, attempts, *, observations=True):
    """Current proposal and unresolved feedback over the existing exact archive.

    Prior complete assessments belong to the reviewed historical proposal; they
    are not fresh judgments for every later revision. Keep the current ones,
    every actual opinion/response and failure, and the original evidence named
    by those opinions. This selects input, never certifies semantic compliance.
    """
    feedback = _historical_revision_feedback(store, task_id, operation_id, attempts, observations=observations)
    if feedback is None:return None
    originals = [store.record_get(ref['kind'], ref['id'])['value'] for ref in feedback['sources']]
    latest = max((i for i, value in enumerate(originals) if 'learning' in value), default=len(originals)-1)
    reviewed = set(); opinions = []; evidence = {}; captures = {}; unbound_sources=set()
    for index, attempt in enumerate(originals):
        review, disposition, rp, dp = _review_pair(attempt)
        if not isinstance(review, dict) or not isinstance(disposition, dict):continue
        responses = disposition.get('opinion_responses', [])
        rows = review.get('opinions', [])
        by_id = {r.get('opinion_id'): r for r in responses if isinstance(r, dict)}
        complete = (isinstance(rows, list) and len(by_id) == len(responses) == len(rows)
            and set(by_id) == {o.get('id') for o in rows if isinstance(o, dict)}
            and all(r.get('disposition') in {'accept','reject','investigate'} and r.get('rationale','').strip() for r in responses)
            and disposition.get('verdict') in {'proceed','revise','hold'} and bool(disposition.get('rationale','').strip()))
        if complete:reviewed.add(index)
        for position, opinion in enumerate(rows):
            if not isinstance(opinion, dict):continue
            response = by_id.get(opinion.get('id'))
            entry = {'opinion': deepcopy(opinion), 'response': deepcopy(response),
                'origin': {'source': index, 'path': rp+'/opinions/'+str(position)},
                'response_origin': {'source': index, 'path': dp+'/opinion_responses/'+str(responses.index(response))} if response else None,
                'state': 'rejected_with_reason_for_captured_scope' if complete and response['disposition']=='reject'
                    else 'unresolved_or_requires_changed_proposal_review'}
            # Supply exact cited originals, not IDs that imply the next actor
            # read a removed historical prompt. Unknown legacy references stay
            # in the complete opinion and its original archive.
            entry['evidence_keys'] = []
            if not opinion.get('evidence_refs'):unbound_sources.add(index)
            for ref in opinion.get('evidence_refs', []):
                try:bound = json.loads(ref)
                except (ValueError, TypeError):
                    unbound_sources.add(index);continue
                if not isinstance(bound, dict) or not bound.get('capture_id'):
                    unbound_sources.add(index);continue
                from . import semantic_wire as wire
                from . import models
                identity = bound['capture_id']
                if identity not in captures:
                    captured = store.record_get('semantic_wire_request', identity)
                    if not captured:raise PolicyError('Learning opinion lost its original captured evidence')
                    wire.load(store, {'id':identity,'sha256':digest(captured)}, captured['canonical_input'],
                        getattr(models,captured['canonical_schema']), captured['role'],captured['phase'],current=False)
                    captures[identity] = captured
                captured = captures[identity]
                selected = {'source_slot':bound['source_slot']} if 'source_slot' in bound else {'pointer':bound['pointer'],'quote':bound['quote']}
                if wire._source_reference(captured,store,selected) != bound:
                    raise PolicyError('Learning opinion original evidence binding changed')
                value = wire._reference_source(captured['canonical_input'],store)
                for token in bound['pointer'][1:].split('/'):
                    token=token.replace('~1','/').replace('~0','~')
                    value=value[int(token)] if isinstance(value,list) else value[token]
                if digest(value) != bound['value_sha256']:
                    raise PolicyError('Learning opinion original evidence value changed')
                key = digest(bound)
                evidence[key] = {'reference':bound,'value':deepcopy(value)}
                entry['evidence_keys'].append(key)
            opinions.append(entry)
    facts=[]
    for fact in feedback['facts']:
        field=fact['field']; kept=[]
        for origin in fact['origins']:
            index=origin['source']
            if field.startswith('learning.') and index != latest and index not in unbound_sources:continue
            if field.startswith('assessment.') and index != latest and index in reviewed and index not in unbound_sources:continue
            if field in {'review','disposition'} and index != latest and index in reviewed and index not in unbound_sources:continue
            kept.append(origin)
        if kept:facts.append(dict(fact,origins=kept))
    feedback.update(facts=facts, fact_projection='current-frontier-v1', opinion_frontier=opinions,
        opinion_evidence=evidence, current_proposal_source=latest,
        instruction='Decide the current proposal and unresolved findings against the actual current result. '
        'Earlier complete assessment detail remains in the exact archive; it is not a new assessment obligation. '
        'Every prior opinion and parent response is supplied with its exact cited original evidence. '
        'A reasoned rejection terminates that opinion for its captured scope under Q18; reuse it only when '
        'its evidence and assumptions still apply, and explain changed evidence if reconsideration is needed. '
        'Accepted/investigated opinions remain unresolved until the necessary changed proposal and review. '
        'No projection, absent opinion, archived reference or self-report proves a correction or completion.')
    return feedback


def revision_feedback(store, task_id, operation_id, attempts, *, observations=True):
    """Current facts with lossless judgment headers and value-bound evidence.

    Keep the saved v1 producer intact. Separating opinion rows from their stage
    judgments must not discard a revise/hold rationale or a Correction binding.
    Response coverage selects historical input; it does not settle the finding.
    """
    feedback = _frontier_v1_revision_feedback(store, task_id, operation_id, attempts,
                                              observations=observations)
    if feedback is None:return None
    originals = [store.record_get(ref['kind'], ref['id'])['value'] for ref in feedback['sources']]
    judgments = []; lookup = {}
    supplied = {entry[k]['source']: set() for entry in feedback['opinion_frontier']
                for k in ('origin', 'response_origin') if entry[k] is not None}
    for entry in feedback['opinion_frontier']:
        for k in ('origin', 'response_origin'):
            origin = entry[k]
            if origin is not None:supplied[origin['source']].add(origin['path'])
    for index, attempt in enumerate(originals):
        review, disposition, rp, dp = _review_pair(attempt)
        for field, value, pointer, member in (('review', review, rp, 'opinions'),
                                             ('disposition', disposition, dp, 'opinion_responses')):
            # Retain invalid/unmatched legacy rows too. Only exact rows already
            # supplied in the frontier are removed from this separate header.
            header = deepcopy(value)
            if isinstance(header, dict) and isinstance(header.get(member), list):
                remaining = {str(i): row for i, row in enumerate(header.pop(member))
                             if pointer+'/'+member+'/'+str(i) not in supplied.get(index, set())}
                if remaining:
                    header[member] = {'original_positions': remaining}
            if not header:continue
            key = digest({'field': field, 'value': header})
            if key not in lookup:
                lookup[key] = len(judgments)
                judgments.append({'field': field, 'value': header, 'origins': []})
            elif judgments[lookup[key]]['value'] != header:
                raise PolicyError('Learning judgment value digest collision')
            judgments[lookup[key]]['origins'].append({'source': index, 'path': pointer})
    evidence = {}; aliases = {}
    for old_key, item in feedback['opinion_evidence'].items():
        key = digest(item['value'])
        if key not in evidence:
            evidence[key] = {'value': item['value'], 'references': []}
        elif evidence[key]['value'] != item['value']:
            raise PolicyError('Learning evidence value digest collision')
        if item['reference'] not in evidence[key]['references']:
            evidence[key]['references'].append(item['reference'])
        aliases[old_key] = key
    for entry in feedback['opinion_frontier']:
        entry['evidence_keys'] = list(dict.fromkeys(aliases[key] for key in entry['evidence_keys']))
    feedback.update(fact_projection='current-frontier-v2', stage_judgments=judgments,
                    opinion_evidence=evidence,
                    facts=[fact for fact in feedback['facts'] if fact['field'] not in {'review', 'disposition'}])
    feedback['instruction'] += (
        ' Stage judgments retain every original header, verdict, rationale, Web reference and correction binding; '
        'their opinion/response rows are supplied once in opinion_frontier at their exact origin paths. '
        'Any unmatched rows retain original_positions. Evidence values are supplied once with all authenticated '
        'reference aliases. Equal values do not merge their source scope or make a response a successful correction.')
    return feedback


def validate_feedback(store, task_id, operation_id, feedback):
    if not feedback or feedback.get('version') != VERSION:return
    attempts = []
    for ref in feedback['sources']:
        record = store.record_get(ref.get('kind'), ref.get('id'))
        if (ref.get('kind') != 'learning_revision_source' or not record or digest(record) != ref.get('sha256')
                or record['id'] != digest({k:v for k,v in record.items() if k!='id'})
                or record['task_id'] != task_id or record['operation_id'] != operation_id):
            raise PolicyError('Learning current projection lost its exact revision source')
        attempts.append(record['value'])
    marker=feedback.get('observation_projection')
    if marker not in (None,'post-observations-v1'):
        raise PolicyError('Learning current observation projection version changed')
    projection=feedback.get('fact_projection')
    if projection not in (None,'current-frontier-v1','current-frontier-v2'):
        raise PolicyError('Learning current fact projection version changed')
    producer={None:_historical_revision_feedback, 'current-frontier-v1':_frontier_v1_revision_feedback,
              'current-frontier-v2':revision_feedback}[projection]
    if producer(store, task_id, operation_id, attempts, observations=marker is not None) != feedback:
        raise PolicyError('Learning current revision facts or lineage changed')
