"""Capacity-aware transport for complete, source-bound controller judgments.

Paging changes transport, never the stored operation/bundle or its authority.
Every original context fragment is actually supplied before a compact synopsis
is used. Synopses are model interpretations, not lossless replacements or proof
of semantic coverage. Exact targets, required ideas, opinions and application
anchors have separate non-summarized consumers and strict identity checks.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from uuid import uuid4
from types import SimpleNamespace

from pydantic import Field

from .models import (AssessmentBatch, Completion, ConfigurationRequired, Disposition, Idea,
                     Learning, LEARNING_SCHEMAS, LearningIdeas, LearningApplications, LearningSynthesis, LearningContractError, PolicyError, ResearchQuery, Review, SkillSelection, StrictModel, DISPOSITION_INSTRUCTION)
from .model_routing import resolve_lease
from .providers import ModelGateway, ProviderError, _token_estimate, request_configuration
from .store import canonical, digest
from .disposition_contracts import sources as disposition_sources, validate as validate_disposition
from .decisions import operation_targets
from .semantic_wire import WireCardinalityError, WirePartialAssessmentError
from .policy_admission import (SCHEMAS as ADMISSION_SCHEMAS, AdmissionContractError,
    validate_engineering, admission_input, admission_call_ref, validate_admission_call,
    retained_admission_input, _binding)


class ContextObservation(StrictModel):
    coverage_ids: list[str]
    summary: str = Field(min_length=1)
    limitations: list[str]


class InputCapacityError(ConfigurationRequired):
    """Only this dependent input is unrepresentable; no content was discarded."""


class _NoSecrets:
    def secret(self, name):
        return None


def _tokens(value):
    return _token_estimate(json.dumps(value, ensure_ascii=False))[0]


def _ids_exact(actual, expected, label):
    if len(actual) != len(set(actual)) or sorted(actual) != sorted(expected):
        raise PolicyError(label + ' omitted, duplicated or invented an identity')


def _join(values):
    return '\n'.join(v for v in values if v)


def _unique(values):
    result = []
    seen = set()
    for value in values:
        key = digest(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


class BoundedJudgments:
    def __init__(self, engine):
        self.e = engine
        self.raw_call = engine._call
        self._token_cache = {}
        self._learning_receipts = {}
        self._legacy_receipts = {}
        self.token_cache_hits = 0
        self.token_cache_misses = 0
        # Same message builder as the production gateway; this fallback is for
        # explicit offline gateways and never reads operator credentials.
        self.packer = engine.gateway if hasattr(engine.gateway, '_messages') else ModelGateway(_NoSecrets(), engine.policy)

    def _configuration(self, task_id, role):
        settings = getattr(self.e.gateway, 'settings', None)
        if settings is None:
            # Existing explicit fixtures have no configured capacity. Production
            # ModelGateway always has settings and cannot take this branch.
            return None
        values = settings.public() if hasattr(settings, 'public') else settings.get()
        task = self.e.store.get_task(task_id)
        if task.get('parent_id'):
            lease = task['state'].get('delegation_lease', {})
            selected = lease.get('model_leases', {}).get(role)
            if selected is None:
                raise ConfigurationRequired('Bounded judgment has no owned role model lease')
            original = lease.get('job_operation') or self.e.store.get_operation(lease['parent_operation_id'])['operation']
            parent = self.e.store.get_task(task['parent_id'])
            values = {**values, **resolve_lease(settings, selected, role=role,
                      job_context=self.e._model_job_context(parent, original, role))}
        if any(type(values.get(k)) is not int or values[k] <= 0 for k in ('model_context_tokens', 'max_output_tokens')):
            raise ConfigurationRequired('Configure context and output capacity before bounded judgment')
        return values

    def measure(self, task_id, phase, payload, schema, *, role=None):
        task = self.e.store.get_task(task_id)
        role = role or task['actor']
        values = self._configuration(task_id, role)
        model_input = self.e._model_input(task_id, phase, payload, schema, role=role)
        wire = self.e._wire_capture(task_id, phase, model_input, schema, role)
        return self._measure_input(phase, model_input, schema, role, values, semantic_wire=wire)

    def _measure_input(self, phase, model_input, schema, role, values, *, semantic_wire=None):
        # Captured historical input is measured as captured, never restamped as
        # the differently ordered current envelope. Configuration stays current.
        if values is None:
            return {'input_tokens_estimate': None, 'counting_method': 'explicit-offline-gateway-no-configured-capacity',
                    'context_tokens': None, 'reserved_output_tokens': None,
                    'model_input_sha256': digest(model_input), 'messages_sha256': None, 'fits': True}
        messages = self._messages(role, phase, model_input, schema, semantic_wire)
        serialized = json.dumps(messages, ensure_ascii=False)
        message_hash = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
        if message_hash in self._token_cache:
            count, method = self._token_cache[message_hash]
            self.token_cache_hits += 1
        else:
            count, method = _token_estimate(serialized)
            self._token_cache[message_hash] = (count, method)
            self.token_cache_misses += 1
        return {'input_tokens_estimate': count, 'counting_method': method,
                **({'semantic_wire': semantic_wire} if semantic_wire is not None else {}),
                **request_configuration(values, role),
                'model_input_sha256': digest(model_input), 'messages_sha256': digest(messages),
                'fits': values is None or count + values['max_output_tokens'] <= values['model_context_tokens']}

    def _messages(self, role, phase, model_input, schema, wire=None):
        packer = self.e.gateway if hasattr(self.e.gateway, '_messages') else self.packer
        return packer._messages(role, phase, model_input, schema,
            **({'semantic_wire': wire} if wire is not None else {}))

    def _wire_event(self, record, schema):
        """Exact successful call -> raw response -> associations -> canonical."""
        wire = record.get('measurement', {}).get('semantic_wire')
        if wire is None:return None
        trace = next((record[k] for k in ('wire_trace','learning_trace','admission_trace','disposition_trace')
                      if record.get(k, {}).get('event')), {})
        ref = trace.get('event', {})
        with self.e.store.lock:
            row = self.e.store.db.execute('SELECT * FROM events WHERE seq=?', (ref.get('seq'),)).fetchone()
        event = dict(row) if row else {};event['detail'] = json.loads(event['detail']) if event else {}
        if (event.get('hash') != ref.get('hash') or event.get('task_id') != record['task_id']
                or event.get('stage') != record['phase'] or event.get('status') != 'succeeded'
                or digest({k:v for k,v in event.items() if k not in {'seq','hash'}}) != event.get('hash')):
            raise PolicyError('WIRE_PROVENANCE: exact terminal event is absent or corrupt')
        from .semantic_wire import validate_wire_receipt
        validate_wire_receipt(self.e.store, record, event, schema)
        return event

    def _receipt_key(self, task, phase, payload, schema, role, measured):
        return digest({'task_id': task['id'], 'source_hash': task['source_hash'], 'policy_hash': self.e.policy.hash,
                       'phase': phase, 'role': role, 'schema': schema.model_json_schema(),
                       'payload': payload, 'measurement': measured})

    def _legacy_metadata(self, record, kind):
        if kind == 'bounded_model_truncated':
            return [record.get('metadata', {})]
        # Earlier successful bounded records predate a configuration snapshot.
        # Use the actual controller success with its retained provider response,
        # not the current settings relabelled as a past call.
        return [event['detail'].get('measurement', {}).get('usage', {})
                for event in self.e.store.events(record['task_id'])
                if event['stage'] == record['phase'] and event['status'] == 'succeeded'
                and digest(event['detail'].get('result')) == digest(record.get('result'))]

    def _legacy_provider_comparison(self, record, kind, task, role, measured):
        configuration = measured['configuration']
        if not configuration['provider'] or not configuration['model']:
            return 'unavailable'
        for metadata in self._legacy_metadata(record, kind):
            if not isinstance(metadata, dict) or not metadata.get('provider') or not metadata.get('requested_model'):
                continue
            reference = metadata.get('response_record')
            if not isinstance(reference, dict) or reference.get('status') != 'retained':
                continue
            response = self.e.store.record_get('model_response', reference.get('id'))
            expected = {'task_id': task['id'], 'source_hash': task['source_hash'],
                        'policy_hash': self.e.policy.hash, 'role': role, 'phase': record['phase'],
                        'messages_sha256': record['measurement']['messages_sha256']}
            if (not response or digest(response) != reference.get('record_sha256')
                    or any(response.get(name) != value or reference.get(name) != value
                           for name, value in expected.items())
                    or response.get('http_status') != 200 or reference.get('http_status') != 200
                    or response.get('request_sha256') != reference.get('request_sha256')
                    or not response.get('request_sha256')):
                continue
            if (metadata['provider'] != configuration['provider']
                    or metadata['requested_model'] != configuration['model']):
                return 'changed'
            if metadata.get('requested_reasoning') is not None:
                expected_reasoning = {'mode': 'provider_default' if configuration['reasoning_effort'] is None else 'configured',
                                      'effort': configuration['reasoning_effort']}
                if metadata['requested_reasoning'] != expected_reasoning:
                    return 'changed'
            return 'match'
        return 'unavailable'

    def _receipt(self, kind, key, task, phase, payload, schema, role, measured, *, legacy_partition_items=None):
        exact = self.e.store.record_get(kind, key)
        if exact:
            if digest(exact['payload']) != digest(payload) or exact['measurement'] != measured:
                raise PolicyError('Bounded model receipt identity changed')
            return exact
        if measured.get('semantic_wire') is not None:
            # A versioned wire changes only future transport. Try the exact old
            # envelope/schema/key before any fresh model send; never restamp it.
            model_input = self.e._model_input(task['id'], phase, payload, schema, role=role, installed_context=None)
            old_measurement = self._measure_input(phase, model_input, schema, role, self._configuration(task['id'], role))
            old_key = self._receipt_key(task, phase, payload, schema, role, old_measurement)
            old = self.e.store.record_get(kind, old_key)
            if old:
                if (old.get('measurement') != old_measurement or old.get('payload') != payload
                        or old.get('key') != old_key or self.e.store.record_get('bounded_model_call', old['id']) != old):
                    raise PolicyError('WIRE_PROVENANCE: historical canonical receipt changed')
                if kind == 'bounded_model_completed':
                    candidates = []
                    for event in self.e.store.events(task['id']):
                        detail = event['detail'];usage = detail.get('measurement', {}).get('usage', {})
                        ref = usage.get('response_record', {})
                        if (event['stage'] != phase or event['status'] != 'succeeded' or detail.get('actor') != role
                                or detail.get('result') != old.get('raw_result', old.get('result'))
                                or ref.get('messages_sha256') != old_measurement['messages_sha256']):continue
                        response = self.e.store.record_get('model_response', ref.get('id'))
                        expected = {'task_id':task['id'],'role':role,'phase':phase,'source_hash':task['source_hash'],
                                    'policy_hash':self.e.policy.hash,'messages_sha256':old_measurement['messages_sha256']}
                        if (not response or digest(response)!=ref.get('record_sha256')
                                or any(response.get(k)!=v or ref.get(k)!=v for k,v in expected.items())
                                or response.get('http_status')!=200 or response.get('request_sha256')!=ref.get('request_sha256')
                                or usage.get('provider')!=old_measurement['configuration']['provider']
                                or usage.get('requested_model')!=old_measurement['configuration']['model']
                                or digest({k:v for k,v in event.items() if k not in {'seq','hash'}})!=event['hash']):
                            raise PolicyError('WIRE_PROVENANCE: old canonical success response differs')
                        candidates.append(event)
                    if len(candidates)!=1:raise PolicyError('WIRE_PROVENANCE: old canonical success is missing or ambiguous')
                return old
        if 'configuration' not in measured:
            return None
        if kind == 'bounded_model_truncated' and (legacy_partition_items is None or len(legacy_partition_items) < 2):
            return None
        # This is compatibility with existing immutable receipts, not a new key
        # written over old wire evidence. Model-input equality and the original
        # key independently bind the full source, payload, role and JSON schema.
        # Old-format receipts cannot be created by this implementation. Index
        # them once per Store/Engine lifetime instead of repeatedly loading all
        # full modern requests on every ordinary cache miss.
        if kind not in self._legacy_receipts:
            self._legacy_receipts[kind] = [record for record in self.e.store.records(kind)
                                          if 'configuration' not in record.get('measurement', {})]
        for record in self._legacy_receipts[kind]:
            old = record.get('measurement', {})
            if ('configuration' in old or record.get('task_id') != task['id']
                    or record.get('phase') != phase or record.get('source_hash') != task['source_hash']
                    or record.get('policy_hash') != self.e.policy.hash
                    or digest(record.get('payload')) != digest(payload)
                    or any(old.get(name) != measured[name] for name in
                           ('model_input_sha256', 'context_tokens', 'reserved_output_tokens'))
                    or old.get('fits') is not True or not old.get('messages_sha256')
                    or self._receipt_key(task, phase, payload, schema, role, old) != record.get('key')):
                continue
            if self.e.store.record_get('bounded_model_call', record['id']) != record:
                continue
            if kind == 'bounded_model_truncated':
                if (record.get('status') != 'failed' or record.get('metadata', {}).get('truncated') is not True
                        or record.get('actual_model_input_sha256') != measured['model_input_sha256']
                        or digest(record.get('actual_model_input')) != measured['model_input_sha256']):
                    continue
            elif record.get('status') != 'succeeded':
                continue
            comparison = self._legacy_provider_comparison(record, kind, task, role, measured)
            if kind == 'bounded_model_completed' and comparison != 'changed':
                # Missing evidence is not evidence that this was another
                # request. Preserve the completed result without fabricating
                # old settings or automatically sending it again. An actually
                # recorded provider/model change permits a new judgment.
                raise ConfigurationRequired('A saved bounded result predates the complete configuration binding; '
                                            'preserve its original receipt and review this dependent result before reuse: ' + record['key'])
            if kind == 'bounded_model_truncated' and comparison == 'match':
                part = legacy_partition_items
                middle = len(part) // 2
                reference = {'id': record['id'], 'key': record['key'], 'sha256': digest(record)}
                children = [{'items_sha256': digest(child), 'item_count': len(child)}
                            for child in (part[:middle], part[middle:])]
                for split in self.e.store.records('bounded_output_split'):
                    if (split.get('task_id') == task['id'] and split.get('phase') == phase
                            and split.get('failed_call') == reference
                            and split.get('parent_items_sha256') == digest(part)
                            and split.get('children') == children
                            and split.get('id') == digest({name: value for name, value in split.items() if name != 'id'})):
                        return record
        return None

    def _fits(self, task_id, phase, payload, schema, role=None):
        measured = self.measure(task_id, phase, payload, schema, role=role)
        if not measured['fits']:
            return False
        maximum = measured['reserved_output_tokens']
        if maximum is None:
            return True
        # A minimum output shape bounds transport count, not the size or quality
        # of an unobserved model answer. The gateway still rejects truncation.
        if schema is AssessmentBatch:
            minimum = {'assessments': [{'target_id': t['id'], 'assessment': {
                'objective_link': 'specific', 'rationale': 'specific', 'success': 'unknown',
                'mistakes': [], 'recurrence': 'unknown', 'efficiency': 'specific',
                'interactions': 'specific', 'thinking_targets': {k: 'specific' for k in self.e.thinking_targets},
                'ideas': [{'id': 'new', 'target': 'task', 'proposal': 'specific', 'disposition': 'investigate', 'rationale': 'specific'}],
                'evidence_refs': []}} for t in payload['targets']]}
        elif schema is Learning:
            minimum = {'outcome_summary': 'specific', 'classifications': ['organize'], 'skill_updates': [],
                       'recurrence': 'unknown', 'next_use_trigger': 'specific', 'ideas': payload.get('required_ideas', []),
                       'evidence_refs': [], 'applications': []}
        elif schema is Disposition:
            minimum = {'verdict': 'hold', 'rationale': 'specific', 'opinion_responses': [
                {'opinion_id': o['id'], 'disposition': 'investigate', 'rationale': 'specific'}
                for o in payload['review']['opinions']], 'web_refs': [s['id'] for s in disposition_sources(payload)]}
        elif schema is ContextObservation:
            minimum = {'coverage_ids': [r['id'] for r in payload['source_records']], 'summary': 'specific', 'limitations': []}
        elif schema is SkillSelection:
            minimum = {'selected': [], 'rejected': [{'id': s['id'], 'reason': 'specific'} for s in payload['knowledge']['skills']],
                       'new_knowledge_needed': [], 'rationale': 'specific'}
        else:
            return True
        return _tokens(minimum) < maximum

    def _validate(self, schema, value, payload, *, task_id=None, learning_receipt=None):
        validate_engineering(schema, value, payload)
        if schema is ContextObservation:
            _ids_exact(value.coverage_ids, [r['id'] for r in payload['source_records']], 'Context source coverage')
        elif schema is AssessmentBatch:
            _ids_exact([a.target_id for a in value.assessments], [t['id'] for t in payload['targets']], 'Assessment batch')
        elif schema is Review:
            _ids_exact([o.id for o in value.opinions], [o.id for o in value.opinions], 'Single response review opinions')
        elif schema is Disposition:
            validate_disposition(payload, value)
        elif schema in LEARNING_SCHEMAS and schema is not Learning:
            owned = set()
            if learning_receipt is not None:
                authenticated = self._learning_call(task_id, learning_receipt, schema)
                if (authenticated != value or self.learning_owner(payload, learning_receipt['phase'])
                        != self.learning_owner(learning_receipt['payload'], learning_receipt['phase'])):
                    raise PolicyError('LEARNING_PROVENANCE: saved output is not owned by this exact focus')
                owned = {i.id for i in authenticated.ideas}
            self.validate_learning_unit(task_id, schema, value, payload, owned_ideas=owned)
        elif schema is Learning:
            self.e._validate_ideas(payload.get('required_ideas', []), value)
            if task_id is not None:
                self.e._validate_learning_proposal(task_id, value, payload)
            if payload.get('source_packet_id'):
                allowed = {s['id'] for s in payload.get('pre', {}).get('skills', {}).get('selected', [])}
                if any(a.get('skill_id') not in allowed for a in value.applications):
                    raise PolicyError('Paged learning application lacks its exact selected Skill consumer')
                packet = self.e.store.record_get('bounded_input', payload['source_packet_id'])
                reserved = {i['id'] for i in (packet or {}).get('value', {}).get('required_ideas', [])}
                focus = {i['id'] for i in payload.get('required_ideas', [])}
                if any(i.id in reserved - focus for i in value.ideas):
                    raise PolicyError('Learning page attempted to dispose an idea belonging to another exact focus page')

    @staticmethod
    def learning_owner(payload, phase):
        return digest({'phase': phase, 'source_packet_id': payload.get('source_packet_id'),
            'operation_id': payload.get('operation', {}).get('id'),
            'focus': payload.get('learning_focus', {
                'ideas': [{k: i[k] for k in ('id', 'proposal', 'target')} for i in payload.get('required_ideas', [])],
                'selection': payload.get('pre', {}).get('skills', {})})})

    def learning_reserved_ideas(self, task_id, payload):
        packet = self.e.store.record_get('bounded_input', payload.get('source_packet_id')) if payload.get('source_packet_id') else None
        reserved = {i['id'] for i in (packet or {}).get('value', {}).get('required_ideas', [])}
        operation_id = payload.get('operation', {}).get('id')
        reserved.update(i['id'] for i in self.e._learning_response_history(task_id, operation_id)['ideas'])
        for record in self.e.store.records('bounded_model_call'):
            if (record.get('task_id') == task_id and record.get('operation_id') == operation_id
                    and record.get('status') == 'succeeded'):
                reserved.update(i['id'] for i in record.get('result', {}).get('ideas', []))
        return reserved

    def learning_correction_ideas(self, task_id, payload, proposed):
        if not payload.get('source_packet_id'):
            return proposed
        focus = {i['id'] for i in payload.get('required_ideas', [])}
        reserved = self.learning_reserved_ideas(task_id, payload)
        return [i for i in proposed if i['id'] in focus or i['id'] not in reserved]

    def application_owned_ideas(self, task_id, payload, *, before_seq=None):
        """Output namespace at the actual request boundary, not a use claim.

        A later completed sibling must not alter an earlier captured prompt on
        reopen. Its actual started-event frontier selects this same derivation.
        """
        packet = self.e.store.record_get('bounded_input', payload.get('source_packet_id')) if payload.get('source_packet_id') else None
        owned = {i['id'] for i in (packet or {}).get('value', {}).get('required_ideas', [])}
        operation_id = payload.get('operation', {}).get('id')
        for original in self.e._learning_response_history(task_id, operation_id)['originals']:
            if before_seq is None or original['source_event']['seq'] < before_seq:
                owned.update(original['idea_ids'])
        for record in self.e.store.records('bounded_model_call'):
            if (record.get('task_id') != task_id or record.get('operation_id') != operation_id
                    or record.get('status') != 'succeeded'):
                continue
            terminal = record.get('learning_trace', {}).get('event', {})
            if before_seq is None or (type(terminal.get('seq')) is int and terminal['seq'] < before_seq):
                owned.update(i['id'] for i in record.get('result', {}).get('ideas', []))
        return owned

    def validate_learning_unit(self, task_id, schema, value, payload, *, owned_ideas=frozenset()):
        self.e._validate_ideas(payload.get('required_ideas', []), value)
        if payload.get('source_packet_id'):
            packet = self.e.store.record_get('bounded_input', payload['source_packet_id'])
            task = self.e.store.get_task(task_id)
            if (not packet or packet['id'] != digest({k: v for k, v in packet.items() if k != 'id'})
                    or packet['task_id'] != task_id or packet['source_hash'] != task['source_hash']
                    or packet['policy_hash'] != self.e.policy.hash):
                raise PolicyError('LEARNING_PROVENANCE: output focus source packet differs')
            original = packet['value']
            validated = self.learning_validation_input(task_id, payload)
            if (validated.get('operation') != original.get('operation') or validated.get('result') != original.get('result')):
                raise PolicyError('LEARNING_PROVENANCE: output focus operation/result differs from its source')
            reserved = self.learning_reserved_ideas(task_id, payload)
            focus = {i['id'] for i in payload.get('required_ideas', [])}
            if any(i.id in reserved - focus - owned_ideas for i in value.ideas):
                raise LearningContractError('Learning decision returned an Idea outside its exact output focus')
        if schema is LearningIdeas:
            return
        selected = payload.get('pre', {}).get('skills', {}).get('selected', [])
        if schema is LearningApplications:
            _ids_exact(value.considered_skill_ids, [s['id'] for s in selected], 'Learning application decisions')
            applications = value.applications
            updates = []
        else:
            composition = self._composition(payload)
            applications = composition.get('applications', [])
            reserved = {i['id'] for i in composition.get('idea_decisions', [])}
            if any(i.id in reserved for i in value.ideas):
                raise LearningContractError('Lifecycle output repeated an already owned Idea decision')
            updates = value.skill_updates
        original = self.learning_validation_input(task_id, payload)
        self.e.knowledge.validate_learning_proposal(self.e.store.get_task(task_id), original['operation'],
            SimpleNamespace(skill_updates=updates, applications=applications, ideas=value.ideas),
            original['result'], [s['id'] for s in selected])

    @staticmethod
    def _call_ref(record):
        return {'id': record['id'], 'key': record['key'], 'sha256': digest(record)}

    @staticmethod
    def _same_evidence_input(current, retained):
        """Only this controller-generated pointer list has legacy ordering."""
        if current == retained:
            return True
        left = deepcopy(current); right = deepcopy(retained)
        try:
            a = left['application_contract'].pop('result_evidence')
            b = right['application_contract'].pop('result_evidence')
        except (KeyError, TypeError):
            return False
        if not isinstance(a, list) or not isinstance(b, list):
            return False
        # No alias, duplicate, omitted member or arbitrary array reordering.
        return (left == right and len({canonical(x) for x in a}) == len(a)
                and len({canonical(x) for x in b}) == len(b)
                and sorted(a, key=canonical) == sorted(b, key=canonical))

    def _learning_history_values(self, task_id, role, measured):
        values = self._configuration(task_id, role)
        if values is not None:
            current = request_configuration(values, role)
            if (measured.get('reserved_output_tokens') == 32768
                    and current['reserved_output_tokens'] == 65536
                    and measured.get('context_tokens') == current['context_tokens']
                    and measured.get('configuration') == current['configuration']):
                # Authenticate the old request with its actual reservation. This
                # is not a send configuration or a re-keyed historical success.
                values = dict(values, max_output_tokens=32768)
        return values

    def _same_learning_envelope(self,current,captured):
        # Only the send-time operation preview is allowed to age. The captured
        # bytes still have to pass the original measurement/event/response chain.
        # This does not expand the sole legacy evidence-list ordering exception.
        if not isinstance(captured,dict) or not isinstance(captured.get('history_lookup'),dict):return False
        comparable=deepcopy(current);comparable['history_lookup']=deepcopy(captured['history_lookup'])
        return self._same_evidence_input(comparable,captured)

    def _learning_history_input(self, task_id, record, schema, role):
        trace = record.get('learning_trace', {})
        captured = trace.get('request_model_input') if isinstance(trace, dict) else None
        captured = captured if captured is not None else record.get('actual_model_input', {})
        legacy = schema is LearningApplications and isinstance(captured, dict) and 'application_output_contract' not in captured
        arguments = {'legacy_application': True} if legacy else {}
        # Reconstruct the saved format, not today's installed environment. The
        # original measurement/event/wire chain below authenticates these bytes.
        arguments['installed_context'] = deepcopy(captured.get('installed_controller_context')) if isinstance(captured, dict) else None
        arguments['captured_input'] = captured if isinstance(captured, dict) else None
        if schema is LearningApplications and not legacy and isinstance(trace, dict):
            start_ref = trace.get('started_event', {})
            if type(start_ref.get('seq')) is not int:
                raise PolicyError('LEARNING_PROVENANCE: Application ownership boundary is absent')
            starts = [e for e in self.e.store.events(task_id, after=start_ref['seq'] - 1) if e['seq'] == start_ref['seq']]
            if (len(starts) != 1 or starts[0]['hash'] != start_ref.get('hash')
                    or starts[0]['stage'] != record['phase'] or starts[0]['status'] != 'started'
                    or starts[0]['detail'] != {'actor': role, 'target': record['payload'].get('target', record['payload'].get('operation', {}).get('id')), 'policy_hash': self.e.policy.hash}
                    or digest({k: v for k, v in starts[0].items() if k not in {'seq', 'hash'}}) != starts[0]['hash']):
                raise PolicyError('LEARNING_PROVENANCE: Application ownership boundary changed')
            arguments['application_before_seq'] = start_ref['seq']
        return self.e._model_input(task_id, record['phase'], record['payload'], schema, role=role,
                                   **arguments)

    def _retained_learning_request(self, task_id, record, schema, role):
        current = self._learning_history_input(task_id, record, schema, role)
        trace = record.get('learning_trace')
        candidates = {}
        if trace:
            if not isinstance(trace, dict):
                raise PolicyError('LEARNING_PROVENANCE: captured request trace is corrupt')
            captured = trace.get('request_model_input')
            # Operation rows grow after a reviewed result and after a governed
            # correction. Their current preview is applicability context, not
            # the immutable request which produced this retained response.
            if not self._same_learning_envelope(current,captured):
                raise PolicyError('LEARNING_PROVENANCE: captured request differs beyond evidence ordering')
            candidates[digest(captured)] = captured
        else:
            candidates[digest(current)] = current
            # Pre-trace calls have no full successful request. A failed sibling
            # can retain the exact old pointer list, including nested producer
            # order. Copy only that representation, never its source/history or
            # role/configuration. The target's complete old measurement and its
            # own terminal response must authenticate the resulting envelope.
            for sibling in self.e.store.records('bounded_model_call'):
                if (sibling.get('task_id') != task_id
                        or sibling.get('phase') != record['phase']
                        or sibling.get('source_hash') != record.get('source_hash')
                        or sibling.get('policy_hash') != record.get('policy_hash')):
                    continue
                actual = sibling.get('actual_model_input')
                if actual is None:
                    continue
                if not isinstance(actual, dict):
                    raise PolicyError('LEARNING_PROVENANCE: retained representation source is corrupt')
                if (actual.get('operation') != current.get('operation')
                        or actual.get('result') != current.get('result')):
                    continue
                if digest(actual) != sibling.get('actual_model_input_sha256'):
                    raise PolicyError('LEARNING_PROVENANCE: retained representation source is corrupt')
                evidence = actual.get('application_contract', {}).get('result_evidence')
                if evidence is None:
                    continue
                candidate = deepcopy(current)
                candidate['application_contract']['result_evidence'] = deepcopy(evidence)
                if self._same_evidence_input(current, candidate):
                    candidates[digest(candidate)] = candidate
        values = self._learning_history_values(task_id, role, record.get('measurement', {}))
        matches = [(candidate, self._measure_input(record['phase'], candidate, schema, role, values,
                    semantic_wire=record.get('measurement', {}).get('semantic_wire')))
                   for candidate in candidates.values()
                   if digest(candidate) == record.get('measurement', {}).get('model_input_sha256')]
        matches = [(candidate, measured) for candidate, measured in matches if measured == record.get('measurement')]
        if len(matches) != 1:
            raise PolicyError('LEARNING_PROVENANCE: exact retained input representation/configuration cannot be authenticated')
        return matches[0]

    @staticmethod
    def _learning_feedback_input(model_input, bound):
        continuation = bound.get('next_input', bound['actual'])
        if bound.get('restored_response') is not None:
            return deepcopy(continuation)
        result = deepcopy(model_input)
        result.pop('installed_controller_context', None)
        for key in ('installed_controller_context', 'actual_format_feedback', 'interrupted_model_resume'):
            if key in continuation:
                result[key] = deepcopy(continuation[key])
        return result

    def _learning_feedback_parent(self, task_id, record, schema, role, request, seen=(), *, start=None):
        """One parent-to-next-input contract for failed and successful returns."""
        trace = record['learning_trace']; payload = record['payload']; phase = record['phase']
        ref = trace['feedback_recovery']; previous = self.e.store.record_get('bounded_model_call', ref.get('id'))
        if (not previous or self._call_ref(previous) != ref
                or self._learning_source_core(previous['payload']) != self._learning_source_core(payload)
                or record.get('learning_owner') != self.learning_owner(payload, phase)):
            raise PolicyError('LEARNING_PROVENANCE: correction lost its exact parent or own focus')
        if start is None:
            starts = [e for e in self.e.store.events(task_id)
                if trace.get('started_event') == {'seq': e['seq'], 'hash': e['hash']}]
            if len(starts) != 1:
                raise PolicyError('LEARNING_PROVENANCE: correction start is missing or ambiguous')
            start = starts[0]
        if (start['stage'] != phase or start['status'] != 'started'
                or start['detail'] != {'actor': role, 'target': payload.get('target', payload.get('operation', {}).get('id')), 'policy_hash': record['policy_hash']}
                or digest({k: v for k, v in start.items() if k not in {'seq', 'hash'}}) != start['hash']):
            raise PolicyError('LEARNING_PROVENANCE: correction start identity changed')
        prior = self._failed_learning_feedback(task_id, previous, schema, role, seen, partition=True,
            resume_ref=trace.get('interrupted_resume', {}).get('resume_event'))
        if prior['held'] or prior['terminal_seq'] >= start['seq']:
            raise PolicyError('LEARNING_PROVENANCE: correction continued a held or later request')
        prior_input = prior.get('next_input', prior['actual'])
        self.e._validate_ideas(prior_input['required_ideas'], LearningIdeas(ideas=request.get('required_ideas', [])))
        if trace.get('interrupted_resume') != prior.get('interrupted_resume'):
            raise PolicyError('LEARNING_PROVENANCE: correction lost its exact normal resume')
        return prior, self._learning_feedback_input(request, prior)

    def _learning_call(self, task_id, record, schema, *, rejected=False):
        """Authenticate stored success before use, including pre-link wire calls.

        Result equality locates possible events; request bytes, role, current
        configuration, retained response and the original call authenticate one.
        No historical record is rewritten or relabelled as a current request.
        """
        if record.get('status') == 'revalidated':
            return self._revalidated_synthesis(task_id, record, schema)
        task = self.e.store.get_task(task_id)
        self.e._assert_parent_source(task)
        if task_id in self.e.stop_requested:
            import asyncio
            raise asyncio.CancelledError()
        if self.e.quiescing and self.e.quiescing != task_id:
            from .engine import Quiesced
            raise Quiesced('Controller update is waiting for this Learning safe boundary')
        role = task['actor']
        payload = record.get('payload', {})
        request, measured = self._retained_learning_request(task_id, record, schema, role)
        if (record.get('task_id') != task_id or record.get('source_hash') != task['source_hash']
                or record.get('policy_hash') != self.e.policy.hash or record.get('status') != ('rejected' if rejected else 'succeeded')
                or record.get('measurement') != measured
                or record.get('key') != self._receipt_key(task, record['phase'], payload, schema, role, measured)
                or self.e.store.record_get('bounded_model_call', record['id']) != record
                or (not rejected and self.e.store.record_get('bounded_model_completed', record['key']) != record)):
            raise PolicyError('LEARNING_PROVENANCE: retained call/source/configuration identity differs')
        raw = record.get('raw_result', record['result'])
        trace = record.get('learning_trace')
        if trace and trace.get('feedback_recovery'):
            _, next_input = self._learning_feedback_parent(task_id, record, schema, role, request, (record['id'],))
            rejections = trace.get('rejected_events', [])
            if rejections:
                first = [e for e in self.e.store.events(task_id)
                    if rejections[0] == {'seq': e['seq'], 'hash': e['hash']}]
                if (len(first) != 1 or first[0]['stage'] != record['phase'] or first[0]['status'] != 'rejected_model_output'
                        or digest({k: v for k, v in first[0].items() if k not in {'seq', 'hash'}}) != first[0]['hash']):
                    raise PolicyError('LEARNING_PROVENANCE: correction first response changed')
                self._feedback_response(task, record['phase'], schema, role, next_input, first[0]['detail']['metadata'], measured, 200)
            elif trace.get('actual_model_input') != next_input:
                raise PolicyError('LEARNING_PROVENANCE: successful correction did not consume its exact next input')
        actual = trace.get('actual_model_input') if trace else request
        if not isinstance(actual, dict):
            raise PolicyError('LEARNING_PROVENANCE: exact actual input is missing or corrupt')
        if trace and (trace.get('call_id') != record['id'] or trace.get('role') != role
                or trace.get('lease') != task['state'].get('delegation_lease')
                or trace.get('request_model_input') != request):
            raise PolicyError('LEARNING_PROVENANCE: captured request, role or lease differs')
        wire = record.get('wire_trace', {}).get('actual_capture')
        if measured.get('semantic_wire') and not wire:
            raise PolicyError('WIRE_PROVENANCE: retained Learning lost its actual capture')
        expected_messages = digest(self._messages(role, record['phase'], actual, schema, wire))
        events = []
        for event in self.e.store.events(task_id):
            detail = event['detail']
            if event['stage'] != record['phase'] or event['status'] != 'succeeded' or detail.get('actor') != role or detail.get('result') != raw:
                continue
            if trace and (trace.get('event') != {'seq': event['seq'], 'hash': event['hash']}
                    or detail.get('learning_call_id') != record['id']
                    or detail.get('learning_request_sha256') != digest(request)
                    or detail.get('learning_actual_input_sha256') != digest(actual)):
                continue
            if digest({k: v for k, v in event.items() if k not in {'seq', 'hash'}}) != event['hash']:
                raise PolicyError('LEARNING_PROVENANCE: terminal event bytes changed')
            if trace and detail.get('learning_owner') != self.learning_owner(payload, record['phase']):
                raise PolicyError('LEARNING_PROVENANCE: exact terminal focus changed')
            metadata = detail.get('measurement', {}).get('usage', {}) or {}
            config = measured.get('configuration', {})
            if config.get('provider') or config.get('model') or not trace:
                reference = metadata.get('response_record', {})
                response = self.e.store.record_get('model_response', reference.get('id')) if reference.get('id') else None
                bound = {'task_id': task_id, 'source_hash': task['source_hash'], 'policy_hash': self.e.policy.hash,
                         'role': role, 'phase': record['phase'], 'messages_sha256': expected_messages}
                if (not response or digest(response) != reference.get('record_sha256')
                        or any(response.get(k) != v or reference.get(k) != v for k, v in bound.items())
                        or not response.get('request_sha256') or response['request_sha256'] != reference.get('request_sha256')
                        or response.get('http_status') != 200 or reference.get('http_status') != 200
                        or metadata.get('provider') != config.get('provider') or metadata.get('requested_model') != config.get('model')):
                    continue
            events.append(event)
        if len(events) != 1:
            raise PolicyError('LEARNING_PROVENANCE: exact terminal request/event/response is missing or ambiguous')
        self._wire_event(record, schema)
        if rejected:
            return schema.model_validate(raw)
        mapping = record.get('idea_namespaces', [])
        required = {i['id'] for i in actual.get('required_ideas', [])}
        response_id = digest({'source_call_key': record['key'], 'original_response': raw})
        expected_map = [{'response_id': response_id, 'original_id': i['id'],
            'controller_id': i['id'] if i['id'] in required or measured.get('semantic_wire') else 'bounded-idea:' + digest({'response_id': response_id, 'original_id': i['id']})}
            for i in raw.get('ideas', [])]
        normalized = dict(raw, ideas=[dict(i, id=m['controller_id']) for i, m in zip(raw.get('ideas', []), expected_map)])
        if mapping != expected_map or record['result'] != normalized:
            raise PolicyError('LEARNING_PROVENANCE: response Idea identity mapping differs')
        return schema.model_validate(record['result'])

    def _learning_return(self, task_id, phase, payload, schema, role, measured):
        task = self.e.store.get_task(task_id)
        key = self._receipt_key(task, phase, payload, schema, role, measured)
        link = self.e.store.record_get('bounded_learning_return', key)
        if link and 'rejected_event' in link:
            terminal = self.e.store.record_get('bounded_model_call', link.get('terminal', {}).get('id'))
            if (not terminal or self._call_ref(terminal) != link['terminal']
                    or terminal.get('recovered_from') != link.get('original')
                    or terminal.get('rejected_event') != link['rejected_event']
                    or terminal.get('key') != key or terminal.get('payload') != payload):
                raise PolicyError('LEARNING_PROVENANCE: revalidated return link changed')
            value = self._learning_call(task_id, terminal, schema)
            self._learning_receipts[digest(value.model_dump())] = terminal
            return value
        if not link and self.e.store.record_get('bounded_model_completed', key):
            return None
        roots = [r for r in self.e.store.records('bounded_model_call')
                 if r.get('key') == key and r.get('status') in {'rejected', 'succeeded'}]
        if link:
            roots = [r for r in roots if self._call_ref(r) == link.get('original')]
            if len(roots) != 1:
                raise PolicyError('LEARNING_PROVENANCE: corrected return lost its exact original')
        if not roots:
            return None
        if len(roots) != 1:
            raise PolicyError('LEARNING_PROVENANCE: original correction request is ambiguous')
        root = roots[0]
        all_calls = self.e.store.records('bounded_model_call')
        terminals = []
        def walk(parent, seen):
            if parent['id'] in seen:
                raise PolicyError('LEARNING_PROVENANCE: correction lineage is cyclic')
            seen = seen | {parent['id']}
            for child in all_calls:
                if child.get('task_id') != task_id or child.get('phase') != phase or child['id'] in seen:
                    continue
                feedback = child.get('payload', {}).get('actual_format_feedback', {})
                explicit = child.get('learning_parent', feedback.get('source_receipt'))
                base = {k: v for k, v in child['payload'].items() if k != 'actual_format_feedback'}
                prior = {k: v for k, v in parent['payload'].items() if k != 'actual_format_feedback'}
                if explicit is not None and explicit != self._call_ref(parent):
                    if base == prior and feedback.get('rejected_response') == parent.get('result'):
                        raise PolicyError('LEARNING_PROVENANCE: correction parent reference differs')
                    continue
                valid = (base == prior and feedback.get('rejected_response') == parent.get('raw_result', parent.get('result'))
                         and feedback.get('validation') == parent.get('first_fault'))
                if explicit is not None:
                    valid = (feedback.get('source_receipt') == explicit
                        and feedback.get('rejected_response') == parent.get('result')
                        and all(child['payload'].get(k) == v for k, v in prior.items() if k != 'required_ideas'))
                if valid and explicit is not None:
                    child_ideas = {i['id']: i for i in child['payload'].get('required_ideas', [])}
                    valid = all(i['id'] in child_ideas and all(child_ideas[i['id']][k] == i[k] for k in ('proposal', 'target'))
                                for i in parent['payload'].get('required_ideas', []))
                if not valid:
                    if explicit is not None:
                        raise PolicyError('LEARNING_PROVENANCE: explicit correction does not bind retained feedback')
                    continue
                if child.get('source_hash') != task['source_hash'] or child.get('policy_hash') != self.e.policy.hash:
                    raise PolicyError('LEARNING_PROVENANCE: correction source changed')
                self._learning_call(task_id, parent, schema, rejected=parent.get('status') == 'rejected')
                if child.get('status') == 'succeeded':
                    terminals.append(child)
                    if self.e.store.record_get('bounded_learning_return', child['key']):
                        walk(child, seen)
                elif child.get('status') == 'rejected':
                    walk(child, seen)
        walk(root, set())
        if link:
            terminals = [r for r in terminals if self._call_ref(r) == link.get('terminal')]
        if not terminals:
            if link:
                raise PolicyError('LEARNING_PROVENANCE: stored correction return is corrupt')
            return None
        if len(terminals) != 1:
            raise PolicyError('LEARNING_PROVENANCE: successful correction return is ambiguous')
        terminal = terminals[0]
        value = self._learning_call(task_id, terminal, schema)
        self.e.store.record('bounded_learning_return', key, {'original': self._call_ref(root), 'terminal': self._call_ref(terminal)})
        self._learning_receipts[digest(value.model_dump())] = terminal
        return value

    def _old_learning_request(self, task_id, phase, payload, schema):
        """Locate an exact old request before a newly ordered prompt is sent."""
        task = self.e.store.get_task(task_id)
        matches = []
        calls=self.e.store.records('bounded_model_call')
        for record in calls:
            old = record.get('payload', {})
            if (record.get('task_id') == task_id and record.get('phase') == phase
                    and record.get('learning_schema', 'Learning') == schema.__name__
                    and old.get('operation', {}).get('id') == payload.get('operation', {}).get('id')
                    and old.get('learning_focus') == payload.get('learning_focus')
                    and (record.get('status') in {'started', 'unobserved'}
                         or record.get('metadata', {}).get('request_effect') == 'response-unobserved')):
                # New presentation or source bytes cannot make an unknown send
                # unexecuted. Independent phases/focuses retain their own work.
                head=record;seen={head['id']}
                while True:
                    children=[r for r in calls if r.get('learning_trace',{}).get('feedback_recovery',{}).get('id')==head['id']]
                    if len(children)>1:raise PolicyError('LEARNING_PROVENANCE: interrupted continuation is ambiguous')
                    if not children:break
                    child=children[0]
                    if (child['id'] in seen or child['learning_trace']['feedback_recovery']!=self._call_ref(head)
                            or any(child.get(k)!=record.get(k) for k in ('task_id','phase','source_hash','policy_hash','learning_schema'))
                            or self._learning_source_core(child['payload'])!=self._learning_source_core(record['payload'])):
                        raise PolicyError('LEARNING_PROVENANCE: interrupted continuation parent/source changed')
                    seen.add(child['id']);head=child
                if head is not record:
                    if head.get('status')=='succeeded':
                        self._learning_call(task_id,head,schema)
                        if self._learning_source_core(head['payload'])==self._learning_source_core(payload):matches.append(head)
                    else:
                        self._failed_learning_feedback(task_id,head,schema,task['actor'],partition=True)
                    continue
                if not self._observed_learning_rejection(task_id, record, schema):
                    raise PolicyError('LEARNING_PROVENANCE: unobserved saved Learning request; no resend')
            if (record.get('task_id') != task_id or record.get('phase') != phase
                    or record.get('source_hash') != task['source_hash'] or record.get('policy_hash') != self.e.policy.hash
                    or record.get('learning_schema', 'Learning') != schema.__name__
                    or record.get('payload') != payload or record.get('status') not in {'succeeded', 'rejected'}):
                continue
            self._learning_call(task_id, record, schema, rejected=record['status'] == 'rejected')
            matches.append(record)
        matches=list({r['id']:r for r in matches}.values())
        if len({r['key'] for r in matches}) > 1:
            raise PolicyError('LEARNING_PROVENANCE: retained request has ambiguous representations')
        return matches[0] if matches else None

    def _observed_learning_rejection(self, task_id, record, schema):
        """Use the ordinary feedback decoder; a label alone cannot clear a send."""
        trace = record.get('learning_trace', {})
        if (record.get('status') != 'unobserved'
                or record.get('metadata', {}).get('request_effect') == 'response-unobserved'
                or not isinstance(trace, dict) or not isinstance(trace.get('actual_model_input'), dict)
                or not isinstance(trace.get('started_event'), dict)):
            return False
        self._failed_learning_feedback(task_id, record, schema, self.e.store.get_task(task_id)['actor'],
            partition=self.e.disposition_recovery.has_interrupted_resume(record))
        return True

    def _feedback_response(self, task, phase, schema, role, model_input, metadata, measured, status):
        reference = metadata.get('response_record', {})
        response = self.e.store.record_get('model_response', reference.get('id')) if reference.get('id') else None
        binding = {'task_id': task['id'], 'source_hash': task['source_hash'], 'policy_hash': self.e.policy.hash,
                   'role': role, 'phase': phase, 'messages_sha256': digest(self._messages(role, phase, model_input, schema, metadata.get('semantic_wire_capture')))}
        config = measured.get('configuration', {})
        if (not response or digest(response) != reference.get('record_sha256')
                or any(response.get(k) != v or reference.get(k) != v for k, v in binding.items())
                or not response.get('request_sha256') or response['request_sha256'] != reference.get('request_sha256')
                or response.get('http_status') != status or reference.get('http_status') != status
                or metadata.get('provider') != config.get('provider') or metadata.get('requested_model') != config.get('model')
                or metadata.get('role') != role
                or (metadata.get('semantic_wire_capture') is not None
                    and response.get('semantic_wire_capture') != metadata['semantic_wire_capture'])):
            raise PolicyError('LEARNING_PROVENANCE: saved correction response/request binding differs')

    def _outer_cardinality(self, record, schema, role, model_input, metadata):
        if schema not in (Learning, LearningIdeas, AssessmentBatch):
            return None
        task_id = record['task_id']
        if task_id in self.e.stop_requested:
            import asyncio
            raise asyncio.CancelledError()
        if self.e.quiescing and self.e.quiescing != task_id:
            raise PolicyError('Controller update is waiting for the partition safe boundary')
        values = self._configuration(task_id, role)
        current = request_configuration(values, role) if values is not None else {}
        measured = record.get('measurement', {})
        if any(measured.get(k) != current.get(k) for k in ('configuration', 'context_tokens', 'reserved_output_tokens')):
            raise PolicyError('WIRE_PROVENANCE: cardinality partition settings changed')
        classify = getattr(self.e.gateway, 'wire_response_cardinality', None)
        if classify is None:
            return None
        return classify(metadata, task_id=task_id, role=role, phase=record['phase'],
            policy_hash=record['policy_hash'], source_hash=record['source_hash'],
            model_input=model_input, schema=schema)

    def _failed_learning_feedback(self, task_id, record, schema, role, seen=(), *, partition=False, complete_return=False, resume_ref=None):
        """Reconstruct the controller's actual sends; no failed result is adopted."""
        def hold(reason):
            raise PolicyError('LEARNING_PROVENANCE: ' + reason)
        if complete_return and schema not in (Learning, LearningSynthesis):
            hold('complete rejected return is outside its whole-proposal consumer')
        task = self.e.store.get_task(task_id)
        if record.get('id') in seen:
            hold('saved correction lineage is cyclic')
        seen = (*seen, record.get('id'))
        phase = record.get('phase'); payload = record.get('payload', {}); trace = record.get('learning_trace', {})
        normal_stop=(record.get('status')=='unobserved' and record.get('first_fault',{}).get('type')=='CancelledError'
            and (resume_ref is not None or self.e.disposition_recovery.has_interrupted_resume(record)))
        observed_rejection = record.get('status') == 'unobserved' and not normal_stop
        if observed_rejection and record.get('metadata', {}).get('request_effect') == 'response-unobserved':
            hold('unobserved saved Learning request; no resend')
        actual = trace.get('actual_model_input') if record.get('status')=='unobserved' and isinstance(trace, dict) else record.get('actual_model_input')
        if (record.get('task_id') != task_id or record.get('source_hash') != task['source_hash']
                or record.get('policy_hash') != self.e.policy.hash or record.get('status') not in {'failed', 'unobserved'}
                or record.get('learning_schema') != schema.__name__ or not isinstance(trace, dict)
                or trace.get('call_id') != record.get('id') or not isinstance(actual, dict)
                or (record.get('status')!='unobserved' and digest(actual) != record.get('actual_model_input_sha256'))
                or record.get('learning_owner') != self.learning_owner(payload, phase)
                or self.e.store.record_get('bounded_model_call', record.get('id')) != record):
            hold('saved correction call/source/focus is missing, changed or unobserved')
        self.e._assert_parent_source(task)
        request = self._learning_history_input(task_id, record, schema, role)
        captured = trace.get('request_model_input')
        if captured is None and set(trace) == {'call_id'}:
            # The old failed-call writer retained only call_id and the actual
            # failed send. Recover only its known controller evidence ordering;
            # the complete initial measurement still has to match below.
            captured = deepcopy(request)
            if 'result_evidence' in actual.get('application_contract', {}):
                captured['application_contract']['result_evidence'] = deepcopy(actual['application_contract']['result_evidence'])
        if (not self._same_learning_envelope(request, captured)
                or (record.get('status')=='unobserved' and (trace.get('role') != role or trace.get('lease') != task['state'].get('delegation_lease')))
                or ('role' in trace and trace['role'] != role)
                or ('lease' in trace and trace['lease'] != task['state'].get('delegation_lease'))):
            hold('saved correction initial request/role/lease changed')
        measured = self._measure_input(phase, captured, schema, role,
                                       self._learning_history_values(task_id, role, record.get('measurement', {})),
                                       semantic_wire=record.get('measurement', {}).get('semantic_wire'))
        if (measured != record.get('measurement')
                or record.get('key') != self._receipt_key(task, phase, payload, schema, role, measured)):
            hold('saved correction initial measurement/configuration changed')
        events = [e for e in self.e.store.events(task_id) if e['stage'] == phase]
        if normal_stop:
            starts=[e for e in events if trace.get('started_event')=={'seq':e['seq'],'hash':e['hash']} and e['status']=='started']
            if len(starts)!=1:hold('interrupted Learning start is absent')
            start=starts[0]
            following=[e for e in events if e['seq']>start['seq'] and e['status'] in {'started','failed','succeeded','interrupted'}]
            if not following or following[0]['status']!='interrupted':hold('interrupted Learning terminal is absent or ambiguous')
            terminal=following[0]
            if trace.get('failure_event') and trace['failure_event']!={'seq':terminal['seq'],'hash':terminal['hash']}:
                hold('interrupted Learning terminal changed')
            window=[e for e in events if start['seq']<e['seq']<terminal['seq']]
        elif observed_rejection:
            starts = [e for e in events if trace.get('started_event') == {'seq': e['seq'], 'hash': e['hash']} and e['status'] == 'started']
            if len(starts) != 1:hold('saved correction lacks its actual start')
            start = starts[0]; window = []
            for event in events:
                if event['seq'] <= start['seq']:continue
                if event['status'] in {'started', 'interrupted'}:break
                window.append(event)
            if not window:hold('unobserved saved Learning request; no resend')
            terminal = window[-1]
        else:
            terminal = [e for e in events if e['status'] == 'failed'
                and e['detail'].get('error_type') == record.get('first_fault', {}).get('type')
                and e['detail'].get('message') == record.get('first_fault', {}).get('message')
                and e['detail'].get('metadata', {}) == record.get('metadata', {})
                and (not trace.get('failure_event') or trace['failure_event'] == {'seq': e['seq'], 'hash': e['hash']})]
            if len(terminal) != 1:
                hold('saved correction terminal event is missing or ambiguous')
            terminal = terminal[0]
            starts = [e for e in events if e['status'] == 'started' and e['seq'] < terminal['seq']]
            if not starts:
                hold('saved correction lacks its actual start')
            start = starts[-1]
            window = [e for e in events if start['seq'] < e['seq'] < terminal['seq']]
        if (start['detail'] != {'actor': role, 'target': payload.get('target', payload.get('operation', {}).get('id')), 'policy_hash': self.e.policy.hash}
                or (trace.get('started_event') and trace['started_event'] != {'seq': start['seq'], 'hash': start['hash']})):
            hold('saved correction start owner changed')
        if any(e['status'] != 'rejected_model_output' for e in window):
            hold('saved correction overlaps a different terminal judgment')
        for event in [start, *window, terminal]:
            if digest({k: v for k, v in event.items() if k not in {'seq', 'hash'}}) != event['hash']:
                hold('saved correction event bytes changed')
        rejection_refs = [{'seq': e['seq'], 'hash': e['hash']} for e in window]
        # The durable final event may precede the interrupted writer's in-memory
        # append. The response/request binding below still proves that last send.
        allowed_rejections = [rejection_refs, rejection_refs[:-1]] if observed_rejection else [rejection_refs]
        if 'rejected_events' in trace and trace['rejected_events'] not in allowed_rejections:
            hold('saved correction rejection sequence changed')
        step = deepcopy(captured); shapes = set(); lineage = []
        if trace.get('feedback_recovery'):
            prior, step = self._learning_feedback_parent(task_id, record, schema, role, step, seen, start=start)
            shapes.update(prior['rejected_shapes']); lineage.extend(prior['lineage'])
        key = digest({'task_id': task_id, 'phase': phase, 'role': role, 'policy_hash': self.e.policy.hash,
                      'source_hash': task['source_hash'], 'payload': payload})
        repeated = False; cardinality = None; proposals = []
        for event in window:
            detail = event['detail']
            if (detail.get('learning_request_key') != key or detail.get('learning_owner') != record['learning_owner']
                    or detail.get('operation_id') != payload.get('operation', {}).get('id')
                    or detail.get('source_hash') != task['source_hash'] or detail.get('policy_hash') != self.e.policy.hash
                    or detail.get('target_effect') != 'none; proposal not admitted' or detail.get('idea_extraction_pending')):
                hold('saved correction formal rejection owner/source differs')
            metadata = detail.get('usage', detail.get('metadata', {}))
            if observed_rejection and (metadata.get('truncated') or metadata.get('request_effect') == 'response-unobserved'):
                hold('interrupted rejection is not a complete observed response')
            self._feedback_response(task, phase, schema, role, step, metadata, measured, 200)
            sent = deepcopy(step)
            if complete_return and 'rejected_response' in detail:
                proposals.append({'event': event, 'model_input': sent})
            feedback = self.e._feedback_from_rejection(detail)
            retained = detail.get('retained_ideas')
            if not isinstance(retained, list):
                hold('saved correction omitted its actual retained Ideas')
            self.e._validate_ideas(step.get('required_ideas', []), LearningIdeas(ideas=retained))
            step = self.e._learning_required_input(step, retained)
            shape = self.e._feedback_shape(schema,feedback['validation'],wire=bool(metadata.get('semantic_wire_capture')))
            if shape in shapes:
                repeated = True
                if event != window[-1]:hold('saved correction continued after a repeated defect')
                cardinality = self._outer_cardinality(record, schema, role, sent, metadata)
                break
            shapes.add(shape); step['actual_format_feedback'] = feedback
        if normal_stop and window and actual==sent and actual!=step:
            # Normal stop before the corrective send: the final observed
            # rejection already has an ordinary feedback continuation.
            observed_rejection=True;normal_stop=False
        if (not partition and not normal_stop and (not shapes or 'actual_format_feedback' not in step)) or (sent if observed_rejection else step) != actual:
            hold('saved correction actual failed send does not follow its formal feedback')
        resumed=None
        if normal_stop:
            if repeated:hold('interrupted Learning followed a repeated defect')
            resumed=self.e.disposition_recovery.interrupted_resume(record,schema,role,actual,start,terminal,resume_ref=resume_ref)
        elif observed_rejection:
            # Actual records remain pre-correction evidence. This separately
            # derived next input is unsent until the normal caller consumes it.
            pass
        elif repeated:
            if not record['first_fault']['message'].startswith('Model repeated the same'):
                hold('saved correction repeated-defect terminal differs')
        else:
            status = record.get('metadata', {}).get('http_status')
            transition = self._application_transition(task_id, record, schema, role)
            http_failure = isinstance(status, int) and status >= 400 and not record.get('metadata', {}).get('truncated')
            # A complete earlier proposal can be reconsidered after a validator
            # correction. This authenticates the later failed send only; its
            # truncated output never becomes a proposal or retry authority.
            length = (complete_return and status == 200 and record['metadata'].get('truncated') is True
                      and record['metadata'].get('finish_reason') == 'length')
            if record['first_fault']['type'] != 'ProviderError' or not (http_failure or transition or length):
                hold('saved correction transport outcome is not an observed HTTP failure')
            self._feedback_response(task, phase, schema, role, actual, record['metadata'], measured, status)
        lineage.append(self._call_ref(record))
        return {'actual': actual, 'rejected_shapes': sorted(shapes), 'lineage': lineage,
                'terminal_seq': terminal['seq'], 'held': repeated, 'cardinality': cardinality,
                **({'next_input': step} if observed_rejection else {}),
                **({'next_input':resumed['next'],'interrupted_resume':resumed['interrupted_resume'],
                    'restored_response':resumed['restored_response']} if resumed else {}),
                **({'rejected_proposals': proposals} if complete_return else {})}

    @staticmethod
    def observed_retryable_failure(record):
        """Pure observed HTTP eligibility; neither authentication nor retry permission."""
        metadata = record.get('metadata', {})
        if not isinstance(metadata, dict):return False
        status = metadata.get('http_status')
        return (type(status) is int and (500 <= status <= 599 or status == 429)
                and not metadata.get('truncated')
                and metadata.get('request_effect') != 'response-unobserved')

    def failed_learning_restart(self, task_id, record, *, schema=Learning, role=None):
        """Authenticate a failed proposal call before a current-stage restart.

        OpenCode classifies observed 5xx/429 as transient API failures. Here
        that classification is only eligibility, never an automatic send or
        authority to replay a target operation. The existing retained-call
        verifier proves the exact request, response and non-admission. Prior
        valid Ideas remain candidates; usage/billing remains as observed.
        """
        role = role or self.e.store.get_task(task_id)['actor']
        if not self.observed_retryable_failure(record):
            raise PolicyError('LEARNING_PROVENANCE: restart requires an observed retryable model API failure')
        metadata = record['metadata']
        status = metadata['http_status']
        bound = self._failed_learning_feedback(task_id, record, schema, role, partition=True)
        if bound['held']:
            raise PolicyError('LEARNING_PROVENANCE: a repeated proposal defect is not a transport restart')
        return {'source_receipt': self._call_ref(record), 'terminal_seq': bound['terminal_seq'],
            'actual_model_input_sha256': digest(bound['actual']),
            'required_ideas': deepcopy(bound['actual'].get('required_ideas', [])),
            'failure_kind': 'model_api_transient_http', 'http_status': status,
            'target_effect': 'none; proposal not admitted',
            'usage': deepcopy(metadata.get('usage')), 'billing': 'unknown',
            'rejected_shapes': bound['rejected_shapes']}

    def _complete_rejected_synthesis(self, task_id, record, schema=LearningSynthesis):
        """Authenticate the last complete proposal before current validation.

        This shared return also serves a whole Learning stopped by an old
        contract. It never resumes a send or manufactures a provider success.
        """
        self._synthesis_peer_closure(record)
        task = self.e.store.get_task(task_id)
        if task_id in self.e.stop_requested:
            import asyncio
            raise asyncio.CancelledError()
        if self.e.quiescing and self.e.quiescing != task_id:
            raise PolicyError('Controller update is waiting for the Learning return boundary')
        bound = self._failed_learning_feedback(task_id, record, schema, task['actor'], complete_return=True)
        candidates = bound['rejected_proposals']
        if schema is Learning:
            # The complete final response, including all retained candidates,
            # can become valid after a controller correction. Earlier valid
            # fragments never replace a later missing or invalid response.
            if (not bound['held'] or not candidates or record.get('status') != 'failed'
                    or not record.get('measurement', {}).get('semantic_wire')):
                raise PolicyError('LEARNING_PROVENANCE: complete rejected Learning is absent')
            self._whole_learning_return_closure(record, bound)
            retained = candidates[-1]
            if record['learning_trace'].get('rejected_events', [])[-1:] != [
                    {'seq': retained['event']['seq'], 'hash': retained['event']['hash']}]:
                raise PolicyError('LEARNING_PROVENANCE: last Learning response is not a complete proposal')
        elif bound['held'] or len(candidates) != 1:
            raise PolicyError('LEARNING_PROVENANCE: complete rejected Synthesis is absent or ambiguous')
        else:
            retained = candidates[0]
        event = retained['event']; detail = event['detail']
        # Later rejections/new candidates must not disappear behind an earlier
        # complete response. Only the exact last unadmitted proposal can return.
        if schema is LearningSynthesis and record['learning_trace'].get('rejected_events') != [{'seq': event['seq'], 'hash': event['hash']}]:
            raise PolicyError('LEARNING_PROVENANCE: rejected Synthesis has another unresolved response')
        usage = detail.get('usage', {})
        if (usage.get('http_status') != 200 or usage.get('finish_reason') != 'stop'
                or usage.get('truncated') or usage.get('response_record', {}).get('truncated') is not False):
            raise PolicyError('LEARNING_PROVENANCE: rejected Synthesis is not a complete normal response')
        from .semantic_wire import validate_wire_receipt
        output_ref = validate_wire_receipt(self.e.store, record, event, schema,
                                           rejected_input=retained['model_input'])
        output = self.e.store.record_get('semantic_wire_output', output_ref['id'])
        # Read the protected retained source, not a diagnostic excerpt or the
        # event's assertion alone. Existing gateway scrubbing/protection applies.
        from .providers import _strict_json
        reader = getattr(self.e.gateway, '_response_view', None)
        if reader is None:
            raise PolicyError('LEARNING_PROVENANCE: retained response reader is unavailable')
        view = reader(usage['response_record'], task_id=task_id)
        try:
            envelope = _strict_json(view['text']); choices = envelope['choices']
            if (view['decoding'] != 'strict' or len(choices) != 1 or choices[0].get('finish_reason') != 'stop'
                    or _strict_json(choices[0]['message']['content']) != output['raw']):
                raise ValueError()
        except (KeyError, ValueError, TypeError, IndexError):
            raise PolicyError('LEARNING_PROVENANCE: complete retained Synthesis bytes differ') from None
        value = schema.model_validate(detail['rejected_response'])
        self.e._validate_ideas(detail.get('retained_ideas', []), value)
        self._validate(schema, value,
            self.e._prepare_learning_payload(task_id, record['payload'], phase=record['phase']), task_id=task_id)
        return value, {'seq': event['seq'], 'hash': event['hash']}

    def _whole_learning_return_closure(self, record, bound):
        """No later send or independent branch may be hidden by reuse."""
        for event in self.e.store.events(record['task_id']):
            if event['stage'] == record['phase'] and event['seq'] > bound['terminal_seq']:
                raise PolicyError('LEARNING_PROVENANCE: rejected Learning has a later unresolved judgment')
        for peer in self.e.store.records('bounded_model_call'):
            if (peer.get('task_id') != record['task_id'] or peer.get('phase') != record['phase']
                    or peer.get('learning_schema') != 'Learning' or peer.get('status') == 'revalidated'):
                continue
            source = peer.get('payload', {})
            if (source.get('operation', {}).get('id') == record['payload'].get('operation', {}).get('id')
                    and source.get('learning_focus') == record['payload'].get('learning_focus')
                    and (self._learning_source_core(source) == self._learning_source_core(record['payload'])
                         or peer.get('status') in {'started', 'unknown', 'unobserved'}
                         or peer.get('metadata', {}).get('request_effect') == 'response-unobserved')
                    and self._call_ref(peer) not in bound['lineage']):
                raise PolicyError('LEARNING_PROVENANCE: failed Learning return branch is ambiguous')

    def _revalidated_synthesis(self, task_id, record, schema):
        if schema not in (Learning, LearningSynthesis) or record.get('learning_schema') != schema.__name__:
            raise PolicyError('LEARNING_PROVENANCE: rejected return schema changed')
        ref = record.get('recovered_from', {})
        original = self.e.store.record_get('bounded_model_call', ref.get('id'))
        if (not original or self._call_ref(original) != ref
                or self.e.store.record_get('bounded_model_call', record.get('id')) != record):
            raise PolicyError('LEARNING_PROVENANCE: rejected return lost its original failed call')
        value, event = self._complete_rejected_synthesis(task_id, original, schema)
        if record != self._synthesis_return_record(original, value, event):
            raise PolicyError('LEARNING_PROVENANCE: revalidated historical proposal changed')
        return value

    def _synthesis_return_record(self, original, value, event):
        record = {k: deepcopy(original[k]) for k in
                  ('key', 'task_id', 'phase', 'source_hash', 'policy_hash', 'payload', 'measurement', 'learning_schema', 'learning_owner')}
        record.update(status='revalidated', recovered_from=self._call_ref(original), rejected_event=event,
                      result=value.model_dump(), raw_result=value.model_dump(),
                      operation_id=original['payload'].get('operation', {}).get('id'),
                      target_effect='none; historical proposal requires normal assessment and independent review')
        record['id'] = digest(record)
        return record

    def _synthesis_peer_closure(self, original):
        peers = [r for r in self.e.store.records('bounded_model_call')
                 if r.get('key') == original['key'] and r.get('status') != 'revalidated']
        if len(peers) != 1 or peers[0] != original:
            raise PolicyError('LEARNING_PROVENANCE: failed Synthesis return branch is ambiguous')

    def _recover_rejected_synthesis(self, task_id, original, schema=LearningSynthesis):
        value, event = self._complete_rejected_synthesis(task_id, original, schema)
        record = self._synthesis_return_record(original, value, event)
        link = {'original': self._call_ref(original), 'terminal': self._call_ref(record), 'rejected_event': event}
        def retain():
            self._synthesis_peer_closure(original)
            if schema is Learning:
                # Repeat the complete check inside the existing atomic write;
                # no concurrent unresolved send or changed witness can slip in.
                current, current_event = self._complete_rejected_synthesis(task_id, original, schema)
                if current != value or current_event != event:
                    raise PolicyError('LEARNING_PROVENANCE: rejected Learning changed before retention')
            old = self.e.store.record_get('bounded_learning_return', original['key'])
            if self.e.store.record_get('bounded_model_call', original['id']) != original or (old is not None and old != link):
                raise PolicyError('LEARNING_PROVENANCE: rejected return changed before retention')
            self.e.store.record('bounded_model_call', record['id'], record)
            self.e.store.record('bounded_learning_return', original['key'], link)
        self.e.store._transaction(retain)
        self._learning_receipts[digest(value.model_dump())] = record
        return value

    def _application_transition(self, task_id, record, schema, role):
        """Known old producer + observed length + exact output-only increase.

        This predicate merely selects authentication. Every old send/event and
        response is still verified by _failed_learning_feedback before reuse.
        """
        values = self._configuration(task_id, role)
        measured = record.get('measurement', {})
        metadata = record.get('metadata', {})
        initial = record.get('learning_trace', {}).get('request_model_input', {})
        return (schema is LearningApplications and values is not None
            and isinstance(initial, dict) and bool(initial) and 'application_output_contract' not in initial
            and measured.get('reserved_output_tokens') == 32768 and values.get('max_output_tokens') == 65536
            and metadata.get('http_status') == 200 and metadata.get('truncated') is True
            and metadata.get('finish_reason') == 'length')

    def _application_partition(self, task_id, phase, payload, part, role):
        if len(part) < 2:
            return None
        role = role or self.e.store.get_task(task_id)['actor']
        matches = []
        middle = len(part) // 2
        children = [{'items_sha256': digest(child), 'item_count': len(child)} for child in (part[:middle], part[middle:])]
        for split in self.e.store.records('bounded_output_split'):
            if (split.get('task_id') != task_id or split.get('phase') != phase
                    or split.get('parent_items_sha256') != digest(part) or split.get('children') != children):
                continue
            ref = split.get('failed_call', {})
            record = self.e.store.record_get('bounded_model_call', ref.get('id')) if ref else None
            if (not record or self._call_ref(record) != ref
                    or split.get('id') != digest({k: v for k, v in split.items() if k != 'id'})):
                raise PolicyError('LEARNING_PROVENANCE: historical Application split receipt changed')
            if record.get('payload', {}).get('source_packet_id') != payload.get('source_packet_id'):
                continue  # A different current source plan is independently judged.
            if record.get('payload') != payload:
                raise PolicyError('LEARNING_PROVENANCE: historical Application partition input changed')
            if not self._application_transition(task_id, record, LearningApplications, role):
                continue
            self._failed_learning_feedback(task_id, record, LearningApplications, role, partition=True)
            matches.append(record)
        if len({r['id'] for r in matches}) > 1:
            raise PolicyError('LEARNING_PROVENANCE: historical Application partition is ambiguous')
        if not matches:
            return None
        record = matches[0]
        error = ProviderError(record['first_fault']['message'], metadata=deepcopy(record['metadata']))
        error.bounded_call_ref = self._call_ref(record)
        error.bounded_partition_only = True
        error.bounded_current_measurement = self.measure(task_id, phase, payload, LearningApplications, role=role)
        return error

    def restore_learning_feedback(self, task_id, phase, payload, schema, role, model_input, *, partition_only=False):
        """Only a normal next invocation may consume this saved feedback."""
        owner = self.learning_owner(payload, phase); operation_id = payload.get('operation', {}).get('id')
        candidates = []; historical = {}
        events = [e for e in self.e.store.events(task_id) if e['stage'] == phase]
        def transport_boundary(metadata):
            status = metadata.get('http_status')
            return ((isinstance(status, int) and status >= 400 and not metadata.get('truncated'))
                    or metadata.get('request_effect') == 'response-unobserved')
        for record in self.e.store.records('bounded_model_call'):
            old = record.get('payload', {}); actual = record.get('actual_model_input', {})
            response_ref = record.get('metadata', {}).get('response_record', {})
            if (task_id not in {record.get('task_id'), response_ref.get('task_id')}
                    or phase not in {record.get('phase'), response_ref.get('phase')}
                    or record.get('status') not in {'failed', 'unobserved'}
                    or operation_id not in {old.get('operation', {}).get('id'),
                        actual.get('operation', {}).get('id') if isinstance(actual, dict) else None}):
                continue
            owners = {record.get('learning_owner'), self.learning_owner(old, phase)}
            if isinstance(actual, dict):owners.add(self.learning_owner(actual, phase))
            if owner not in owners and (old.get('learning_focus') or payload.get('learning_focus')):
                continue  # Another exact output unit remains independent.
            if not isinstance(actual, dict):
                raise PolicyError('LEARNING_PROVENANCE: saved correction input is corrupt')
            trace = record.get('learning_trace', {})
            if not isinstance(trace, dict):
                raise PolicyError('LEARNING_PROVENANCE: saved correction trace is corrupt')
            request_key = digest({'task_id': task_id, 'phase': phase,
                'role': record.get('metadata', {}).get('role', role), 'policy_hash': record.get('policy_hash'),
                'source_hash': record.get('source_hash'), 'payload': old})
            rejections = [e for e in events if e['status'] == 'rejected_model_output'
                          and e['detail'].get('learning_request_key') == request_key]
            retryable_http=self.observed_retryable_failure(record)
            normal_stop=self.e.disposition_recovery.has_interrupted_resume(record)
            if (not actual.get('actual_format_feedback') and not rejections and not trace.get('feedback_recovery')
                    and not trace.get('rejected_events') and not payload.get('analysis_transition') and not retryable_http and not normal_stop):
                continue  # A different error family does not invent a correction.
            # Existing extraction, revision, capacity and deliberate-pause
            # recovery retain their own consumers. This route restores actual
            # feedback only after an observed HTTP failure. An unobserved
            # transport, or an unresolved bound descendant, is held instead.
            # Read the original terminal as well: deleting HTTP metadata from
            # a saved record must not turn corruption into a fresh request.
            terminals = [e for e in events if trace.get('failure_event') == {'seq': e['seq'], 'hash': e['hash']}]
            if set(trace) == {'call_id'}:
                for rejected in rejections:
                    later = [e for e in events if e['seq'] > rejected['seq']
                             and e['status'] in {'started', 'failed', 'succeeded', 'interrupted'}]
                    if later and later[0]['status'] == 'failed':terminals.append(later[0])
            application_length = schema is LearningApplications and any(m.get('truncated') is True
                for m in [record.get('metadata', {}), *(e['detail'].get('metadata', {}) for e in terminals)])
            repeated_wire = bool(record.get('measurement',{}).get('semantic_wire')) and (
                record.get('first_fault',{}).get('message','').startswith('Model repeated the same')
                or any(e['detail'].get('message','').startswith('Model repeated the same') for e in terminals))
            if not (record.get('status') == 'unobserved' or application_length or repeated_wire or self._application_transition(task_id, record, schema, role)
                    or trace.get('feedback_recovery') or transport_boundary(record.get('metadata', {}))
                    or transport_boundary(response_ref)
                    or any(transport_boundary(e['detail'].get('metadata', {})) for e in terminals)):
                continue
            bound = self._failed_learning_feedback(task_id, record, schema, role,
                partition=bool(payload.get('analysis_transition')) or retryable_http or normal_stop)
            if payload.get('analysis_transition') and self.e.web_judgments.validate_analysis_transition(
                    task_id,payload,self._call_ref(record)):
                # Saved transport-time judgment remains exact history. Safe
                # extraction supplied a new result; old format feedback and
                # installed facts cannot be imposed on this new decision.
                self.e._validate_ideas(bound['actual'].get('required_ideas',[]),
                    LearningIdeas(ideas=payload.get('required_ideas',[])))
                historical.setdefault(digest(self._learning_source_core(old)),[]).append((record,bound))
                continue
            saved_context = deepcopy(bound['actual'].get('installed_controller_context'))
            expected = self.e._model_input(task_id, phase, self.e._prepare_learning_payload(task_id, old, phase=phase), schema, role=role,
                                          captured_input=bound['actual'])
            if schema is Learning and self._later_learning_revision(old, payload):
                # Both inputs have passed _model_input's revision-source validation.
                # Retain the original failure and its ambiguity guard, without
                # applying old format feedback to the new semantic decision.
                self.e._validate_ideas(old.get('required_ideas', []), LearningIdeas(ideas=payload.get('required_ideas', [])))
                historical.setdefault(digest(self._learning_source_core(old)), []).append((record, bound))
                continue
            comparable = deepcopy(model_input)
            comparable.pop('installed_controller_context', None)
            if saved_context is not None: comparable['installed_controller_context'] = saved_context
            if expected != comparable:
                raise PolicyError('LEARNING_PROVENANCE: current correction source/focus/history differs')
            candidates.append((record, bound))
        for group in [candidates, *historical.values()]:
            if not group:
                continue
            _, latest = max(group, key=lambda item: item[1]['terminal_seq'])
            if any(self._call_ref(other) not in latest['lineage'] for other, _ in group):
                raise PolicyError('LEARNING_PROVENANCE: saved correction continuations are ambiguous')
        if not candidates:
            return None
        record, bound = max(candidates, key=lambda item: item[1]['terminal_seq'])
        if bound['held']:
            if bound.get('cardinality'):
                evidence = dict(bound['cardinality'], required_ideas=deepcopy(bound['actual'].get('required_ideas', [])))
                raise WireCardinalityError(evidence, self._call_ref(record))
            raise PolicyError('Learning saved correction repeated a known defect; dependent proposal remains unadmitted')
        if partition_only:
            return None
        next_input = self._learning_feedback_input(model_input, bound)
        measured = self._measure_input(phase, next_input, schema, role, self._configuration(task_id, role),
            semantic_wire=self.e._wire_capture(task_id, phase, next_input, schema, role))
        if not measured['fits']:
            raise InputCapacityError('Saved Learning correction cannot fit current configured input/output capacity')
        if bound.get('interrupted_resume') and any(row['unresolved'] for row in self.e.disposition_recovery.resume_context(task_id)['operation_frontier']):
            raise PolicyError('LEARNING_PROVENANCE: target effect became unknown before resumed judgment')
        return {'model_input': next_input, 'rejected_shapes': bound['rejected_shapes'], 'source_receipt': self._call_ref(record),
            **({'interrupted_resume':bound['interrupted_resume'],'restored_response':bound.get('restored_response')}
               if bound.get('interrupted_resume') else {})}

    async def _consume_learning_return(self, task_id, phase, payload, schema, role, key, value, rejected):
        terminal = self._learning_receipts[digest(value.model_dump())]
        try:
            self._validate(schema, value, self.e._prepare_learning_payload(task_id, terminal['payload'], phase=phase),
                           task_id=task_id, learning_receipt=terminal)
        except PolicyError as error:
            if str(error).startswith('LEARNING_PROVENANCE:'):
                raise
            value = await self._call(task_id, phase, terminal['payload'], schema, role=role, _rejected_shapes=rejected)
            actual = self._learning_receipts[digest(value.model_dump())]
            link = self.e.store.record_get('bounded_learning_return', key)
            self.e.store.record('bounded_learning_return', key, dict(link, terminal=self._call_ref(actual)))
        self.e._validate_ideas(payload.get('required_ideas', []), value)
        return value

    async def _correct_learning(self, task_id, phase, record, revised, schema, role, rejected):
        revised = deepcopy(revised)
        proposed = record.get('result', {}).get('ideas', [])
        revised['required_ideas'] = self.e._carry_learning_ideas(revised.get('required_ideas', []),
            self.learning_correction_ideas(task_id, record['payload'], proposed))
        revised['actual_format_feedback']['source_receipt'] = self._call_ref(record)
        value = await self._call(task_id, phase, revised, schema, role=role, _rejected_shapes=rejected,
                                 _learning_parent=self._call_ref(record))
        terminal = self._learning_receipts[digest(value.model_dump())]
        self.e.store.record('bounded_learning_return', record['key'],
            {'original': self._call_ref(record), 'terminal': self._call_ref(terminal)})
        return value

    async def admission_call(self, task_id, phase, payload, schema):
        """Return the exact terminal call and event, including recursive feedback."""
        if schema.__name__ not in ADMISSION_SCHEMAS:
            raise PolicyError('Admission provenance requires an Engineering schema')
        task = self.e.store.get_task(task_id)
        measured = self.measure(task_id, phase, payload, schema, role='reviewer')
        sink = {'request_key': self._receipt_key(task, phase, payload, schema, 'reviewer', measured)}
        value = await self._call(task_id, phase, payload, schema, role='reviewer', _admission_sink=sink)
        return value, sink

    async def _call(self, task_id, phase, payload, schema, *, role=None, _rejected_shapes=None, _legacy_partition_items=None,
                    _admission_sink=None, _admission_parent=None, _learning_parent=None, _expected_request_configuration=None,
                    _retained_only=False):
        if task_id in self.e.stop_requested:
            import asyncio
            raise asyncio.CancelledError()
        self.e._assert_parent_source(self.e.store.get_task(task_id))
        if _retained_only and schema not in (Review, Disposition, ContextObservation):
            raise PolicyError('Retained review consumption cannot create another judgment family')
        if schema is Disposition:
            if _retained_only:
                return self.e.disposition_recovery.completed(task_id,phase,payload,role=role)
            return await self.e.disposition_recovery.call(task_id, phase, payload, role=role,
                expected_request_configuration=_expected_request_configuration)
        recovery = self.e._wire_recovery(schema)
        if recovery is not None and (recovery.has_request(task_id,phase,payload)
                or (schema is AssessmentBatch and 'assessment_revision_transition' in payload)):
            if _retained_only:return recovery.completed(task_id,phase,payload,role=role)
            return await recovery.call(task_id,phase,payload,role=role,
                expected_request_configuration=_expected_request_configuration,
                admission_sink=_admission_sink,admission_parent=_admission_parent)
        if _expected_request_configuration is not None:
            task = self.e.store.get_task(task_id)
            values = self._configuration(task_id,role or task['actor'])
            current = request_configuration(values,role or task['actor']) if values is not None else {}
            if _expected_request_configuration != {k:current.get(k) for k in ('configuration','context_tokens','reserved_output_tokens')}:
                raise ConfigurationRequired('Requested settings differ from the measured dispatch; no request sent')
        original_saved = None
        if schema in LEARNING_SCHEMAS:
            # An internal schema correction may have added N to response history.
            # Authenticate the original request before adding that new history to
            # a different prompt; its successful return already owns N.
            task = self.e.store.get_task(task_id)
            original_measurement = self.measure(task_id, phase, payload, schema, role=role)
            original_key = self._receipt_key(task, phase, payload, schema, role or task['actor'], original_measurement)
            if (not self.e.store.record_get('bounded_model_completed', original_key)
                    and not self.e.store.record_get('bounded_learning_return', original_key)):
                historical = self._old_learning_request(task_id, phase, payload, schema)
                if historical is not None:
                    original_measurement = historical['measurement']
                    original_key = historical['key']
            returned = self._learning_return(task_id, phase, payload, schema, role or task['actor'], original_measurement)
            if returned is not None:
                return await self._consume_learning_return(task_id, phase, payload, schema, role, original_key, returned, _rejected_shapes)
            original_saved = self.e.store.record_get('bounded_model_completed', original_key)
            if original_saved:
                value = self._learning_call(task_id, original_saved, schema)
                try:
                    self._validate(schema, value, self.e._prepare_learning_payload(task_id, payload, phase=phase),
                                   task_id=task_id, learning_receipt=original_saved)
                except PolicyError as error:
                    if str(error).startswith('LEARNING_PROVENANCE:'):raise
                    pass  # Authenticated defect enters the existing feedback path below.
                else:
                    self._learning_receipts[digest(value.model_dump())] = original_saved
                    return value
            else:
                payload = self.e._prepare_learning_payload(task_id, payload, phase=phase)
        measured = original_saved['measurement'] if original_saved is not None else self.measure(task_id, phase, payload, schema, role=role)
        if not self._fits(task_id, phase, payload, schema, role):
            raise InputCapacityError('Complete dependent judgment input/output cannot fit configured capacity: ' + phase)
        task = self.e.store.get_task(task_id)
        actual_role = role or task['actor']
        key = self._receipt_key(task, phase, payload, schema, actual_role, measured)
        if schema in LEARNING_SCHEMAS:
            returned = self._learning_return(task_id, phase, payload, schema, actual_role, measured)
            if returned is not None:
                return await self._consume_learning_return(task_id, phase, payload, schema, role, key, returned, _rejected_shapes)
        saved = self._receipt('bounded_model_completed', key, task, phase, payload, schema, actual_role, measured)
        if saved:
            self._wire_event(saved, schema)
            validation_input = payload
            if schema.__name__ in ADMISSION_SCHEMAS:
                if self.e.store.record_get('bounded_model_call', saved['id']) != saved:
                    raise PolicyError('ADMISSION_CALL_PROVENANCE: cached call differs from original record')
                event_ref = validate_admission_call(self.e.store, _binding(self.e.store, task_id, self.e.policy.hash),
                                                    phase, schema, saved)
                validation_input = retained_admission_input(self.e.store,
                    _binding(self.e.store, task_id, self.e.policy.hash), phase, saved['payload'])
            value = self._learning_call(task_id, saved, schema) if schema in LEARNING_SCHEMAS else schema.model_validate(saved['result'])
            try:
                self._validate(schema, value, self.e._prepare_learning_payload(task_id, validation_input, phase=phase) if schema in LEARNING_SCHEMAS else validation_input,
                               task_id=task_id, learning_receipt=saved if schema in LEARNING_SCHEMAS else None)
            except PolicyError as error:
                if str(error).startswith('LEARNING_PROVENANCE:'):raise
                if schema.__name__ in ADMISSION_SCHEMAS:
                    if not isinstance(error, AdmissionContractError):
                        raise
                    diagnostic = {'kind': 'proposal_contract', 'message': str(error)}
                    rejected = set(_rejected_shapes or ())
                    shape = digest(diagnostic)
                    if shape in rejected:
                        raise PolicyError('Saved admission repeats a known structural defect after actual feedback: ' + str(error)) from error
                    rejected.add(shape)
                    parent = admission_call_ref(saved)
                    revised = dict(admission_input(validation_input), actual_format_feedback={'validation': diagnostic,
                        'rejected_response': value.model_dump(), 'source_receipt': parent,
                        'instruction': 'Produce a NEW complete response to the original admission request. Correct the exact structural defect using the explicit evidence namespace. Preserve needs_evidence and missing readiness; no completed operation, Web acquisition or Knowledge application is replayed.'})
                    return await self._call(task_id, phase, revised, schema, role=role, _rejected_shapes=rejected,
                                            _admission_sink=_admission_sink, _admission_parent=parent)
                if schema not in LEARNING_SCHEMAS:
                    raise
                diagnostic = {'kind': 'saved_learning_contract', 'message': str(error)}
                rejected = set(_rejected_shapes or ())
                shape = digest(diagnostic)
                if shape in rejected:
                    raise PolicyError('Stored Learning repeats a known proposal defect after actual feedback: ' + str(error)) from error
                rejected.add(shape)
                observation = {'source_receipt': {'id': saved['id'], 'key': saved['key'], 'sha256': digest(saved)},
                    'task_id': task_id, 'validation': diagnostic, 'target_effect': 'none; cached proposal not admitted'}
                observation['id'] = digest(observation)
                self.e.store.record('bounded_learning_rejection', observation['id'], observation)
                required = {i['id']: i for i in payload.get('required_ideas', [])}
                required.update({i.id: i.model_dump() for i in value.ideas})
                revised = dict(payload, required_ideas=list(required.values()), actual_format_feedback={
                    'validation': diagnostic, 'rejected_response': value.model_dump(), 'source_receipt': observation['source_receipt'],
                    'instruction': 'Produce a NEW complete Learning for the same original result. The saved proposal is unchanged and unadmitted under the current actual contract; preserve every supplied idea identity and fact. No action or knowledge application is replayed.'})
                return await self._correct_learning(task_id, phase, saved, revised, schema, role, rejected)
            if schema in LEARNING_SCHEMAS:
                self._learning_receipts[digest(value.model_dump())] = saved
            if _admission_sink is not None:
                _admission_sink.update(call=deepcopy(saved), event=event_ref)
            return value
        failed = self._receipt('bounded_model_truncated', key, task, phase, payload, schema, actual_role, measured,
                               legacy_partition_items=_legacy_partition_items)
        if failed:
            if failed.get('metadata', {}).get('truncated') is not True:
                raise PolicyError('Bounded output failure receipt identity changed')
            if schema is LearningSynthesis and failed.get('learning_trace', {}).get('rejected_events'):
                return self._recover_rejected_synthesis(task_id, failed)
            # Resume the smaller pending work, not the identical known failed
            # request. A different input, source or capacity has a different key.
            error = ProviderError(failed['first_fault']['message'], metadata=deepcopy(failed['metadata']))
            error.bounded_call_ref = {'id': failed['id'], 'key': failed['key'], 'sha256': digest(failed)}
            if failed['measurement'] != measured:
                # Old wire ordering is not reconstructed. The historical
                # failure only guides a conservative current partition; it is
                # not a failure claim about the current route/configuration.
                error.bounded_partition_only = True
                error.bounded_current_measurement = deepcopy(measured)
            if 'actual_model_input' in failed:
                error.controller_model_input = deepcopy(failed['actual_model_input'])
            raise error
        if _retained_only:raise PolicyError('Retained review has no complete authenticated response; no new request sent')
        if recovery is not None:
            return await recovery.call(task_id,phase,payload,role=role,
                expected_request_configuration=_expected_request_configuration,
                admission_sink=_admission_sink,admission_parent=_admission_parent)
        record = {'id': uuid4().hex, 'key': key, 'task_id': task_id, 'phase': phase,
                  'source_hash': task['source_hash'], 'policy_hash': self.e.policy.hash,
                  'payload': deepcopy(payload), 'measurement': measured, 'status': 'started'}
        if schema in LEARNING_SCHEMAS:
            record['learning_schema'] = schema.__name__
            record['learning_owner'] = self.learning_owner(payload, phase)
            # Resolve retained sends before registering another call. The new
            # call already carries its exact parent when it becomes visible;
            # it cannot be mistaken for an incomparable unknown peer.
            restored=self.restore_learning_feedback(task_id,phase,payload,schema,actual_role,
                self.e._model_input(task_id,phase,payload,schema,role=actual_role))
            record['learning_trace']={'call_id':record['id']}
            if restored is not None:
                record['learning_trace']['feedback_recovery']=restored['source_receipt']
                if restored.get('interrupted_resume'):
                    record['learning_trace']['interrupted_resume']=deepcopy(restored['interrupted_resume'])
        if _learning_parent is not None:
            record['learning_parent'] = deepcopy(_learning_parent)
        if _admission_parent is not None:
            record['admission_parent'] = deepcopy(_admission_parent)
        self.e.store.record('bounded_model_call', record['id'], record)
        try:
            arguments = {}
            if measured.get('semantic_wire') is not None:
                record['wire_trace'] = {'initial_capture': measured['semantic_wire']}
                arguments['wire_trace'] = record['wire_trace']
            if schema in LEARNING_SCHEMAS:
                arguments['learning_trace'] = record['learning_trace']
                arguments['learning_recovery'] = restored
            if schema.__name__ in ADMISSION_SCHEMAS:
                record['admission_trace'] = {'call_id': record['id']}
                arguments['admission_trace'] = record['admission_trace']
            if getattr(self.e.gateway, 'supports_request_configuration', False):
                arguments['expected_request_configuration'] = deepcopy({
                    field: measured[field] for field in ('configuration', 'context_tokens', 'reserved_output_tokens')})
            value = await self.raw_call(task_id, phase, payload, schema, role=role, **arguments)
        except BaseException as error:
            record.update(status='unobserved' if not isinstance(error, Exception) else 'failed',
                          first_fault={'type': type(error).__name__, 'message': str(error)})
            if isinstance(error, ProviderError):
                # Provider metadata already excludes credentials/private
                # reasoning. Its full response_record, actual usage and failed
                # request binding must survive this transport-level decision.
                record['metadata'] = deepcopy(error.metadata)
            actual_input = getattr(error, 'controller_model_input', None)
            if actual_input is not None:
                record['actual_model_input'] = deepcopy(actual_input)
                record['actual_model_input_sha256'] = digest(actual_input)
            self.e.store.record('bounded_model_call', record['id'], record)
            if self._output_truncated(error):
                self.e.store.record('bounded_model_truncated', key, record)
                error.bounded_call_ref = {'id': record['id'], 'key': key, 'sha256': digest(record)}
            elif (schema in (Learning, LearningIdeas) and measured.get('semantic_wire')
                    and str(error).startswith('Model repeated the same')):
                bound = self._failed_learning_feedback(task_id, record, schema, actual_role)
                if bound.get('cardinality'):
                    evidence = dict(bound['cardinality'], required_ideas=deepcopy(bound['actual'].get('required_ideas', [])))
                    raise WireCardinalityError(evidence, self._call_ref(record)) from error
            raise
        if schema.__name__ in ADMISSION_SCHEMAS:
            try:
                event_ref = validate_admission_call(self.e.store, _binding(self.e.store, task_id, self.e.policy.hash),
                    phase, schema, dict(record, status='succeeded', result=value.model_dump()))
            except PolicyError as error:
                record.update(status='failed', result=value.model_dump(),
                    first_fault={'type': type(error).__name__, 'message': str(error)},
                    target_effect='none; response provenance not admitted')
                self.e.store.record('bounded_model_call', record['id'], record)
                raise
        try:
            self._validate(schema, value, self.e._prepare_learning_payload(task_id, payload, phase=phase) if schema in LEARNING_SCHEMAS else payload, task_id=task_id)
        except PolicyError as error:
            if str(error).startswith('LEARNING_PROVENANCE:'):raise
            if schema.__name__ in ADMISSION_SCHEMAS and not isinstance(error, AdmissionContractError):
                raise
            diagnostic = {'kind': 'proposal_contract' if schema.__name__ in ADMISSION_SCHEMAS else 'bounded_proposal_contract', 'message': str(error)}
            record.update(status='rejected', result=value.model_dump(), first_fault=diagnostic,
                          target_effect='none; model proposal was not admitted')
            self.e.store.record('bounded_model_call', record['id'], record)
            rejected = set(_rejected_shapes or ())
            shape = digest(diagnostic)
            if shape in rejected:
                raise PolicyError('Model repeated the same bounded proposal defect after actual feedback: ' + str(error)) from error
            rejected.add(shape)
            revised = dict(payload, actual_format_feedback={'validation': diagnostic, 'rejected_response': value.model_dump(),
                'instruction': 'Produce a NEW complete proposal with the exact supplied identities and actual evidence. The previous response is preserved and unadmitted; no tool action was executed. Do not fabricate success or weaken a condition.'})
            if schema.__name__ in ADMISSION_SCHEMAS:
                parent = admission_call_ref(record)
                revised = admission_input(revised)
                revised['actual_format_feedback']['source_receipt'] = parent
                return await self._call(task_id, phase, revised, schema, role=role, _rejected_shapes=rejected,
                                        _admission_sink=_admission_sink, _admission_parent=parent)
            if schema in LEARNING_SCHEMAS:
                return await self._correct_learning(task_id, phase, record, revised, schema, role, rejected)
            return await self._call(task_id, phase, revised, schema, role=role, _rejected_shapes=rejected)
        if schema in LEARNING_SCHEMAS:
            original = value.model_dump()
            required_ids = {i['id'] for i in payload.get('required_ideas', [])}
            # A corrected wire response can contain newly discovered required
            # Ideas from an earlier schema-invalid response of this same call.
            # Their durable identities must survive this transport boundary too.
            required_ids.update(i['id'] for i in record.get('learning_trace', {}).get('actual_model_input', {}).get('required_ideas', []))
            response_id = digest({'source_call_key': key, 'original_response': original})
            mapping = []
            normalized = []
            for proposed in value.ideas:
                identity = proposed.id if proposed.id in required_ids or measured.get('semantic_wire') else 'bounded-idea:' + digest({'response_id': response_id, 'original_id': proposed.id})
                normalized.append(proposed.model_copy(update={'id': identity}))
                mapping.append({'response_id': response_id, 'original_id': proposed.id, 'controller_id': identity})
            value = value.model_copy(update={'ideas': normalized})
            record.update(raw_result=original, idea_namespaces=mapping,
                          operation_id=payload.get('operation', {}).get('id'))
        record.update(status='succeeded', result=value.model_dump())
        self._wire_event(record, schema)
        self.e.store.record('bounded_model_call', record['id'], record)
        self.e.store.record('bounded_model_completed', key, record)
        if schema in LEARNING_SCHEMAS:
            self._learning_receipts[digest(value.model_dump())] = record
        if _admission_sink is not None:
            _admission_sink.update(call=deepcopy(record), event=event_ref)
        return value

    def _packet(self, task_id, phase, value):
        task = self.e.store.get_task(task_id)
        packet = {'task_id': task_id, 'phase': phase, 'policy_hash': self.e.policy.hash,
                  'source_hash': task['source_hash'], 'value': deepcopy(value)}
        packet['id'] = digest(packet)
        existing = self.e.store.record_get('bounded_input', packet['id'])
        if existing and existing != packet:
            raise PolicyError('Bounded input source changed')
        if not existing:
            self.e.store.record('bounded_input', packet['id'], packet)
        if self.e.store.record_get('bounded_input', packet['id']) != packet:
            raise PolicyError('Bounded input could not retain exact source bytes')
        return packet

    @staticmethod
    def _record(path, value):
        return {'id': digest({'path': path, 'value': value}), 'path': path,
                'source_sha256': digest(value), 'value': deepcopy(value)}

    def _split(self, record):
        value = record['value']; path = record['path']
        if isinstance(value, dict) and value:
            return [self._record(path + '/' + str(k).replace('~', '~0').replace('/', '~1'), v) for k, v in value.items()]
        if isinstance(value, list) and value:
            return [self._record(path + '/' + str(i), v) for i, v in enumerate(value)]
        text = value if record.get('fragment') else canonical(value)
        if len(text) < 2:
            raise InputCapacityError('One exact source fragment cannot fit beside complete policy and envelope')
        start = record.get('start', 0); middle = len(text) // 2
        whole = record.get('whole_source_sha256', record['source_sha256'])
        return [dict(self._record(path, part), fragment=True, encoding='canonical-json-text',
                     start=start + offset, end=start + offset + len(part), whole_source_sha256=whole,
                     id=digest({'path': path, 'whole': whole, 'start': start + offset, 'text': part}))
                for offset, part in ((0, text[:middle]), (middle, text[middle:]))]

    def _pages(self, task_id, phase, records, build, schema, *, role=None, split=False, start=0):
        pending = list(records); number = start
        while pending:
            actual_phase = phase + ':page:' + str(number)
            if not self._fits(task_id, actual_phase, build(pending[:1]), schema, role):
                if not split:
                    raise InputCapacityError('One exact mandatory target cannot fit beside complete policy and envelope: ' + phase)
                pieces = self._split(pending[0])
                if len(pieces) == 1 and pieces[0] == pending[0]:
                    raise InputCapacityError('Source decomposition made no progress')
                pending[:1] = pieces
                continue
            # Probe exact complete messages by exponential growth, then binary
            # search the boundary. Do not tokenize the stable policy once per
            # candidate record in a long catalog merely to find the same page.
            best = 1; upper = min(2, len(pending))
            while upper > best and self._fits(task_id, actual_phase, build(pending[:upper]), schema, role):
                best = upper
                if best == len(pending):
                    break
                upper = min(upper * 2, len(pending))
            low, high = best + 1, upper - 1
            while low <= high:
                middle = (low + high) // 2
                if self._fits(task_id, actual_phase, build(pending[:middle]), schema, role):
                    best = middle; low = middle + 1
                else:
                    high = middle - 1
            yield pending[:best]
            pending = pending[best:]
            number += 1

    @staticmethod
    def _output_truncated(error):
        return isinstance(error, ProviderError) and error.metadata.get('truncated') is True

    def _learning_cardinality_failure(self, task_id, phase, payload, schema, role=None):
        if not getattr(self.e.gateway, 'supports_semantic_wire', False):
            return None
        task = self.e.store.get_task(task_id); role = role or task['actor']
        prepared = self.e._prepare_learning_payload(task_id, payload, phase=phase)
        current = self.e._model_input(task_id, phase, prepared, schema, role=role)
        try:
            self.restore_learning_feedback(task_id, phase, prepared, schema, role, current, partition_only=True)
        except WireCardinalityError as error:
            return error
        return None

    def _smaller_parts(self, part, *, split, phase, error=None, logical_ideas=False):
        if isinstance(error, WireCardinalityError):
            evidence = error.evidence
            limit = evidence['logical_count'] - 1
            if limit < 1:
                raise PolicyError('An indivisible wire decision cannot be partitioned') from error
            if evidence['field'] == 'existing_idea_decisions':
                from .learning_projection import groups
                retained = evidence['required_ideas']
                self.e._validate_ideas(part, LearningIdeas(ideas=retained))
                by_id = {i['id']: i for i in retained}
                units = [[by_id[identity] for identity in g['members']] for g in groups(retained)]
            else:
                if [[v['id']] for v in part] != evidence['members']:
                    raise PolicyError('WIRE_PROVENANCE: partition targets differ from the failed capture')
                units = [[item] for item in part]
            # New separately valid Ideas can grow the retained set. Every child
            # is still smaller than the ACTUAL failed association, not that set.
            width = min(limit, max(1, len(units) // 2))
            return [[item for unit in units[i:i+width] for item in unit]
                    for i in range(0, len(units), width)]
        if logical_ideas:
            from .learning_projection import groups
            by_id = {i['id']: i for i in part}
            units = [[by_id[identity] for identity in g['members']] for g in groups(part)]
            if len(units) > 1:
                middle = len(units) // 2
                return [[item for unit in side for item in unit] for side in (units[:middle], units[middle:])]
        elif len(part) > 1:
            middle = len(part) // 2
            return [part[:middle], part[middle:]]
        if split and part and not logical_ideas:
            try:
                pieces = self._split(part[0])
            except InputCapacityError:
                pieces = []
            if pieces and pieces != part:
                return [[piece] for piece in pieces]
        message = ('Observed model output was truncated for one indivisible mandatory item; '
                   'the exact input, failed response and usage are retained. Configure sufficient '
                   'output capacity or provide a reviewed representable operation: ' + phase
                   if error is not None else
                   'One exact mandatory item cannot fit beside complete policy and envelope: ' + phase)
        boundary = InputCapacityError(message)
        if error is not None:
            boundary.metadata = deepcopy(error.metadata)
            boundary.bounded_call_ref = getattr(error, 'bounded_call_ref', None)
        raise boundary from error

    async def _complete_page(self, task_id, phase, part, build, schema, *, role=None, split=False, failure=None, retained_only=False, logical_ideas=False):
        """Yield complete leaves; known failures strictly reduce pending work.

        The phase stays stable and the exact payload distinguishes child calls.
        Re-entering this tree consumes durable successes/failures using their
        original call keys; neither a fixed retry count nor tool replay is used.
        """
        payload = build(part)
        if failure is None and schema is LearningIdeas:
            failure = self._learning_cardinality_failure(task_id, phase, payload, schema, role)
        if failure is None and schema is LearningApplications:
            failure = self._application_partition(task_id, phase, payload, part, role)
        if failure is None and self._fits(task_id, phase, payload, schema, role):
            try:
                value = await self._call(task_id, phase, payload, schema, role=role,
                                         _legacy_partition_items=part if len(part) > 1 else None, _retained_only=retained_only)
            except (ProviderError, WireCardinalityError, WirePartialAssessmentError) as error:
                if not self._output_truncated(error) and not isinstance(error, (WireCardinalityError, WirePartialAssessmentError)):
                    raise
                failure = error
            except Exception as error:
                from .disposition_recovery import AssessmentRevisionTransition
                if not isinstance(error, AssessmentRevisionTransition):
                    raise
                if schema is not AssessmentBatch or len(part) != len(payload.get('targets', [])):
                    raise PolicyError('Assessment revision transition has a foreign page') from error
                revision_payload = error.payload
                if not self._fits(task_id, phase, revision_payload, schema, role):
                    raise ConfigurationRequired('Assessment revision input exceeds configured context; no request sent')
                async for completed in self._complete_page(
                        task_id, phase, part, lambda items: self._assessment_part_payload(revision_payload, items), schema,
                        role=role, split=split, retained_only=retained_only):
                    yield completed
                return
            else:
                yield part, payload, value
                return
        if isinstance(failure, WirePartialAssessmentError):
            if schema is not AssessmentBatch: raise PolicyError('Assessment subset has a foreign consumer')
            original, observed = self._assessment_partial(task_id, failure.bounded_call_ref, role=role)
            if (original['payload'] != payload or original['phase'] != phase
                    or observed['partial_assessments'] != failure.evidence):
                raise PolicyError('Assessment subset does not belong to this pending page')
            if failure.evidence['retained']:
                retained_payload, retained_value = self._assessment_subset_leaf(task_id, failure.bounded_call_ref, role=role)
                yield retained_payload['targets'], retained_payload, retained_value
            pending = [part[row['index']] for row in failure.evidence['unresolved']]
            if not pending:
                return
            correction = lambda items: self._assessment_correction_payload(original, observed, items)
            async for completed in self._complete_page(task_id, phase, pending, correction, schema, role=role,
                                                       split=split, retained_only=retained_only):
                yield completed
            return
        logical_ideas = logical_ideas or (isinstance(failure, WireCardinalityError)
            and failure.evidence['field'] == 'existing_idea_decisions')
        children = self._smaller_parts(part, split=split, phase=phase, error=failure, logical_ideas=logical_ideas)
        if failure is not None:
            source = getattr(failure, 'bounded_call_ref', None)
            decision = {'task_id': task_id, 'phase': phase, 'failed_call': source,
                        'parent_items_sha256': digest(part),
                        'children': [{'items_sha256': digest(child), 'item_count': len(child)} for child in children],
                        'reason': 'Known observed output truncation; reduce only the pending exact page',
                        'target_effect': 'none; incomplete proposal is not admitted'}
            if isinstance(failure, WireCardinalityError):
                from .learning_projection import groups
                decision.update(reason='Authenticated outer slot count; strictly smaller logical decisions',
                    cardinality=deepcopy(failure.evidence),
                    child_logical_counts=[len(groups(child))
                        if failure.evidence['field'] == 'existing_idea_decisions' else len(child) for child in children])
            if getattr(failure, 'bounded_partition_only', False):
                decision.update(reason='Historical truncation guides only a conservative current partition; '
                                       'every leaf needs a current configuration-bound result',
                                basis='legacy-partition-only',
                                current_measurement=deepcopy(failure.bounded_current_measurement),
                                old_configuration_complete=False, wire_equality_claimed=False)
            decision['id'] = digest(decision)
            self.e.store.record('bounded_output_split', decision['id'], decision)
        for child in children:
            async for completed in self._complete_page(task_id, phase, child, build, schema, role=role, split=split, retained_only=retained_only, logical_ideas=logical_ideas):
                yield completed

    async def _page_calls(self, task_id, phase, records, build, schema, *, role=None, split=False, start=0, exact_phase=False, failure=None, retained_only=False):
        if failure is not None:
            async for completed in self._complete_page(task_id, phase, list(records), build, schema, role=role, split=split, failure=failure, retained_only=retained_only):
                yield completed
            return
        for index, part in enumerate(self._pages(task_id, phase, records, build, schema, role=role, split=split, start=start), start=start):
            call_phase = phase if exact_phase else phase + ':page:' + str(index)
            async for completed in self._complete_page(task_id, call_phase, part, build, schema, role=role, split=split, retained_only=retained_only):
                yield completed

    async def research_payload(self, task_id, phase, payload, *, role=None):
        """Keep large pre/post bundles from reappearing in the query consumer.

        Only ResearchQuery uses this route: private-data exclusion and URL/query
        validation, actual acquisition and its review remain the caller's job.
        """
        if self._fits(task_id, phase, payload, ResearchQuery, role):
            return payload
        base = {key: deepcopy(payload[key]) for key in ('operation', 'instruction', 'web_configuration') if key in payload}
        compact = await self.compact_context(task_id, phase, payload, base, ResearchQuery, role=role)
        return {**base, 'bounded_context': compact,
                'target': {'source_sha256': digest(payload), 'transport': 'Original query context was actually supplied in recorded pages; synopsis remains an interpretation'}}

    async def compact_context(self, task_id, phase, context, base, schema, *, role=None, force_observation=False, context_field='bounded_context', retained_only=False):
        """Read complete context in bounded reviewer calls, then supply a synopsis.

        base is the exact final consumer without context_field. A composition
        synopsis may use another field while retaining the original anchor context.
        Callers must separately transport all operative identities/evidence.
        """
        if not force_observation and self._fits(task_id, phase, {**base, context_field: context}, schema, role):
            return context
        packet = self._packet(task_id, phase + ':context', context)
        config = self._configuration(task_id, role or self.e.store.get_task(task_id)['actor'])
        maximum = config['max_output_tokens'] if config else 32768
        empty = self.measure(task_id, phase, {**base, context_field: {}}, schema, role=role)
        available = (empty['context_tokens'] - empty['reserved_output_tokens'] - empty['input_tokens_estimate']) if config else maximum
        if available <= 0:
            raise InputCapacityError('Mandatory consumer anchors plus full policy/envelope exceed configured capacity')
        summary_budget = max(1, min(maximum // 4, available // 4))
        records = [self._record('', context)]; all_reads = []; rounds = []
        level = 0
        previous_size = None
        while True:
            read_phase = phase + ':context:' + str(level)
            def build(part):
                return {'source_packet_id': packet['id'], 'source_records': part,
                        'summary_token_budget': summary_budget,
                        'instruction': 'Tool-free context reading, not a new operative decision. Read each exact supplied source record/fragment; return every coverage_id once. Give a concise source-specific factual synopsis with uncertainties and cross-record interactions. Preserve contrary facts. References are not evidence of unseen content. The controller stores full originals; this synopsis is an interpretation and cannot establish semantic completeness.'}
            summaries = []
            async for part, actual, value in self._page_calls(task_id, read_phase, records, build, ContextObservation, role='reviewer', split=True, retained_only=retained_only):
                _ids_exact(value.coverage_ids, [r['id'] for r in part], 'Context source coverage')
                all_reads.extend(part if level == 0 else [])
                summaries.append({'id': digest({'part': part, 'report': value.model_dump()}),
                                  'summary': value.summary, 'limitations': value.limitations})
            rounds.append(summaries)
            synopsis = {'packet_id': packet['id'], 'source_sha256': digest(context),
                        'actual_source_fragment_count': len(all_reads), 'coverage_sha256': digest(all_reads),
                        'synopsis': _join(x['summary'] for x in summaries),
                        'limitations': _unique([x for s in summaries for x in s['limitations']]),
                        'proof_ceiling': 'Every recorded source fragment was supplied to a model. Synopsis fidelity and semantic completeness are unproven; mandatory identities/evidence have separate exact consumers.'}
            if self._fits(task_id, phase, {**base, context_field: synopsis}, schema, role):
                self.e.store.record('bounded_context', packet['id'], {'id': packet['id'], 'task_id': task_id,
                                    'source_records': all_reads, 'rounds': rounds, 'result': synopsis})
                return synopsis
            size = _tokens(summaries)
            if previous_size is not None and size >= previous_size:
                raise InputCapacityError('Actual context recomposition did not shrink; complete source and attempts retained')
            previous_size = size
            records = [self._record('/summaries/' + str(i), s) for i, s in enumerate(summaries)]
            level += 1

    def _assessment_key(self,task_id,phase,targets,context,result):
        return digest({'task_id':task_id,'phase':phase,'targets':targets,'context':context,'result':result})

    def _assessment_partial(self, task_id, ref, *, role=None):
        """A subset derives from one failed head; a later/unknown send holds."""
        recovery = self.e._wire_recovery(AssessmentBatch)
        if recovery is None: raise PolicyError('Assessment subset requires an authenticated wire response')
        record = recovery._read(ref)
        actual_role = role or self.e.store.get_task(task_id)['actor']
        if record['task_id'] != task_id or record.get('binding', {}).get('role') != actual_role:
            raise PolicyError('Assessment subset task or role changed')
        # This is read-only authentication, including after a correction page
        # already succeeded. A stop must not recast that observed success as an
        # unknown send. The next actual call retains its normal control gates.
        try: recovery.inspect_request(task_id, record['phase'], record['payload'], role=actual_role, check_control=False)
        except WirePartialAssessmentError as error:
            if error.bounded_call_ref != ref: raise PolicyError('Assessment subset has a later request')
            # inspect_request has already authenticated current configuration,
            # source/policy/lease, all prior sends and the complete protected raw.
            return record, {'partial_assessments': error.evidence,
                'shapes': record['feedback_trace']['shapes']}
        raise PolicyError('Assessment subset no longer has its exact failed head')

    @staticmethod
    def _assessment_part_payload(payload, part):
        return dict(deepcopy(payload), targets=deepcopy(part), target={'ids': [t['id'] for t in part]})

    def _assessment_subset_leaf(self, task_id, ref, *, role=None):
        record, observed = self._assessment_partial(task_id, ref, role=role)
        retained = observed['partial_assessments']['retained']
        part = [record['payload']['targets'][row['index']] for row in retained]
        payload = self._assessment_part_payload(record['payload'], part)
        payload['assessment_retained_subset'] = deepcopy(ref)
        value = AssessmentBatch.model_validate({'assessments': [row['value'] for row in retained]}, strict=True)
        return payload, value

    def _assessment_correction_payload(self, record, observed, part):
        partial = observed['partial_assessments']; source = record['payload']
        allowed = {source['targets'][row['index']]['id']: source['targets'][row['index']]
                   for row in partial['unresolved']}
        if (not part or len(part) != len({t['id'] for t in part})
                or any(allowed.get(t['id']) != t for t in part)):
            raise PolicyError('Assessment correction contains a resolved or foreign target')
        previous = source.get('assessment_correction', {})
        payload = self._assessment_part_payload(source, part)
        payload['assessment_correction'] = {
            'version': partial['version'], 'parent_call': self._call_ref(record),
            'capture': partial['capture'], 'response': partial['response'], 'raw_sha256': partial['raw_sha256'],
            'original_targets': deepcopy(previous.get('original_targets', source['targets'])),
            'retained_assessments': deepcopy(previous.get('retained_assessments', []))
                + [deepcopy(row['value']) for row in partial['retained']],
            'unresolved_rows': deepcopy(partial['unresolved']), 'rejected_shapes': list(observed['shapes']),
            'instruction': 'Return one full assessment for each supplied targets entry, in its exact order. '
                'Only those unresolved targets are response slots. Read every original target, retained peer '
                'assessment and exact invalid row/diagnostic for interactions and dependencies. Correct the '
                'actual meaning and wire defect yourself; do not strip text, invent a judgment, or retranscribe '
                'retained rows. Preserved rows are structurally valid proposals, not semantic approvals. '
                'Explain cross-row concerns in your assessment; the complete recomposed batch still goes '
                'through the normal independent review and correction before use. If one old candidate '
                'and a changed proposal are both intended, return separate ideas: candidate_slot with '
                'consideration for the unchanged candidate, and a complete NewIdea for the change. '
                'Do not combine both representations or silently drop either judgment.'}
        retained_idea_rows = []
        for target in part:
            row = next(item for item in partial['unresolved'] if item['target_id'] == target['id'])
            if 'retained_ideas' in row:
                retained_idea_rows.append({'target_id': target['id'],
                    'total_ideas': row['total_ideas'],
                    'retained_ideas': deepcopy(row['retained_ideas']),
                    'unresolved_idea_indexes': list(row['unresolved_idea_indexes'])})
            else:
                retained_idea_rows.append(None)
        if any(row is not None for row in retained_idea_rows):
            payload['assessment_correction']['retained_idea_rows'] = retained_idea_rows
            payload['assessment_correction']['instruction'] += (
                ' Valid ideas in retained_idea_rows are code-owned and restored at their original positions. '
                 'For each non-null retained_idea_rows entry return only its unresolved ideas, ordered by '
                 'unresolved_idea_indexes; for a null entry return its complete ideas. '
                 'In a reference-slot response put unresolved existing selections in existing_idea_decisions '
                 'and unresolved new proposals in new_ideas; code restores retained decisions at their original positions. '
                 'Still return every other required assessment field and reconsider it using the saved original. '
                'For a mixed old/new item decide explicitly whether the old selection, a complete changed '
                'NewIdea, or both are warranted; do not assume the controller inferred that choice. '
                'The original mixed text and all retained ideas remain available to independent review.')
        return payload

    def _validate_assessment_correction(self, task_id, payload, *, role=None):
        marker = payload.get('assessment_correction')
        if not isinstance(marker, dict): raise PolicyError('Assessment correction source is malformed')
        record, observed = self._assessment_partial(task_id, marker.get('parent_call'), role=role)
        if self._assessment_correction_payload(record, observed, payload.get('targets', [])) != payload:
            raise PolicyError('Assessment correction original context or retained rows changed')
        return record

    def _assessment_result_parts(self, task_id, phase, refs, context):
        recovery = self.e._wire_recovery(AssessmentBatch); completed = []
        for ref in refs:
            if set(ref) == {'assessment_subset'}:
                call = recovery._read(ref['assessment_subset'])
                payload, value = self._assessment_subset_leaf(task_id, ref['assessment_subset'])
            else:
                call = recovery._read(ref)
                found = recovery.inspect_request(task_id, call['phase'], call['payload'])
                if not found or self._call_ref(found[0]) != ref or 'value' not in found[1]:
                    raise PolicyError('Assessment stage lost its actual successful return')
                payload, value = call['payload'], found[1]['value']
            if call['phase'] != phase and not call['phase'].startswith(phase + ':page:'):
                raise PolicyError('Assessment stage phase changed')
            if self._assessment_origin(task_id, payload) != context:
                raise PolicyError('Assessment stage original context changed')
            completed.append((payload, value))
        return completed

    @staticmethod
    def _ordered_assessments(targets,batches):
        expected={t['id']:t for t in targets};values={}
        if len(expected)!=len(targets):raise PolicyError('Assessment source target order is ambiguous')
        for payload,value in batches:
            part=payload['targets'];items=value.model_dump()['assessments']
            _ids_exact([a['target_id'] for a in items],[t['id'] for t in part],'Assessment saved leaf')
            for target in part:
                if expected.get(target['id'])!=target:raise PolicyError('Assessment saved leaf target changed')
            for item in items:
                identity=item['target_id']
                if identity in values:raise PolicyError('Assessment saved leaf coverage is ambiguous')
                values[identity]=item
        if set(values)!=set(expected):raise PolicyError('Assessment saved leaf coverage is incomplete')
        return [values[t['id']] for t in targets]

    def _retain_assessments(self,task_id,phase,targets,context,result,completed):
        recovery=self.e._wire_recovery(AssessmentBatch)
        if recovery is None:return
        if self._ordered_assessments(targets,completed)!=result:
            raise PolicyError('Assessment stage composition changed')
        positions={t['id']:i for i,t in enumerate(targets)}
        completed=sorted(completed,key=lambda pair:min(positions[t['id']] for t in pair[0]['targets']))
        # Query only phase names; do not reload every historical input body.
        with self.e.store.lock:
            rows=self.e.store.db.execute("SELECT DISTINCT json_extract(body,'$.phase') AS phase FROM records WHERE kind='bounded_model_call' AND json_extract(body,'$.task_id')=? AND (json_extract(body,'$.phase')=? OR substr(json_extract(body,'$.phase'),1,?)=?)",(task_id,phase,len(phase+':page:'),phase+':page:')).fetchall()
        phases=[r['phase'] for r in rows];calls=[]
        for payload,value in completed:
            if 'assessment_retained_subset' in payload:
                ref = payload['assessment_retained_subset']
                expected_payload, expected_value = self._assessment_subset_leaf(task_id, ref)
                if (payload, value) != (expected_payload, expected_value):
                    raise PolicyError('Assessment retained subset composition changed')
                calls.append({'assessment_subset': deepcopy(ref)})
                continue
            found=[]
            for actual_phase in phases:
                actual=recovery.inspect_request(task_id,actual_phase,payload)
                if actual and 'value' in actual[1]:
                    if actual[1]['value']!=value:raise PolicyError('Assessment completed response differs')
                    found.append(actual[0])
            if len(found)!=1:raise PolicyError('Assessment completion has no unique exact request')
            calls.append(self._call_ref(found[0]))
        packet=self._packet(task_id,phase+':assessment-result',{'targets':targets,'context':context})
        key=self._assessment_key(task_id,phase,targets,context,result)
        self.e.store.record('bounded_assessment_result',key,{'id':key,'task_id':task_id,'phase':phase,
            'input':{'id':packet['id'],'sha256':digest(packet)},'calls':calls,'result':deepcopy(result)})

    def authenticate_assessments(self,task_id,phase,targets,context,result):
        recovery=self.e._wire_recovery(AssessmentBatch)
        if recovery is None:return  # Explicit old offline fixtures, not a product fallback.
        key=self._assessment_key(task_id,phase,targets,context,result)
        retained=self.e.store.record_get('bounded_assessment_result',key)
        if retained is None:
            # Exact old full request; absent paged provenance is held, never
            # silently turned into a new call or a successful current result.
            instruction='Return one independent full assessment for every exact target ID. Keep all prescribed thinking targets, specific ideas and evidence. Paged context never excuses an omitted decision; consider the supplied interaction synopsis and preserve its limits.'
            payload={**context,'targets':targets,'target':{'ids':[t['id'] for t in targets]},'instruction':instruction}
            actual=recovery.inspect_request(task_id,phase,payload)
            if actual and 'value' in actual[1] and actual[1]['value'].model_dump()['assessments']==result:return
            original_targets,_,completed=self._retained_assessment_parts(task_id,phase,result,context=context)
            if original_targets!=targets:raise PolicyError('Assessment stage original targets changed')
            self._retain_assessments(task_id,phase,targets,context,result,completed)
            return
        ref=retained['input'];packet=self.e.store.record_get('bounded_input',ref['id']);task=self.e.store.get_task(task_id)
        if (retained.get('id')!=key or retained.get('task_id')!=task_id or retained.get('phase')!=phase
                or retained.get('result')!=result or not packet or digest(packet)!=ref['sha256']
                or packet['id']!=digest({k:v for k,v in packet.items() if k!='id'})
                or packet['task_id']!=task_id or packet['source_hash']!=task['source_hash'] or packet['policy_hash']!=self.e.policy.hash
                or packet['value']!={'targets':targets,'context':context}):
            raise PolicyError('Assessment stage input/result binding changed')
        completed=self._assessment_result_parts(task_id,phase,retained['calls'],context)
        if self._ordered_assessments(targets,completed)!=result:raise PolicyError('Assessment stage composition changed')

    def assessment_targets(self,task_id,phase,result,allowed):
        observed=getattr(self,'_last_assessment_input',{})
        if (any(observed.get(k)!=v for k,v in {'task_id':task_id,'phase':phase,'result':result}.items())
                or observed.get('targets') not in allowed):raise PolicyError('Assessment return target provenance differs')
        return deepcopy(observed['targets'])

    def assessment_context(self,task_id,phase,targets,result):
        observed=getattr(self,'_last_assessment_input',{})
        if any(observed.get(k)!=v for k,v in {'task_id':task_id,'phase':phase,'targets':targets,'result':result}.items()):
            raise PolicyError('Assessment return metadata belongs to another request')
        context=deepcopy(observed['context'])
        return context

    async def assess_targets(self, task_id, phase, targets, context, *, previous=None):
        if not targets:
            return []
        _ids_exact([t['id'] for t in targets], [t['id'] for t in targets], 'Assessment source')
        instruction = 'Return one independent full assessment for every exact target ID. Keep all prescribed thinking targets, specific ideas and evidence. Paged context never excuses an omitted decision; consider the supplied interaction synopsis and preserve its limits.'
        if previous is not None:
            old_targets,old_context=previous
            old_payload={**old_context,'targets':old_targets,'target':{'ids':[t['id'] for t in old_targets]},'instruction':instruction}
            recovery=self.e._wire_recovery(AssessmentBatch)
            try:observed=recovery.inspect_request(task_id,phase,old_payload) if recovery else None
            except (ProviderError, WireCardinalityError, WirePartialAssessmentError) as error:
                if (not self._output_truncated(error) and not isinstance(error, (WireCardinalityError, WirePartialAssessmentError))) or not getattr(error,'bounded_call_ref',None):raise
                return await self.assess_targets(task_id,phase,old_targets,old_context)
            if observed is None and recovery and self._has_retained_assessment_pages(task_id,phase,old_context):
                # Continue the exact old partition so already successful leaves
                # retain their identities. Only the next decision uses the new
                # projection; this does not relabel or resend those old leaves.
                return await self.assess_targets(task_id,phase,old_targets,old_context)
            if observed:
                record,returned=observed
                if 'value' in returned:
                    # A complete unaffected sibling is retained against its OLD
                    # request. It is not a result for the new grouped targets.
                    result=[a.model_dump() for a in returned['value'].assessments]
                    self._retain_assessments(task_id,phase,old_targets,old_context,result,[(old_payload,returned['value'])])
                    self._last_assessment_input={'task_id':task_id,'phase':phase,'targets':deepcopy(old_targets),'context':deepcopy(old_context),'result':deepcopy(result)}
                    return result
                # Authentication has excluded unknown sends, foreign settings,
                # source/role changes and repeated format defects. A changed
                # current projection is a new request, with the old failure kept.
                context=dict(context,previous_assessment_request={'call':self._call_ref(record),
                    'old_targets_sha256':digest(old_targets),'new_targets_sha256':digest(targets),
                    'transition':'Explicit current Learning projection; original failed request remains unchanged.'})
        build = lambda part: {**context, 'targets': part, 'target': {'ids': [t['id'] for t in part]}, 'instruction': instruction}
        from .semantic_wire import _assessment_evidence
        target_specific = len(targets) > 1 and _assessment_evidence(context) is not None
        revision_source = None
        if target_specific:
            recovery = self.e._wire_recovery(AssessmentBatch)
            if recovery is not None:
                from .disposition_recovery import AssessmentRevisionTransition
                from .semantic_wire import ASSESSMENT_TARGET
                candidate = build(targets)
                seen = set()
                while True:
                    identity = digest(candidate)
                    if identity in seen:
                        raise PolicyError('Assessment revision transition repeats a held request')
                    seen.add(identity)
                    try:
                        old = recovery.inspect_request(task_id, phase, candidate)
                    except AssessmentRevisionTransition as transition:
                        candidate = transition.payload
                        revision_source = candidate
                        if candidate['assessment_revision_transition']['new_wire_revision'] == ASSESSMENT_TARGET:
                            break
                        continue
                    if old is None:
                        break
                    if old is not None and 'value' in old[1]:
                        value = old[1]['value']
                        result = self._ordered_assessments(targets, [(candidate, value)])
                        self._retain_assessments(task_id, phase, targets, context, result, [(candidate, value)])
                        self._last_assessment_input = {'task_id': task_id, 'phase': phase,
                            'targets': deepcopy(targets), 'context': deepcopy(context), 'result': deepcopy(result)}
                        return result
                    break
            if revision_source is not None:
                build = lambda part: self._assessment_part_payload(revision_source, part)
                if revision_source['assessment_revision_transition']['new_wire_revision'] != ASSESSMENT_TARGET:
                    target_specific = False
            if target_specific and all(self._fits(task_id, phase, build([target]), AssessmentBatch) for target in targets):
                async def target_calls():
                    continued = ((previous is not None and previous[1] == context)
                                 or self._has_retained_assessment_pages(task_id, phase, context)
                                 or self._has_saved_assessment_target_leaves(task_id, phase, context))
                    groups = ([[target] for target in targets] if continued
                              else self._assessment_target_groups(task_id, phase, targets, build))
                    for group in groups:
                        async for completed in self._complete_page(task_id, phase, group, build, AssessmentBatch):
                            yield completed
                calls = target_calls()
            elif target_specific:
                if revision_source is not None:
                    raise ConfigurationRequired('Assessment target continuation exceeds configured context; no request sent')
                target_specific = False
        if not target_specific and self._fits(task_id, phase, build(targets), AssessmentBatch):
            calls = self._complete_page(task_id, phase, targets, build, AssessmentBatch)
        elif not target_specific:
            base = {'operation': context.get('operation'), 'timing': context.get('timing'),
                    'targets': [targets[0]], 'target': {'ids': [targets[0]['id']]}, 'instruction': instruction}
            from .learning_projection import active
            if active(context):
                packet=self._packet(task_id,phase+':assessment-candidates',context)
                base.update({k:deepcopy(context[k]) for k in ('learning_projection','learning_correction_scope') if k in context})
                base['learning_candidate_source']={'id':packet['id'],'sha256':digest(packet)}
            compact = await self.compact_context(task_id, phase, context, base, AssessmentBatch)
            build = lambda part: {**base, 'targets': part, 'target': {'ids': [t['id'] for t in part]}, 'bounded_context': compact}
            calls = self._page_calls(task_id, phase, targets, build, AssessmentBatch)
        result = []; completed=[]
        async for part, actual, value in calls:
            _ids_exact([a.target_id for a in value.assessments], [t['id'] for t in part], 'Assessment batch')
            result.extend(a.model_dump() for a in value.assessments)
            completed.append((actual,value))
        result = self._ordered_assessments(targets, completed)
        from .learning_projection import active
        if active(context) or any('assessment_retained_subset' in item for item, _ in completed):
            self._retain_assessments(task_id,phase,targets,context,result,completed)
        self._last_assessment_input={'task_id':task_id,'phase':phase,'targets':deepcopy(targets),'context':deepcopy(context),'result':deepcopy(result)}
        return result

    async def select_skills(self, task, operation):
        selected = []; rejected = []; needs = []; reasons = []; all_ids = set(); cursor = None; number = 0
        while True:
            page = self.e.knowledge.index(task, 'skill', cursor=cursor)
            skills = [self.e.knowledge.read(task, **item['read_args'])['value'] for item in page['items'] if item['status'] != 'retired']
            def build(part):
                return {'operation': operation, 'knowledge': {'skills': part},
                        'catalog_binding': {'index_hash': page['index_hash'], 'offset': page['offset']},
                        'instruction': 'These are complete exact Skill records. Account for each ID exactly once as selected or rejected with reason. Selected requires exact hash, application and exact procedure_clause. Selection and reading never prove use. Other records are separate transport pages.'}
            async def consume(part, payload):
                nonlocal number
                # A single long-history Skill has a separately observed context
                # view; preserve that exact payload for its indivisible consumer.
                subset = lambda items: payload if items == part else build(items)
                async for completed, actual, value in self._complete_page(task['id'], 'skill_selection:page:' + str(number), part, subset, SkillSelection):
                    ids = [s['id'] for s in completed]
                    _ids_exact([s['id'] for s in value.selected + value.rejected], ids, 'Skill selection')
                    if all_ids.intersection(ids):
                        raise PolicyError('Skill source repeated across catalog pages')
                    self.e.store.record('skill_selection_page', operation['id'] + ':bounded:' + str(number), {
                        'id': operation['id'] + ':bounded:' + str(number), 'task_id': task['id'],
                        'index_hash': page['index_hash'], 'read_values': [{'id': s['id'], 'hash': s['hash'], 'source_sha256': digest(s)} for s in completed],
                        'model_proposal': value.model_dump(), 'scope': 'Full exact values actually supplied, not application or semantic proof'})
                    selected.extend(value.selected); rejected.extend(value.rejected); needs.extend(value.new_knowledge_needed); reasons.append(value.rationale)
                    all_ids.update(ids); number += 1
            ordinary = []
            for skill in skills:
                if self._fits(task['id'], 'skill_selection:page:' + str(number), build([skill]), SkillSelection):
                    ordinary.append(skill)
                    continue
                for part in self._pages(task['id'], 'skill_selection', ordinary, build, SkillSelection, start=number):
                    await consume(part, build(part))
                ordinary = []
                # Historical uses can grow independently of the actual procedure.
                # Read all of them in context transport; keep every other source
                # field, full content and exact version at the selection consumer.
                view = deepcopy(skill)
                uses = view.pop('uses', [])
                view['uses_count'] = len(uses) if isinstance(uses, list) else uses
                base = build([view])
                base['skill_source_sha256'] = digest(skill)
                base['instruction'] += ' The uses history is fully observed through bounded_context; this exact procedure view retains all other original fields. Do not infer use or successful effects from its count.'
                compact = await self.compact_context(task['id'], 'skill_selection:page:' + str(number),
                                                     {'full_skill': skill}, base, SkillSelection)
                await consume([skill], {**base, 'bounded_context': compact})
            for part in self._pages(task['id'], 'skill_selection', ordinary, build, SkillSelection, start=number):
                await consume(part, build(part))
            cursor = page['next_cursor']
            if cursor is None:
                break
        return SkillSelection(selected=selected, rejected=rejected, new_knowledge_needed=needs,
                              rationale=_join(reasons) or 'No active Skill in the complete exact current catalog'), all_ids

    def _combine_reviews(self, task_id, packet_id, reviews):
        opinions = []; namespaces = []
        for index, response in enumerate(reviews):
            _ids_exact([o.id for o in response.opinions], [o.id for o in response.opinions], 'Single response review opinions')
            response_id = digest({'packet_id': packet_id, 'index': index, 'response': response.model_dump()})
            mapping = []
            for opinion in response.opinions:
                identity = opinion.id if len(reviews) == 1 else digest({'response_id': response_id, 'original_opinion_id': opinion.id})
                opinions.append(opinion.model_copy(update={'id': identity}))
                mapping.append({'original_id': opinion.id, 'controller_id': identity})
            namespaces.append({'response_id': response_id, 'source_index': index, 'original_response': response.model_dump(), 'opinion_ids': mapping})
        self.e.store.record('bounded_review_responses', packet_id, {'id': packet_id, 'task_id': task_id, 'responses': namespaces})
        review = Review(summary=_join(r.summary for r in reviews), opinions=opinions)
        _ids_exact([o.id for o in review.opinions], [o.id for o in review.opinions], 'Review opinions')
        return review

    def _review_owner(self,task_id,phase,payload):
        """Identify and authenticate a retained old tree before new projection."""
        recovery=self.e._wire_recovery(Review)
        if recovery is None:return None
        root=None
        try:root=recovery.inspect_request(task_id,phase,payload,role='reviewer')
        except ProviderError as error:
            if not self._output_truncated(error) or not getattr(error,'bounded_call_ref',None):raise
            root=True
        import json
        with self.e.store.lock:
            rows=self.e.store.db.execute("SELECT body FROM records WHERE kind='bounded_model_call' AND json_extract(body,'$.task_id')=? AND json_extract(body,'$.phase')=? AND json_extract(body,'$.payload.source_packet_id') IS NOT NULL",(task_id,phase)).fetchall()
        pages={}
        for row in rows:
            record=json.loads(row['body']);part=record['payload']
            packet=self.e.store.record_get('bounded_input',part['source_packet_id'])
            if not packet:
                if part.get('operation')==payload.get('operation'):raise PolicyError('Retained Review page lost its source packet')
                continue
            if packet.get('value')!=payload:continue
            if (packet['id']!=digest({k:v for k,v in packet.items() if k!='id'})
                    or packet['task_id']!=task_id or packet['phase']!=phase+':review-source'):
                raise PolicyError('Retained Review source packet changed')
            try:observed=recovery.inspect_request(task_id,phase,part,role='reviewer')
            except ProviderError as error:
                if not self._output_truncated(error) or not getattr(error,'bounded_call_ref',None):raise
                # A known truncated parent has no admitted opinion. Its exact
                # successful children and uncovered source remain the work.
                continue
            else:
                if observed is None:raise PolicyError('Retained Review page lost its request')
            actual,returned=observed;pages[actual['id']]=(actual,returned)
        return {'pages':list(pages.values())} if root is not None or pages else None

    def _remaining_review_records(self,payload,covered):
        """Subtract exact supplied source fragments, never response equality."""
        coverage={}
        for record in covered:
            path=record['path'];value=payload
            try:
                if not isinstance(path,str) or (path and not path.startswith('/')):raise ValueError(path)
                for component in path.split('/')[1:]:
                    key=component.replace('~1','/').replace('~0','~')
                    value=value[int(key)] if isinstance(value,list) else value[key]
            except (KeyError,IndexError,TypeError,ValueError) as error:
                raise PolicyError('Review source path changed') from error
            entry=coverage.setdefault(path,{'full':False,'ranges':[]})
            if record.get('fragment'):
                text=canonical(value);start=record['start'];end=record['end']
                if type(start)is not int or type(end)is not int or not 0<=start<end<=len(text):
                    raise PolicyError('Review source fragment changed')
                expected=dict(self._record(path,text[start:end]),fragment=True,encoding='canonical-json-text',
                    start=start,end=end,whole_source_sha256=digest(value),
                    id=digest({'path':path,'whole':digest(value),'start':start,'text':text[start:end]}))
                if record!=expected:
                    raise PolicyError('Review source fragment changed')
                entry['ranges'].append((start,end))
            else:
                if record!=self._record(path,value) or entry['full']:raise PolicyError('Review source coverage changed or duplicated')
                entry['full']=True
        for path,entry in coverage.items():
            if ((entry['full'] and entry['ranges'])
                    or any(other!=path and other.startswith(path+'/') for other in coverage)):
                raise PolicyError('Review source coverage overlaps')
            end=0
            for start,next_end in sorted(entry['ranges']):
                if start<end:raise PolicyError('Review source fragments overlap')
                end=next_end
        def remaining(path,value):
            entry=coverage.get(path)
            if entry:
                if entry['full']:return []
                text=canonical(value);cursor=0;parts=[]
                for start,end in [*sorted(entry['ranges']),(len(text),len(text))]:
                    if cursor<start:
                        fragment=text[cursor:start]
                        parts.append(dict(self._record(path,fragment),fragment=True,encoding='canonical-json-text',
                            start=cursor,end=start,whole_source_sha256=digest(value),
                            id=digest({'path':path,'whole':digest(value),'start':cursor,'text':fragment})))
                    cursor=end
                return parts
            if not any(p.startswith(path+'/') for p in coverage):return [self._record(path,value)]
            parts=[]
            children=value.items() if isinstance(value,dict) else enumerate(value)
            for key,item in children:parts.extend(remaining(path+'/'+str(key).replace('~','~0').replace('/','~1'),item))
            return parts
        return remaining('',payload)

    async def _resume_review_pages(self,task_id,phase,payload,packet,owner,*,retained_only):
        pages=sorted(owner['pages'],key=lambda item:item[1]['started_event']['seq'])
        base=None;covered=[];reviews=[]
        for record,returned in pages:
            part=record['payload']['source_records']
            current={k:v for k,v in record['payload'].items() if k!='source_records'}
            if base is not None and current!=base:raise PolicyError('Review retained pages have inconsistent source context')
            base=current
            self._remaining_review_records(payload,[*covered,*part])
            if 'value' in returned:
                reviews.append(returned['value']);covered.extend(part)
            else:
                async for actual_part,actual,value in self._complete_page(task_id,phase,part,
                        lambda items:{**base,'source_records':items},Review,role='reviewer',split=True,retained_only=retained_only):
                    reviews.append(value);covered.extend(actual_part)
        remaining=self._remaining_review_records(payload,covered)
        if remaining:
            async for part,actual,value in self._page_calls(task_id,phase,remaining,lambda items:{**base,'source_records':items},
                    Review,role='reviewer',split=True,exact_phase=True,retained_only=retained_only):
                reviews.append(value);covered.extend(part)
        if self._remaining_review_records(payload,covered):raise PolicyError('Review retained source coverage is incomplete')
        self.e.store.record('bounded_review_coverage',packet['id'],{'id':packet['id'],'task_id':task_id,
            'source_records':covered,'source_sha256':digest(payload)})
        return reviews

    async def review_proposal(self, task_id, operation, phase, payload, *, previous=None, retained_only=False):
        """Return only the review; the caller persists it before disposition.

        Existing finite Web stage names and normal-size payloads remain exact.
        A later disposition never calls this API or regenerates a saved review.
        """
        owner=self._review_owner(task_id,phase,previous) if previous is not None else None
        if owner is not None:payload=previous
        else:owner=self._review_owner(task_id,phase,payload)
        packet = self._packet(task_id, phase + ':review-source', payload)
        reviews = []
        failure = None
        if owner and owner['pages']:
            reviews=await self._resume_review_pages(task_id,phase,payload,packet,owner,retained_only=retained_only)
        elif self._fits(task_id, phase, payload, Review, 'reviewer'):
            try:
                reviews.append(await self._call(task_id, phase, payload, Review, role='reviewer',_retained_only=retained_only))
            except ProviderError as error:
                if not self._output_truncated(error):
                    raise
                failure = error
        if not reviews:
            base = {'operation': operation, 'source_records': [], 'instruction': payload.get('instruction', '') +
                    ' Review this exact source fragment together with the relationship synopsis. Every original fragment is separately supplied; the synopsis is an interpretation, not proof of completeness. You have no tools or execution authority.'}
            compact = await self.compact_context(task_id, phase, payload, base, Review, role='reviewer',retained_only=retained_only)
            build = lambda part: {**base, 'source_packet_id': packet['id'], 'source_records': part, 'bounded_context': compact}
            transported = []
            async for part, actual, value in self._page_calls(task_id, phase, [self._record('', payload)], build, Review, role='reviewer', split=True, exact_phase=True, failure=failure,retained_only=retained_only):
                # Keep the original Web/controller review stage name. Source
                # records and response identities distinguish transport pages.
                reviews.append(value)
                transported.extend(part)
            self.e.store.record('bounded_review_coverage', packet['id'], {'id': packet['id'], 'task_id': task_id,
                                'source_records': transported, 'source_sha256': digest(payload)})
        combined=self._combine_reviews(task_id, packet['id'], reviews)
        self._last_review_input={'task_id':task_id,'phase':phase,'input':deepcopy(payload),'result':combined.model_dump()}
        return combined

    def review_input(self,task_id,phase,result):
        observed=getattr(self,'_last_review_input',{})
        if any(observed.get(k)!=v for k,v in {'task_id':task_id,'phase':phase,'result':result}.items()):
            raise PolicyError('Review return input belongs to another result')
        return deepcopy(observed['input'])

    async def completion_proposal(self, task_id, payload):
        """Keep full final evidence off the ordinary recent-history preview.

        The independent refuter already receives every original via review_proposal.
        This parent decision likewise reads the complete context; exact acceptance,
        result identities and the independent response remain at its consumer.
        A synopsis is an interpretation, not proof that a criterion is achieved.
        """
        phase='completion_proposal'
        if self._fits(task_id,phase,payload,Completion):
            return await self._call(task_id,phase,payload,Completion)
        anchors=[{'id':row['operation']['id'],'operation':{'id':row['operation']['id'],'kind':row['operation']['kind']},
                  'status':row['status'],'source_hash':row.get('source_hash'),
                  'result':None if row.get('result') is None else {
                      'status':row['result'].get('status'),'effect':row['result'].get('effect'),
                      'sha256':digest(row['result'])},'full_record_sha256':digest(row)}
                 for row in payload['operations']]
        base={'operations':anchors,'acceptance':payload['acceptance'],
              'independent_refutation':payload['independent_refutation'],
              'frozen_evidence_sha256':digest(payload['frozen_evidence']),
              'instruction':payload['instruction']+' Complete original context is transported before this call. Operation anchors are identities and observed status only; they do not establish artifact content or success. Preserve every limitation and contrary fact from bounded_context.'}
        compact=await self.compact_context(task_id,phase,payload,base,Completion)
        return await self._call(task_id,phase,{**base,'bounded_context':compact},Completion)

    async def disposition_proposal(self, task_id, operation, phase, payload, *, retained_only=False):
        """Consume the exact previously stored review, without reviewing again."""
        review = Review.model_validate(payload['review'])
        sources = disposition_sources(payload)
        self._packet(task_id, phase + ':disposition-source', payload)
        return await self._dispose(task_id, operation, phase, payload, {'sources': sources}, review,
                                   exact_phase=phase, original_payload=payload,retained_only=retained_only)

    def _disposition_route(self,task_id,phase,bundle,full,calls,result):
        from .learning_projection import active
        if not (active(full) and full.get('learning_correction_scope') and getattr(self.e.gateway,'supports_semantic_wire',False)):
            return
        owned=[]
        for actual_phase,payload in calls:
            found=self.e.disposition_recovery.inspect_request(task_id,actual_phase,payload,role='parent')
            if found is None:raise PolicyError('Learning disposition return is missing')
            record,_=found
            owned.append({'call':self._call_ref(record),'scope':self.e.disposition_recovery.revision_scope(record)})
        packet=self._packet(task_id,phase+':revision-route',bundle)
        key=digest({'task_id':task_id,'phase':phase,'payload':bundle})
        self.e.store.record('learning_disposition_route',key,{'id':key,'task_id':task_id,'phase':phase,
            'source_packet':{'id':packet['id'],'sha256':digest(packet)},'calls':owned,'result':result.model_dump(),
            'scope':'governed' if any(x['scope']=='governed' for x in owned) else 'learning' if any(x['scope']=='learning' for x in owned) else 'none'})

    def disposition_route(self,task_id,phase,payload,result):
        if not getattr(self.e.gateway,'supports_semantic_wire',False):return None
        key=digest({'task_id':task_id,'phase':phase,'payload':payload})
        route=self.e.store.record_get('learning_disposition_route',key)
        if not route or route.get('id')!=key or route.get('task_id')!=task_id or route.get('phase')!=phase or route.get('result')!=result:
            raise PolicyError('Learning disposition route has no exact current input/result')
        ref=route['source_packet'];packet=self.e.store.record_get('bounded_input',ref['id'])
        task=self.e.store.get_task(task_id)
        if (not packet or digest(packet)!=ref['sha256'] or packet['value']!=payload
                or packet['id']!=digest({k:v for k,v in packet.items() if k!='id'})
                or packet['task_id']!=task_id or packet['source_hash']!=task['source_hash']
                or packet['policy_hash']!=self.e.policy.hash):
            raise PolicyError('Learning disposition route input changed')
        scopes=[]
        for item in route['calls']:
            call=self.e.disposition_recovery._read(item['call'])
            if (call['task_id']!=task_id or call['phase']!=phase or call['payload'].get('operative_disposition_contract',{}).get('payload_sha256')!=digest(payload)):
                raise PolicyError('Learning disposition route points to another proposal')
            scope=self.e.disposition_recovery.revision_scope(call)
            if scope!=item['scope']:raise PolicyError('Learning disposition scope changed')
            scopes.append(scope)
        expected='governed' if 'governed' in scopes else 'learning' if 'learning' in scopes else 'none'
        if not scopes or expected!=route['scope']:raise PolicyError('Learning disposition route differs from its returns')
        return route

    @staticmethod
    def review_bundle_input(operation,bundle,web):
        instruction = 'Tool-free adversarial review of this exact bundle and every finalized choice. Challenge omissions, normal success, source authority, learning, evidence, interactions and efficiency. You have no execution authority.'
        payload = {'operation': operation, 'bundle': bundle, 'web': web, 'instruction': instruction}
        for key in ('learning_projection','learning_correction_scope'):
            if key in bundle:payload[key]=deepcopy(bundle[key])
        return payload

    async def review_bundle(self, task_id, operation, phase, bundle, web, additional_reviews=None, *, previous=None, saved=None):
        packet = self._packet(task_id, phase + ':review', {'operation': operation, 'bundle': bundle, 'web': web})
        payload=self.review_bundle_input(operation,bundle,web)
        review = await self.review_proposal(task_id, operation, phase + '_review', payload,previous=previous,retained_only=saved is not None)
        if additional_reviews:
            review = self._combine_reviews(task_id, packet['id'], [review, *additional_reviews])
        disposition = await self._dispose(task_id, operation, phase, bundle, web, review,retained_only=saved is not None)
        self.e._validate_disposition(review, disposition, web['sources'])
        if saved is not None:
            if (saved.get('review')!=review.model_dump() or saved.get('disposition')!=disposition.model_dump()
                    or saved.get('bundle_hash')!=digest(bundle) or saved.get('policy_hash')!=self.e.policy.hash
                    or saved.get('transport_ref')!=packet['id']
                    or self.e.store.record_get('review',saved['id'])!=dict(saved,task_id=task_id,phase=phase)):
                raise PolicyError('Saved reviewed outcome differs from its authenticated original')
            return deepcopy(saved)
        result = {'id': uuid4().hex, 'review': review.model_dump(), 'disposition': disposition.model_dump(),
                  'bundle_hash': digest(bundle), 'policy_hash': self.e.policy.hash,
                  'transport_ref': packet['id'], 'proof_ceiling': 'Complete recorded transport and opinion dispositions; semantic judgment correctness remains unproven'}
        self.e.store.record('review', result['id'], dict(result, task_id=task_id, phase=phase))
        return result

    async def _dispose(self, task_id, operation, phase, bundle, web, review, *, exact_phase=None, original_payload=None, retained_only=False):
        phase = exact_phase or phase + '_disposition'
        instruction = DISPOSITION_INSTRUCTION
        full = deepcopy(original_payload) if original_payload is not None else {'operation': operation, 'bundle': bundle, 'review': review.model_dump(), 'web': web}
        if original_payload is None and bundle.get('learning_projection'):
            full.update({k:deepcopy(bundle[k]) for k in ('learning_projection','learning_correction_scope') if k in bundle})
        contract = {'version': 'unchanged-reviewed-payload-v1', 'operation_sha256': digest(operation),
                    'payload_sha256': digest(bundle), 'review_sha256': digest(review.model_dump()),
                    'instruction': instruction}
        full['operative_disposition_contract'] = contract
        # New scoped Learning pages share the exact phase; their frozen payload
        # still distinguishes each leaf, including output-truncation children.
        if full.get('learning_projection'):exact_phase=phase
        failure = None
        if self._fits(task_id, phase, full, Disposition, 'parent'):
            try:
                result = await self._call(task_id, phase, full, Disposition, role='parent',_retained_only=retained_only)
            except ProviderError as error:
                if not self._output_truncated(error):
                    raise
                failure = error
            else:
                self.e._validate_disposition(review, result, web['sources'])
                self._disposition_route(task_id,phase,bundle,full,[(phase,full)],result)
                return result
        base = {'operation': operation, 'review': {'summary': 'Part of exact stored review', 'opinions': []},
                'web': {'sources': []}, 'instruction': instruction, 'bundle_hash': digest(bundle),
                'operative_disposition_contract': contract}
        for k in ('learning_projection','learning_correction_scope'):
            if k in full:base[k]=deepcopy(full[k])
        compact = await self.compact_context(task_id, phase, full, base, Disposition, role='parent',retained_only=retained_only)
        units = [{'kind': 'opinion', 'value': o.model_dump()} for o in review.opinions]
        def build(part):
            sources = {}
            for unit in part:
                if unit['kind'] == 'source':
                    sources[unit['value']['id']] = unit['value']
                elif unit['kind'] == 'source_fragment':
                    item = sources.setdefault(unit['source_id'], {'id': unit['source_id'],
                        'source_sha256': unit['source_sha256'], 'source_fragments': []})
                    item['source_fragments'].append(unit['fragment'])
            return {**base, 'bounded_context': compact,
                    'review': {'summary': 'Exact opinions; full source-bound review stored', 'opinions': [u['value'] for u in part if u['kind'] == 'opinion']},
                    'web': {'sources': list(sources.values())}}
        for source in web['sources']:
            unit = {'kind': 'source', 'value': source}
            if self._fits(task_id, phase + ':page:' + str(len(units)), build([unit]), Disposition, 'parent'):
                units.append(unit)
                continue
            pending = [self._record('/web/sources/' + source['id'], source)]
            while pending:
                record = pending.pop(0)
                unit = {'kind': 'source_fragment', 'source_id': source['id'],
                        'source_sha256': digest(source), 'fragment': record}
                if self._fits(task_id, phase + ':page:' + str(len(units)), build([unit]), Disposition, 'parent'):
                    units.append(unit)
                else:
                    pending[:0] = self._split(record)
        if not units:
            units = [{'kind': 'empty', 'value': None}]
        dispositions = [];route_calls=[]
        async for part, payload, result in self._page_calls(task_id, phase, units, build, Disposition, role='parent', exact_phase=exact_phase is not None, failure=failure,retained_only=retained_only):
            self.e._validate_disposition(Review.model_validate(payload['review']), result, payload['web']['sources'])
            dispositions.append(result)
            # The successful call's exact phase is carried by the request index;
            # page number differs only in the ordinary non-exact transport.
            actual_phase=phase if exact_phase is not None else phase+':page:'+str(len(route_calls))
            route_calls.append((actual_phase,payload))
        verdict = 'hold' if any(d.verdict == 'hold' for d in dispositions) else 'revise' if any(d.verdict == 'revise' for d in dispositions) else 'proceed'
        result = Disposition(verdict=verdict, rationale=_join(d.rationale for d in dispositions),
                             opinion_responses=[r for d in dispositions for r in d.opinion_responses],
                             web_refs=_unique([r for d in dispositions for r in d.web_refs]))
        self.e._validate_disposition(review, result, web['sources'])
        self._disposition_route(task_id,phase,bundle,full,route_calls,result)
        return result

    def _assessment_origin(self,task_id,payload):
        if 'assessment_retained_subset' in payload:
            ref = payload['assessment_retained_subset']
            expected, _ = self._assessment_subset_leaf(task_id, ref)
            if expected != payload: raise PolicyError('Assessment retained source context changed')
            original, _ = self._assessment_partial(task_id, ref)
            return self._assessment_origin(task_id, original['payload'])
        if 'assessment_correction' in payload:
            original = self._validate_assessment_correction(task_id, payload)
            return self._assessment_origin(task_id, original['payload'])
        if 'assessment_revision_transition' in payload:
            recovery = self.e._wire_recovery(AssessmentBatch)
            original = recovery._read(payload['assessment_revision_transition']['parent_call'])
            recovery._validate_revision_payload(task_id, original['phase'], payload, original['binding']['role'])
            return self._assessment_origin(task_id, original['payload'])
        origin={k:v for k,v in payload.items() if k not in {'targets','target','instruction'}}
        if 'bounded_context' in payload:
            origin=payload['bounded_context']
            if origin.get('packet_id'):
                packet=self.e.store.record_get('bounded_input',origin['packet_id'])
                if (not packet or packet['id']!=digest({k:v for k,v in packet.items() if k!='id'})
                        or packet['task_id']!=task_id or digest(packet['value'])!=origin['source_sha256']):
                    raise PolicyError('Assessment old context source differs')
                origin=packet['value']
        return origin

    _assessment_group_limit = 3

    def _assessment_target_groups(self, task_id, phase, targets, build):
        """Bounded same-stage groups for a fresh target assessment.

        Capacity alone once packed nine targets into one response that
        returned a single row. Keep the first send bounded; the existing
        split and partial-correction machinery reduces a failing group.
        """
        groups = []
        pending = list(targets)
        while pending:
            group = []
            while pending and len(group) < self._assessment_group_limit:
                if group and not self._fits(task_id, phase, build(group + [pending[0]]), AssessmentBatch):
                    break
                group.append(pending.pop(0))
            groups.append(group)
        return groups

    def _has_saved_assessment_target_leaves(self, task_id, phase, context):
        """True when an exact old single-target partition owns this phase.

        A resume must continue the old call units so saved leaves keep
        their identities; grouping applies only where no such owner exists.
        """
        import json
        with self.e.store.lock:
            rows = self.e.store.db.execute(
                "SELECT body FROM records WHERE kind='bounded_model_call' AND json_extract(body,'$.task_id')=? AND json_extract(body,'$.phase')=?",
                (task_id, phase)).fetchall()
        singleton = False
        grouped = False
        for row in rows:
            record = json.loads(row['body'])
            if self._assessment_origin(task_id, record['payload']) != context:
                continue
            size = len(record.get('payload', {}).get('targets', []))
            singleton |= size == 1
            grouped |= size > 1
        # A grouped request owns its own exact split tree. Its correction
        # leaves must not turn successful sibling groups into new singleton
        # requests on resume. A phase containing only singleton calls is the
        # legacy partition and keeps those original call identities.
        return singleton and not grouped

    def _has_retained_assessment_pages(self,task_id,phase,context):
        import json
        with self.e.store.lock:
            rows=self.e.store.db.execute("SELECT body FROM records WHERE kind='bounded_model_call' AND json_extract(body,'$.task_id')=? AND substr(json_extract(body,'$.phase'),1,?)=?",(task_id,len(phase+':page:'),phase+':page:')).fetchall()
        found=False;recovery=self.e._wire_recovery(AssessmentBatch)
        for row in rows:
            record=json.loads(row['body'])
            if self._assessment_origin(task_id,record['payload'])!=context:continue
            # Even one exact old page establishes the old continuation owner.
            # Unknown/foreign/repeated-defect pages hold here before new calls.
            try:actual=recovery.inspect_request(task_id,record['phase'],record['payload'])
            except (ProviderError, WirePartialAssessmentError) as error:
                if (not self._output_truncated(error) and not isinstance(error, WirePartialAssessmentError)) or not getattr(error,'bounded_call_ref',None):raise
                found=True;continue
            if actual is None:raise PolicyError('Assessment old page lost its receipt')
            found=True
        return found

    def _retained_assessment_parts(self,task_id,phase,result,*,context=None,subset=None):
        """Authenticate retained exact leaves and their original context together."""
        import json
        recovery=self.e._wire_recovery(AssessmentBatch)
        if recovery is None:raise PolicyError('Assessment legacy recovery needs actual receipts')
        # A composed subset is explicitly different from a successful provider
        # call. Its existing result packet owns original ordering and context.
        with self.e.store.lock:
            packets=self.e.store.db.execute("SELECT body FROM records WHERE kind='bounded_assessment_result' AND json_extract(body,'$.task_id')=? AND json_extract(body,'$.phase')=?",(task_id,phase)).fetchall()
        candidates=[]
        for row in packets:
            saved=json.loads(row['body'])
            if saved.get('result')!=result:continue
            packet=self.e.store.record_get('bounded_input',saved.get('input',{}).get('id'))
            if not packet:raise PolicyError('Assessment stage original input packet is absent')
            original=packet['value']['context'];targets=packet['value']['targets']
            if saved.get('id')!=self._assessment_key(task_id,phase,targets,original,result):
                raise PolicyError('Assessment stage composed key changed')
            if context is not None and original!=context:continue
            if subset and any(original.get(k)!=v for k,v in subset.items()):continue
            self.authenticate_assessments(task_id,phase,targets,original,result)
            completed=self._assessment_result_parts(task_id,phase,saved['calls'],original)
            candidates.append((targets,original,completed))
        if len(candidates)>1:raise PolicyError('Assessment stage original input is ambiguous')
        if candidates:return candidates[0]
        expected={a['target_id']:a for a in result};matched={};contexts=[];completed=[];ordered=[]
        if len(expected)!=len(result):raise PolicyError('Assessment retained targets are ambiguous')
        with self.e.store.lock:
            rows=self.e.store.db.execute("SELECT body FROM records WHERE kind='bounded_model_call' AND json_extract(body,'$.task_id')=? AND (json_extract(body,'$.phase')=? OR substr(json_extract(body,'$.phase'),1,?)=?) AND json_extract(body,'$.status')='succeeded'",(task_id,phase,len(phase+':page:'),phase+':page:')).fetchall()
        for row in rows:
            call=json.loads(row['body']);payload=call.get('payload',{})
            values=call.get('result',{}).get('assessments',[])
            if not values or any(expected.get(a['target_id'])!=a for a in values):continue
            origin=self._assessment_origin(task_id,payload)
            if context is not None and origin!=context:continue
            if subset and any(origin.get(k)!=v for k,v in subset.items()):continue
            actual=recovery.inspect_request(task_id,call['phase'],payload)
            if not actual or 'value' not in actual[1] or actual[1]['value'].model_dump()['assessments']!=values:
                raise PolicyError('Assessment retained leaf differs from its actual return')
            targets={t['id']:t for t in payload['targets']}
            for value in values:
                identity=value['target_id']
                if identity in matched:raise PolicyError('Assessment retained leaf coverage is ambiguous')
                matched[identity]=targets[identity]
            contexts.append(origin);completed.append((payload,actual[1]['value']))
            # The producer awaits each exact leaf before advancing. Its retained
            # started event and supplied target list establish order; database
            # insertion order and an untrusted saved result array do not.
            ordered.append((actual[1]['started_event']['seq'],payload['targets']))
        if set(matched)!=set(expected) or not contexts or any(c!=contexts[0] for c in contexts):
            raise PolicyError('Assessment stage has no complete authenticated original targets/context')
        targets=[target for _,part in sorted(ordered,key=lambda item:item[0]) for target in part]
        if self._ordered_assessments(targets,completed)!=result:
            raise PolicyError('Assessment retained original target order changed')
        return targets,contexts[0],completed

    def retained_learning_choices(self,task_id,phase,attempt,context):
        targets,original,completed=self._retained_assessment_parts(task_id,phase,attempt['choice_assessments'],subset=context)
        from .learning_projection import merge_ideas
        recomposed=deepcopy(original['learning'])
        recomposed['ideas']=merge_ideas(recomposed['ideas'],[i for a in attempt['choice_assessments'] for i in a['assessment']['ideas']])
        if recomposed!=attempt['learning']:raise PolicyError('Learning completed assessment composition changed')
        self._retain_assessments(task_id,phase,targets,original,attempt['choice_assessments'],completed)
        return targets,original

    def adopt_post_attempt(self,task_id,row,attempt):
        """Recover old producer metadata only from authenticated original stages."""
        attempt=deepcopy(attempt)
        context={'operation':row['operation'],'result':row['result'],'pre':row['pre_bundle']}
        targets=row['pre_bundle'].get('targets') or operation_targets(row['operation'],row['pre_bundle']['skills'])
        if 'assessment_context' not in attempt:
            original_targets,original,completed=self._retained_assessment_parts(task_id,'post_assessment',attempt['assessments'],subset=context)
            if original_targets!=targets:raise PolicyError('Saved post assessment original targets changed')
            self._retain_assessments(task_id,'post_assessment',targets,original,attempt['assessments'],completed)
            attempt['assessment_context']=original
        original=attempt['assessment_context']
        if any(original.get(k)!=v for k,v in context.items()):
            raise PolicyError('Saved post assessment original scope changed')
        self.authenticate_assessments(task_id,'post_assessment',targets,original,attempt['assessments'])
        if 'learning' in attempt:
            predecessor=original.get('actual_revision_feedback')
            if 'choice_assessments' in attempt:
                attempt['choices'],attempt['choice_context']=self.authenticate_learning_choices(task_id,
                    'learning_proposal','learning_choices_before',attempt,context,predecessor)
            else:self.authenticate_learning_attempt(task_id,'learning_proposal',attempt,context,predecessor)
        return attempt

    def authenticate_learning_choices(self,task_id,learning_phase,assessment_phase,attempt,context,predecessor,
                                      *,assessment_field='choice_assessments',target_field='choices'):
        """Both owners consume the original proposal and exact ordered returns."""
        values=attempt[assessment_field]
        if not attempt.get('choice_context'):
            targets,original=self.retained_learning_choices(task_id,assessment_phase,
                dict(attempt,choice_assessments=values),{k:v for k,v in context.items() if k in {'operation','result','parent_operation','acquisition'}})
        else:targets,original=attempt[target_field],attempt['choice_context']
        self.authenticate_assessments(task_id,assessment_phase,targets,original,values)
        self.authenticate_learning_attempt(task_id,learning_phase,dict(attempt,learning=original['learning']),context,predecessor)
        from .learning_projection import merge_ideas
        recomposed=deepcopy(original['learning'])
        recomposed['ideas']=merge_ideas(recomposed['ideas'],[i for a in values for i in a['assessment']['ideas']])
        if recomposed!=attempt['learning']:raise PolicyError('Learning retained assessment composition changed')
        return targets,original

    def authenticate_learning_attempt(self,task_id,phase,attempt,context,predecessor):
        """Bind a pending old full proposal before projecting its NEXT decision."""
        if not getattr(self.e.gateway,'supports_semantic_wire',False):return
        reference=attempt.get('proposal_receipt')
        if reference:
            record=self.e.disposition_recovery._read(reference)
            if (record['phase']!=phase or any(record['payload'].get(k)!=v for k,v in context.items()
                    if k in {'operation','result','pre','parent_operation','acquisition'})):
                raise PolicyError('Learning retained proposal belongs to another request scope')
            value=self._learning_call(task_id,record,Learning)
            if value.model_dump()!=attempt['learning']:raise PolicyError('Learning retained proposal changed')
            return
        if attempt.get('learning_composition'):
            current=self.learning_review_context(task_id,context['operation']['id'],attempt['learning'])
            if current!=attempt['learning_composition']:raise PolicyError('Learning retained composition changed')
            return
        # Old Web attempts did not retain a direct call reference. Match their
        # exact known scope and predecessor as well as their result, then use
        # the existing full input/messages/event/response authenticator.
        with self.e.store.lock:
            rows=self.e.store.db.execute("SELECT body FROM records WHERE kind='bounded_model_call' AND json_extract(body,'$.task_id')=? AND json_extract(body,'$.phase')=? AND json_extract(body,'$.status')='succeeded'",
                (task_id,phase)).fetchall()
        import json
        matches=[]
        for row in rows:
            record=json.loads(row['body']);payload=record.get('payload',{})
            if (record.get('result')!=attempt['learning'] or payload.get('operation')!=context['operation']
                    or payload.get('result')!=context['result'] or payload.get('pre')!=context.get('pre')
                    or payload.get('parent_operation')!=context.get('parent_operation')
                    or payload.get('acquisition')!=context.get('acquisition')):continue
            saved_input=attempt.get('learning_input')
            if saved_input is not None:
                # A promoted post_review is this attempt's own return. The
                # retained proposal input, when present, remains the authority
                # for its predecessor and all other original source fields.
                if self._learning_source_core(payload)!=self._learning_source_core(saved_input):continue
            elif payload.get('actual_revision_feedback')!=predecessor:continue
            value=self._learning_call(task_id,record,Learning)
            if value.model_dump()!=attempt['learning']:raise PolicyError('Learning saved response differs from actual return')
            matches.append(record)
        if len(matches)!=1:raise PolicyError('Learning pending proposal has no unique authenticated original request')
        attempt['proposal_receipt']=self._call_ref(matches[0])

    def learning_application_contract(self, task_id, payload):
        """Resolve only a controller-owned, fully observed original-source view.

        This does not admit a model-authored replacement operation or result.
        The eventual Knowledge consumer still validates the untouched originals.
        """
        if 'learning_anchor_ref' not in payload:
            return None
        reference = payload['learning_anchor_ref']
        if (not isinstance(reference, dict) or set(reference) != {'id', 'sha256'}
                or any(not isinstance(reference[k], str) or len(reference[k]) != 64
                       or any(c not in '0123456789abcdef' for c in reference[k]) for k in reference)):
            raise PolicyError('LEARNING_PROVENANCE: Learning anchor reference is malformed')
        record = self.e.store.record_get('bounded_learning_anchors', reference['id'])
        if (not record or digest(record) != reference['sha256']
                or record['id'] != digest({k: v for k, v in record.items() if k != 'id'})):
            raise PolicyError('LEARNING_PROVENANCE: Learning anchor reference is missing or changed')
        task = self.e.store.get_task(task_id)
        if (record['task_id'] != task_id or record['source_hash'] != task['source_hash']
                or record['policy_hash'] != self.e.policy.hash):
            raise PolicyError('LEARNING_PROVENANCE: Learning anchor source is stale or belongs to another task')
        packet = self.e.store.record_get('bounded_input', record['source_packet_id'])
        observed = self.e.store.record_get('bounded_context', record['observed_context_id'])
        context_packet = self.e.store.record_get('bounded_input', record['observed_context_id'])
        if (not packet or digest(packet) != record['source_packet_sha256']
                or packet['task_id'] != task_id or packet['source_hash'] != task['source_hash']
                or packet['policy_hash'] != self.e.policy.hash
                or payload.get('source_packet_id') != packet['id']
                or packet['id'] != digest({k: v for k, v in packet.items() if k != 'id'})):
            raise PolicyError('LEARNING_PROVENANCE: Learning anchor original packet changed or is foreign')
        if (not observed or digest(observed) != record['observed_context_sha256']
                or observed['task_id'] != task_id or not observed['source_records']
                or not context_packet or context_packet['value'] != packet['value']
                or context_packet['task_id'] != task_id or context_packet['source_hash'] != task['source_hash']
                or context_packet['policy_hash'] != self.e.policy.hash
                or context_packet['id'] != digest({k: v for k, v in context_packet.items() if k != 'id'})
                or observed['result']['source_sha256'] != digest(packet['value'])
                or observed['result']['coverage_sha256'] != digest(observed['source_records'])
                or payload.get('bounded_context') != observed['result']):
            raise PolicyError('LEARNING_PROVENANCE: Learning anchor requires its complete unchanged source observation')
        if payload.get('operation') != record['operation_view'] or payload.get('result') != record['result_view']:
            raise PolicyError('LEARNING_PROVENANCE: Learning anchor view differs from the controller-owned source')
        originals = packet['value']
        # A constant number of exact output-root references replaces the former
        # recursively repeated index. Every descendant remains in the original
        # packet and observed source pages; any claimed pointer is still checked
        # against the complete original by Knowledge._applications.
        evidence = [{'pointer': '/' + name, 'sha256': digest(originals['result'][name])}
                    for name in ('stdout', 'stderr', 'data', 'artifacts') if name in originals['result']]
        return {'instruction': 'These hashes bind the complete original operation/result, not their reference displays. Every source fragment was supplied in the protected context observation. Selection is not use; applications require the exact selected Skill/version/procedure and concrete original-result JSON-pointer evidence. Do not infer use or verified benefit from a reference, status or summary. Evidence roots cover the original output; descendants remain in the source pages and the final consumer verifies any claimed pointer against the untouched original.',
                'operation_sha256': digest(originals['operation']), 'result_sha256': digest(originals['result']),
                'result_evidence': evidence, 'source_packet_id': packet['id'],
                'source_observation': {'id': observed['id'], 'sha256': digest(observed)},
                'proof_ceiling': 'Complete recorded source transport; synopsis fidelity and semantic judgment remain unproven'}

    def learning_validation_input(self, task_id, payload):
        """Resolve the same verified originals for effect-free proposal checks."""
        contract = self.learning_application_contract(task_id, payload)
        if contract is None:
            return payload
        return self.e.store.record_get('bounded_input', contract['source_packet_id'])['value']

    async def _learning_anchor_view(self, task_id, phase, payload, packet, base):
        operation = payload['operation']; result = payload['result']
        operation_view = {'id': operation['id'], 'kind': operation['kind'],
                          'representation': 'reference to complete recorded operation', 'source_sha256': digest(operation)}
        result_view = {key: deepcopy(result[key]) for key in ('operation_id', 'status', 'effect') if key in result}
        result_view.update(representation='reference to complete recorded result; output is in the observed source pages',
                           source_sha256=digest(result))
        # Observe the complete original before allowing a Learning contract to
        # cite it. Review transport avoids re-inserting Learning's recursive
        # evidence index while preparing that very same index's source.
        compact = await self.compact_context(task_id, phase + ':original-anchors', payload,
                    {'operation': operation_view, 'result': result_view}, Review, force_observation=True)
        observed = self.e.store.record_get('bounded_context', compact['packet_id'])
        if not observed or observed['result'] != compact:
            raise PolicyError('Learning original-source observation was not retained')
        task = self.e.store.get_task(task_id)
        record = {'schema': 'bounded-learning-anchors-v1', 'task_id': task_id,
                  'source_hash': task['source_hash'], 'policy_hash': self.e.policy.hash,
                  'source_packet_id': packet['id'], 'source_packet_sha256': digest(packet),
                  'observed_context_id': observed['id'], 'observed_context_sha256': digest(observed),
                  'operation_view': operation_view, 'result_view': result_view}
        record['id'] = digest(record)
        previous = self.e.store.record_get('bounded_learning_anchors', record['id'])
        if previous is not None and previous != record:
            raise PolicyError('Learning original-source anchor identity changed')
        self.e.store.record('bounded_learning_anchors', record['id'], record)
        return {**base, 'operation': operation_view, 'result': result_view,
                'learning_anchor_ref': {'id': record['id'], 'sha256': digest(record)}}, compact

    def _composition(self, payload):
        ref = payload.get('composition_ref', {})
        value = self.e.store.record_get('bounded_learning_composition', ref.get('id')) if ref.get('id') else None
        if (not value or digest(value) != ref.get('sha256')
                or value['id'] != digest({k: v for k, v in value.items() if k != 'id'})
                or value['source_packet_id'] != payload.get('source_packet_id')
                or value['operation_id'] != payload.get('operation', {}).get('id')):
            raise PolicyError('LEARNING_PROVENANCE: exact composition record differs')
        packet = self.e.store.record_get('bounded_input', value['source_packet_id'])
        if not packet or value.get('task_id') != packet.get('task_id'):
            raise PolicyError('LEARNING_PROVENANCE: composition owner differs')
        for receipt in value['part_receipts']:
            record = self.e.store.record_get('bounded_model_call', receipt['id'])
            if not record or self._call_ref(record) != receipt:
                raise PolicyError('LEARNING_PROVENANCE: composition lost a source call')
        return value

    def learning_review_context(self, task_id, operation_id, learning):
        # Choice assessments append new Ideas after this proposal. Match the
        # exact non-Idea result and contained original Idea values, not a digest
        # of a growing bundle. This is review evidence, never effect authority.
        result = learning.model_dump() if hasattr(learning, 'model_dump') else learning
        matches = []
        for record in self.e.store.records('bounded_learning'):
            if record.get('task_id') != task_id or not record.get('composition_ref'):
                continue
            original = record['result']
            if (any(result.get(k) != v for k, v in original.items() if k != 'ideas')
                    or not all(i in result['ideas'] for i in original['ideas'])):
                continue
            packet = self.e.store.record_get('bounded_input', record['id'])
            if packet['value']['operation']['id'] != operation_id:
                continue
            matches.append(self._composition({'composition_ref': record['composition_ref'],
                'source_packet_id': packet['id'], 'operation': {'id': operation_id}}))
        if len(matches) > 1:
            raise PolicyError('LEARNING_PROVENANCE: composed review source is ambiguous')
        return matches[0] if matches else None

    @staticmethod
    def _learning_source_core(payload):
        # These two fields grow from retained proposal evidence on ordinary
        # Web/Engine reopen. All actual source, result, selection, review and
        # current Knowledge context remain exact applicability boundaries.
        return {k: v for k, v in payload.items() if k not in {'required_ideas', 'historical_response_evidence', 'actual_format_feedback'}}

    @classmethod
    def _later_learning_revision(cls, old, current):
        """Applicability only: callers authenticate both projected inputs first."""
        from .learning_projection import VERSION, active
        if not active(old) or not active(current) or old.get('learning_focus') or current.get('learning_focus'):
            return False
        before = cls._learning_source_core(old); after = cls._learning_source_core(current)
        previous = before.pop('actual_revision_feedback', None)
        feedback = after.pop('actual_revision_feedback', None)
        if before != after or not feedback or feedback.get('version') != VERSION:
            return False
        if previous is not None and previous.get('version') != VERSION:
            return False
        sources = previous['sources'] if previous else []
        following = feedback['sources']
        extended = len(following) > len(sources) and following[:len(sources)] == sources
        # Both source projections were authenticated using their saved
        # definition. A current-only view of the same exact archive is new
        # judgment input, never an edit to an old request or accepted result.
        versions = {None: 0, 'current-frontier-v1': 1, 'current-frontier-v2': 2}
        projected = (following == sources and previous is not None
            and previous.get('fact_projection') in versions and feedback.get('fact_projection') in versions
            and versions[previous.get('fact_projection')] < versions[feedback.get('fact_projection')])
        return extended or projected

    def _prior_learning(self, task_id, phase, payload):
        task = self.e.store.get_task(task_id)
        current = {i['id']: i for i in payload.get('required_ideas', [])}
        core = self._learning_source_core(payload)
        matches = []
        for record in self.e.store.records('bounded_model_call'):
            if (record.get('task_id') != task_id or record.get('source_hash') != task['source_hash']
                    or record.get('policy_hash') != self.e.policy.hash or record.get('status') not in {'succeeded', 'revalidated', 'failed'}
                    or record.get('phase', '').split(':page:', 1)[0] != phase
                    or record.get('learning_schema', 'Learning') != 'Learning'):
                continue
            if any(r['id'] == record['id'] for r, _, _ in matches):
                continue  # The exact terminal was already authenticated via its root.
            request = record.get('payload', {})
            packet_id = request.get('source_packet_id')
            packet = self.e.store.record_get('bounded_input', packet_id) if packet_id else None
            original = packet.get('value') if packet else request
            if not original or self._learning_source_core(original) != core:
                continue
            if record.get('status') == 'failed':
                # A repeated output defect remains held unless its exact final
                # whole proposal now passes the current contract. Other failed
                # calls retain their existing transport/partition recovery.
                if (packet_id or record['phase'] != phase or not record.get('measurement', {}).get('semantic_wire')
                        or not record.get('first_fault', {}).get('message', '').startswith('Model repeated the same')):
                    continue
                # A held outer-count defect owns the existing partition and
                # recompose path; complete-proposal re-acceptance can never
                # admit it (its final response fails the count by definition).
                # Classify through the same feedback authority that path uses
                # and leave the record to it.
                held = self._failed_learning_feedback(task_id, record, Learning, task['actor'], partition=True)
                if held['held'] and held.get('cardinality'):
                    continue
                try:
                    value = self._recover_rejected_synthesis(task_id, record, Learning)
                except PolicyError as error:
                    if str(error).startswith(('LEARNING_PROVENANCE:', 'WIRE_PROVENANCE:')):
                        raise
                    raise PolicyError('Learning saved correction repeated a known defect; dependent proposal remains unadmitted: ' + str(error)) from error
                record = self._learning_receipts[digest(value.model_dump())]
                request = record['payload']
            if packet and (packet.get('id') != digest({k: v for k, v in packet.items() if k != 'id'})
                    or packet.get('task_id') != task_id or packet.get('source_hash') != task['source_hash']
                    or packet.get('policy_hash') != self.e.policy.hash):
                raise PolicyError('LEARNING_PROVENANCE: historical full source packet changed')
            for idea in original.get('required_ideas', []):
                actual = current.get(idea['id'])
                if not actual or any(actual[k] != idea[k] for k in ('proposal', 'target')):
                    raise PolicyError('LEARNING_PROVENANCE: retained Learning source Idea changed')
            value = self._learning_call(task_id, record, Learning)
            linked = self.e.store.record_get('bounded_learning_return', record['key'])
            if linked:
                value = self._learning_return(task_id, record['phase'], request, Learning, task['actor'], record['measurement'])
                record = self._learning_receipts[digest(value.model_dump())]
                request = record['payload']
            if request.get('actual_format_feedback') and not linked:
                feedback = request['actual_format_feedback']
                explicit = record.get('learning_parent', feedback.get('source_receipt'))
                if explicit is not None:
                    parent = self.e.store.record_get('bounded_model_call', explicit.get('id'))
                    parents = [parent] if parent and self._call_ref(parent) == explicit else []
                else:
                    parents = [r for r in self.e.store.records('bounded_model_call')
                        if r.get('task_id') == task_id and r.get('phase') == record['phase']
                        and r.get('result') == feedback.get('rejected_response')
                        and {k: v for k, v in r.get('payload', {}).items() if k != 'actual_format_feedback'}
                           == {k: v for k, v in request.items() if k != 'actual_format_feedback'}]
                if len(parents) != 1:
                    raise PolicyError('LEARNING_PROVENANCE: historical corrected response lacks an exact original')
                parent = parents[0]
                returned = self._learning_return(task_id, record['phase'], parent['payload'], Learning,
                    task['actor'], parent['measurement'])
                if returned is None or self._learning_receipts[digest(returned.model_dump())]['id'] != record['id']:
                    raise PolicyError('LEARNING_PROVENANCE: historical corrected return differs')
            if not any(r['id'] == record['id'] for r, _, _ in matches):
                matches.append((record, value, not packet_id))
        return matches

    async def learning_proposal(self, task_id, phase, payload):
        # Inspect authentic prior leaves BEFORE preparing a new prompt or
        # compacting new history. Reopen often has extra retained Ideas.
        prior = self._prior_learning(task_id, phase, payload)
        corrected = []
        for record, value, full in prior:
            try:
                self._validate(Learning, value, self.e._prepare_learning_payload(task_id, record['payload'], phase=record['phase']), task_id=task_id)
            except PolicyError as error:
                if str(error).startswith('LEARNING_PROVENANCE:'):raise
                # Authentication was outside this branch. Only an authentic
                # retained proposal's actual defect enters effect-free feedback.
                value = await self._call(task_id, record['phase'], record['payload'], Learning)
                record = self._learning_receipts[digest(value.model_dump())]
            corrected.append((record, value, full))
        prior = corrected
        payload = self.e._prepare_learning_payload(task_id, payload, phase=phase)
        required = payload.get('required_ideas', [])
        _ids_exact([i['id'] for i in required], [i['id'] for i in required], 'Required ideas')
        whole = [(r, v) for r, v, full in prior if full]
        if len(whole) > 1:
            raise PolicyError('LEARNING_PROVENANCE: whole Learning completion is ambiguous')
        if whole:
            self.e._validate_learning_proposal(task_id, whole[0][1], payload)
            return whole[0][1]
        task = self.e.store.get_task(task_id)
        configuration = self._configuration(task_id, task['actor'])
        plan_identity = {'task_id': task_id, 'phase': phase, 'source': task['source_hash'],
            'policy': self.e.policy.hash, 'role': task['actor'], 'lease': task['state'].get('delegation_lease'),
            'configuration': configuration, 'source_core': self._learning_source_core(payload)}
        plan_key = digest(plan_identity)
        plan = self.e.store.record_get('bounded_learning_plan', plan_key)
        if not plan and configuration is not None and configuration.get('max_output_tokens') == 65536:
            # The stable source packet keeps each successful output owner's
            # original identities. Its calls still authenticate independently;
            # this lookup does not promote a plan into success evidence.
            previous_key = digest(dict(plan_identity, configuration=dict(configuration, max_output_tokens=32768)))
            previous = self.e.store.record_get('bounded_learning_plan', previous_key)
            if previous is not None:
                if previous.get('id') != previous_key or previous.get('task_id') != task_id:
                    raise PolicyError('LEARNING_PROVENANCE: previous Learning plan identity changed')
                plan = previous
        failure = self._learning_cardinality_failure(task_id, phase, payload, Learning) if not prior and not plan else None
        if not prior and not plan and self._fits(task_id, phase, payload, Learning):
            if failure is None:
                try:
                    value = await self._call(task_id, phase, payload, Learning)
                except (ProviderError, WireCardinalityError) as error:
                    if not self._output_truncated(error) and not isinstance(error, WireCardinalityError):
                        raise
                    failure = error
                else:
                    self.e._validate_learning_proposal(task_id, value, payload)
                    return value
            if isinstance(failure, WireCardinalityError):
                payload = self.e._prepare_learning_payload(task_id, payload, phase=phase)
                required = payload.get('required_ideas', [])
        if plan:
            packet = self.e.store.record_get('bounded_input', plan['source_packet_id'])
            if (not packet or digest(packet) != plan['source_packet_sha256']
                    or self._learning_source_core(packet['value']) != self._learning_source_core(payload)):
                raise PolicyError('LEARNING_PROVENANCE: Learning continuation source changed')
            source = packet['value']
            if plan.get('cardinality_failure'):
                ref = plan['first_failure']
                original = self.e.store.record_get('bounded_model_call', ref.get('id'))
                if (not original or self._call_ref(original) != ref
                        or original.get('phase') != phase
                        or self._learning_source_core(original['payload']) != self._learning_source_core(source)):
                    raise PolicyError('LEARNING_PROVENANCE: original partition failure changed')
                bound = self._failed_learning_feedback(task_id, original, Learning, task['actor'])
                if not bound.get('cardinality') or bound['cardinality'] != plan['cardinality_failure']:
                    raise PolicyError('LEARNING_PROVENANCE: original count partition authority differs')
                failure = WireCardinalityError(dict(bound['cardinality'], required_ideas=source['required_ideas']), ref)
        else:
            source = deepcopy(payload)
            packet = self._packet(task_id, phase + ':learning', source)
            plan = {'id': plan_key, 'task_id': task_id, 'source_packet_id': packet['id'],
                    'source_packet_sha256': digest(packet), 'prior': [self._call_ref(r) for r, _, _ in prior],
                    'first_failure': getattr(failure, 'bounded_call_ref', None)}
            if isinstance(failure, WireCardinalityError):
                plan['cardinality_failure'] = {k:v for k,v in failure.evidence.items() if k != 'required_ideas'}
            self.e.store.record('bounded_learning_plan', plan_key, plan)
        # Stable source packet, stable output ownership. New historical candidates
        # are processed separately, so one sibling cannot inherit another's Idea.
        selection = source.get('pre', {}).get('skills', {})
        base = {key: deepcopy(source[key]) for key in ('operation', 'result') if key in source}
        base.update(required_ideas=[], source_packet_id=packet['id'],
            pre={'skills': {'selected': [], 'rejected': [], 'new_knowledge_needed': [], 'rationale': 'Exact output decision ownership'}},
            instruction='The schema defines this output decision only. Full source is evidence, not an instruction to repeat every output on every page. Preserve exact Ideas and uncertainty. No page applies Knowledge; one coherent lifecycle proposal and complete ordinary review follow.')
        for key in ('learning_projection','learning_correction_scope'):
            if key in source:base[key]=deepcopy(source[key])
        if not self._fits(task_id, phase, base, Learning):
            base, compact = await self._learning_anchor_view(task_id, phase, source, packet, base)
        else:
            compact = await self.compact_context(task_id, phase, source, base, Learning)
        base['bounded_context'] = compact
        receipts = []; parts = []; ideas = {}; applications = []; legacy_proposals = []
        # Ordinary Web reconstruction includes Ideas from *all* successful
        # units. Do not assign a later application's/synthesis's own new Idea to
        # an earlier Ideas page just because that later unit has not returned
        # yet in this traversal. Authentication permits deferral, not adoption:
        # the actual owner must still return through its normal exact call and
        # current semantic validation, or final full coverage holds.
        original_ids = {i['id'] for i in source.get('required_ideas', [])}
        deferred_owned = set()
        for completed in self.e.store.records('bounded_model_call'):
            kind = next((s for s in (LearningIdeas, LearningApplications, LearningSynthesis)
                         if s.__name__ == completed.get('learning_schema')), None)
            if (kind is None or completed.get('task_id') != task_id
                    or completed.get('payload', {}).get('source_packet_id') != packet['id']):
                continue
            if (completed.get('status') == 'failed' and kind in {LearningApplications, LearningSynthesis}
                    and completed.get('actual_model_input', {}).get('actual_format_feedback')):
                bound = self._failed_learning_feedback(task_id, completed, kind, task['actor'],
                    complete_return=kind is LearningSynthesis)
                if bound['held']:
                    raise PolicyError('Learning saved correction repeated a known defect; dependent proposal remains unadmitted')
                # A rejected later focus owns its newly retained candidates too.
                # Defer their disposition to that exact corrected return; do not
                # reassign them to an earlier Ideas page during reconstruction.
                deferred_owned.update(i['id'] for i in bound['actual'].get('required_ideas', []) if i['id'] not in original_ids)
                continue
            if completed.get('status') != 'succeeded':
                continue
            owned = self._learning_call(task_id, completed, kind)
            deferred_owned.update(i.id for i in owned.ideas if i.id not in original_ids)
        def retain(record, value):
            receipts.append(record); parts.append(value.model_dump())
            for idea in value.ideas:
                if idea.id in ideas:
                    raise LearningContractError('Composed output repeats an Idea decision; a new coherent proposal is required')
                ideas[idea.id] = idea
        candidate_ideas = {}; candidate_applications = {}
        for record, value, _ in prior:
            receipts.append(record); parts.append(value.model_dump())
            legacy_proposals.append({'source_receipt': self._call_ref(record), 'proposal': value.model_dump()})
            for idea in value.ideas: candidate_ideas.setdefault(idea.id, []).append(idea)
            for application in value.applications: candidate_applications.setdefault(application['skill_id'], []).append(application)
        ideas.update({key: values[0] for key, values in candidate_ideas.items() if len(values) == 1})
        applications.extend(values[0] for values in candidate_applications.values() if len(values) == 1)
        if legacy_proposals:
            base['retained_proposal_context'] = await self.compact_context(task_id, phase + ':page:ideas',
                legacy_proposals, base, LearningIdeas, context_field='retained_proposal_context')
            base['retained_proposals_instruction'] = 'All page proposals remain evidence. Reconsider overlapping Idea/application decisions instead of choosing an arbitrary winner.'
        def build_ideas(part):
            return {**base, 'required_ideas': part,
                'learning_focus': {'kind': 'ideas', 'identities': [{k: i[k] for k in ('id', 'proposal', 'target')} for i in part]}}
        # Old successful dispositions satisfy the same exact obligations. The
        # full legacy lifecycle proposal remains input to synthesis, never lost.
        pending = [i for i in source.get('required_ideas', []) if i['id'] not in ideas]
        seen_pending = set()
        while pending:
            state = digest(pending)
            if state in seen_pending:
                raise LearningContractError('Learning candidate obligations repeated without progress')
            seen_pending.add(state)
            root_failure = failure if isinstance(failure, WireCardinalityError) and not ideas else None
            async for _, _, value in self._page_calls(task_id, phase + ':page:ideas', pending, build_ideas, LearningIdeas, exact_phase=True, failure=root_failure):
                retain(self._learning_receipts[digest(value.model_dump())], value)
            all_required = self.e._carry_learning_ideas(required, self.e._learning_response_history(task_id, source['operation']['id'])['ideas'])
            pending = [i for i in all_required if i['id'] not in ideas and i['id'] not in deferred_owned]
        covered = {a['skill_id'] for a in applications}
        def build_applications(part):
            return {**base, 'pre': {'skills': {**base['pre']['skills'], 'selected': part}},
                'learning_focus': {'kind': 'applications', 'selected': part}}
        pending_selected = [s for s in selection.get('selected', []) if s['id'] not in covered]
        if pending_selected:
            async for _, _, value in self._page_calls(task_id, phase + ':page:applications', pending_selected,
                                                     build_applications, LearningApplications, exact_phase=True):
                retain(self._learning_receipts[digest(value.model_dump())], value)
                applications.extend(value.applications)
        pending = [i for i in self.e._carry_learning_ideas(required,
            self.e._learning_response_history(task_id, source['operation']['id'])['ideas'])
            if i['id'] not in ideas and i['id'] not in deferred_owned]
        if pending:
            async for _, _, value in self._page_calls(task_id, phase + ':page:retained-ideas', pending,
                                                     build_ideas, LearningIdeas, exact_phase=True):
                retain(self._learning_receipts[digest(value.model_dump())], value)
        # Every full proposal/receipt is supplied to the lifecycle owner. Even
        # superficially compatible creates can conflict semantically: none are
        # concatenated, deduplicated or applied without this new judgment/review.
        composition = {'source_packet_id': packet['id'], 'task_id': task_id, 'operation_id': source['operation']['id'],
            'idea_decisions': [i.model_dump() for i in ideas.values()], 'applications': applications,
            'part_receipts': [self._call_ref(r) for r in receipts], 'legacy_proposals': legacy_proposals}
        composition['id'] = digest(composition)
        self.e.store.record('bounded_learning_composition', composition['id'], composition)
        composition_ref = {'id': composition['id'], 'sha256': digest(composition)}
        synthesis = {**base, 'pre': source.get('pre', {}), 'required_ideas': [], 'composition_ref': composition_ref,
            'learning_focus': {'kind': 'synthesis', 'source_packet_id': packet['id']},
            'composition_applications': applications,
            'composition': {'idea_decisions': [i.model_dump() for i in ideas.values()],
                'part_receipts': [self._call_ref(r) for r in receipts], 'legacy_proposals': legacy_proposals,
                'selection': selection,
                'instruction': 'Produce ONE coherent lifecycle/outcome proposal for the actual result. Preserve observations from every retained proposal; decide which updates to keep, improve, merge or reject with rationale. Resolve competing existing IDs, duplicate creates and application/update interactions. Ideas already disposed are retained outside this narrow output; return only genuinely new candidates. Normal full choice assessment and independent review still follow.'}}
        conflicts = [u for item in legacy_proposals for u in item['proposal']['skill_updates']]
        if conflicts:
            synthesis['actual_composition_feedback'] = {'kind': 'retained-proposal-interactions', 'proposals': legacy_proposals,
                'instruction': 'These separate page proposals are uncommitted and cannot be applied in page order. Return a coherent current proposal with substantive reasons; no arbitrary winning page or silent dropped observation.'}
        # Input capacity is separate from output capacity. This one global
        # decision does not echo every Idea or application in its output.
        if not self._fits(task_id, phase + ':page:synthesis', synthesis, LearningSynthesis):
            final_base = {k: v for k, v in base.items() if k not in {'retained_proposal_context', 'retained_proposals_instruction'}}
            final_base.update(pre={'skills': {'selected': [{'id': x['id']} for x in selection.get('selected', [])]}},
                required_ideas=[], learning_focus=synthesis['learning_focus'], composition_ref=composition_ref)
            context = await self.compact_context(task_id, phase + ':page:synthesis', synthesis, final_base,
                LearningSynthesis, context_field='composition_context')
            synthesis = {**final_base, 'composition_context': context}
        global_value = await self._call(task_id, phase + ':page:synthesis', synthesis, LearningSynthesis)
        retain(self._learning_receipts[digest(global_value.model_dump())], global_value)
        value = Learning(**{k: v for k, v in global_value.model_dump().items() if k != 'ideas'},
                         ideas=list(ideas.values()), applications=applications)
        all_required = self.e._carry_learning_ideas(required, self.e._learning_response_history(task_id, source['operation']['id'])['ideas'])
        self.e._validate_ideas(all_required, value)
        self.e._validate_learning_proposal(task_id, value, payload)
        self.e.store.record('bounded_learning', packet['id'], {'id': packet['id'], 'task_id': task_id,
            'source_sha256': digest(source), 'composition_ref': composition_ref, 'parts': parts, 'raw_parts': [r.get('raw_result', r['result']) for r in receipts],
            'part_receipts': [self._call_ref(r) for r in receipts],
            'idea_namespaces': [item for r in receipts for item in r.get('idea_namespaces', [])],
            'result': value.model_dump(), 'proof_ceiling': 'Complete composed proposal only; normal full assessment/review and atomic Knowledge admission remain mandatory'})
        return value
