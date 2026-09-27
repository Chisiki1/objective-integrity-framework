"""Shared captured feedback return; an index locates evidence, never authorizes it."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from uuid import uuid4

from .disposition_contracts import VERSION, DispositionContractError, contract, recurrence, validate
from .models import ConfigurationRequired, Disposition, PolicyError
from .providers import ProviderError, request_configuration
from .store import digest, redact


def hold(reason):
    raise PolicyError('DISPOSITION_PROVENANCE: ' + reason)


def reference(record):
    return {'id': record['id'], 'key': record['key'], 'sha256': digest(record)}


class AssessmentRevisionTransition(Exception):
    """A saved held request needs one distinct, source-bound presentation."""
    def __init__(self, payload):
        self.payload = payload


class DispositionRecovery:
    def __init__(self, engine, schema=Disposition):
        self.e = engine
        self.schema = schema
        self.generic = schema is not Disposition
        self.trace_field = "feedback_trace" if self.generic else "disposition_trace"
        self.index_kind = "wire_feedback_request" if self.generic else "disposition_request"
        self.version = "wire-feedback-return-v1" if self.generic else VERSION
        self.active = {}
        self.legacy_index = {}

    @property
    def b(self):
        return self.e.bounded_judgments

    def _hold(self, reason):
        raise PolicyError(('WIRE_RECOVERY_PROVENANCE: ' if self.generic else 'DISPOSITION_PROVENANCE: ') + reason)

    def _shape(self, diagnostic, *, wire=True, model_input=None, wire_capture=None):
        return self.e._feedback_shape(self.schema, diagnostic, wire=wire,
            model_input=model_input, wire_capture=wire_capture)

    def _initial_shapes(self, payload):
        from .policy_admission import SCHEMAS
        if self.schema.__name__ == 'AssessmentBatch' and 'assessment_correction' in payload:
            return list(payload['assessment_correction']['rejected_shapes'])
        prior = payload.get('actual_format_feedback', {}).get('validation', {})
        return [digest(prior)] if self.schema.__name__ in SCHEMAS and prior.get('kind') == 'proposal_contract' else []

    def _revision_payload(self, record, observed, part=None):
        if self.schema.__name__ != 'AssessmentBatch':
            return None
        from .semantic_wire import (ASSESSMENT_EVIDENCE, ASSESSMENT_COMPACT, ASSESSMENT_TARGET,
                                    ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES,
                                    ASSESSMENT_REFERENCE_SLOTS,
                                    REFERENCED_ASSESSMENT_DEFAULTS, _assessment_evidence,
                                    requested_assessment_ranges, load)
        if not _assessment_evidence(record['payload']):
            return None
        capture = record.get('wire_trace', {}).get('initial_capture')
        if not isinstance(capture, dict):
            return None
        old = load(self.e.store, capture, record[self.trace_field]['root_model_input'], self.schema,
                   record['binding']['role'], record['phase'])
        continued_ranges = None
        reference_fault = False
        trace = record[self.trace_field]
        actual_input = trace.get('actual_model_input')
        actual_capture = record.get('wire_trace', {}).get('actual_capture')
        if actual_capture and isinstance(actual_input, dict):
            actual = load(self.e.store, actual_capture, actual_input, self.schema,
                          record['binding']['role'], record['phase'])
            if actual.get('wire_revision') == ASSESSMENT_TARGET_CORRECTION and trace.get('rejected_events'):
                last = self._event(trace['rejected_events'][-1], record, {'rejected_model_output'})
                requested = requested_assessment_ranges(actual_input,
                    last['detail'].get('metadata', {}).get('validation_diagnostic'))
                if requested:
                    expected = [*actual_input.get('additional_source_ranges', []), *requested]
                    if trace.get('next_model_input', {}).get('additional_source_ranges') != expected:
                        self._hold('saved source-page continuation differs from the observed request')
                    continued_ranges = expected
                    old, capture = actual, actual_capture
            if actual.get('wire_revision') in {ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES} and trace.get('send_state') == 'held' and trace.get('rejected_events'):
                last = self._event(trace['rejected_events'][-1], record, {'rejected_model_output'})
                diagnostic = last['detail'].get('metadata', {}).get('validation_diagnostic', {})
                try:
                    body = json.loads(diagnostic['sanitized_response']) if diagnostic.get('representation') == 'json' and diagnostic.get('diagnostic_truncated') is False else {}
                except (KeyError, ValueError, TypeError):
                    body = {}
                if (isinstance(body, dict) and body.get('kind') == 'semantic_wire' and
                        any(item.get('type') == 'foreign_range' and item.get('loc') == ['assessments/0.source_citations']
                            for item in body.get('errors', []) if isinstance(item, dict))):
                    old, capture = actual, actual_capture
                    reference_fault = True
        if old.get('wire_revision') not in {REFERENCED_ASSESSMENT_DEFAULTS, ASSESSMENT_EVIDENCE,
                                            ASSESSMENT_COMPACT, ASSESSMENT_TARGET,
                                            ASSESSMENT_TARGET_CORRECTION, ASSESSMENT_TARGET_RANGES}:
            return None
        # A captured marker is verified using the transition that was valid
        # for its immutable parent. Only a held compact-v2 parent advances to
        # the new target-specific revision; older parents still own compact-v2.
        next_revision = (ASSESSMENT_REFERENCE_SLOTS if reference_fault or old['wire_revision'] == ASSESSMENT_TARGET_RANGES else
                         ASSESSMENT_TARGET_RANGES if old['wire_revision'] == ASSESSMENT_TARGET_CORRECTION else
                         ASSESSMENT_TARGET_CORRECTION if old['wire_revision'] == ASSESSMENT_TARGET else
                         ASSESSMENT_TARGET if old['wire_revision'] == ASSESSMENT_COMPACT else ASSESSMENT_COMPACT)
        if old['wire_revision'] == ASSESSMENT_TARGET_CORRECTION and continued_ranges is None and not reference_fault:
            return None
        source = record['payload']
        part = source['targets'] if part is None else part
        allowed = {item['id']: item for item in source['targets']}
        if (not part or len(part) != len({item['id'] for item in part})
                or any(allowed.get(item['id']) != item for item in part)):
            self._hold('assessment revision contains a foreign target')
        marker = {'version': 'assessment-revision-transition-v1',
                  'parent_call': reference(record), 'parent_capture': deepcopy(capture),
                  'parent_wire_revision': old['wire_revision'],
                  'parent_wire_schema_sha256': old['wire_schema_sha256'],
                  'new_wire_revision': next_revision,
                  'rejected_shapes': list(observed['shapes'])}
        return dict(deepcopy(source),
                    **({'additional_source_ranges': deepcopy(continued_ranges)} if continued_ranges is not None else {}),
                    targets=deepcopy(part),
                    target={'ids': [item['id'] for item in part]},
                    assessment_revision_transition=marker)

    def _validate_revision_payload(self, task_id, phase, payload, role):
        marker = payload.get('assessment_revision_transition')
        if marker is None:
            return
        if self.schema.__name__ != 'AssessmentBatch' or not isinstance(marker, dict):
            self._hold('foreign assessment revision transition')
        parent = self._read(marker.get('parent_call'))
        if parent.get('task_id') != task_id or parent.get('phase') != phase:
            self._hold('assessment revision parent belongs to another request')
        observed = self._authenticate(parent, role)
        expected = self._revision_payload(parent, observed, payload.get('targets')) if observed.get('held') else None
        if expected != payload:
            self._hold('assessment revision transition or original evidence changed')

    def has_request(self, task_id, phase, payload):
        if self.e.store.record_get(self.index_kind, self._request_key(task_id, phase, payload)):
            return True
        previous = self._find_legacy(task_id, phase, payload)
        # Old stage judgments use the same strict cache/wire verifier below,
        # also when discovery happens before the ordinary bounded cache lookup.
        # Other successful families retain their existing admission consumers.
        return previous is not None and (previous.get('status') != 'succeeded'
            or self.schema.__name__ in {'Review', 'AssessmentBatch', 'ContextObservation'})

    def inspect_request(self, task_id, phase, payload, *, role=None, check_control=True):
        """Authenticate a retained request without sending or manufacturing it."""
        if check_control:
            if task_id in self.e.stop_requested:raise asyncio.CancelledError()
            if self.e.quiescing and self.e.quiescing!=task_id:self._hold('controller is quiescing')
        task=self.e.store.get_task(task_id);role=role or task['actor']
        key=self._request_key(task_id,phase,payload)
        index=self.e.store.record_get(self.index_kind,key)
        if index and (index.get('id')!=key or index.get('binding')!=self._binding(task_id,role)):
            self._hold('indexed request binding changed')
        record=self._read(index['call']) if index else self._find_legacy(task_id,phase,payload)
        if record is None:return None
        if any(record.get(k)!=v for k,v in {'task_id':task_id,'phase':phase,'payload':payload}.items()):
            self._hold('index points to a foreign request')
        observed=self._authenticate(record,role)
        if observed.get('partial_assessments'):
            from .semantic_wire import WirePartialAssessmentError
            raise WirePartialAssessmentError(observed['partial_assessments'], reference(record))
        if observed.get('held'):
            revision = self._revision_payload(record, observed)
            if revision is not None:
                raise AssessmentRevisionTransition(revision)
            if observed.get('cardinality'):
                from .semantic_wire import WireCardinalityError
                raise WireCardinalityError(observed['cardinality'], reference(record))
            self._hold('retained repeated defect requires a changed semantic decision')
        return record,observed

    def revision_scope(self, record):
        """The actual typed semantic return, never a phrase in model prose."""
        observed=self._authenticate(record,'parent')
        if 'value' not in observed:self._hold('revision scope has no successful disposition')
        event=self._event(record['disposition_trace']['event'],record,{'succeeded'})
        from .semantic_wire import validate_wire_receipt
        ref=validate_wire_receipt(self.e.store,record,event,Disposition)
        output=self.e.store.record_get('semantic_wire_output',ref['id']) if ref else None
        scope=(output or {}).get('raw',{}).get('revision_scope')
        if scope not in {'none','learning','governed'}:self._hold('current Learning disposition has no typed revision scope')
        return scope

    def completed(self, task_id, phase, payload, *, role=None):
        """Consume an existing response; an absent/pending return is never a send."""
        observed=self.inspect_request(task_id,phase,payload,role=role)
        if observed is None or 'value' not in observed[1]:
            self._hold('saved outcome has no complete authenticated response; no new request sent')
        return observed[1]['value']

    def _sink(self, record, sink):
        if sink is not None:
            from .policy_admission import validate_admission_call, _binding
            event = validate_admission_call(self.e.store,
                _binding(self.e.store, record['task_id'], self.e.policy.hash), record['phase'], self.schema, record)
            sink.update(call=deepcopy(record), event=event)

    def _binding(self, task_id, role):
        task = self.e.store.get_task(task_id)
        self.e._assert_parent_source(task)
        values = self.b._configuration(task_id, role)
        measured = request_configuration(values, role) if values is not None else {}
        return {'task_id': task_id, 'source_hash': task['source_hash'], 'policy_hash': self.e.policy.hash,
                'role': role, 'owner': task['actor'], 'parent_id': task['parent_id'],
                'lease': deepcopy(task['state'].get('delegation_lease')),
                'settings': {k: measured.get(k) for k in ('configuration', 'context_tokens', 'reserved_output_tokens')}}

    def _request_key(self, task_id, phase, payload):
        # Deliberately excludes settings/role/source: a changed binding must be
        # examined, not quietly become a miss which resends an old judgment.
        return digest({'task_id': task_id, 'phase': phase, 'payload': payload,
                       **({'schema': self.schema.__name__} if self.generic else {})})

    def _measure(self, record, model_input, role):
        wire = None
        if record.get('measurement', {}).get('semantic_wire'):
            wire = record.get(self.trace_field, {}).get('wire_captures', {}).get(digest(model_input))
            if wire is None and record['measurement']['model_input_sha256'] == digest(model_input):
                wire = record['measurement']['semantic_wire']
            if wire is None:self._hold('actual semantic wire capture is absent')
        return self.b._measure_input(record['phase'], model_input, self.schema, role,
                                     self.b._configuration(record['task_id'], role), semantic_wire=wire)

    def _read(self, ref):
        record = self.e.store.record_get('bounded_model_call', ref.get('id')) if isinstance(ref, dict) else None
        if not record or reference(record) != ref:
            self._hold('call reference is missing or changed')
        return record

    def _event(self, ref, record, statuses):
        with self.e.store.lock:
            row = self.e.store.db.execute('SELECT * FROM events WHERE seq=?', (ref.get('seq'),)).fetchone()
        event = dict(row) if row else {}
        if event: event['detail'] = json.loads(event['detail'])
        if (not event or event.get('hash') != ref.get('hash') or event.get('task_id') != record['task_id']
                or event.get('stage') != record['phase'] or event.get('status') not in statuses
                or digest({k: v for k, v in event.items() if k not in {'seq', 'hash'}}) != event['hash']):
            self._hold('event reference, scope or bytes differ')
        return event

    def resume_context(self, task_id):
        """The ordinary resume event records code-known effects, not permission."""
        task=self.e.store.get_task(task_id)
        self.e._assert_parent_source(task)
        stops=[e for e in self.e.store.events(task_id) if e['stage']=='task' and e['status']=='stopped']
        effects=[]
        for row in self.e.store.operations(task_id):
            result=row.get('result')
            effects.append({'operation_id':row['operation']['id'],'operation_sha256':digest(row['operation']),
                'result_sha256':digest(result) if result is not None else None,
                'unresolved':self.e.store.operation_started_without_result(row)
                    or (result or {}).get('effect')=='unknown' or (result or {}).get('status')=='unknown'})
        return {'source_hash':task['source_hash'],'policy_hash':self.e.policy.hash,
            'owner':task['actor'],'parent_id':task['parent_id'],'lease':deepcopy(task['state'].get('delegation_lease')),
            'stopped_event':{'seq':stops[-1]['seq'],'hash':stops[-1]['hash']} if stops else None,
            'operation_frontier':effects}

    def has_interrupted_resume(self, record):
        """Discovery only; interrupted_resume authenticates every dependency."""
        return (record.get('status')=='unobserved' and record.get('first_fault',{}).get('type')=='CancelledError'
            and any(e['stage']=='task' and e['status']=='resume_requested'
                    for e in self.e.store.events(record['task_id'])))

    def _interrupted_response(self, record, schema, role, actual, captured):
        """Read a complete matching response before considering another send."""
        from .providers import ModelGateway, _strict_json
        from . import semantic_wire as wire
        gateway=self.e.gateway
        if not isinstance(gateway,ModelGateway):self._hold('interrupted gateway has no installed tool-free contract')
        messages=digest(self.b._messages(role,record['phase'],actual,schema,captured))
        candidates=[r for r in self.e.store.records('model_response')
            if r.get('task_id')==record['task_id'] and (r.get('semantic_wire_capture')==captured
                or (r.get('phase')==record['phase'] and r.get('messages_sha256')==messages))]
        if not candidates:return None
        if len(candidates)!=1:self._hold('interrupted response is ambiguous; no resend')
        response=candidates[0]
        expected={'task_id':record['task_id'],'role':role,'phase':record['phase'],
            'source_hash':record['source_hash'],'policy_hash':record['policy_hash'],
            'messages_sha256':messages,'semantic_wire_capture':captured,'http_status':200,'truncated':False}
        if any(response.get(k)!=v for k,v in expected.items()) or not response.get('request_sha256'):
            self._hold('observed interrupted response requires its existing error recovery; no blind resend')
        ref=gateway._response_reference(response)
        view=gateway._response_view(ref,task_id=record['task_id'])
        try:
            envelope=_strict_json(view['text']);choices=envelope['choices']
            if (view['decoding']!='strict' or len(choices)!=1 or choices[0].get('finish_reason')!='stop'
                    or choices[0]['message'].get('tool_calls') or choices[0]['message'].get('function_call')):
                raise ValueError()
            raw=_strict_json(choices[0]['message']['content'])
            capture=wire.load(self.e.store,captured,actual,schema,role,record['phase'])
            value=wire.decode(self.e.store,capture,schema,raw)
        except (KeyError,ValueError,TypeError,IndexError):
            self._hold('observed interrupted response is incomplete or invalid; no resend')
        outputs=[r for r in self.e.store.records('semantic_wire_output')
            if r.get('capture')==captured and r.get('response')==ref]
        if len(outputs)>1:self._hold('interrupted wire return is ambiguous')
        if outputs:
            output=outputs[0]
            if (output['id']!=digest({k:v for k,v in output.items() if k!='id'})
                    or output['raw']!=raw or output['canonical']!=value.model_dump()):
                self._hold('interrupted wire return differs from its complete response')
        elif response.get('confidential_exclusions_applied'):
            self._hold('unaccepted redacted response has no original validated wire return')
        validation_payload=record['payload']
        if schema.__name__.startswith('Learning'):
            validation_payload=self.e._prepare_learning_payload(record['task_id'],validation_payload,phase=record['phase'])
            self.e._validate_ideas(actual.get('required_ideas',[]),value)
        self.e._validate_generated(schema,value,validation_payload,task_id=record['task_id'])
        self.b._validate(schema,value,validation_payload,task_id=record['task_id'])
        config=record['measurement']['configuration']
        metadata={'provider':config['provider'],'requested_model':config['model'],'role':role,
            'http_status':200,'finish_reason':'stop','usage':envelope.get('usage'),
            'response_record':ref,'semantic_wire_capture':captured,'recovered_response':True}
        return {'value':value,'metadata':metadata,'raw':raw,'capture':capture}

    def _interrupted_effects(self, record, actual, started, resume, events, *, historical):
        """Use the actual operation/application consumer, not a status label."""
        from .models import Learning
        from .semantic_wire import _reference_source, _applied_review
        task=self.e.store.get_task(record['task_id'])
        frontier=resume['detail'].get('operation_frontier')
        if (not isinstance(frontier,list) or any(row.get('unresolved') is not False for row in frontier)
                or len({r.get('operation_id') for r in frontier})!=len(frontier)):
            self._hold('normal resume retains an unknown target effect; reconcile it first')
        for item in frontier:
            try:row=self.e.store.get_operation(item['operation_id'])
            except (KeyError,TypeError):self._hold('normal resume operation frontier is missing')
            if row['task_id']!=task['id'] or digest(row['operation'])!=item.get('operation_sha256'):
                self._hold('normal resume target identity changed')
            if not historical:
                result=row.get('result')
                if ((digest(result) if result is not None else None)!=item.get('result_sha256')
                        or self.e.store.operation_started_without_result(row)):
                    self._hold('target result changed after the normal resume frontier')
        # _execute writes its start before dispatch. Both Knowledge callers do
        # the same before applying. No missing model return may hide a later
        # target dispatch, even when that dispatch subsequently succeeded.
        for observed in events:
            if (started['seq']<observed['seq']<resume['seq']
                    and observed['stage'] in {'execution','knowledge_application','web_knowledge_application'}
                    and observed['status']=='started'):
                self._hold('a dependent effect started after the unaccepted model judgment')
        source=_reference_source(actual,self.e.store,applied=True)
        body=source
        while isinstance(body.get('bundle'),dict):body=body['bundle']
        operation=body.get('operation') or source.get('operation') or actual.get('operation')
        if not operation:
            operation=actual.get('evidence',{}).get('operation',{}).get('value')
        if not isinstance(operation,dict) or not operation.get('id'):
            return  # This model-only source/summary unit has no target executor.
        result=body.get('original_operation_result',body.get('result'))
        if _applied_review(source):
            consumer=body.get('consumer',{})
            if consumer.get('kind')=='web_acquisition':
                applied=self.e.web_judgments.applied_source(task,operation['id'])
                if (applied['consumer']!=consumer or applied['operation']!=operation
                        or applied['result']!=result or applied['outcome']!=body['actual_result']):
                    self._hold('pending applied Review differs from the committed Web/Knowledge result')
                return
            if consumer.get('kind')!='operation' or consumer.get('operation_id')!=operation['id']:
                self._hold('pending applied Review has no exact original consumer')
            context=body['accepted_input']
            state=self.e.knowledge.application_state(task,operation,Learning.model_validate(context['accepted_learning']),
                result,[s['id'] for s in context.get('pre',{}).get('skills',{}).get('selected',[])])
            application=self.e.store.record_get('knowledge_application',task['id']+':'+operation['id'])
            if (state['state']!='committed' or not application or state['input_hash']!=application['input_hash']
                    or state['application']['sha256']!=digest(application) or application['outcome']!=body['actual_result']):
                self._hold('pending applied Review differs from its atomic Knowledge commit')
        rows=[row for row in frontier if row['operation_id']==operation['id']]
        if rows:
            if (rows[0]['operation_sha256']!=digest(operation)
                    or rows[0]['result_sha256']!=(digest(result) if result is not None else None)):
                self._hold('pending judgment target is not at its captured pre/post effect boundary')
        elif operation.get('kind')=='web_fetch':
            # Acquisition operations are journaled by Web, not the executor.
            saved=self.e.store.record_get('web_work',operation['id']+(':after' if result is not None else ':before'))
            if (not saved or saved.get('task_id')!=task['id'] or saved.get('acquisition_operation')!=operation
                    or (result is not None and saved.get('acquisition_result')!=result)):
                self._hold('interrupted acquisition judgment lost its exact saved exchange')
            self.e.web_judgments._assert_binding(task['id'],self.e.web_judgments._saved_binding(saved))
            if result is None and not self.e.web_judgments.prepared_exchange(task['id'],saved['operation'],saved['detail']):
                self._hold('pre-acquisition judgment already has a dispatched exchange')
        elif result is not None:
            self._hold('post-operation judgment has no recorded original result')

    def interrupted_resume(self, record, schema, role, actual, started, terminal, *, resume_ref=None):
        """Authenticate one normal resume of a tool-free, unaccepted judgment.

        The old provider effect/billing stays unknown. Previously committed
        operations are preserved; they are not a future action of this call.
        """
        from .providers import ModelGateway
        from .semantic_wire import load
        if (record.get('status')!='unobserved' or record.get('first_fault',{}).get('type')!='CancelledError'
                or terminal['status']!='interrupted' or not isinstance(self.e.gateway,ModelGateway)):
            self._hold('unobserved dispatch lacks the installed interrupted-model boundary')
        task=self.e.store.get_task(record['task_id'])
        binding=self._binding(record['task_id'],role)
        measured=record.get('measurement',{})
        if (record.get('source_hash')!=binding['source_hash'] or record.get('policy_hash')!=binding['policy_hash']
                or {k:measured.get(k) for k in binding['settings']}!=binding['settings']):
            self._hold('interrupted request source/policy/settings changed')
        capture_ref=record.get('wire_trace',{}).get('actual_capture')
        if capture_ref is None:
            trace=record.get('feedback_trace',record.get('disposition_trace',{}))
            capture_ref=trace.get('wire_captures',{}).get(digest(actual))
        if capture_ref is None:self._hold('interrupted actual request capture is absent')
        load(self.e.store,capture_ref,actual,schema,role,record['phase'])
        events=self.e.store.events(record['task_id'])
        def event(ref,stage,status):
            found=[e for e in events if isinstance(ref,dict) and e['seq']==ref.get('seq')]
            if (len(found)!=1 or found[0]['hash']!=ref.get('hash') or found[0]['stage']!=stage
                    or found[0]['status']!=status
                    or digest({k:v for k,v in found[0].items() if k not in {'seq','hash'}})!=found[0]['hash']):
                self._hold('normal stop/resume event changed')
            return found[0]
        def ref(e):return {'seq':e['seq'],'hash':e['hash']}
        event(ref(started),record['phase'],'started');event(ref(terminal),record['phase'],'interrupted')
        if started['detail']!={'actor':role,'target':record['payload'].get('target',record['payload'].get('operation',{}).get('id')),'policy_hash':record['policy_hash']}:
            self._hold('interrupted actual caller changed')
        stops=[e for e in events if e['stage']=='task' and e['status']=='stop_requested'
               and started['seq']<e['seq']<terminal['seq']]
        if not stops:self._hold('interrupted call has no explicit normal stop')
        stop=event(ref(stops[-1]),'task','stop_requested')
        resumes=[e for e in events if e['stage']=='task' and e['status']=='resume_requested'
                 and e['seq']>terminal['seq'] and (resume_ref is None or ref(e)==resume_ref)]
        if not resumes:self._hold('interrupted dispatch requires explicit ordinary resume')
        resume=event(ref(resumes[0]),'task','resume_requested')
        stopped=event(resume['detail'].get('stopped_event'),'task','stopped')
        if not terminal['seq']<stopped['seq']<resume['seq']:
            self._hold('normal resume does not follow the stopped call')
        expected={k:binding[k] for k in ('source_hash','policy_hash','owner','parent_id','lease')}
        if any(resume['detail'].get(k)!=v for k,v in expected.items()):
            self._hold('normal resume source/owner/lease changed')
        self._interrupted_effects(record,actual,started,resume,events,historical=resume_ref is not None)
        trace=record.get('feedback_trace',record.get('disposition_trace',record.get('learning_trace',{})))
        rejection_refs=trace.get('rejected_events',[])
        window=[e for e in events if e['stage']==record['phase'] and started['seq']<e['seq']<terminal['seq']]
        if ([ref(e) for e in window]!=rejection_refs or any(e['status']!='rejected_model_output' for e in window)):
            self._hold('interrupted judgment has an accepted or overlapping outcome')
        # Only exact descendants can account for another unresolved call.
        calls=[r for r in self.e.store.records('bounded_model_call') if r.get('task_id')==record['task_id']
               and r.get('phase')==record['phase']]
        related={record['id']};ancestor=record
        while True:
            parent=ancestor.get('parent',ancestor.get('learning_trace',{}).get('feedback_recovery'))
            if parent is None:break
            original=next((r for r in calls if r['id']==parent.get('id')),None)
            if original is None or reference(original)!=parent or original['id'] in related:
                self._hold('interrupted ancestor is changed or cyclic')
            related.add(original['id']);ancestor=original
        changed=True
        while changed:
            changed=False
            for row in calls:
                parent=row.get('parent',row.get('learning_trace',{}).get('feedback_recovery'))
                if isinstance(parent,dict) and parent.get('id') in related:
                    original=next((r for r in calls if r['id']==parent['id']),None)
                    if original is None or reference(original)!=parent:self._hold('interrupted descendant changed its parent')
                    if row['id'] not in related:related.add(row['id']);changed=True
        for identity in related:
            children=[r for r in calls if r.get('parent',r.get('learning_trace',{}).get('feedback_recovery',{})).get('id')==identity]
            if len(children)>1:self._hold('interrupted continuation has ambiguous descendants')
        for row in calls:
            if row['id'] not in related and row.get('status') in {'started','unobserved'}:
                if (row.get('payload')==record['payload'] or (schema.__name__.startswith('Learning')
                        and self.b._learning_source_core(row.get('payload',{}))==self.b._learning_source_core(record['payload']))):
                    self._hold('interrupted request has an incomparable unknown peer')
        transition={'version':'explicit-model-resume-v1','source_call':reference(record),
            'interrupted_event':ref(terminal),'stop_event':ref(stop),'resume_event':ref(resume),
            'original_provider_effect':'unknown','original_billing':'unknown'}
        restored=self._interrupted_response(record,schema,role,actual,capture_ref)
        next_input=deepcopy(actual)
        if restored is None:next_input['interrupted_model_resume']=deepcopy(transition)
        return {'interrupted_resume':transition,'next':next_input,'restored_response':restored}

    def _response(self, record, role, metadata, model_input=None, *, messages=None, status=200):
        measured = record['measurement']
        if not measured.get('configuration'):
            # Only explicit offline fixtures lack provider response evidence.
            if self.b._configuration(record['task_id'], role) is not None:
                self._hold('configured response evidence is absent')
            return
        ref = metadata.get('response_record', {})
        response = self.e.store.record_get('model_response', ref.get('id')) if ref.get('id') else None
        wire = None
        if model_input is not None:
            actual = self._measure(record, model_input, role)
            messages = actual['messages_sha256'];wire = actual.get('semantic_wire')
        expected = {'task_id': record['task_id'], 'source_hash': record['source_hash'],
                    'policy_hash': record['policy_hash'], 'role': role, 'phase': record['phase'],
                    'messages_sha256': messages, 'http_status': status}
        config = measured['configuration']
        if (not messages or not response or digest(response) != ref.get('record_sha256')
                or any(response.get(k) != v or ref.get(k) != v for k, v in expected.items())
                or not response.get('request_sha256') or response['request_sha256'] != ref.get('request_sha256')
                or metadata.get('provider') != config.get('provider')
                or metadata.get('requested_model') != config.get('model') or metadata.get('role') != role
                or (wire is not None and (metadata.get('semantic_wire_capture') != wire
                    or response.get('semantic_wire_capture') != wire))):
            self._hold('response/request/configuration evidence differs')

    def checkpoint(self, trace):
        record = self.active[trace['call_id']]
        record[self.trace_field] = deepcopy(trace)
        self.e.store.record('bounded_model_call', record['id'], record)
        self.e.store.record(self.index_kind, record['request_key'], {
            'id': record['request_key'], 'binding': record['binding'], 'call': reference(record)})

    def event_binding(self, trace, model_input):
        if trace is None: return {}
        prefix = 'feedback' if self.generic else 'disposition'
        return {prefix+'_call_id': trace['call_id'],
                prefix+'_request_sha256': digest(trace['request_model_input']),
                prefix+'_actual_input_sha256': digest(model_input)}

    def before_send(self, trace, model_input):
        record = self.active[trace['call_id']]
        if self._binding(record['task_id'], record['binding']['role']) != record['binding']:
            self._hold('source/policy/owner/lease/settings changed before correction')
        wire = self.e._wire_capture(record['task_id'],record['phase'],model_input,self.schema,record['binding']['role'])
        if wire is not None:trace.setdefault('wire_captures', {})[digest(model_input)] = wire
        record[self.trace_field] = deepcopy(trace)
        measured = self._measure(record, model_input, record['binding']['role'])
        if not measured['fits']:
            raise ConfigurationRequired(self.schema.__name__+' correction exceeds configured context; retained feedback remains pending')
        trace.update(actual_model_input=redact(model_input), send_state='dispatching')
        self.checkpoint(trace)

    def rejected(self, trace, event, model_input, shape, repeated):
        trace['rejected_events'].append({'seq': event['seq'], 'hash': event['hash']})
        trace['shapes'] = sorted(set(trace['shapes']) | {shape})
        trace['next_model_input'] = self._feedback_input(model_input, event['detail'])
        trace['send_state'] = 'held' if repeated else 'pending_feedback'
        if self.schema.__name__ == 'AssessmentBatch':
            from . import semantic_wire as wire
            metadata = event['detail'].get('metadata', {})
            capture_ref = metadata.get('semantic_wire_capture')
            if isinstance(capture_ref, dict):
                capture = wire.load(self.e.store, capture_ref, model_input, self.schema,
                                    self.active[trace['call_id']]['binding']['role'],
                                    self.active[trace['call_id']]['phase'])
                if (capture.get('wire_revision') in {wire.ASSESSMENT_TARGET_RANGES, wire.ASSESSMENT_REFERENCE_SLOTS}
                        and wire.supplied_assessment_request(metadata.get('validation_diagnostic'))):
                    trace['send_state'] = 'held'
                    self.checkpoint(trace)
                    raise PolicyError('Requested source range was already supplied; no further model send')
        partial = self._partial_assessments(self.active[trace['call_id']], event, model_input)
        if partial is not None:
            trace['partial_assessments'] = partial
            trace['send_state'] = 'pending_assessments'
        self.checkpoint(trace)
        return partial

    def _feedback_input(self, model_input, detail):
        updated = dict(model_input, actual_format_feedback=self.e._feedback_from_rejection(detail))
        if self.schema.__name__ == 'AssessmentBatch':
            from .semantic_wire import requested_assessment_ranges
            ranges = requested_assessment_ranges(model_input, detail.get('metadata', {}).get('validation_diagnostic'))
            if ranges:
                updated['additional_source_ranges'] = [*model_input.get('additional_source_ranges', []), *ranges]
                updated['actual_format_feedback']['instruction'] = (
                    'The requested exact original source byte pages are now supplied in this same assessment stage. '
                    'Judge the current targets again using the new pages and prior exact evidence. '
                    'If another omitted range is needed, request it; do not claim omitted text reviewed.')
        return updated

    def _partial_assessments(self, record, event, model_input, *, current_defaults=False,
                             allow_unresolved_only=False, allow_note_only=False):
        """Reuse the protected response reader; no diagnostic preview is parsed."""
        if self.schema.__name__ != 'AssessmentBatch': return None
        from pydantic import ValidationError
        from .providers import _strict_json
        from . import semantic_wire as wire
        metadata = event['detail'].get('metadata', {})
        captured = metadata.get('semantic_wire_capture'); response_ref = metadata.get('response_record')
        reader = getattr(self.e.gateway, '_response_view', None)
        if (not captured or not response_ref or reader is None or metadata.get('truncated') is not False
                or metadata.get('schema_validation_failed') is not True
                or metadata.get('credential_content_detected') or metadata.get('request_effect') == 'response-unobserved'):
            return None
        role = record['binding']['role']
        self._response(record, role, metadata, model_input)
        capture = wire.load(self.e.store, captured, model_input, self.schema, role, record['phase'])
        view = reader(response_ref, task_id=record['task_id'])
        if response_ref.get('truncated') is not False or view['decoding'] != 'strict': return None
        try:
            envelope = _strict_json(view['text']); choices = envelope['choices']
            if (len(choices) != 1 or choices[0].get('finish_reason') != 'stop'
                    or choices[0]['message'].get('tool_calls') or choices[0]['message'].get('function_call')
                    or choices[0]['message'].get('refusal')): return None
            raw = _strict_json(choices[0]['message']['content'])
        except (ValueError, KeyError, TypeError, IndexError): return None
        if current_defaults and capture.get('wire_revision') not in {None, wire.LEARNING_BINDINGS}:
            return None
        try: wire.decode(self.e.store, capture, self.schema, raw)
        except ValidationError as error:
            expected = error.errors(include_url=False, include_input=False, include_context=False)
        except wire.WireShapeError as error:
            expected = error.diagnostic
        else: self._hold('partial assessment response has no actual wire defect')
        observed = metadata.get('validation_diagnostic', {})
        if observed.get('representation') != 'json' or observed.get('diagnostic_truncated') is not False: return None
        try: actual = _strict_json(observed['sanitized_response'])
        except (ValueError, KeyError, TypeError): self._hold('partial assessment diagnostic is malformed')
        if digest(actual) != digest(expected): self._hold('partial assessment diagnostic differs from its original response')
        if allow_note_only:
            if (not isinstance(expected, list) or len(expected) != 1
                    or expected[0].get('type') != 'extra_forbidden'
                    or list(expected[0].get('loc', [])) != ['assessments_note']):
                return None
        partial = wire.partial_assessments(capture, raw, current_defaults=current_defaults,
                                           allow_unresolved_only=allow_unresolved_only,
                                           allow_note_only=allow_note_only)
        if partial is None: return None
        if current_defaults:
            # Reconsider only the demonstrated optional-presence mismatch.
            # Authenticate using the original codec above; do not restamp it or
            # infer any missing semantic declaration in the returned rows.
            if (not isinstance(expected, list) or not expected
                    or any(item.get('type') != 'missing' or len(item.get('loc', [])) != 3
                        or item['loc'][0] != 'assessments' or type(item['loc'][1]) is not int
                        or item['loc'][2] != 'evidence_refs' for item in expected)):
                return None
            revision = (wire.REFERENCED_ASSESSMENT_DEFAULTS if capture.get('wire_revision') == wire.LEARNING_BINDINGS
                        else wire.ASSESSMENT_DEFAULTS)
            partial = dict(partial, default_transition={
                'from_wire_schema_sha256': capture['wire_schema_sha256'], 'to_wire_revision': revision,
                'to_wire_schema_sha256': digest(wire.wire_schema('AssessmentBatch',model_input,revision=revision).model_json_schema()),
                'meaning': 'Original complete response and diagnostic preserved; only the canonical optional list default is applied to current row eligibility. No semantic decision or old success is invented.'})
        return {**partial, 'capture': deepcopy(captured), 'response': deepcopy(response_ref),
                'rejected_event': {'seq': event['seq'], 'hash': event['hash']}}

    def _diagnostic(self, record, event):
        detail = event['detail']
        if 'rejected_response' in detail:
            value = self.schema.model_validate(detail['rejected_response'])
            if self.generic:
                try:
                    self.e._validate_generated(self.schema, value, record['payload'], task_id=record['task_id'])
                    self.b._validate(self.schema, value, record['payload'], task_id=record['task_id'])
                except PolicyError as error:
                    if str(error).startswith(('WIRE_', 'LEARNING_PROVENANCE:')):raise
                    return getattr(error, 'diagnostic', None) or {'kind': 'proposal_contract', 'message': str(error)}
                self._hold('retained rejected result has no current proposal defect')
            try: validate(record['payload'], value)
            except DispositionContractError as error: return error.diagnostic
            self._hold('retained rejected result has no current structural defect')
        diagnostic = detail.get('metadata', {}).get('validation_diagnostic')
        if not isinstance(diagnostic, dict): self._hold('retained schema diagnostic is unavailable')
        return diagnostic

    def _legacy(self, record, role, *, frozen_root=None):
        """Recognize the old writer, with no invented old full-wire input."""
        task = self.e.store.get_task(record['task_id'])
        if task['parent_id'] or role != task['actor']:
            self._hold('legacy owner/child lease is not recoverable')
        with self.e.store.lock:
            rows = self.e.store.db.execute('SELECT * FROM events WHERE task_id=? AND stage=? ORDER BY seq',
                                           (record['task_id'], record['phase'])).fetchall()
        events = []
        for row in rows:
            event = dict(row); event['detail'] = json.loads(event['detail'])
            events.append(event)
        starts = []
        for i, event in enumerate(events):
            if event['status'] != 'started' or event['detail'].get('actor') != role: continue
            if event['detail'].get('target') != record['payload'].get('target', record['payload'].get('operation', {}).get('id')): continue
            end = next((j for j in range(i + 1, len(events)) if events[j]['status'] in {'started', 'succeeded', 'failed', 'interrupted'}), None)
            if end is None or events[end]['status'] not in {'succeeded', 'failed'}: continue
            part = events[i:end + 1]
            first = next((e for e in part if e['status'] in {'rejected_model_output', 'succeeded'}), None)
            if not first: continue
            detail = first['detail']
            meta = detail.get('usage', detail.get('metadata', detail.get('measurement', {}).get('usage', {})))
            if meta.get('response_record', {}).get('messages_sha256') == record['measurement'].get('messages_sha256'):
                starts.append(part)
        if len(starts) != 1: self._hold('legacy request event window is absent or ambiguous')
        events = starts[0]
        for event in events:
            self._event({'seq': event['seq'], 'hash': event['hash']}, record, {event['status']})
        terminal = events[-1]
        root = deepcopy(frozen_root) if frozen_root is not None else self.e._model_input(record['task_id'], record['phase'], record['payload'], self.schema, role=role)
        if record['status'] == 'succeeded':
            if terminal['status'] != 'succeeded' or terminal['detail'].get('result') != record.get('result'):
                self._hold('legacy success event/result differs')
            # Unlike legacy failure observation, success reuse needs the complete
            # reconstructed old request and its unchanged schema/message bytes.
            candidates = [root, {k:v for k,v in root.items() if k!='exact_response_contract'}]
            candidates += [{k:v for k,v in candidate.items() if k!='installed_controller_context'} for candidate in candidates[:]]
            matches = {digest(v):v for v in candidates if self._measure(record,v,role)==record['measurement']}
            if len(matches)!=1:
                self._hold('legacy successful full request cannot be reconstructed')
            root = next(iter(matches.values()))
            self._response(record, role, terminal['detail']['measurement']['usage'], root)
            value = Disposition.model_validate(record['result']); validate(record['payload'], value)
            return {'value': value, 'root': root, 'shapes': []}
        if (record['status'] != 'failed' or terminal['status'] != 'failed'
                or terminal['detail'].get('message') != record.get('first_fault', {}).get('message')
                or terminal['detail'].get('error_type') != record.get('first_fault', {}).get('type')):
            self._hold('legacy failure outcome differs')
        rejected = [e for e in events if e['status'] == 'rejected_model_output']
        if not rejected: self._hold('legacy failure has no observed rejected response')
        for event in rejected:
            detail = event['detail']; meta = detail.get('usage', detail.get('metadata', {}))
            self._response(record, role, meta, messages=meta.get('response_record', {}).get('messages_sha256'))
        last = rejected[-1]; diagnostic = self._diagnostic(record, last)
        transition = {'from': 'legacy-generic-disposition-feedback', 'to': VERSION,
                      'source_receipt': reference(record),
                      'events': [{'seq': e['seq'], 'hash': e['hash']} for e in events],
                      'proof_limit': 'Exact retained failure, payload, configuration and response observations. Old full wire input/trace was not retained and is not authenticated. This is a new corrective request.'}
        feedback = self.e._feedback_from_rejection(dict(last['detail'], validation=diagnostic,
                    rejected_response=last['detail'].get('rejected_response')))
        feedback.update(source_receipt=reference(record), contract_transition=transition)
        return {'root': root, 'next': dict(root, actual_format_feedback=feedback),
                'shapes': [digest(recurrence(diagnostic))], 'transition': transition}

    def _legacy_completed(self, record, role):
        """Adopt the old successful writer, without inventing a feedback trace."""
        task = self.e.store.get_task(record['task_id'])
        measured = record['measurement']; wire = measured.get('semantic_wire')
        root = None
        if wire:
            from .semantic_wire import load
            captured = self.e.store.record_get('semantic_wire_request', wire.get('id'))
            if not captured:self._hold('historical successful input capture is absent')
            root = load(self.e.store, wire, captured['canonical_input'], self.schema, role, record['phase'])['canonical_input']
        current = self.e._model_input(record['task_id'], record['phase'], record['payload'], self.schema, role=role,
            captured_input=root)
        if root is None:
            candidates=[current,{k:v for k,v in current.items() if k!='installed_controller_context'}]
            matches={digest(value):value for value in candidates if self._measure(record,value,role)==measured}
            if len(matches)!=1:self._hold('historical successful input format cannot be authenticated')
            root=next(iter(matches.values()))
            current=self.e._model_input(record['task_id'],record['phase'],record['payload'],self.schema,role=role,
                captured_input=root)
        if ({k:v for k,v in root.items() if k != 'history_lookup'} !=
                {k:v for k,v in current.items() if k != 'history_lookup'}
                or self._measure(record, root, role) != measured):
            self._hold('historical successful request differs from its captured source')
        saved = self.b._receipt('bounded_model_completed', record['key'], task, record['phase'],
            record['payload'], self.schema, role, measured)
        if saved != record or self.e.store.record_get('bounded_model_call', record['id']) != record:
            self._hold('historical successful call/completed receipt differs')
        event = self.b._wire_event(record, self.schema)
        events = self.e.store.events(record['task_id'])
        if event is None:
            matches = [e for e in events if e['stage'] == record['phase'] and e['status'] == 'succeeded'
                and e['detail'].get('actor') == role and e['detail'].get('result') == record['result']
                and e['detail'].get('measurement', {}).get('usage', {}).get('response_record', {}).get('messages_sha256') == measured.get('messages_sha256')]
            if len(matches) != 1:self._hold('historical successful event is absent or ambiguous')
            event = matches[0]
        event = self._event({'seq':event['seq'], 'hash':event['hash']}, record, {'succeeded'})
        prior = [e for e in events if e['stage'] == record['phase'] and e['seq'] < event['seq']]
        starts = [e for e in prior if e['status'] == 'started']
        if not starts:self._hold('historical successful start is absent')
        start = starts[-1]
        self._event({'seq':start['seq'], 'hash':start['hash']}, record, {'started'})
        if start['detail'] != {'actor':role, 'target':record['payload'].get('target', record['payload'].get('operation', {}).get('id')), 'policy_hash':record['policy_hash']}:
            self._hold('historical successful owner/target differs')
        model_input = deepcopy(root); shapes = []
        for rejected in [e for e in prior if e['seq'] > start['seq']]:
            if rejected['status'] != 'rejected_model_output':
                self._hold('historical successful window contains another outcome')
            self._event({'seq':rejected['seq'], 'hash':rejected['hash']}, record, {'rejected_model_output'})
            meta = rejected['detail'].get('usage', rejected['detail'].get('metadata', {}))
            transport = dict(record, **{self.trace_field:{'wire_captures':{digest(model_input):meta.get('semantic_wire_capture')}}})
            self._response(transport, role, meta, model_input)
            diagnostic = self._diagnostic(record, rejected)
            if diagnostic != rejected['detail'].get('validation', meta.get('validation_diagnostic')):
                self._hold('historical successful rejection diagnostic differs')
            shape = self._shape(diagnostic, wire=bool(meta.get('semantic_wire_capture')),
                                model_input=model_input, wire_capture=meta.get('semantic_wire_capture'))
            if shape in shapes:self._hold('historical success followed a repeated defect')
            shapes.append(shape)
            model_input = dict(model_input, actual_format_feedback=self.e._feedback_from_rejection(rejected['detail']))
        trace = record.get('wire_trace', {})
        if wire and (trace.get('request_model_input') != root or trace.get('actual_model_input') != model_input):
            self._hold('historical successful captured input/feedback differs')
        transport = dict(record, **{self.trace_field:{'wire_captures':{digest(model_input):trace.get('actual_capture', wire)}}})
        self._response(transport, role, event['detail']['measurement']['usage'], model_input)
        value = self.schema.model_validate(record['result'])
        self.e._validate_generated(self.schema, value, record['payload'], task_id=record['task_id'])
        self.b._validate(self.schema, value, record['payload'], task_id=record['task_id'])
        return {'value':value, 'root':root, 'shapes':shapes,
                'started_event':{'seq':start['seq'], 'hash':start['hash']}}

    def _authenticate(self, record, role, seen=(), *, frozen_legacy_root=None, resume_ref=None):
        if record['id'] in seen: self._hold('cyclic correction lineage')
        seen = (*seen, record['id'])
        binding = self._binding(record['task_id'], role)
        if self.schema.__name__ == 'AssessmentBatch' and 'assessment_correction' in record['payload']:
            self.b._validate_assessment_correction(record['task_id'], record['payload'], role=role)
        if 'assessment_correction' not in record['payload']:
            self._validate_revision_payload(record['task_id'], record['phase'], record['payload'], role)
        task = self.e.store.get_task(record['task_id'])
        measured = record.get('measurement', {})
        if (record.get('source_hash') != binding['source_hash'] or record.get('policy_hash') != binding['policy_hash']
                or {k: measured.get(k) for k in binding['settings']} != binding['settings']
                or self.b._receipt_key(task, record['phase'], record['payload'], self.schema, role, measured) != record.get('key')):
            self._hold('request source/policy/role/settings/key differs')
        trace = record.get(self.trace_field)
        if trace is None:
            if self.generic:
                if record.get('status') == 'succeeded':return self._legacy_completed(record, role)
                # The bounded cache still authenticates exact older successes.
                # No modern failure trace is invented from events or current
                # history. An old failed/unknown call is not a cache miss.
                self._hold('historical failed request has no authenticated continuation; retain it for an explicit new transition')
            return self._legacy(record, role, frozen_root=frozen_legacy_root)
        if (record.get('binding') != binding or trace.get('version') != self.version or trace.get('call_id') != record['id']
                or record.get('request_key') != self._request_key(record['task_id'], record['phase'], record['payload'])):
            self._hold('captured owner/lease/request identity differs')
        root = trace.get('root_model_input')
        if not isinstance(root,dict):self._hold('captured source envelope differs')
        current = self.e._model_input(record['task_id'], record['phase'], record['payload'], self.schema, role=role,
            captured_input=root)
        if {k: v for k, v in root.items() if k != 'history_lookup'} != {k: v for k, v in current.items() if k != 'history_lookup'}:
            self._hold('captured source envelope differs')
        request = root; shapes = self._initial_shapes(record['payload'])
        if record.get('parent'):
            parent = self._read(record['parent'])
            if any(parent.get(k) != record.get(k) for k in ('task_id', 'phase', 'payload')):
                self._hold('correction parent belongs to another request')
            # The first new transition captured this complete corrective root.
            # Reconstructing it from today's history would change a request that
            # was already sent. Authenticate the immutable old failure/window,
            # then derive its feedback over this exact captured new root.
            prior = self._authenticate(parent, role, seen, frozen_legacy_root=root,
                resume_ref=record.get('interrupted_resume',{}).get('resume_event'))
            if 'next' not in prior: self._hold('parent has no resumable actual feedback')
            if record.get('interrupted_resume')!=prior.get('interrupted_resume'):
                self._hold('interrupted continuation lost its exact normal resume')
            request = prior['next']; shapes = prior['shapes']
        if (trace.get('request_model_input') != request or trace.get('inherited_shapes') != shapes
                or self._measure(record, root if self.generic else request, role) != measured
                or (self.generic and trace.get('request_measurement') != self._measure(record, request, role))):
            self._hold('captured corrective request or recurrence lineage differs')
        started = self._event(trace.get('started_event', {}), record, {'started'})
        if started['detail'].get('actor') != role: self._hold('actual caller role differs')
        model_input = deepcopy(request); repeated = False; previous_seq = started['seq']; cardinality = None; partial = None
        last_rejection = None
        for ref in trace.get('rejected_events', []):
            event = self._event(ref, record, {'rejected_model_output'})
            if event['seq'] <= previous_seq: self._hold('rejection order differs')
            previous_seq = event['seq']; detail = event['detail']
            if any(detail.get(k) != v for k, v in self.event_binding(trace, model_input).items()):
                self._hold('rejected response belongs to another actual input')
            meta = detail.get('usage', detail.get('metadata', {}))
            self._response(record, role, meta, model_input)
            diagnostic = self._diagnostic(record, event)
            observed = detail.get('validation', detail.get('metadata', {}).get('validation_diagnostic'))
            if diagnostic != observed: self._hold('saved diagnostic is not the source-owned defect')
            last_rejection = (event, deepcopy(model_input))
            shape = self._shape(diagnostic, wire=bool(meta.get('semantic_wire_capture')),
                                model_input=model_input, wire_capture=meta.get('semantic_wire_capture'))
            repeated = shape in shapes
            if trace.get('partial_assessments') and ref == trace['rejected_events'][-1]:
                partial = self._partial_assessments(record, event, model_input)
                if partial != trace['partial_assessments'] or partial is None:
                    self._hold('retained assessment subset differs from the exact rejected response')
            # Keep the earliest authenticated outer count. Later feedback may
            # reveal a different defect (for example a foreign Idea selector),
            # but that does not erase the first response's smaller-page route.
            # The classifier reads the protected response and checks its exact
            # original diagnostic; it never accepts any of its rows.
            if self.schema.__name__ == 'AssessmentBatch' and cardinality is None:
                cardinality = self.b._outer_cardinality(record, self.schema, role, model_input, meta)
            if repeated and ref != trace['rejected_events'][-1]: self._hold('request continued after repeated defect')
            shapes = sorted(set(shapes) | {shape})
            model_input = self._feedback_input(model_input, detail)
        if trace.get('shapes') != shapes: self._hold('saved recurrence set differs')
        # A local consumer can fail after the exact response/event/result were
        # retained. Keep that first fault, but authenticate the received result
        # through the same guards instead of treating it as a failed send.
        received_success=(record['status'] in {'failed','unobserved'}
            and trace.get('send_state')=='succeeded' and not trace.get('failure_event'))
        if record['status'] == 'succeeded' or received_success:
            if partial is not None: self._hold('a retained subset cannot replace a successful whole return')
            event = self._event(trace.get('event', {}), record, {'succeeded'})
            if (repeated or event['seq'] <= previous_seq or event['detail'].get('result') != record.get('result')
                    or any(event['detail'].get(k) != v for k, v in self.event_binding(trace, model_input).items())
                    or trace.get('actual_model_input') != model_input):
                self._hold('successful return does not belong to the actual request')
            self._response(record, role, event['detail']['measurement']['usage'], model_input)
            from .semantic_wire import validate_wire_receipt
            validate_wire_receipt(self.e.store,record,event,self.schema)
            value = self.schema.model_validate(record['result'])
            if self.generic:
                self.e._validate_generated(self.schema,value,record['payload'],task_id=record['task_id'])
                self.b._validate(self.schema,value,record['payload'],task_id=record['task_id'])
            else:validate(record['payload'], value)
            return {'value': value, 'root': root, 'shapes': shapes, 'started_event':trace['started_event']}
        failure = self._event(trace.get('failure_event', {}), record, {'failed', 'interrupted'})
        if (failure['seq'] <= previous_seq
                or (failure['status'] == 'failed' and (failure['detail'].get('message') != record.get('first_fault', {}).get('message')
                    or failure['detail'].get('error_type') != record.get('first_fault', {}).get('type')
                    or failure['detail'].get('metadata', {}) != record.get('metadata', {})))):
            self._hold('failure precedes its input or differs from the retained first fault')
        if partial is not None:
            if (record['status'] != 'failed' or trace.get('send_state') != 'pending_assessments'
                    or trace.get('next_model_input') != model_input
                    or record.get('first_fault', {}).get('type') != 'WirePartialAssessmentError'):
                self._hold('partial assessment has a later, unknown or incompatible send')
            return {'partial_assessments': partial, 'root': root, 'shapes': shapes,
                    'started_event': trace['started_event']}
        if (self.schema.__name__ == 'AssessmentBatch' and record['status'] == 'failed'
                and repeated and trace.get('send_state') == 'held' and last_rejection is not None
                and trace.get('next_model_input') == model_input
                and trace.get('actual_model_input') == last_rejection[1]):
            # Existing source/settings/lease, event order, every response and
            # final failure have now been authenticated. A saved held call stays
            # held in history; only its valid rows may feed the existing subset
            # consumer. The unresolved rows receive a distinct normal request.
            transitioned = self._partial_assessments(record, *last_rejection,
                                                    allow_unresolved_only=True)
            if transitioned is None:
                transitioned = self._partial_assessments(record, *last_rejection,
                                                        current_defaults=True)
            if transitioned is None:
                transitioned = self._partial_assessments(record, *last_rejection,
                                                        allow_note_only=True)
            if transitioned is not None:
                return {'partial_assessments': transitioned, 'root': root, 'shapes': shapes,
                        'started_event': trace['started_event']}
        if repeated or trace.get('send_state') == 'held':
            return {'held': True, 'root': root, 'shapes': shapes, 'cardinality': cardinality}
        if self.b._output_truncated(ProviderError('', metadata=record.get('metadata', {}))):
            error = ProviderError(record['first_fault']['message'], metadata=deepcopy(record['metadata']))
            error.bounded_call_ref = reference(record)
            raise error
        if trace.get('send_state') == 'failed_response':
            metadata = record.get('metadata', {})
            status = metadata.get('response_record', {}).get('http_status')
            if type(status) is not int or status < 400: self._hold('failed send has unknown transport effect')
            self._response(record, role, metadata, model_input, status=status)
            if trace.get('actual_model_input') != model_input: self._hold('failed request input differs')
        elif trace.get('send_state') != 'pending_feedback':
            if trace.get('send_state')!='dispatching' or trace.get('actual_model_input')!=model_input:
                self._hold('unobserved or interrupted dispatch cannot be replayed')
            resumed=self.interrupted_resume(record,self.schema,role,model_input,started,failure,resume_ref=resume_ref)
            return {'root':root,'shapes':shapes,'started_event':trace['started_event'],**resumed}
        # An observed, authenticated HTTP failure can affect the first request
        # as well as a correction. A later caller resumes those captured bytes;
        # unknown dispatches and unsupported pre-send states remain held above.
        if (trace.get('send_state') != 'failed_response'
                and not trace.get('rejected_events') and not record.get('parent')):
            self._hold('transport failure has no retained corrective request')
        if trace.get('rejected_events') and trace.get('next_model_input') != model_input:
            self._hold('saved feedback input differs')
        return {'root': root, 'next': model_input, 'shapes': shapes, 'started_event':trace['started_event']}

    def _find_legacy(self, task_id, phase, payload):
        bucket = (task_id, phase)
        if bucket not in self.legacy_index:
            # One scoped retrieval pass per Engine, never a history scan for
            # every fresh page. Stored refs/contents are rechecked on each use.
            with self.e.store.lock:
                rows = self.e.store.db.execute("SELECT body FROM records WHERE kind='bounded_model_call' AND json_extract(body,'$.task_id')=? AND json_extract(body,'$.phase')=?",
                                               bucket).fetchall()
            index = {}
            for row in rows:
                record = json.loads(row['body'])
                if self.generic:
                    capture = self.e.store.record_get('semantic_wire_request', record.get('measurement', {}).get('semantic_wire', {}).get('id'))
                    if record.get('feedback_schema') not in {None, self.schema.__name__}:
                        continue
                    if capture and capture.get('canonical_schema') != self.schema.__name__:
                        continue
                    index.setdefault(digest(record.get('payload')), []).append(reference(record))
                elif 'disposition_trace' not in record:
                    index.setdefault(digest(record.get('payload')), []).append(reference(record))
            self.legacy_index[bucket] = index
        refs = self.legacy_index[bucket].get(digest(payload), [])
        if len(refs) > 1: self._hold('legacy attempts are ambiguous; no implicit retry')
        record = self._read(refs[0]) if refs else None
        if self.generic and record and self.trace_field in record:
            self._hold('captured request lost its exact return index; no automatic replay')
        return record

    async def call(self, task_id, phase, payload, *, role=None, expected_request_configuration=None,
                   admission_sink=None, admission_parent=None):
        if not self.generic:contract(payload)  # conflicting aliases/input IDs are never sent
        if task_id in self.e.stop_requested: raise asyncio.CancelledError()
        if self.e.quiescing and self.e.quiescing != task_id:
            raise PolicyError('Controller update is waiting for the '+self.schema.__name__+' safe boundary')
        task = self.e.store.get_task(task_id); role = role or task['actor']
        if self.schema.__name__ == 'AssessmentBatch' and 'assessment_correction' in payload:
            self.b._validate_assessment_correction(task_id, payload, role=role)
        if 'assessment_correction' not in payload:
            self._validate_revision_payload(task_id, phase, payload, role)
        binding = self._binding(task_id, role)
        if expected_request_configuration is not None and expected_request_configuration != binding['settings']:
            if self.generic:raise ConfigurationRequired('Requested settings differ from the measured dispatch; no request sent')
            self._hold('requested settings differ from measured dispatch')
        key = self._request_key(task_id, phase, payload)
        index = self.e.store.record_get(self.index_kind, key)
        if index and (index.get('id') != key or index.get('binding') != binding):
            self._hold('indexed request binding changed')
        previous = self._read(index['call']) if index else self._find_legacy(task_id, phase, payload)
        if previous and any(previous.get(k) != v for k, v in {'task_id': task_id, 'phase': phase, 'payload': payload}.items()):
            self._hold('index points to a foreign request')
        recovered = self._authenticate(previous, role) if previous else None
        if recovered and 'value' in recovered:
            self._sink(previous,admission_sink)
            return recovered['value']
        if recovered and recovered.get('partial_assessments'):
            from .semantic_wire import WirePartialAssessmentError
            raise WirePartialAssessmentError(recovered['partial_assessments'], reference(previous))
        if recovered and recovered.get('held'):
            revision = self._revision_payload(previous, recovered)
            if revision is not None:
                raise AssessmentRevisionTransition(revision)
            if recovered.get('cardinality'):
                from .semantic_wire import WireCardinalityError
                raise WireCardinalityError(recovered['cardinality'], reference(previous))
            raise PolicyError('Model repeated the same '+self.schema.__name__+' defect after actual feedback; retained judgment remains unadmitted')
        root = recovered['root'] if recovered else self.e._model_input(task_id, phase, payload, self.schema, role=role)
        request = recovered['next'] if recovered else root
        wire = self.e._wire_capture(task_id,phase,request,self.schema,role)
        request_measurement = self.b._measure_input(phase, request, self.schema, role, self.b._configuration(task_id, role), semantic_wire=wire)
        initial_wire = self.e._wire_capture(task_id,phase,root,self.schema,role) if self.generic else wire
        measured = self.b._measure_input(phase,root,self.schema,role,self.b._configuration(task_id,role),semantic_wire=initial_wire) if self.generic else request_measurement
        if not request_measurement['fits']: raise ConfigurationRequired('Corrective input exceeds configured context; no request sent')
        record = {'id': uuid4().hex, 'key': self.b._receipt_key(task, phase, payload, self.schema, role, measured),
                  'request_key': key, 'task_id': task_id, 'phase': phase, 'source_hash': task['source_hash'],
                  'policy_hash': self.e.policy.hash, 'binding': binding, 'payload': deepcopy(payload),
                  'measurement': measured, 'status': 'started'}
        if previous: record['parent'] = reference(previous)
        if recovered and recovered.get('interrupted_resume'):
            if any(row['unresolved'] for row in self.resume_context(task_id)['operation_frontier']):
                self._hold('dependent target effect became unknown before the resumed judgment')
            record['interrupted_resume']=deepcopy(recovered['interrupted_resume'])
        shapes = recovered['shapes'] if recovered else self._initial_shapes(payload)
        trace = {'version': self.version, 'call_id': record['id'], 'root_model_input': deepcopy(root),
                 'request_model_input': deepcopy(request), 'inherited_shapes': shapes,
                  'shapes': list(shapes), 'rejected_events': [], 'send_state': 'not_sent'}
        if self.generic:
            record['feedback_schema'] = self.schema.__name__
            trace['request_measurement'] = request_measurement
            trace['wire_captures'] = {digest(root):initial_wire,digest(request):wire}
            record['wire_trace'] = {'initial_capture': initial_wire}
            from .policy_admission import SCHEMAS
            if self.schema.__name__ in SCHEMAS:
                record['admission_trace'] = {'call_id':record['id']}
                if admission_parent is not None:record['admission_parent'] = deepcopy(admission_parent)
        self.legacy_index.pop((task_id,phase),None)
        self.active[record['id']] = record; self.checkpoint(trace)
        try:
            arguments = {'expected_request_configuration': binding['settings']} if getattr(self.e.gateway, 'supports_request_configuration', False) else {}
            if self.generic:
                arguments.update(feedback_trace=trace,wire_trace=record['wire_trace'])
                if 'admission_trace' in record:arguments['admission_trace']=record['admission_trace']
            else:arguments['disposition_trace']=trace
            if recovered and recovered.get('restored_response') is not None:
                arguments['restored_response']=recovered['restored_response']
            value = await self.e._call(task_id, phase, payload, self.schema, role=role, **arguments)
            record.update(status='succeeded', result=value.model_dump())
            self.checkpoint(trace)
            self._authenticate(record, role)
            self._sink(record,admission_sink)
            self.e.store.record('bounded_model_completed', record['key'], record)
            return value
        except BaseException as error:
            record.update(status='unobserved' if not isinstance(error, Exception) else 'failed',
                          first_fault={'type': type(error).__name__, 'message': str(error)})
            if isinstance(error, ProviderError): record['metadata'] = deepcopy(error.metadata)
            self.checkpoint(trace)
            from .semantic_wire import WirePartialAssessmentError
            if isinstance(error, WirePartialAssessmentError):
                authenticated = self._authenticate(record, role)
                if authenticated.get('partial_assessments') != error.evidence:
                    self._hold('assessment subset changed before its bounded consumer')
                error.bounded_call_ref = reference(record)
            elif self.b._output_truncated(error):
                self.e.store.record('bounded_model_truncated', record['key'], record)
                error.bounded_call_ref = reference(record)
            elif self.schema.__name__ == 'AssessmentBatch' and trace.get('send_state') == 'held':
                authenticated = self._authenticate(record, role)
                if authenticated.get('cardinality'):
                    from .semantic_wire import WireCardinalityError
                    raise WireCardinalityError(authenticated['cardinality'], reference(record)) from error
            raise
        finally:
            self.active.pop(record['id'], None)
