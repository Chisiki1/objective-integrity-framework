from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import AsyncExitStack
import inspect
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import NodeCancelledError

from .models import (LEARNING_SCHEMAS, LearningIdeas, LearningApplications, LearningSynthesis, Assessment, AssessmentBatch, Cleanup, Completion, ConfigurationRequired, Disposition,
    Idea, IdeaResolution, Learning, LearningContractError, Operation, OperationResult, PolicyError, ResearchQuery,
    Review, SkillSelection, TaskPlan, learning_update_contract, learning_applications_contract,
    application_evidence_pointer, learning_application_output_contract, APPLICATION_OUTPUT_VERSION, now,
    optional_improvement_contract, scoped_policy_input)
from .store import canonical, digest, redact, redact_source_range
from .capabilities import confined
from .decisions import targets as decision_targets, operation_targets, task_plan_targets
from .providers import ProviderError
from .sources import contract as source_contract, validate_reconciliation
from .web_judgments import WebJudgments, WebSelectionChanged, WebCorrectionNeeded
from .bounded_judgments import BoundedJudgments
from .policy_admission import (PolicyAdmission, AdmissionPreparationRequired, GOVERNED_KINDS,
    independent_context, validate_admission, admission_model_input, admission_anchors, AdmissionContractError, validate_engineering, SCHEMAS as ADMISSION_SCHEMAS)
from .source_preparation import SourcePreparation, prepared_source_texts
from .model_routing import catalog as model_catalog, bind_selection, resolve_lease
from .operation_contracts import file_write_input, operation_effect
from .disposition_contracts import contract as disposition_contract, validate as validate_disposition, recurrence as disposition_recurrence
from .disposition_recovery import DispositionRecovery


class CycleState(TypedDict):
    task_id: str
    operation_id: str


class CycleHeld(PolicyError):
    pass


class RevisionNeeded(CycleHeld):
    pass


class WebRevisionNeeded(RevisionNeeded):
    def __init__(self,message,acquisition_id,stage,verdict):
        super().__init__(message)
        self.acquisition_id=acquisition_id;self.stage=stage;self.verdict=verdict
        self.query=None


class Quiesced(Exception):
    """Current known state is parked for a fresh-process source boundary."""


class GeneratedProposalError(PolicyError):
    """Only the returned proposal failed an identity or coverage constraint."""


class Engine:
    """The model proposes; this controller owns transitions and actual tool access.

    Every enabled normal action passes the same durable cycle. No API accepts a
    model-authored permit, role, stage receipt or completed-task assertion.
    Unobservable or dishonest semantic judgments are not solved by a schema.
    """
    def __init__(self, store, policy, executor, gateway, web, knowledge, updates=None):
        self.store,self.policy,self.executor=store,policy,executor
        self.gateway,self.web,self.knowledge=gateway,web,knowledge
        self.knowledge.optional_deferral_enabled=lambda:bool(getattr(self.policy,'role_scoped_enabled',False))
        if hasattr(gateway,'response_store'):gateway.response_store=store
        self.web.acquisition_store=store
        self.web_judgments=WebJudgments(self)
        self.bounded_judgments=BoundedJudgments(self)
        self.disposition_recovery=DispositionRecovery(self)
        self.wire_recoveries={}
        self.policy_admission=PolicyAdmission(self)
        self.source_preparation=SourcePreparation(store,executor)
        self.updates=updates
        if updates is not None and hasattr(updates,'runtime_record'):
            self.knowledge.runtime_evidence=updates.runtime_record
        self.on_restart=None
        self.controller_lock=asyncio.Lock()
        self.restart_pending=None
        self.quiescing=None
        self.restart_resume_ids=[]
        self.cycle_boundary=asyncio.Event()
        self.active_cycles=set()
        self.controller_waiters=set()
        self.store.policy_hash=policy.hash
        self.running={}
        self.workspace_locks={}
        self.source_locks={}
        self.stack=AsyncExitStack()
        self.graph=None
        self.init_lock=asyncio.Lock()
        self.stop_requested=set()
        self.completed_conditions=set()
        self.thinking_targets={'discovery','method_generation','evaluation','learning','improvement','new_axes','structuring','path','exploration','information_collection','knowledge','execution','coordination'}
        for amendment in self.store.records('policy_amendment'):
            if amendment.get('status')=='applied':self.policy.apply_amendment(amendment)
        self.store.policy_hash=self.policy.hash
        self.web_judgments.recover_legacy()
        # A previous process might have exited after the side effect. Never replay
        # its executing operation from a checkpoint just because no result exists.
        for task in self.store.list_tasks():
            if task['status']=='running' or any(self.store.operation_started_without_result(o) for o in self.store.operations(task['id'])):
                self._preserve_started_result_gaps(task['id'])
                self.store.update_task(task['id'],status='recovery_required')
                self.store.event(task['id'],'recovery','required',{'reason':'Previous controller exited with unfinished task','automatic_replay':False})

    async def _graph(self):
        async with self.init_lock:
            if self.graph is None:
                saver=await self.stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(self.store.data_dir/'checkpoints.sqlite3')))
                graph=StateGraph(CycleState)
                for name in ('select','pre','execute','post','learn','close_cycle'):
                    graph.add_node(name,getattr(self,'_'+name))
                graph.add_edge(START,'select')
                for a,b in zip(('select','pre','execute','post','learn'),('pre','execute','post','learn','close_cycle')):graph.add_edge(a,b)
                graph.add_edge('close_cycle',END)
                self.graph=graph.compile(checkpointer=saver)
        return self.graph

    def start_task(self, task_id):
        task=self.store.get_task(task_id)
        if task['status'] in {'completed','retired'}:raise PolicyError('Use a source update or reviewed child scope to reopen a terminal task')
        current=self.running.get(task_id)
        if current and not current.done():return current
        self.stop_requested.discard(task_id)
        handle=asyncio.create_task(self.run_task(task_id),name='policy-task-'+task_id)
        self.running[task_id]=handle
        return handle

    def resume_task(self,task_id):
        current=self.running.get(task_id)
        if current and not current.done():return current
        task=self.store.get_task(task_id)
        if task['status'] in {'completed','retired'}:
            raise PolicyError('Use a source update or reviewed child scope to reopen a terminal task')
        self.store.event(task_id,'task','resume_requested',self.disposition_recovery.resume_context(task_id))
        return self.start_task(task_id)

    async def stop_task(self,task_id):
        if self.store.get_task(task_id)['status']=='retired':return self.snapshot(task_id)
        self.stop_requested.add(task_id)
        self.store.event(task_id,'task','stop_requested',{'reason':'Explicit user request. Preserve in-flight effects and child states.'})
        handle=self.running.get(task_id)
        if handle and not handle.done():
            handle.cancel()
            await asyncio.gather(handle,return_exceptions=True)
        for child in self.store.list_tasks():
            if child['parent_id']==task_id and child['status'] not in ('completed','stopped','retired'):
                await self.stop_task(child['id'])
        if self.store.get_task(task_id)['status']!='completed':self.store.update_task(task_id,status='stopped')
        return self.snapshot(task_id)

    async def submit_instruction(self,task_id,text,expected_source_hash):
        return await self._submit_source(task_id,text,expected_source_hash)

    async def submit_attachment(self,task_id,filename,raw_bytes,expected_source_hash):
        if not isinstance(filename,str) or not filename.strip() or filename in {'.','..'} or any(x in filename for x in ('/','\\','\0')):
            raise PolicyError('Attachment filename must be a plain basename')
        if not isinstance(raw_bytes,bytes) or len(raw_bytes)>10*1024*1024:raise PolicyError('Attachment limit is 10 MiB')
        return await self._submit_source(task_id,'Attached source: '+filename,expected_source_hash,
            attachment={'filename':filename,'bytes':raw_bytes})

    async def _submit_source(self,task_id,text,expected_source_hash,attachment=None):
        lock=self.source_locks.setdefault(task_id,asyncio.Lock())
        async with lock:
            original=self.store.get_task(task_id)
            if original['parent_id']:raise PolicyError('Submit user instructions to the primary task; child scope is parent-owned')
            # Commit the exact incoming source before waiting on cancellation.
            # No old permit can run against the new source hash after this point.
            updated=self.store.append_instruction(task_id,text,expected_source_hash,attachment=attachment)
            await self.stop_task(task_id)
            current=self.store.get_task(task_id);state=dict(current['state'])
            state.pop('plan',None)
            state['source_update_pending']=updated['source_hash']
            operation_id=state.get('operation_id')
            if operation_id:
                old=self.store.get_operation(operation_id)
                if old['result'] is None and not self.store.operation_started_without_result(old):
                    self.store.update_operation(operation_id,status='superseded',revision_reason='Exact user source changed before execution')
                    state.pop('operation_id',None);state.pop('cycle_id',None)
            if current.get('final'):
                self.store.record('task_final_history',task_id+':'+str(current['source_version']),
                    {'id':task_id+':'+str(current['source_version']),'source_hash':expected_source_hash,'final':current['final']})
            self.store.update_task(task_id,state=state,final=None,status='source_update_required')
            self.start_task(task_id)
            return self.snapshot(task_id)

    def _assert_parent_source(self,task):
        lease=task['state'].get('delegation_lease')
        if lease and task['parent_id'] and lease.get('parent_source_hash')!=self.store.get_task(task['parent_id'])['source_hash']:
            raise CycleHeld('Parent source changed; child needs a reviewed scope disposition before continuation')

    async def close(self):
        for identity,handle in list(self.running.items()):
            if not handle.done():await self.stop_task(identity)
        await self.stack.aclose()

    def snapshot(self,task_id):
        task=self.store.get_task(task_id)
        return {'task':task,'operations':self.store.operations(task_id),
                'children':[t for t in self.store.list_tasks() if t['parent_id']==task_id],
                'events':self.store.events(task_id),'knowledge':self.knowledge.snapshot(task_id),
                 'readiness':{'policy_hash':self.policy.hash,'claims':'Observed stage records are not proof of infallible judgments.'}}

    def _model_input(self,task_id,phase,payload,schema,*,role=None,legacy_application=False,application_before_seq=None,installed_context=...,captured_input=None):
        """Shared input; captured requests keep their original construction."""
        task=self.store.get_task(task_id);role=role or task['actor']
        fresh_context=installed_context is ...
        if captured_input is not None:
            if not isinstance(captured_input,dict):raise PolicyError('Unknown captured model input format')
            input_contract=captured_input.get('policy_input_contract')
            if input_contract not in (None,'role-scoped-v1'):raise PolicyError('Unknown captured policy input contract')
            installed_context=deepcopy(captured_input.get('installed_controller_context'))
            fresh_context=False
        else:
            input_contract=('role-scoped-v1' if getattr(self.policy,'role_scoped_enabled',False) and
                (fresh_context or isinstance(installed_context,dict) and
                 installed_context.get('policy_input_contract')=='role-scoped-v1') else None)
        from .learning_projection import active,validate_feedback
        if active(payload):
            validate_feedback(self.store,task_id,payload.get('operation',{}).get('id'),payload.get('actual_revision_feedback'))
        if phase.startswith('policy_admission_'):
            if role!='reviewer':raise PolicyError('Independent preparation requires the reviewer role')
            model_input=admission_model_input(task,self.policy.hash,phase,payload)
            if input_contract=='role-scoped-v1':
                if getattr(self.gateway,'supports_semantic_wire',False):
                    from .semantic_wire import retained_input
                    retained=retained_input(self.store,task,role,phase,model_input,schema)
                    if retained is not None:return retained
                model_input=admission_model_input(task,self.policy.hash,phase,payload,
                    policy_input_contract='role-scoped-v1')
            return model_input
        if schema in {Assessment,AssessmentBatch}:
            payload=dict(payload,required_thinking_targets=sorted(self.thinking_targets),
                thinking_instruction='thinking_targets must have every exact listed key, each with a specific assessment of this target. Compare unchanged reuse, smaller correction and structural alternatives. This checks declared coverage, not private thought quality.')
        if schema in LEARNING_SCHEMAS:
            application_contract=self.bounded_judgments.learning_application_contract(task_id,payload)
            if application_contract is None:
                application_contract={'instruction':'Selection is not use. applications may be empty when actual application is unobserved. An observed application must cite exact pre-selected skill/version/procedure and actual operation/result JSON-pointer evidence; use the supplied hashes, never invent them. Existing Skill changes require expected_hash; merge requires merge_hashes for every source. Model output cannot grant a missing selection or evidence.',
                    'operation_sha256':digest(payload['operation']) if payload.get('operation') else None,
                    'result_sha256':digest(payload['result']) if payload.get('result') else None,
                    'result_evidence':[p for p in self._evidence_pointers(payload.get('result',{})) if application_evidence_pointer(p['pointer'])]}
            application_contract={**learning_applications_contract(),**application_contract}
            if schema is LearningApplications and not legacy_application:
                payload=dict(payload,application_contract=application_contract,
                    application_output_contract=learning_application_output_contract(payload,
                        self.bounded_judgments.application_owned_ideas(task_id,payload,before_seq=application_before_seq)))
            else:
                # Exact pre-v2 Application construction remains available only
                # to authenticate retained requests. Normal sends use v2 above.
                payload=dict(payload,application_contract=application_contract,skill_update_contract=learning_update_contract(),
                    idea_disposition_contract='Keep every required idea identity/proposal/target. Reassess its disposition with reasons: an already-enforced standing rule or unchanged reuse need not become a new implementation project. Rejecting a redundant new proposal does not reject the standing policy. Adopt only a worthwhile actual change; record uncertainty as investigate. Do not invent use or benefit.')
        if schema is Disposition:
            payload=dict(payload,exact_response_contract=disposition_contract(payload))
        model_input={'task_id':task_id,'objective':task['objective'],'acceptance':task['acceptance'],'policy_hash':self.policy.hash,
            'source_context':self._source_context(task),'history_lookup':self._history_lookup(task_id),
            'role_context':{'actual_role':role,'task_owner_role':task['actor'],'parent_task_id':task['parent_id'],
                'reviewer_tools':[],'review_call_is_real':role=='reviewer','root_needs_child_lease':False,
                'phase_semantics':'Proposals and before-assessments have no target execution yet. Proceed before an action authorizes its future execution; it does not assert the future result already succeeded.'},
            'delegation_lease':task['state'].get('delegation_lease'),**redact(payload)}
        # This namespace is controller-owned, not a model-supplied capability or
        # permission. The unchanged envelope is the historical lookup key.
        model_input.pop('installed_controller_context',None)
        if installed_context is ...:
            if getattr(self.gateway,'supports_semantic_wire',False):
                from .semantic_wire import retained_input
                retained=retained_input(self.store,task,role,phase,model_input,schema)
                if retained is not None:return retained
            installed_context=self._installed_controller_context(task,payload)
        if installed_context is not None:
            from .semantic_wire import INSTALLED_CONTEXT
            if not isinstance(installed_context,dict) or installed_context.get('version')!=INSTALLED_CONTEXT:
                raise PolicyError('Unknown installed controller context version')
            model_input['installed_controller_context']=redact(deepcopy(installed_context))
        if input_contract=='role-scoped-v1':
            model_input=scoped_policy_input(model_input)
        return model_input

    def _installed_controller_context(self,task,payload):
        from .semantic_wire import INSTALLED_CONTEXT,idea_revision_contract
        from .providers import WebCollector
        # Planning/proposal already supply these complete existing definitions.
        # Other stages get the file interfaces and their actual target contract,
        # rather than another copy of every model/update/delegation setting.
        if (('controller_guide' in payload and 'capability_guide' in payload)
                or ('available_operations' in payload and 'executor_capabilities' in payload)):
            return None
        guide=self._controller_guide()
        kinds={'file_write','file_read','file_list'}
        for key in ('operation','parent_operation'):
            target=payload.get(key)
            if isinstance(target,dict):kinds.add(target.get('kind'))
        capabilities=getattr(self.executor,'catalog',lambda **kw:{})(workspace=Path(task['workspace']))
        if inspect.isawaitable(capabilities):
            if inspect.iscoroutine(capabilities):capabilities.close()
            raise ConfigurationRequired('Installed operation context requires the configured synchronous executor catalog')
        return {'version':INSTALLED_CONTEXT,'operation_contracts':{key:value for key,value in guide.items() if key in kinds},
            **({'policy_input_contract':'role-scoped-v1','optional_improvements':optional_improvement_contract()}
                if getattr(self.policy,'role_scoped_enabled',False) else {}),
            'executor_capabilities':capabilities,
            'web_execution':WebCollector.execution_contract(),
            'idea_revision':idea_revision_contract(),
            **({'learning_applications':{**learning_applications_contract(),
                'purpose':'This is the stored Learning representation under assessment, not this judgment response schema. applications records evidenced uses only; selection count does not determine application count. Assess applicability and actual use from the selected original clauses and observed evidence. Non-use or uncertainty belongs in the existing reasoning, not a fabricated application or unsupported field.'}}
                if isinstance(payload.get('learning'),dict) and 'application_contract' not in payload else {}),
            'parent_progression':{
                'pre_collection_return':['select_skills','assess_parent_operation_and_decisions',
                    'independent_review_and_disposition','admission_gate','execute_if_authorized'],
                'post_collection_return':['assess_actual_parent_result','propose_learning',
                    'review_learning_and_apply_if_authorized','review_actual_knowledge_effect'],
                'meaning':'These are installed subsequent duties, not completed work. A pre-collection callback '
                    'does not yet perform the parent operation review or its later Skill selection. The post '
                    'path retains the pre-selected Skill and actual result. Selection is not use. '
                    'Knowledge application changes stored Skills/projections and retains its required '
                    'pre/post judgment and independent review. No recursive Web acquisition is required '
                    'merely to review this same exchange; a genuinely new requirement uses the governed route.'},
            'meaning':'Installed controller interfaces and current executor catalog, supplied as facts for judgment. Availability is not task permission, reviewer tool access, preparation completion, or evidence that an operation ran. Apply all source/policy/lease/admission gates. File content, byte counts, hashes and success require the actual governed operation result and readback; preserve failed, partial and unknown effects.'}

    @staticmethod
    def _learning_required_input(model_input, required):
        updated=dict(model_input,required_ideas=required)
        if model_input.get('application_output_contract',{}).get('schema')==APPLICATION_OUTPUT_VERSION:
            updated['application_output_contract']=learning_application_output_contract(updated,
                model_input['application_output_contract']['already_owned_idea_ids'])
        return updated

    @staticmethod
    def _feedback_from_rejection(detail):
        if 'validation' in detail and 'rejected_response' in detail:
            return {'validation':detail['validation'],'rejected_response':detail['rejected_response'],
                'instruction':'Generate a NEW complete phase proposal. Use the exact supplied identities and observed facts; preserve unknowns and decide the substance yourself. Do not invent evidence, waive conditions, or claim execution. The rejected response has not authorized any target action.'}
        metadata=detail['metadata']
        return {'validation':metadata.get('validation_diagnostic',{'kind':'schema_validation_withheld','detail':'Original schema failure is retained; safe diagnostic was unavailable.'}),
            'rejected_response':metadata.get('response_diagnostic'),
            'response_idea_extraction':detail.get('response_idea_extraction'),
            'instruction':'Generate a NEW schema-valid phase proposal using the original source and exact supplied schema. Correct the reported wire shape, preserve source facts and unknowns, never invent success or convert a false outcome to true. No tool action has been admitted. Diagnostic may be explicitly bounded; omitted content is not evidence.'}

    def _wire_capture(self,task_id,phase,model_input,schema,role):
        if not getattr(self.gateway,'supports_semantic_wire',False):return None
        from .semantic_wire import capture
        return capture(self.store,self.store.get_task(task_id),role,phase,model_input,schema)

    def _wire_recovery(self,schema):
        from .semantic_wire import WIRE_SCHEMAS
        if (not getattr(self.gateway,'supports_semantic_wire',False)
                or schema in LEARNING_SCHEMAS or schema is Disposition or schema.__name__ not in WIRE_SCHEMAS):
            return None
        if schema not in self.wire_recoveries:self.wire_recoveries[schema]=DispositionRecovery(self,schema)
        return self.wire_recoveries[schema]

    def _feedback_shape(self,schema,diagnostic,*,wire=False,model_input=None,wire_capture=None):
        if schema is Disposition:return digest(disposition_recurrence(diagnostic))
        if schema is AssessmentBatch and wire and model_input is not None and isinstance(wire_capture,dict):
            from . import semantic_wire as assessment_wire
            captured=self.store.record_get('semantic_wire_request',wire_capture.get('id'))
            if (captured and digest(captured)==wire_capture.get('sha256')
                    and captured.get('canonical_input')==model_input
                    and captured.get('wire_revision') in {assessment_wire.ASSESSMENT_TARGET_RANGES,
                                                          assessment_wire.ASSESSMENT_REFERENCE_SLOTS}):
                requested=assessment_wire.requested_assessment_ranges(model_input,diagnostic)
                if requested:
                    return digest({'kind':'assessment_source_request_v3',
                        'ranges':sorted((r['source_id'],r['source_sha256'],r['start_byte'],r['end_byte'])
                                        for r in requested)})
        return digest(disposition_recurrence(diagnostic,schema=schema) if wire else diagnostic)

    async def _call(self,task_id,phase,payload,schema,*,role=None,expected_request_configuration=None, admission_trace=None, learning_trace=None, disposition_trace=None, wire_trace=None, feedback_trace=None, restored_response=None, learning_recovery=...):
        if schema is Disposition and disposition_trace is None:
            return await self.disposition_recovery.call(task_id,phase,payload,role=role,expected_request_configuration=expected_request_configuration)
        recovery=self.disposition_recovery if schema is Disposition else self._wire_recovery(schema)
        active_trace=disposition_trace if disposition_trace is not None else feedback_trace
        if recovery is not None and active_trace is None:
            # Direct and paged consumers share the same completed-return and
            # interrupted-feedback boundary; a direct call cannot bypass it.
            return await self.bounded_judgments._call(task_id,phase,payload,schema,role=role,
                _expected_request_configuration=expected_request_configuration)
        if task_id in self.stop_requested:raise asyncio.CancelledError()
        if self.quiescing and self.quiescing!=task_id:raise Quiesced('Controller update is waiting for this task safe boundary')
        task=self.store.get_task(task_id)
        self._assert_parent_source(task)
        source_hash=task['source_hash']
        role=role or task['actor']
        started=time.monotonic()
        policy_hash=self.policy.hash
        started_event=self.store.event(task_id,phase,'started',{'actor':role,'target':payload.get('target',payload.get('operation',{}).get('id')),'policy_hash':self.policy.hash})
        try:
            validation_payload=self._prepare_learning_payload(task_id,payload,phase=phase) if schema in LEARNING_SCHEMAS else payload
            learning_request_key=None
            if schema in LEARNING_SCHEMAS:
                learning_request_key=digest({'task_id':task_id,'phase':phase,'role':role,
                    'policy_hash':policy_hash,'source_hash':source_hash,'payload':payload})
            # Recovery selected/authenticated the saved request before arriving
            # here. Do not construct current context and then overwrite it.
            if active_trace is not None:
                model_input=deepcopy(active_trace['root_model_input'])
            else:
                model_input=self._model_input(task_id,phase,validation_payload,schema,role=role)
            if wire_trace is not None:
                first=self._wire_capture(task_id,phase,model_input,schema,role)
                if first!=wire_trace['initial_capture']:raise PolicyError('WIRE_PROVENANCE: measured initial input differs before dispatch')
                wire_trace['request_model_input']=deepcopy(model_input)
            initial_model_input=deepcopy(model_input) if learning_trace is not None else None
            expected_request_configuration=deepcopy(expected_request_configuration)
            rejected_shapes=set()
            if active_trace is not None:
                active_trace['started_event']={'seq':started_event['seq'],'hash':started_event['hash']}
                model_input=deepcopy(active_trace['request_model_input'])
                rejected_shapes=set(active_trace['inherited_shapes'])
                recovery.checkpoint(active_trace)
            if learning_trace is not None:
                learning_trace.update(started_event={'seq':started_event['seq'],'hash':started_event['hash']},
                    request_model_input=redact(initial_model_input),role=role,
                    lease=deepcopy(task['state'].get('delegation_lease')),rejected_events=[])
            if schema in LEARNING_SCHEMAS:
                restored=(self.bounded_judgments.restore_learning_feedback(task_id,phase,payload,schema,role,model_input)
                    if learning_recovery is ... else learning_recovery)
                if restored is not None:
                    model_input=restored['model_input'];rejected_shapes=set(restored['rejected_shapes'])
                    if learning_trace is not None:
                        learning_trace['feedback_recovery']=restored['source_receipt']
                        if restored.get('interrupted_resume'):
                            learning_trace['interrupted_resume']=deepcopy(restored['interrupted_resume'])
                    restored_response=restored.get('restored_response')
            if schema.__name__ in ADMISSION_SCHEMAS:
                admission_anchors(payload)
                prior=payload.get('actual_format_feedback',{}).get('validation',{})
                if prior.get('kind')=='proposal_contract':rejected_shapes.add(digest(prior))
            while True:
                try:
                    if task_id in self.stop_requested:raise asyncio.CancelledError()
                    if self.quiescing and self.quiescing!=task_id:raise Quiesced('Controller update is waiting for this task safe boundary')
                    if schema.__name__ in ADMISSION_SCHEMAS or schema in LEARNING_SCHEMAS or active_trace is not None:
                        current=self.store.get_task(task_id)
                        if current['source_hash']!=source_hash or self.policy.hash!=policy_hash:
                            raise RevisionNeeded('Source or policy changed before admission correction')
                        self._assert_parent_source(current)
                        if independent_context(current,policy_hash)!=independent_context(task,policy_hash):
                            raise RevisionNeeded('Actual reviewer lease changed before admission correction')
                    model_arguments={}
                    if task['parent_id']:
                        lease=task['state'].get('delegation_lease',{})
                        model_lease=lease.get('model_leases',{}).get(role)
                        if model_lease is None or not getattr(self.gateway,'supports_model_leases',False):
                            raise ConfigurationRequired('Child model selection is missing or gateway does not support bound model leases; parent must reassess this job')
                        original=lease.get('job_operation') or self.store.get_operation(lease['parent_operation_id'])['operation']
                        parent=self.store.get_task(task['parent_id'])
                        context=self._model_job_context(parent,original,role)
                        model_arguments={'model_lease':model_lease,'job_context':context}
                    if expected_request_configuration is not None:
                        if not getattr(self.gateway,'supports_request_configuration',False):
                            raise ConfigurationRequired('Gateway cannot bind the measured request settings; this dispatch was not sent')
                        # Every schema/proposal correction is another actual
                        # send. A final settings comparison would miss A-B-A.
                        model_arguments['expected_request_configuration']=expected_request_configuration
                    if learning_trace is not None:learning_trace['actual_model_input']=redact(model_input)
                    if active_trace is not None:recovery.before_send(active_trace,model_input)
                    wire_capture=self._wire_capture(task_id,phase,model_input,schema,role)
                    if wire_capture is not None:
                        model_arguments['semantic_wire']=wire_capture
                        if wire_trace is not None:
                            wire_trace.update(actual_model_input=deepcopy(model_input),actual_capture=wire_capture)
                    if restored_response is not None:
                        from .semantic_wire import save_output
                        retained=restored_response;restored_response=None
                        if retained['capture']['canonical_input']!=model_input:
                            raise PolicyError('WIRE_PROVENANCE: recovered response belongs to another actual input')
                        value=retained['value'];usage=deepcopy(retained['metadata'])
                        usage['semantic_wire_output']=save_output(self.store,wire_capture,retained['capture'],
                            schema,retained['raw'],value,usage)
                    else:
                        value,usage=await self.gateway.generate(role,phase,model_input,schema,**model_arguments)
                    value=schema.model_validate(value)
                    try:
                        self._validate_generated(schema,value,validation_payload,task_id=task_id)
                        if feedback_trace is not None:self.bounded_judgments._validate(schema,value,validation_payload,task_id=task_id)
                    except PolicyError as error:
                        if str(error).startswith('LEARNING_PROVENANCE:'):raise
                        if schema.__name__ in ADMISSION_SCHEMAS and not isinstance(error,AdmissionContractError):raise
                        rejected=GeneratedProposalError(str(error))
                        rejected.diagnostic=getattr(error,'diagnostic',None)
                        raise rejected from error
                    break
                except GeneratedProposalError as error:
                    # Only validation of the newly returned proposal is inside
                    # this branch. No executor or mutation has been called.
                    diagnostic=getattr(error,'diagnostic',None) or {'kind':'proposal_contract','message':str(error)}
                    retained={}
                    if schema in LEARNING_SCHEMAS:
                        required=self._carry_learning_ideas(validation_payload.get('required_ideas',[]),self.bounded_judgments.learning_correction_ideas(task_id,validation_payload,[i.model_dump() for i in value.ideas]))
                        validation_payload=dict(validation_payload,required_ideas=required)
                        model_input=self._learning_required_input(model_input,required)
                        retained={'learning_request_key':learning_request_key,'operation_id':payload['operation']['id'],
                            'source_hash':source_hash,'policy_hash':policy_hash,'learning_owner':self.bounded_judgments.learning_owner(payload,phase),
                            'retained_ideas':required}
                    rejected_event=self.store.event(task_id,phase,'rejected_model_output',{
                        'validation':diagnostic,'rejected_response':value.model_dump(),
                        'usage':usage,'target_effect':'none; proposal not admitted',
                        **({'admission_call_id':admission_trace['call_id']} if admission_trace is not None else {}),
                        **(recovery.event_binding(active_trace,model_input) if active_trace is not None else {}),**retained})
                    if learning_trace is not None:learning_trace['rejected_events'].append({'seq':rejected_event['seq'],'hash':rejected_event['hash']})
                    shape=self._feedback_shape(schema,diagnostic,wire=bool(usage.get('semantic_wire_capture')),
                        model_input=model_input,wire_capture=usage.get('semantic_wire_capture'))
                    if active_trace is not None:recovery.rejected(active_trace,rejected_event,model_input,shape,shape in rejected_shapes)
                    if shape in rejected_shapes:
                        raise PolicyError('Model repeated the same proposal-contract defect after actual feedback: '+str(error)) from error
                    rejected_shapes.add(shape)
                    model_input=(recovery._feedback_input(model_input,rejected_event['detail'])
                                 if schema is AssessmentBatch and recovery is not None else
                                 dict(model_input,actual_format_feedback=self._feedback_from_rejection(rejected_event['detail'])))
                except ProviderError as error:
                    # Preserve the exact input used for this failed call,
                    # including feedback from an earlier rejected wire shape.
                    # The bounded consumer owns its durable attempt record.
                    error.controller_model_input=deepcopy(model_input)
                    metadata=getattr(error,'metadata',{})
                    if 'validation_diagnostic' not in metadata and not (schema in LEARNING_SCHEMAS and metadata.get('schema_validation_failed')):
                        if active_trace is not None:
                            # A transport error without an observed response may
                            # have reached the provider. Keep the durable send
                            # frontier unknown instead of inventing a response.
                            if not (metadata.get('request_effect') == 'response-unobserved'
                                    and not metadata.get('response_record')):
                                active_trace['send_state']='failed_response'
                            recovery.checkpoint(active_trace)
                        raise
                    retained={};extraction_error=None
                    if schema in LEARNING_SCHEMAS:
                        try:extraction=self._response_learning_ideas(metadata,task_id,role,phase,policy_hash,source_hash)
                        except PolicyError as fault:
                            extraction_error=fault
                            extraction={'status':'unavailable','ideas':[],'response_ref':metadata.get('response_record'),
                                'reason':str(fault),'extraction_unproven':True}
                        required=self._carry_learning_ideas(validation_payload.get('required_ideas',[]),self.bounded_judgments.learning_correction_ideas(task_id,validation_payload,extraction['ideas']))
                        validation_payload=dict(validation_payload,required_ideas=required)
                        model_input=self._learning_required_input(model_input,required)
                        retained={'learning_request_key':learning_request_key,'operation_id':payload['operation']['id'],
                            'source_hash':source_hash,'policy_hash':policy_hash,'learning_owner':self.bounded_judgments.learning_owner(payload,phase),
                            'retained_ideas':required,
                            'response_idea_extraction':extraction,'idea_extraction_pending':extraction_error is not None}
                    rejected_event=self.store.event(task_id,phase,'rejected_model_output',{'message':str(error),'metadata':metadata,
                        'target_effect':'none; proposal not admitted',
                        **({'admission_call_id':admission_trace['call_id']} if admission_trace is not None else {}),
                        **(recovery.event_binding(active_trace,model_input) if active_trace is not None else {}),**retained})
                    if learning_trace is not None:learning_trace['rejected_events'].append({'seq':rejected_event['seq'],'hash':rejected_event['hash']})
                    if extraction_error is not None:raise extraction_error from error
                    diagnostic=metadata.get('validation_diagnostic',{'kind':'schema_validation_withheld','detail':'Original schema failure is retained; safe diagnostic was unavailable.'})
                    shape=self._feedback_shape(schema,diagnostic,wire=bool(metadata.get('semantic_wire_capture')),
                        model_input=model_input,wire_capture=metadata.get('semantic_wire_capture'))
                    if active_trace is not None:
                        partial=recovery.rejected(active_trace,rejected_event,model_input,shape,shape in rejected_shapes)
                        if partial is not None:
                            from .semantic_wire import WirePartialAssessmentError
                            raise WirePartialAssessmentError(partial) from error
                    if shape in rejected_shapes:raise PolicyError('Model repeated the same schema defect after actual feedback; dependent proposal remains unadmitted') from error
                    rejected_shapes.add(shape)
                    model_input=(recovery._feedback_input(model_input,rejected_event['detail'])
                                 if schema is AssessmentBatch and recovery is not None else
                                 dict(model_input,actual_format_feedback=self._feedback_from_rejection(rejected_event['detail'])))
            if self.policy.hash!=policy_hash:raise RevisionNeeded('Policy changed during judgment; reassess this target with the exact approved current version')
            if self.store.get_task(task_id)['source_hash']!=source_hash:raise RevisionNeeded('User source changed during judgment; old proposal is unadmitted')
            self._assert_parent_source(self.store.get_task(task_id))
            assessments=[value] if isinstance(value,Assessment) else [x.assessment for x in value.assessments] if isinstance(value,AssessmentBatch) else []
            for assessment in assessments:
                if set(assessment.thinking_targets)!=self.thinking_targets or any(not x.strip() for x in assessment.thinking_targets.values()):
                    raise PolicyError('Every prescribed thinking target needs its own substantive assessment: '+','.join(sorted(self.thinking_targets)))
                if not usage.get('semantic_wire_output'):
                    assessment.ideas=[x.model_copy(update={'id':uuid4().hex}) for x in assessment.ideas]
            success_event=self.store.event(task_id,phase,'succeeded',{'actor':role,'result':value.model_dump(),
                **(recovery.event_binding(active_trace,model_input) if active_trace is not None else {}),
                **({'learning_call_id':learning_trace['call_id'],'learning_request_sha256':digest(initial_model_input),
                    'learning_actual_input_sha256':digest(model_input),'learning_owner':self.bounded_judgments.learning_owner(payload,phase)} if learning_trace is not None else {}),
                **({'admission_request_sha256':digest(self._model_input(task_id,phase,payload,schema,role=role)),
                    'admission_actual_input_sha256':digest(model_input),
                    **({'admission_call_id':admission_trace['call_id']} if admission_trace is not None else {})} if phase.startswith('policy_admission_') else {}),'measurement':{
                'elapsed_seconds':time.monotonic()-started,'usage':usage,'benefit':None,'missed_errors':None}})
            if admission_trace is not None:
                admission_trace.update(event={'seq':success_event['seq'],'hash':success_event['hash']},
                                       actual_model_input=redact(model_input))
            if learning_trace is not None:
                learning_trace.update(event={'seq':success_event['seq'],'hash':success_event['hash']},
                    request_model_input=redact(initial_model_input),actual_model_input=redact(model_input),
                    role=role,lease=deepcopy(task['state'].get('delegation_lease')))
            if active_trace is not None:
                active_trace.update(event={'seq':success_event['seq'],'hash':success_event['hash']},actual_model_input=redact(model_input),send_state='succeeded')
                recovery.checkpoint(active_trace)
            if wire_trace is not None:wire_trace['event']={'seq':success_event['seq'],'hash':success_event['hash']}
            return value
        except asyncio.CancelledError:
            interrupted=self.store.event(task_id,phase,'interrupted',{'elapsed_seconds':time.monotonic()-started,'result':'UNOBSERVED'})
            if active_trace is not None:
                active_trace['failure_event']={'seq':interrupted['seq'],'hash':interrupted['hash']}
                recovery.checkpoint(active_trace)
            if learning_trace is not None:
                learning_trace['failure_event']={'seq':interrupted['seq'],'hash':interrupted['hash']}
            raise
        except Exception as error:
            failed_event=self.store.event(task_id,phase,'failed',{'error_type':type(error).__name__,'message':str(error),'metadata':getattr(error,'metadata',{}),'elapsed_seconds':time.monotonic()-started,'first_fault_preserved':True})
            if active_trace is not None:
                active_trace['failure_event']={'seq':failed_event['seq'],'hash':failed_event['hash']}
                recovery.checkpoint(active_trace)
            if learning_trace is not None:
                learning_trace['failure_event']={'seq':failed_event['seq'],'hash':failed_event['hash']}
                if 'model_input' in locals():error.controller_model_input=deepcopy(model_input)
            raise

    def _validate_learning_proposal(self,task_id,value,payload):
        self._validate_ideas(payload.get('required_ideas',[]),value)
        original=self.bounded_judgments.learning_validation_input(task_id,payload)
        self.knowledge.validate_learning_proposal(self.store.get_task(task_id),original['operation'],value,original['result'],
            [item['id'] for item in payload.get('pre',{}).get('skills',{}).get('selected',[])])

    def _validate_generated(self,schema,value,payload,*,task_id=None):
        """Identity/coverage checks on a proposal, never a semantic truth test."""
        validate_engineering(schema,value,payload)
        if task_id is not None and hasattr(value,'ideas'):
            for idea in value.ideas:self.knowledge.idea_disposition(self.store.get_task(task_id),idea)
        if schema is Disposition:
            validate_disposition(payload,value)
        elif schema is Review:
            ids=[opinion.id for opinion in value.opinions]
            if len(ids)!=len(set(ids)):raise PolicyError('Review opinion IDs must be unique')
        elif schema in LEARNING_SCHEMAS and schema is not Learning:
            self.bounded_judgments.validate_learning_unit(task_id,schema,value,payload)
        elif schema is Learning:
            self._validate_ideas(payload.get('required_ideas',[]),value)
            if task_id is not None:self._validate_learning_proposal(task_id,value,payload)
        elif schema is Operation:
            if payload.get('source_preparation') and value.kind not in payload['source_preparation']['allowed_preparation_operations']:
                raise PolicyError('Opaque-source preparation cannot adopt source meaning or finish the task')
            if task_id is not None:
                task=self.store.get_task(task_id)
                if value.kind=='file_write':file_write_input(Path(task['workspace']),value.args)
                self._require_operation_independence(task,value.model_dump())
        elif schema is TaskPlan:
            task=self.store.get_task(payload['source_task_id']) if payload.get('source_task_id') else None
            if task:validate_reconciliation(task,value.model_dump(),self.store.source_texts(task))
            deferred=payload.get('deferred_for_planning',{})
            expected={x['idea_id']:x for x in deferred.get('candidates',[])}
            actual=value.deferred_considerations
            if expected:
                if value.deferred_index_hash!=deferred['index_hash'] or len(actual)!=len(expected) or {x.get('idea_id') for x in actual}!=set(expected):
                    raise PolicyError('Plan must consider every deferred candidate exactly once using the supplied deferred_index_hash')
                for item in actual:
                    if item.get('source_hash')!=expected[item['idea_id']]['source_hash'] or item.get('disposition') not in {'include','exclude'} or not item.get('reason') or (item['disposition']=='include' and not item.get('planned_application')):
                        raise PolicyError('Deferred consideration needs exact idea_id/source_hash, include or exclude, reasons and an included planned_application')
        elif schema is SkillSelection:
            expected={s['id']:s for s in payload['knowledge']['skills']}
            selected=[x.get('id') for x in value.selected];rejected=[x.get('id') for x in value.rejected]
            if sorted(selected+rejected)!=sorted(expected):raise PolicyError('Account for every exact Skill ID once as selected or rejected')
            for item in value.selected:
                actual=expected[item['id']]
                if item.get('hash')!=actual['hash'] or not item.get('application') or not item.get('reason'):
                    raise PolicyError('Selected Skill needs its exact supplied hash, reason and actual intended application')
                if item.get('procedure_clause',actual['content']) not in actual['content']:
                    raise PolicyError('Selected procedure_clause must be exact text in the supplied Skill')
        assessments=[value] if schema is Assessment else [x.assessment for x in value.assessments] if schema is AssessmentBatch else []
        if schema is AssessmentBatch:
            expected=[x['id'] for x in payload['targets']]
            actual=[x.target_id for x in value.assessments]
            if sorted(actual)!=sorted(expected):raise PolicyError('Assessment must include each exact supplied target ID once, without omission or invention')
        for assessment in assessments:
            if task_id is not None:
                for idea in assessment.ideas:self.knowledge.idea_disposition(self.store.get_task(task_id),idea)
            if set(assessment.thinking_targets)!=self.thinking_targets or any(not x.strip() for x in assessment.thinking_targets.values()):
                raise PolicyError('Every prescribed thinking target needs its own assessment: '+','.join(sorted(self.thinking_targets)))

    async def _assess_targets(self,task_id,phase,targets,context,*,previous=None):
        return await self.bounded_judgments.assess_targets(task_id,phase,targets,context,previous=previous)

    def _response_learning_ideas(self,metadata,task_id,role,phase,policy_hash,source_hash):
        reader=getattr(self.gateway,'learning_response_ideas',None)
        if not callable(reader):
            return {'status':'unavailable','ideas':[],'response_ref':metadata.get('response_record'),
                'reason':'Gateway has no complete protected-response Idea reader; extraction is unproven.'}
        arguments={'semantic_wire':metadata['semantic_wire_capture']} if metadata.get('semantic_wire_capture') is not None else {}
        return reader(metadata.get('response_record'),task_id=task_id,role=role,phase=phase,
            policy_hash=policy_hash,source_hash=source_hash,**arguments)

    def _learning_response_originals(self,task_id,operation_id):
        """Incrementally index immutable response events, independent of new inputs."""
        def index_new_events():
            index=self.store.record_get('learning_response_index',task_id) or {
                'schema':'learning-response-index-v1','task_id':task_id,'after_seq':0,'last_event_hash':None,'operations':{}}
            if index.get('task_id')!=task_id:raise PolicyError('Learning response index task differs')
            cursor=index['after_seq']
            events=self.store.events(task_id,after=max(0,cursor-1))
            if cursor:
                if not events or events[0]['seq']!=cursor or events[0]['hash']!=index['last_event_hash']:
                    raise PolicyError('Learning response event frontier changed; preserve its original evidence')
                events=events[1:]
            for event in events:
                detail=event['detail']
                if event['status']=='rejected_model_output' and detail.get('learning_request_key') and detail.get('operation_id'):
                    metadata=detail.get('metadata',{});reference=metadata.get('response_record')
                    original={'task_id':task_id,'operation_id':detail['operation_id'],
                        'source_event':{'seq':event['seq'],'hash':event['hash']},
                        'binding':{'task_id':task_id,'role':metadata.get('role',(reference or {}).get('role')),
                            'phase':event['stage'],'source_hash':detail.get('source_hash'),'policy_hash':detail.get('policy_hash')},
                         'response_ref':reference,'retained_ideas':detail.get('retained_ideas',[]),
                         **({'semantic_wire_capture':metadata['semantic_wire_capture']} if metadata.get('semantic_wire_capture') is not None else {}),
                        'pending':bool(detail.get('idea_extraction_pending')),
                        'extraction':detail.get('response_idea_extraction')}
                    originals=index['operations'].setdefault(detail['operation_id'],[])
                    if any(o['source_event']['seq']==event['seq'] for o in originals):
                        raise PolicyError('Learning response event was indexed twice')
                    originals.append(original)
                index.update(after_seq=event['seq'],last_event_hash=event['hash'])
            if events:self.store.record('learning_response_index',task_id,index)
            return deepcopy(index['operations'].get(operation_id,[]))
        return self.store._transaction(index_new_events)

    def _learning_response_history(self,task_id,operation_id):
        """Recover original evidence once; it is historical data, never new authority."""
        self.store.get_task(task_id)
        required=[];references=[]
        for original in self._learning_response_originals(task_id,operation_id):
            required=self._carry_learning_ideas(required,original['retained_ideas'])
            extraction=original.get('extraction')
            recovery_ref=None
            if original['pending']:
                binding=original['binding'];reference=original['response_ref']
                if (not isinstance(reference,dict) or reference.get('task_id')!=task_id
                        or not all(isinstance(v,str) and v for v in binding.values())):
                    raise PolicyError('Pending Learning response has an incomplete original request binding')
                key='response:'+digest({'task_id':task_id,'source_event':original['source_event']})
                recovered=self.store.record_get('learning_response_recovery',key)
                if recovered:
                    if (recovered.get('original_sha256')!=digest(original) or recovered.get('source_event')!=original['source_event']
                            or recovered.get('extraction',{}).get('response_ref')!=reference):
                        raise PolicyError('Recovered Learning response differs from its original event or response')
                    extraction=recovered['extraction']
                else:
                    try:
                        extraction=self._response_learning_ideas({'response_record':reference,
                            **({'semantic_wire_capture':original['semantic_wire_capture']} if original.get('semantic_wire_capture') is not None else {})},**binding)
                        if extraction.get('response_ref')!=reference or not extraction.get('view_sha256'):
                            raise PolicyError('Original Learning response remains unread; no replacement proposal is admitted')
                    except PolicyError as error:
                        failure={'task_id':task_id,'operation_id':operation_id,'source_event':original['source_event'],
                            'original_binding':binding,'response_ref':reference,'observed_at':now(),
                            'error':{'type':type(error).__name__,'message':str(error)},'read_state':'unresolved'}
                        self.store.record('learning_response_recovery_failure',digest(failure),failure)
                        raise
                    recovered={'id':key,'task_id':task_id,'operation_id':operation_id,
                        'phase':binding['phase'],'source_hash':binding['source_hash'],'policy_hash':binding['policy_hash'],
                        'source_event':original['source_event'],'original_binding':binding,'original_sha256':digest(original),
                        'extraction':extraction,'retained_ideas':extraction['ideas'],'observed_at':now(),
                        'read_state':'observed-with-extraction-uncertainty' if extraction['status']=='unavailable' else 'observed',
                        'meaning':'Historical candidates only; current source may replace or withdraw their premises. New reviewed disposition is required; no original target or acquisition replay.'}
                    def retain():
                        previous=self.store.record_get('learning_response_recovery',key)
                        if previous is not None and previous!=recovered:
                            raise PolicyError('Original Learning response recovery was concurrently changed')
                        self.store.record('learning_response_recovery',key,recovered)
                    self.store._transaction(retain)
                required=self._carry_learning_ideas(required,extraction['ideas'])
                recovery_ref={'id':key,'sha256':digest(recovered)}
            event=self.store.events(task_id,after=original['source_event']['seq']-1)[0]
            if (event['hash']!=original['source_event']['hash']
                    or digest({k:v for k,v in event.items() if k not in {'seq','hash'}})!=event['hash']
                    or event['detail'].get('retained_ideas',[])!=original['retained_ideas']):
                raise PolicyError('LEARNING_PROVENANCE: Learning response source event changed')
            references.append({'source_event':original['source_event'],'original_binding':original['binding'],
                'learning_owner':event['detail'].get('learning_owner'),
                'learning_request_key':event['detail'].get('learning_request_key'),
                'response_ref':original['response_ref'],'recovery_ref':recovery_ref,
                'idea_ids':[i['id'] for i in self._carry_learning_ideas(original['retained_ideas'],(extraction or {}).get('ideas',[]))],
                'extraction_status':extraction.get('status') if extraction else 'schema-valid-rejected-proposal'})
        return {'ideas':required,'originals':references}

    def _prepare_learning_payload(self,task_id,payload,*,phase=None):
        operation_id=payload.get('operation',{}).get('id')
        if not operation_id:return payload
        history=self._learning_response_history(task_id,operation_id)
        if not history['originals']:return payload
        if payload.get('source_packet_id'):
            # The complete source was prepared before paging. Other focus pages
            # retain their own Ideas; only this page's newly rejected output may
            # add Ideas which were not yet in that original packet.
            packet=self.store.record_get('bounded_input',payload['source_packet_id'])
            task=self.store.get_task(task_id)
            if (not packet or packet['id']!=digest({k:v for k,v in packet.items() if k!='id'})
                    or packet['task_id']!=task_id or packet['source_hash']!=task['source_hash']
                    or packet['policy_hash']!=self.policy.hash
                    or packet['value'].get('operation',{}).get('id')!=operation_id):
                raise PolicyError('Historical Learning page has a stale, changed or foreign source packet')
            reserved={i['id'] for i in packet['value'].get('required_ideas',[])}
            focus={i['id'] for i in payload.get('required_ideas',[])}
            owner=self.bounded_judgments.learning_owner(payload,phase)
            request_key=digest({'task_id':task_id,'phase':phase,'role':task['actor'],
                'policy_hash':self.policy.hash,'source_hash':task['source_hash'],'payload':payload})
            owned={identity for ref in history['originals']
                if ref.get('learning_owner')==owner or ref.get('learning_request_key')==request_key
                for identity in ref['idea_ids']}
            required=[i for i in history['ideas'] if i['id'] in focus or (i['id'] not in reserved and i['id'] in owned)]
            return dict(payload,required_ideas=self._carry_learning_ideas(payload.get('required_ideas',[]),required))
        return dict(payload,required_ideas=self._carry_learning_ideas(payload.get('required_ideas',[]),history['ideas']),
            historical_response_evidence={'originals':history['originals'],
                'instruction':'These are historical candidates, not current instructions or adopted changes. Preserve each identity and meaning while deciding its disposition under the CURRENT user source and policy, including explicit withdrawal or supersession. Do not replay the original operation or acquisition.'})

    def _journal_judgment_result(self,task,operation,phase,assessments,actual_result,review):
        """Terminal control records retain every idea; they are not a new tool.

        Accepted method changes remain ordinary pending work. This journal does
        not label selected Skills used or claim an observed improvement benefit.
        """
        input_hash=digest({'operation':operation,'phase':phase,'assessments':assessments,'actual_result':actual_result,
            'review':review,'policy_hash':self.policy.hash,'source_hash':task['source_hash']})
        identity=operation['id']+':'+phase+':'+input_hash
        if self.store.record_get('judgment_result',identity):return
        episode={'id':identity,'task_id':task['id'],'operation_id':operation['id'],'created_at':now(),'operation':operation,
            'source_objective':task['objective'],'result':actual_result if 'status' in actual_result else {'status':'succeeded','data':actual_result},
            'learning':{'outcome_summary':phase+' actual reviewed outcome','classifications':['organize'],'ideas':[idea for a in assessments for idea in a['assessment']['ideas']]},
            'classification_reason':'Source-bound evaluation and control journaling for an already governed operation; no new procedure use/effect is inferred.'}
        def commit():
            self.store.record('episode',identity,episode)
            for a in assessments:
                for idea in a['assessment']['ideas']:
                    row=dict(idea,id=digest({'task':task['id'],'episode':identity,'target':a['target_id'],'idea':idea})[:32],task_id=task['id'],source_episode=identity,
                        created_at=episode['created_at'],source_operation_id=operation['id'],source_target_id=a['target_id'],
                        applicability=task['objective'],next_trigger='Next operation resolving this exact pending judgment or related task',
                        verification_refs=[],actual_use_refs=[])
                    row.update(self.knowledge.idea_disposition(task,idea))
                    self.store.record('idea',row['id'],row)
                    if row['status']=='deferred':
                        self.store.record('deferred_idea_index',row['id'],self.knowledge._deferred_source(row))
            self.store.record('judgment_result',identity,{'id':identity,'input_hash':input_hash,'policy_hash':self.policy.hash,
                'source_hash':task['source_hash'],'assessments':assessments,'actual_result':actual_result,'review':review,
                'task_id':task['id'],'operation_id':operation['id'],'phase':phase,'episode_sha256':digest(episode)})
        self.store._transaction(commit)

    def _pending_judgments(self,task_id):
        return [x for x in self.store.records('judgment_correction') if x['task_id']==task_id and x['status']=='pending']

    def _judgment_source(self,task,correction):
        if correction.get('task_id')!=task['id']:
            raise PolicyError('Judgment source belongs to another task')
        consumer=correction.get('consumer')
        if consumer and consumer.get('stage')=='uncommitted_learning':
            return self.web_judgments.learning_correction_source(task,correction)
        if consumer and consumer.get('kind')=='web_acquisition':
            return self.web_judgments.correction_source(task,correction)
        try:original=self.store.get_operation(correction['operation_id'])
        except KeyError:
            if consumer is not None:raise PolicyError('Judgment original operation is absent')
            return self.web_judgments.correction_source(task,correction)
        expected={'kind':'operation','operation_id':original['operation']['id'],
                  'operation_sha256':digest(original['operation'])}
        if original['task_id']!=task['id'] or (consumer is not None and consumer!=expected):
            raise PolicyError('Judgment original operation is foreign or changed')
        return {'consumer':expected,'operation':original['operation'],'result':original.get('result')}

    @staticmethod
    def _judgment_evidence_contract():
        return {'instruction':'Use judgment_resolve {review_id,expected_hash,reason,evidence_operation_ids:[...]} after actual governed diagnosis/correction. Evidence must be an owned subsequent observed known result. No particular tool kind or completed primary artifact is required: an independently useful read/diagnostic/confirmation can be structurally eligible. The ordinary independent review must still assess whether those exact results answer each objection; eligibility alone is not correction proof. Preserve the original outcome and return to its parked parent.',
                'requires_primary_artifact_completion':False,
                'requires_specific_operation_kind':False,
                'semantic_relevance':'Decided by the existing independent operation review and parent disposition, never by an evidence ID or success label.'}

    @staticmethod
    def _judgment_evidence_eligible(task,correction,row):
        result=row.get('result')
        return bool(row.get('task_id')==task['id'] and result
            and result.get('operation_id')==row['operation']['id']
            and result.get('status') in {'succeeded','failed'} and result.get('effect')!='unknown'
            and isinstance(result.get('started_at'),str) and result['started_at']>=correction['created_at'])

    def _judgment_evidence(self,task,correction):
        return {'eligible_evidence':[{'operation_id':row['operation']['id'],'kind':row['operation']['kind'],
                    'result_sha256':digest(row['result']),'status':row['result']['status'],
                    'effect':row['result']['effect'],'started_at':row['result']['started_at']}
                for row in self.store.operations(task['id']) if self._judgment_evidence_eligible(task,correction,row)]}

    def _knowledge_judgment_facts(self,task,consumer,*,source=None):
        """Bind timing and controller behavior without rewriting old judgments."""
        facts={'next_use_trigger':{'role':'knowledge selection metadata',
                                  'dispatches_operations_or_urls':False},
               'application_meaning':'A Knowledge commit is not evidence of subsequent method use, '
                   'artifact delivery or verified benefit. Original proposal statements predate '
                   'their accepting review and their eventual application.'}
        if consumer.get('kind')!='web_acquisition' or consumer.get('stage')=='uncommitted_learning':
            return facts
        source=source if source is not None else self.web_judgments.applied_source(task,consumer['acquisition_id'])
        if source['consumer']!=consumer:raise PolicyError('Knowledge judgment consumer changed')
        saved=self.store.record_get('web_work',consumer['web_work_id'])
        provenance=self.web_judgments._accepted_learning_context(saved)['accepted_learning_provenance']
        index=provenance['accepted_attempt_index']
        facts['application']={k:consumer[k] for k in ('application_id','application_input_hash','episode_id','episode_sha256')}
        if index is not None:
            attempt=saved['learning_attempts'][index]
            prior=saved['learning_attempts'][index-1] if index else None
            facts['review_lineage']={'web_work_id':saved['id'],'attempt_index':index,
                'proposal_receipt':deepcopy(attempt.get('proposal_receipt')),
                'proposal_created_at':attempt.get('created_at'),
                'accepted_learning_sha256':digest(attempt['learning']),
                'predecessor_review':deepcopy(prior.get('review')) if prior else None,
                'accepting_review_sha256':digest(attempt.get('review')),
                'meaning':'predecessor_review was available to this proposal; accepting_review was '
                    'produced afterward. Compare a historical statement to its actual referent, '
                    'not automatically to the later accepting review.'}
        retained=[r for r in self.store.records('web_exchange')
                  if r.get('record',{}).get('id')==consumer['acquisition_id']]
        facts['retained_exchanges']=[{'id':r['id'],'stage':r['stage'],
            'response_sha256':r.get('record',{}).get('sha256'),
            'protected_response_present':bool(r.get('protected_exchange')),
            'extraction':'Not established by transport retention; normal extraction/acceptance must still complete.'}
            for r in retained]
        return facts

    def _record_only_learning(self,task,operation,phase,outcome,result,context,consumer):
        """Consume existing accepted meaning once; confirm only its storage."""
        if not getattr(self.policy,'role_scoped_enabled',False):return None
        if 'learning' not in context:return None  # Legacy contexts keep their original review route.
        learning=Learning.model_validate(context['learning'])
        if not self.knowledge.record_only_candidate(learning):return None
        base=phase.split(':',1)[0]
        # Any old applied work keeps its exact recovery route, including an
        # unobserved send or a capture made before dispatch. Read IDs only.
        with self.store.lock:
            old=self.store.db.execute("SELECT 1 FROM records WHERE kind='applied_knowledge_review' AND substr(id,1,?)=? LIMIT 1",
                (len(operation['id']+':'+base),operation['id']+':'+base)).fetchone()
            captures=self.store.db.execute("""SELECT
                json_extract(body,'$.operation_id'),json_extract(body,'$.payload.operation.id'),
                json_extract(body,'$.payload.bundle.operation.id'),json_extract(body,'$.canonical_input.operation.id'),
                json_extract(body,'$.canonical_input.bundle.operation.id')
                FROM records WHERE kind IN ('bounded_model_call','semantic_wire_request')
                AND json_extract(body,'$.task_id')=? AND substr(json_extract(body,'$.phase'),1,?)=?""",
                (task['id'],len(base),base)).fetchall()
        if old or any(operation['id'] in row or all(x is None for x in row) for row in captures):return None
        if consumer.get('kind')=='web_acquisition':
            saved=self.store.record_get('web_work',consumer.get('web_work_id'))
            if not saved or saved.get('learning_application_binding')!=self.web_judgments._binding(task):return None
            accepted=self.web_judgments._accepted_learning_context(saved)
            provenance=accepted['accepted_learning_provenance']
            if (provenance['accepted_attempt_index'] is None or not provenance['review_input_sha256']
                    or not provenance['disposition_input_sha256']):return None
            original=self.web_judgments.applied_source(task,operation['id'])
            if (original['consumer']!=consumer or original['operation']!=operation or original['result']!=result
                    or original['outcome']!=outcome or original['learning']!=learning.model_dump()
                    or any(context.get(k)!=v for k,v in accepted.items())):
                raise PolicyError('Control Learning differs from its accepted Web provenance')
            selected=[s['id'] for s in original['selection']['selected']]
        elif consumer=={'kind':'operation','operation_id':operation['id'],'operation_sha256':digest(operation)}:
            row=self.store.get_operation(operation['id'])
            if (row.get('source_hash')!=task['source_hash'] or row.get('policy_hash')!=self.policy.hash
                    or row.get('result')!=result):return None
            row=self.knowledge._reviewed_row(task['id'],operation['id'],post=True)
            review=row['post_review'];stored=self.store.record_get('review',review.get('id'))
            if stored is None:return None  # Missing legacy provenance is not reconstructed.
            provenance=context.get('accepted_learning_provenance',{})
            if (row['operation']!=operation or row['post_bundle'].get('learning')!=learning.model_dump()
                    or context.get('accepted_learning_review')!=review
                    or context.get('accepted_learning_disposition')!=review['disposition']
                    or stored.get('task_id')!=task['id'] or any(stored.get(k)!=v for k,v in review.items())
                    or provenance.get('bundle_sha256')!=digest(row['post_bundle'])
                    or provenance.get('learning_sha256')!=digest(learning.model_dump())
                    or provenance.get('review_id')!=review['id']):
                raise PolicyError('Control Learning differs from its accepted operation provenance')
            selected=[s['id'] for s in row['pre_bundle']['skills']['selected']]
        else:return None
        confirmation=self.knowledge.confirm_record_only_application(task,operation,learning,result,selected,outcome)
        if confirmation is None:return None
        if consumer['kind']=='operation' and (provenance.get('application_id')!=confirmation['application_id']
                or provenance.get('application_input_hash')!=confirmation['application_input_hash']):
            raise PolicyError('Control Learning accepted application reference changed')
        return dict(confirmation,accepted_learning_provenance=deepcopy(provenance))

    async def _judge_applied_knowledge(self,task,operation,phase,choices,outcome,result,web,*,context,consumer):
        confirmed=self._record_only_learning(task,operation,phase,outcome,result,context,consumer)
        if confirmed is not None:return {'record_only':confirmed,'consumer':consumer}
        review_input={'contract':'applied-knowledge-review-v2','operation':operation,
            'actual_result':outcome,'original_operation_result':result,
            'accepted_input':context,'choices':choices,'consumer':consumer,
            'history':self._history_lookup(task['id']),
            'timing':'after the actual committed knowledge application',
            'result_semantics':'Judge the committed Knowledge outcome against the accepted Learning and exact original operation/result. Transport is not task-artifact delivery. outcome.skills includes newly created Skills; skill_transitions describes changes to preexisting selected/update targets only. Empty transitions do not erase a created Skill, and projection/application receipts do not establish verified benefit.'}
        input_hash=digest({'review_input':review_input,'web':web,
            'policy_hash':self.policy.hash,'source_hash':task['source_hash']})
        key=operation['id']+':'+phase+':'+input_hash
        saved=self.store.record_get('applied_knowledge_review',key)
        previous_input=review_input
        # A completed historical judgment keeps its original input definition.
        # Supply additional code-owned facts only to a genuinely new judgment.
        if not saved:
            review_input=dict(review_input,contract='applied-knowledge-review-v3',
                controller_facts=self._knowledge_judgment_facts(task,consumer))
            input_hash=digest({'review_input':review_input,'web':web,
                'policy_hash':self.policy.hash,'source_hash':task['source_hash']})
            key=operation['id']+':'+phase+':'+input_hash
            saved=self.store.record_get('applied_knowledge_review',key)
        if saved:
            if saved['outcome_hash']!=digest(outcome):raise PolicyError('Previously assessed knowledge outcome changed')
            return saved
        if not choices:
            previous_review=self.bounded_judgments.review_bundle_input(
                operation,dict(previous_input,assessments=[]),web)
            if self.bounded_judgments._review_owner(task['id'],phase+'_review',previous_review) is not None:
                review_input=previous_input
        assessed=await self._assess_targets(task['id'],phase+'_choices_after',choices,review_input,
                                           previous=(choices,previous_input))
        if choices:
            review_input=self.bounded_judgments.assessment_context(
                task['id'],phase+'_choices_after',choices,assessed)
        # Partial successful leaves keep their original request definition,
        # including the input used by the later review and its aggregate key.
        input_hash=digest({'review_input':review_input,'web':web,
            'policy_hash':self.policy.hash,'source_hash':task['source_hash']})
        key=operation['id']+':'+phase+':'+input_hash
        saved=self.store.record_get('applied_knowledge_review',key)
        if saved:
            if saved['outcome_hash']!=digest(outcome):raise PolicyError('Previously assessed knowledge outcome changed')
            return saved
        reviewed=await self._review_bundle(task['id'],operation,phase,dict(review_input,assessments=assessed),web)
        saved={'id':key,'input_hash':input_hash,'policy_hash':self.policy.hash,'source_hash':task['source_hash'],
            'outcome_hash':digest(outcome),'assessments':assessed,'review':reviewed,
            'input_contract':review_input['contract'],'consumer':consumer}
        def commit():
            self._journal_judgment_result(task,operation,phase,assessed,result,reviewed)
            self.store.record('applied_knowledge_review',key,saved)
            if reviewed['disposition']['verdict']!='proceed':
                correction={'id':reviewed['id'],'task_id':task['id'],'operation_id':operation['id'],'status':'pending',
                    'created_at':now(),'review':reviewed,'actual_result':outcome,'consumer':consumer,
                    'instruction':'Resolve the referenced objections through ordinary governed work, then judgment_resolve with observed evidence. Distinguish a current after-assessment, an interpretation of immutable accepted input, a committed outcome and remaining parent work. Correct the actual defect supported by the review; neither a reference nor an objection proves the committed mutation is defective. Preserve original captures and never replay the prior mutation.'}
                correction['hash']=digest(correction)
                self.store.record('judgment_correction',correction['id'],correction)
        self.store._transaction(commit)
        return saved

    async def _web_acquisition_evaluate(self,task_id,operation,stage,detail,research_choice=None):
        self.web_judgments.require_parent_return(task_id,operation['id'])
        try:outcome=await self.web_judgments.evaluate(task_id,operation,stage,detail,research_choice)
        except WebSelectionChanged as error:
            if stage!='before' or not self.web_judgments.prepared_exchange(task_id,operation,detail):raise
            self.store.event(task_id,'web_selection','refused_before_dispatch',{
                'acquisition_id':detail['id'],'reason':str(error),'network_dispatched':False})
            raise WebRevisionNeeded(str(error),detail['id'],stage,'revise') from error
        if outcome.get('pending_applied_reviews'):
            raise WebCorrectionNeeded(operation['id'],outcome['pending_applied_reviews'])
        if outcome['disposition']['verdict']!='proceed':
            raise WebRevisionNeeded('Web acquisition requires a new reviewed proposal: '+outcome['disposition']['rationale'],
                detail['id'],stage,outcome['disposition']['verdict'])
        return outcome

    async def _research(self,task_id,operation,phase,target):
        self.web_judgments.require_parent_return(task_id,operation['id'])
        binding=(self.store.get_task(task_id)['source_hash'],self.policy.hash,digest(operation))
        feedback=None;seen=set()
        while True:
            try:return await self._research_attempt(task_id,operation,phase,target,feedback)
            except WebRevisionNeeded as error:
                current=self.store.get_task(task_id)
                if (current['source_hash'],self.policy.hash,digest(operation))!=binding:
                    raise RevisionNeeded('Task source or policy changed during local Web correction; reprepare the parent proposal') from error
                try:row=self.store.get_operation(operation['id'])
                except KeyError:row=None
                if row and (row['task_id']!=task_id or row['operation']!=operation or row.get('source_hash')!=binding[0]):
                    raise RevisionNeeded('The parent operation changed during local Web correction') from error
                if error.verdict!='revise':raise CycleHeld(str(error)) from error
                feedback={'scope':'this Web acquisition only; retain the unchanged parent operation',
                    'acquisition_id':error.acquisition_id,'stage':error.stage,'query':error.query,
                    'reason':str(error),'parent_operation_id':operation['id'],
                    'instruction':'Address this exact review with a newly reviewed local query or selection. Prior exchanges and applications remain stored; repeating a query reuses its exact available response, never reconstructs missing bytes. If this evidence requires changing the parent operation or source, keep it held for that separate governed revision.'}
                fingerprint=digest(feedback)
                if fingerprint in seen:
                    raise CycleHeld('Local Web revision repeated unchanged after actual feedback: '+str(error)) from error
                seen.add(fingerprint)
                self.store.event(task_id,'web_revision','required',feedback)

    def _research_choice(self,task,operation,phase,work):
        """Read the existing full choice only within its saved collection binding."""
        if (not work or work.get('task_id')!=task['id'] or work.get('operation')!=operation
                or work.get('source_hash')!=task['source_hash'] or work.get('policy_hash')!=self.policy.hash
                or work.get('detail',{}).get('phase')!=phase):return None
        query=work.get('research_choice')
        if not query:return None
        value=ResearchQuery.model_validate(query).model_dump()
        if value['query']!=work['detail'].get('collection_context',{}).get('query'):
            raise PolicyError('Saved research choice differs from its collection')
        return value

    def _retain_research_choice(self,task,operation,phase,query,retained=None):
        """The normal and correction routes use the same durable return slot."""
        value={'operation_sha256':digest(operation),'source_hash':task['source_hash'],
            'policy_hash':self.policy.hash,'query':query.model_dump()}
        if retained is not None and all(retained.get(k)==v for k,v in value.items()):return retained
        value['id']=digest({'task_id':task['id'],'operation_id':operation['id'],'phase':phase,**value})
        def commit():
            current=self.store.get_task(task['id'])
            if current['source_hash']!=task['source_hash'] or current['policy_hash']!=task['policy_hash']:
                raise RevisionNeeded('Research source changed before its return was saved')
            state=deepcopy(current['state']);returns=state.setdefault('web_research_returns',{})
            key=operation['id']+':'+phase;previous=returns.get(key)
            if previous and previous!=value:
                self.store.record('web_research_return',previous['id'],dict(previous,status='continued',next_return_id=value['id']))
            returns[key]=value;self.store.update_task(task['id'],state=state)
            return value
        return self.store._transaction(commit)

    def _restore_research_choice(self,task,operation,phase):
        # Older ordinary attempts already stored the complete choice in each
        # acquisition. Reuse it without manufacturing a model-input receipt.
        with self.store.lock:
            rows=self.store.db.execute("SELECT body FROM records WHERE kind='web_work' AND json_extract(body,'$.task_id')=? AND json_extract(body,'$.operation.id')=? AND json_extract(body,'$.detail.phase')=?",
                (task['id'],operation['id'],phase)).fetchall()
        choices={}
        for row in rows:
            value=self._research_choice(task,operation,phase,json.loads(row['body']))
            if value is not None:choices[digest(value)]=value
        if len(choices)>1:
            raise PolicyError('Saved research choices are ambiguous; preserve their revision lineage before continuation')
        if not choices:return None
        # An old revision without an explicit current return must not silently
        # turn its rejected choice into a new query or choose a latest record.
        for event in self.store.events(task['id']):
            detail=event['detail']
            if event['stage']!='web_revision' or detail.get('parent_operation_id')!=operation['id']:continue
            # before/after names the acquisition judgment, not its research
            # phase. Resolve the existing acquisition instead of rejecting all
            # phases of an operation because one had a local revision.
            stage=detail.get('stage');acquisition_id=detail.get('acquisition_id')
            saved=(self.store.record_get('web_work',acquisition_id+':'+stage)
                if isinstance(acquisition_id,str) and stage in {'before','after'} else None)
            saved_phase=(saved or {}).get('detail',{}).get('phase')
            choice=(self._research_choice(task,operation,saved_phase,saved)
                if saved_phase in {'pre','post','final'} else None)
            if (not choice or saved['detail'].get('id')!=acquisition_id
                    or choice['query']!=detail.get('query') or saved_phase==phase):
                raise PolicyError('Saved research revision requires its exact continuation')
        return ResearchQuery.model_validate(next(iter(choices.values())))

    async def _research_attempt(self,task_id,operation,phase,target,revision_feedback=None):
        configuration=getattr(getattr(self.web,'settings',None),'public',lambda:{})()
        task=self.store.get_task(task_id);return_key=operation['id']+':'+phase
        retained=task['state'].get('web_research_returns',{}).get(return_key)
        query=None
        if (retained and revision_feedback is None and retained['operation_sha256']==digest(operation)
                and retained['source_hash']==task['source_hash'] and retained['policy_hash']==self.policy.hash):
            query=ResearchQuery.model_validate(retained['query'])
        elif revision_feedback is None:
            query=self._restore_research_choice(task,operation,phase)
        feedback=None
        while query is None:
            public_urls=configuration.get('web_provider')=='public_url'
            payload={'operation':operation,'target':target,
                'local_revision_feedback':revision_feedback,
                'previous_acquisition_reviews':[x for x in self.store.records('web_review') if x['task_id']==task_id and x['disposition']['verdict']!='proceed'],
                'instruction':('The installed Web service is PUBLIC_URL, not keyword search. query MUST consist only of authoritative public https:// URLs separated by spaces. Do not return keywords. Choose source URLs relevant to this target and contrary evidence; the controller will actually retrieve them.' if public_urls else 'Choose a specific Web query to investigate this target and contrary evidence.')+' Exclude private task text, identifiers, paths and keys. Never invent retrieved evidence.',
                'actual_previous_format_error':feedback,'web_configuration':configuration}
            payload=await self.bounded_judgments.research_payload(task_id,phase+'_research_query',payload)
            query=await self._call(task_id,phase+'_research_query',payload,ResearchQuery)
            if not public_urls or re.fullmatch(r'https://\S+(?:\s+https://\S+)*',query.query):break
            feedback={'invalid_proposal':query.model_dump(),'reason':'Configured public_url acquisition accepts public HTTPS URLs only; no DNS/HTTP acquisition was attempted. Produce a new proposal in that actual supported format.'}
            self.store.event(task_id,phase+'_research_query','rejected_format',feedback)
            query=None
        if not query.private_data_excluded or redact(query.query)!=query.query:
            raise PolicyError('Web query privacy has not been established')
        retained=self._retain_research_choice(task,operation,phase,query,retained)
        self.store.event(task_id,phase+'_web','started',{'query':query.query,'operation_id':operation['id']})
        try:
            result=await self.web.collect(query.query,phase=phase,task_id=task_id,operation_id=operation['id'],
                evaluate=lambda stage,detail:self._web_acquisition_evaluate(task_id,operation,stage,detail,query.model_dump()))
        except Exception as error:
            if isinstance(error,WebRevisionNeeded):error.query=query.query
            self.store.event(task_id,phase+'_web','failed',{'type':type(error).__name__,'message':str(error)})
            raise
        if not result.get('sources'):
            raise PolicyError('Required Web collection produced no acquired evidence')
        self.store.event(task_id,phase+'_web','succeeded',result)
        if retained:
            current=self.store.get_task(task_id);state=deepcopy(current['state'])
            if state.get('web_research_returns',{}).get(return_key)!=retained:
                raise PolicyError('Research continuation owner changed before its result returned')
            state['web_research_returns'].pop(return_key)
            if not state['web_research_returns']:state.pop('web_research_returns')
            self.store.record('web_research_return',retained['id'],dict(retained,status='consumed',result_sha256=digest(result)))
            self.store.update_task(task_id,state=state)
        return result

    @staticmethod
    def _validate_disposition(review,disposition,sources):
        validate_disposition({'review':review.model_dump(),'sources':sources},disposition)

    @staticmethod
    def _carry_learning_ideas(required,observed):
        # Existing identity/meaning is immutable. An invalid attempted rewrite
        # remains in rejected_response; distinct newly observed candidates must
        # receive a disposition in the corrected response, including on resume.
        retained={i['id']:deepcopy(i) for i in required}
        for idea in observed:
            retained.setdefault(idea['id'],deepcopy(idea))
        return list(retained.values())

    @staticmethod
    def _validate_ideas(required,learning):
        actual={x.id:x for x in learning.ideas}
        if len(actual)!=len(learning.ideas):raise PolicyError('Duplicate improvement identity')
        for idea in required:
            if idea['id'] not in actual or actual[idea['id']].proposal!=idea['proposal'] or actual[idea['id']].target!=idea['target']:
                raise PolicyError('An assessed improvement candidate was dropped or silently changed')

    async def _review_bundle(self,task_id,operation,phase,bundle,web,additional_reviews=None,**kwargs):
        return await self.bounded_judgments.review_bundle(task_id,operation,phase,bundle,web,additional_reviews,**kwargs)

    def _current_cycle(self,task_id):
        """Use the durable current owner, including after adoption closed its cycle."""
        def commit():
            task=self.store.get_task(task_id)
            if task['state'].get('cycle_id'):return task
            return self.store.update_task(task_id,state=dict(task['state'],cycle_id=uuid4().hex))
        return self.store._transaction(commit)

    async def _initialize_task(self,task):
        if task['state'].get('plan'):return
        task=self._current_cycle(task['id'])
        if task['state'].get('operation_id'):
            graph=await self._graph()
            await graph.ainvoke({'task_id':task['id'],'operation_id':task['state']['operation_id']},
                {'configurable':{'thread_id':task['id']+':'+task['state']['cycle_id']}})
            return
        unread=[]
        prepared=prepared_source_texts(self.store,task)
        for source in task['source_history']:
            if source['kind']=='attachment' and source['status']=='pending' and source['id'] not in prepared:
                progress=self.store.record_get('source_read_progress',source['id']) or {'next_offset':0,'complete':False}
                if not progress['complete']:unread.append((source,progress))
        if unread:
            source,progress=unread[0]
            proposal=Operation(kind='source_read',args={'source_id':source['id'],'expected_hash':source['sha256'],'offset':progress['next_offset']},
                purpose='Read the exact newly submitted attachment before classifying its meaning',expected_result='Actual source bytes and range evidence',
                decisions=[{'id':'source-read','statement':'Read the next unobserved range of this task-owned immutable attachment','rationale':'Source must be understood before reconciliation; no workspace mutation'}])
            self.store.save_operation(task['id'],proposal.model_dump(),policy_hash=self.policy.hash)
            state=dict(task['state'],operation_id=proposal.id,cycle_id=uuid4().hex)
            self.store.update_task(task['id'],state=state)
            graph=await self._graph()
            await graph.ainvoke({'task_id':task['id'],'operation_id':proposal.id},{'configurable':{'thread_id':task['id']+':'+state['cycle_id']}})
            return
        if self.web_judgments.correction_frontier(task['id']) or task['state'].get('web_correction_returns'):
            # A first plan may itself be the parked consumer. Correction still
            # uses the normal proposal/admission/graph, without inventing a plan.
            graph=await self._graph()
            await graph.ainvoke({'task_id':task['id'],'operation_id':''},
                {'configurable':{'thread_id':task['id']+':'+task['state']['cycle_id']}})
            return
        preparation=self.source_preparation.pending(task)
        if preparation['pending']:
            capabilities=self.executor.catalog(workspace=Path(task['workspace']))
            if inspect.isawaitable(capabilities):capabilities=await capabilities
            proposal=await self._call(task['id'],'source_preparation_proposal',{
                'source_preparation':preparation,'operations':self._history_context(task['id']),
                'controller_guide':self._controller_guide(),'capability_guide':capabilities,
                'knowledge':self._knowledge_context(task),'required_judgment_corrections':self._pending_judgments(task['id']),
                'instruction':'Prepare ONE necessary action to understand the pending exact attachment, or apply an explicit later user withdrawal. The opaque original remains unadopted. Use only allowed_preparation_operations, preserve all existing acceptance/effects, and return to normal source planning after confirmed extraction or withdrawal. Every proposed operation and decision receives the complete normal review/learning cycle.'},Operation)
            if proposal.kind not in preparation['allowed_preparation_operations']:
                raise PolicyError('Pending opaque source permits preparation only; no ordinary source adoption')
            if not getattr(self.gateway,'supports_semantic_wire',False):proposal.id=uuid4().hex
            self.store.save_operation(task['id'],proposal.model_dump(),policy_hash=self.policy.hash)
            state=dict(task['state'],operation_id=proposal.id,cycle_id=uuid4().hex)
            self.store.update_task(task['id'],state=state)
            graph=await self._graph()
            await graph.ainvoke({'task_id':task['id'],'operation_id':proposal.id},
                {'configurable':{'thread_id':task['id']+':'+state['cycle_id']}})
            return
        knowledge=self._knowledge_context(task)
        deferred=self.knowledge.deferred_for_planning(task)
        capabilities=self.executor.catalog(workspace=Path(task['workspace']))
        if inspect.isawaitable(capabilities):capabilities=await capabilities
        plan=await self._call(task['id'],'task_plan',{'knowledge':knowledge,'source_task_id':task['id'],
            'available_operations':self._controller_guide(),'executor_capabilities':capabilities,'workspace':task['workspace'],
            'runtime_roles':{'parent':'Owns final opinion disposition under current policy','reviewer':'Actual tool-free model calls are supplied by controller at each required stage','web':'Actual public acquisition with finite pre/post evaluation','worker':'Independent child workspace when useful'},
            'required_policy_ids':[*self.policy.conditions,*self.policy.rules],
                'previous_attempts':self._history_context(task['id']),
                'deferred_for_planning':deferred,
                'deferred_contract':'Use deferred_index_hash=index_hash and list every candidate once in deferred_considerations as {idea_id,source_hash,disposition:include|exclude,reason,planned_application}. Decide relevance from its source/applicability; a shared candidate is not new task authority. An empty index has no operative considerations.',
            'instruction':'Extract complete source outcomes, acceptance, actual user prohibitions, deliverables and required capabilities. Do not invent prohibitions against required Web/review/knowledge support. Preserve the actual user goal; support work is not delivery. Phases describe task deliverable steps; they never replace or relax the fixed controller cycle. Current D-02 Web collection applies even to simple local file tasks; not_applicable/optional archival exceptions are superseded. Unknown recurrence goes to provisional knowledge under Q04. Leave source_coverage empty: the controller supplies the complete immutable source inventory, which is bookkeeping, never an applicability waiver or semantic PASS. State every operative task-specific choice in the other fields.'},TaskPlan)
        # This inventory is derived from actual policy bytes, not 254 repeated
        # model decisions claiming to have obeyed each clause. Task-specific
        # choices remain explicit and receive their own pre/post assessments.
        plan.source_coverage=[{'id':identity,'binding':'Complete current mandatory source; applicability and evidence remain subject to its exact text, never a PASS label'} for identity in [*self.policy.conditions,*self.policy.rules]]
        if not deferred['candidates']:
            plan.deferred_index_hash=deferred['index_hash']
            plan.deferred_considerations=[]
        proposal=Operation(kind='plan_task',args={'plan':plan.model_dump()},purpose='確定前に元の依頼全体と必要能力・工程を整理する',expected_result='Reviewed source-bound plan with all original outcomes retained',
            decisions=[{'id':x['id'],'statement':x['statement'],'rationale':plan.rationale} for x in task_plan_targets(plan)])
        self.store.save_operation(task['id'],proposal.model_dump(),policy_hash=self.policy.hash)
        state=dict(task['state'],operation_id=proposal.id,cycle_id=uuid4().hex)
        self.store.update_task(task['id'],state=state,policy_hash=self.policy.hash)
        graph=await self._graph()
        await graph.ainvoke({'task_id':task['id'],'operation_id':proposal.id},
            {'configurable':{'thread_id':task['id']+':'+state['cycle_id']}})

    def _history_context(self,task_id):
        # Lossless raw records remain in Store and can be requested by history_read.
        # This projection keeps every operation identity, first fault and result;
        # it does not pretend that a model has read omitted historical drafts.
        rows=[]
        for o in self.store.operations(task_id)[-16:]:
            identity=o['operation']['id'];result=o['result']
            rows.append({'id':identity,'operation':dict(o['operation'],args=self._preview(o['operation']['args'],2400,identity+':operation/args'),
                    decisions=self._preview(o['operation']['decisions'],1200,identity+':operation/decisions')),
                'status':o['status'],'source_hash':o.get('source_hash'),
                'result':self._preview(result,3600,identity+':result'),
                'actual_result_status':result.get('status') if result else None,'effect':result.get('effect') if result else None,
                'pre_disposition':self._preview(o.get('pre_review',{}).get('disposition'),1500,identity+':pre_review'),
                'post_disposition':self._preview(o.get('post_review',{}).get('disposition'),1500,identity+':post_review'),
                'revision_reason':self._preview(o.get('revision_reason'),2400,identity+':revision_reason'),
                'completion_deferred':self._preview(o.get('completion_deferred'),1500,identity+':completion_deferred'),
                'full_history_ref':identity,'full_history_sha256':digest(o)})
        return rows

    @staticmethod
    def _preview(value,limit,reference):
        encoded=canonical(value)
        if len(encoded)<=limit:return value
        return {'projection_only':True,'full_value_read':False,'preview':encoded[:limit],
            'full_value_chars':len(encoded),'full_value_sha256':digest(value),'read_ref':reference,
            'instruction':'This is an explicitly partial preview. Retrieve exact original ranges before relying on omitted content.'}

    def _history_lookup(self,task_id):
        index=self.store.operation_index(task_id)
        pending=[o['id'] for o in index if o['status']!='cycle_complete' or o.get('effect')=='unknown']
        return {'operation_count':len(index),'current_observation_hash':digest(index),
            'pending_count':len(pending),'pending_ids':pending[-24:],'pending_ids_complete':len(pending)<=24,
            'pending_operations':[o for o in index if o['id'] in pending[-24:]],
            'namespace':'Operation lifecycle only. These IDs are not source_context.pending_ids or pending Knowledge corrections. A revision_required/superseded unstarted proposal has no observed target effect; an executing/result gap remains unknown. Full original reasons/results are available through each row read_args.',
            'projection_scope':'Latest 16 operations; full immutable history remains available, never claimed read.',
            'read_args':{'mode':'index','offset':0,'limit':20},
            'instruction':'Use read_args to obtain the first immutable index. current_observation_hash describes this current preview and is not an expected_hash argument. The actual index response provides its own index_hash, snapshot_id and complete next_args. Preserve those arguments while reading all required pages.'}

    def _history_index(self,task_id,rows=None,snapshot_id=None):
        return [{'id':r['operation']['id'],'kind':r['operation']['kind'],'status':r['status'],
            'effect':(r.get('result') or {}).get('effect'),'sha256':digest(r),
            'read_args':{'operation_ids':[r['operation']['id']],'expected_hash':digest([r]),'offset':0,'max_chars':24000,
                **({'snapshot_id':snapshot_id} if snapshot_id else {})}}
            for r in (self.store.operations(task_id) if rows is None else rows)]

    def _history_snapshot(self,task_id,identity=None):
        if identity:
            saved=self.store.record_get('history_snapshot',identity)
            if not saved or saved['task_id']!=task_id or digest(saved['rows'])!=saved['rows_hash']:
                raise PolicyError('History snapshot is absent, changed or foreign')
            return saved
        rows=self.store.operations(task_id);sha=digest(rows);identity=task_id+':'+sha
        saved=self.store.record_get('history_snapshot',identity)
        if saved:return saved
        saved={'id':identity,'task_id':task_id,'rows_hash':sha,'rows':rows,'observed_at':now(),
            'scope':'Immutable observation cut; later operation state remains separate current evidence.'}
        self.store.record('history_snapshot',identity,saved)
        return saved

    def _final_evidence(self,task,children):
        """Freeze original records for the independent completion consumer.

        Ordinary navigation previews are deliberately unsuitable here. Keep all
        operation results, contrary observations and source text at one cut;
        bounded transport supplies originals when that cut exceeds one request.
        """
        def history(identity):
            snapshot=self._history_snapshot(identity)
            return {'snapshot_id':snapshot['id'],'rows_hash':snapshot['rows_hash'],
                    'operations':[dict(row,id=row['operation']['id']) for row in snapshot['rows']]}
        return {'task_id':task['id'],'source_hash':task['source_hash'],'policy_hash':self.policy.hash,
                'source':self._source_context(task),**history(task['id']),
                'children':[{'task':child,'source':self._source_context(child),**history(child['id'])}
                            for child in children],
                'scope':'Complete stored source/operation/result observation cut. Unobserved artifact content and external behavior remain unobserved, never inferred from a status or hash.'}

    def _source_context(self,task):
        value=source_contract(task)
        texts=self.store.source_texts(task)
        prepared=prepared_source_texts(self.store,task)
        ready_ids=set(prepared)
        for source in task['source_history']:
            progress=self.store.record_get('source_read_progress',source['id']) or {}
            if progress.get('text_decoded') and progress.get('complete'):ready_ids.add(source['id'])
        value['attachment_sources']=[{'id':s['id'],'sha256':s['sha256'],'text':texts[s['id']],
            'read_progress':self.store.record_get('source_read_progress',s['id']),
            'extraction':self.store.record_get('source_extraction',s['id']) if s['id'] in prepared else None,
            'scope':'Complete decoded source text supplied to this independent call; read receipts do not prove semantic correctness.'}
            for s in task['source_history'] if s['kind']=='attachment'
            and s.get('classification')!='withdraw'
            and s['id'] in ready_ids]
        return value

    def _source_boundary(self,task):
        if not any(s['status']=='pending' for s in task['source_history']):return task
        state=dict(task['state'])
        if not state.get('plan') and state.get('source_update_pending')==task['source_hash']:return task
        state.pop('plan',None);state['source_update_pending']=task['source_hash']
        identity=state.get('operation_id')
        if identity:
            old=self.store.get_operation(identity)
            if old.get('source_hash')!=task['source_hash'] and old['result'] is None and not self.store.operation_started_without_result(old):
                self.store.update_operation(identity,status='superseded',revision_reason='New source recovered at safe boundary; no old permit reuse')
                state.pop('operation_id',None);state.pop('cycle_id',None)
        return self.store.update_task(task['id'],state=state)

    def _web_correction_boundary(self,task_id):
        """Park only a dependent parent; restore its exact cycle after return.

        The stack handles a corrective operation's own Web correction. It is
        persisted in the same Store as operations, so ordinary run/resume and a
        reopened process select the same governed work before an initial plan.
        """
        self.web_judgments.recover_reviewed_returns(task_id)
        def commit():
            task=self.store.get_task(task_id);state=deepcopy(task['state'])
            frontier=self.web_judgments.correction_frontier(task_id)
            blocked={c['consumer']['parent_operation_id'] for c in frontier}
            frames=state.get('web_correction_returns',[])
            identity=state.get('operation_id')
            if identity in blocked:
                row=self.store.get_operation(identity)
                if row['task_id']!=task_id:raise PolicyError('Correction parent is foreign')
                if any(f['operation_id']==identity for f in frames):
                    raise PolicyError('A parked correction parent already has an active owner')
                frame={'id':uuid4().hex,'task_id':task_id,'operation_id':identity,
                    'cycle_id':state.get('cycle_id') or uuid4().hex,
                    'operation_sha256':digest(row['operation']),'source_hash':row.get('source_hash'),
                    'policy_hash':row.get('policy_hash'),'parked_at':now(),
                    'reviews':[c for c in frontier if c['consumer']['parent_operation_id']==identity]}
                research={}
                for correction in frame['reviews']:
                    if not correction['consumer'].get('web_work_id'):continue
                    work=self.store.record_get('web_work',correction['consumer']['web_work_id'])
                    phase=work['detail'].get('phase');query=self._research_choice(task,row['operation'],phase,work)
                    if phase and query:
                        key=identity+':'+phase
                        saved={'id':frame['id']+':'+phase,'operation_sha256':frame['operation_sha256'],
                            'source_hash':work['source_hash'],'policy_hash':work['policy_hash'],'query':query}
                        if key in research and research[key]!=saved:
                            raise PolicyError('Parked research has conflicting original collection choices')
                        research[key]=saved
                frame['research']=research
                frames.append(frame)
                state.pop('operation_id',None);state.pop('cycle_id',None)
                self.store.event(task_id,'revision','required',dict(frame,disposition='parent_parked'))
            if not state.get('operation_id'):
                for correction in frontier:
                    resolution_id=correction.get('resolution_operation_id')
                    if (resolution_id and resolution_id not in blocked
                            and not any(f['operation_id']==resolution_id for f in frames)):
                        resolution=self.store.get_operation(resolution_id)
                        if resolution['task_id']!=task_id:raise PolicyError('Web resolution belongs to another task')
                        if resolution['status'] not in {'cycle_complete','revision_required','superseded'}:
                            # Reattach the already dispatched operation. Existing
                            # result/unknown-effect guards decide its next node.
                            state.update(operation_id=resolution_id,cycle_id=uuid4().hex)
                            break
            if not state.get('operation_id') and frames:
                frame=frames[-1];row=self.store.get_operation(frame['operation_id'])
                if (frame.get('task_id')!=task_id or row['task_id']!=task_id
                        or digest(row['operation'])!=frame['operation_sha256']
                        or row.get('source_hash')!=frame['source_hash']
                        or row.get('policy_hash')!=frame['policy_hash']):
                    raise PolicyError('Parked correction continuation identity changed')
                # A never-accepted plan cannot consume any outstanding earlier
                # applied-review correction. Independent diagnostic cycles can.
                waiting=(frame['operation_id'] in blocked or
                    (row['operation']['kind']=='plan_task' and frontier))
                if not waiting:
                    unstarted=row['result'] is None and not self.store.operation_started_without_result(row)
                    changed=(frame['source_hash']!=task['source_hash'] or frame['policy_hash']!=self.policy.hash)
                    disposition='returned'
                    if unstarted and (changed or row['status'] in {'revision_required','superseded','cycle_complete'}):
                        disposition='superseded'
                        if row['status'] not in {'revision_required','superseded','cycle_complete'}:
                            self.store.update_operation(frame['operation_id'],status='superseded',
                                revision_reason='Parked unstarted parent needs a new current source/policy proposal')
                    else:
                        if frame['policy_hash']!=self.policy.hash and row['result'] is not None:
                            if task['policy_hash']!=self.policy.hash:return task
                            self.store.record('operation_policy_history',uuid4().hex,row)
                            self.store.update_operation(frame['operation_id'],status='result_recorded',post_policy_hash=self.policy.hash)
                        state.update(operation_id=frame['operation_id'],cycle_id=frame['cycle_id'])
                        if frame['research']:
                            returns=state.setdefault('web_research_returns',{})
                            for key,value in frame['research'].items():
                                previous=returns.get(key)
                                if previous is not None and previous!=value:
                                    self.store.record('web_research_return',previous['id'],
                                        dict(previous,status='continued',next_return_id=value['id']))
                                returns[key]=value
                    frames.pop()
                    self.store.record('web_correction_continuation',frame['id'],
                        dict(frame,status=disposition,closed_at=now()))
                    self.store.event(task_id,'revision','reconciled' if disposition=='returned' else 'revision_required',
                        {'disposition':disposition,'continuation_id':frame['id'],
                         'operation_id':frame['operation_id'],'cycle_id':frame['cycle_id'],'replayed':False})
            if frames:state['web_correction_returns']=frames
            else:state.pop('web_correction_returns',None)
            if state!=task['state']:return self.store.update_task(task_id,state=state)
            return task
        return self.store._transaction(commit)

    async def run_task(self,task_id):
        if self.store.get_task(task_id)['status'] in {'completed','retired'}:return self.snapshot(task_id)
        try:
            self._preserve_started_result_gaps(task_id)
            self.policy.verify_current()
            self.store.update_task(task_id,status='running')
            self.store.event(task_id,'task','started',{'policy_hash':self.policy.hash})
            graph=await self._graph()
            while task_id not in self.stop_requested:
                task=self._source_boundary(self.store.get_task(task_id))
                if task['status']=='completed':break
                if self.restart_pending or (self.quiescing and self.quiescing!=task_id):break
                task=self._web_correction_boundary(task_id)
                task=self._current_cycle(task_id)
                # Each invocation is one bounded action cycle. LangGraph persists
                # node checkpoints; node methods additionally reconcile real
                # operation receipts before any side effect can be repeated.
                try:
                    self.active_cycles.add(task_id)
                    await self.web_judgments.drain(task_id)
                    await self._adopt_current_policy(task)
                    current=self._current_cycle(task_id)
                    if not current['state'].get('plan'):
                        await self._initialize_task(current)
                    else:
                        await graph.ainvoke({'task_id':task_id,'operation_id':current['state'].get('operation_id','')},
                            {'configurable':{'thread_id':task_id+':'+current['state']['cycle_id']}})
                except WebCorrectionNeeded as error:
                    self._web_correction_boundary(task_id)
                    self.store.event(task_id,'revision','required',{'disposition':'web_correction_required','parent_operation_id':error.parent_operation_id,
                        'corrections':error.corrections,'next':'Select actual governed correction; preserve acquired results and the parent cycle.'})
                    continue
                except RevisionNeeded as error:
                    # Revised semantic work is new controlled work. Actual effects
                    # and first faults remain attached to their original identity.
                    self.store.event(task_id,'revision','required',{'reason':str(error),'action':'Revise the dependent proposal using actual feedback.'})
                    current=self.store.get_task(task_id);identity=current['state'].get('operation_id')
                    if identity:
                        old=self.store.get_operation(identity)
                        if old['result'] is None and not self.store.operation_started_without_result(old):
                            self.store.update_operation(identity,status='revision_required',revision_reason=str(error))
                            revised=dict(current['state']);revised.pop('operation_id',None);revised.pop('cycle_id',None)
                            self.store.update_task(task_id,state=revised)
                    continue
                finally:
                    self.active_cycles.discard(task_id)
                    self.cycle_boundary.set()
            if self.restart_pending:
                await self._restart_at_boundary(task_id)
        except Quiesced as error:
            self._record_task_interruption(task_id,'restart_required','controller_boundary','parked',{'reason':str(error),'replay':False})
        except asyncio.CancelledError:
            self._record_task_interruption(task_id,'stopped','task','stopped',{'unknown_effects':[o['operation']['id'] for o in self.store.operations(task_id) if (o.get('result') or {}).get('effect')=='unknown']})
        except NodeCancelledError as error:
            if task_id in self.stop_requested:
                self._record_task_interruption(task_id,'stopped','task','stopped',{'type':type(error).__name__,
                    'node':error.node,'message':str(error),'stop_task_id':task_id,
                    'unknown_effects':[o['operation']['id'] for o in self.store.operations(task_id) if (o.get('result') or {}).get('effect')=='unknown']})
            else:
                self._record_task_interruption(task_id,'attention_required','task','attention_required',{'type':type(error).__name__,
                    'node':error.node,'message':str(error),'resume':'Node cancellation has no matching stop request; preserve effects and investigate.'})
        except ConfigurationRequired as error:
            self._record_task_interruption(task_id,'configuration_required','task','configuration_required',{'message':str(error),'resume':'Configure the missing service, then resume this task.'})
        except Exception as error:
            self._record_task_interruption(task_id,'attention_required','task','attention_required',{'type':type(error).__name__,'message':str(error),'resume':'Preserve real effects and use the recorded cause to revise the dependent work.'})
        return self.snapshot(task_id)

    def _record_task_interruption(self,task_id,status,stage,event_status,detail):
        def commit():
            task=self.store.get_task(task_id)
            if task['status']=='retired' or (task['status']=='completed' and task.get('final') is not None):
                self.store.event(task_id,stage,'terminal_preserved',{'observed_interruption':event_status,
                    'detail':detail,'terminal_status':task['status'],'final_sha256':digest(task['final']) if task.get('final') is not None else None})
                return
            self.store.update_task(task_id,status=status)
            self.store.event(task_id,stage,event_status,detail)
        self.store._transaction(commit)

    def _preserve_started_result_gaps(self,task_id):
        # Called only at a stopped/new-cycle boundary, never while dispatch is
        # in flight. The executor journal remains the authority for real effects.
        for row in self.store.operations(task_id):
            if self.store.operation_started_without_result(row):
                identity=row['operation']['id']
                if not self.store.record_get('operation_result_gap',identity):
                    self.store.record('operation_result_gap',identity,{'id':identity,'task_id':task_id,
                        'original':row,'observed_at':now(),'automatic_replay':False})
                result=OperationResult(operation_id=identity,status='unknown',effect='unknown',
                    stderr='Controller has a consumed/start record without a saved result; reconcile original executor evidence without replay.',
                    data={'original_operation_sha256':digest(row['operation']),'result_gap_ref':identity,'replayed':False})
                self.store.update_operation(identity,status='result_recorded',result=result.model_dump())

    async def _adopt_current_policy(self,task):
        self._preserve_started_result_gaps(task['id'])
        if task['policy_hash'] in ('',self.policy.hash):return
        state=dict(task['state'])
        suspended=state.get('operation_id')
        if suspended and self.store.get_operation(suspended)['operation']['kind']=='adopt_policy':return
        if suspended:
            row=self.store.get_operation(suspended)
            if row['result'] is None and not self.store.operation_started_without_result(row):
                self.store.update_operation(suspended,status='superseded',supersession_reason='Exact user-approved policy version changed before execution')
                suspended=None
        operation=Operation(kind='adopt_policy',args={'old_policy_hash':task['policy_hash'],'new_policy_hash':self.policy.hash,
            'suspended_operation_id':suspended,'original_acceptance':task['acceptance']},
            purpose='承認済み方針の変更を未完了作業と照合して適用する',expected_result='Reviewed current policy adoption with prior effects preserved',
            decisions=[{'id':'policy-adoption','statement':'Apply the exact approved current policy without dropping unfinished effects','rationale':'User approved exact amendment; unchanged conditions and original outcomes remain binding'}])
        self.store.save_operation(task['id'],operation.model_dump(),policy_hash=self.policy.hash)
        state.update(operation_id=operation.id,cycle_id=uuid4().hex)
        self.store.update_task(task['id'],state=state)
        graph=await self._graph()
        await graph.ainvoke({'task_id':task['id'],'operation_id':operation.id},{'configurable':{'thread_id':task['id']+':'+state['cycle_id']}})

    def _controller_commit(self,task,operation,fn):
        def commit():
            saved=self.store.record_get('controller_effect',operation.id)
            if saved:
                if saved['action_hash']!=digest(operation.model_dump()):raise PolicyError('Controller action identity changed')
                return OperationResult.model_validate(saved['result'])
            result=fn()
            self.store.record('controller_effect',operation.id,{'id':operation.id,'task_id':task['id'],'action_hash':digest(operation.model_dump()),'result':result.model_dump()})
            return result
        try:return self.store._transaction(commit)
        except Exception as error:
            saved=self.store.record_get('controller_effect',operation.id)
            if saved and saved['action_hash']==digest(operation.model_dump()):
                return OperationResult.model_validate(saved['result'])
            # These functions affect only this transaction and DB-derived
            # knowledge projections. A delegate can create a workspace; it is
            # deliberately excluded from this rollback conclusion.
            atomic_kinds={'adopt_policy','plan_task','knowledge_update','knowledge_integrate','candidate_disposition','judgment_resolve','policy_amendment','child_scope'}
            if operation.kind not in atomic_kinds or self.store.db.in_transaction:raise
            failure={'id':operation.id,'task_id':task['id'],'action_hash':digest(operation.model_dump()),
                'transaction_rolled_back':True,'first_fault':{'type':type(error).__name__,'message':str(error)},'observed_at':now()}
            self.store.record('controller_failure',operation.id,failure)
            projection=self.knowledge.rebuild_projections()
            unresolved=bool(projection.get('conflicts'))
            result=OperationResult(operation_id=operation.id,status='unknown' if unresolved else 'failed',effect='unknown' if unresolved else 'none',
                stderr=type(error).__name__+': '+str(error),data={'transaction_rolled_back':True,'projection_recovery':projection,'first_fault':failure['first_fault']})
            if not unresolved:self.store.record('controller_effect',operation.id,{'id':operation.id,'task_id':task['id'],'action_hash':failure['action_hash'],'result':result.model_dump()})
            return result

    @staticmethod
    def _evidence_pointers(value,prefix=''):
        rows=[]
        if prefix:rows.append({'pointer':prefix,'sha256':digest(value)})
        if isinstance(value,dict):
            for key in sorted(value):rows.extend(Engine._evidence_pointers(value[key],prefix+'/'+str(key).replace('~','~0').replace('/','~1')))
        elif isinstance(value,list):
            for index,item in enumerate(value):rows.extend(Engine._evidence_pointers(item,prefix+'/'+str(index)))
        return rows

    def _knowledge_context(self,task,operation=None):
        if hasattr(self.knowledge,'context'):
            query=((operation.get('kind','')+' '+operation.get('purpose',''))[:2048]) if operation else ''
            context=self.knowledge.context(task,query=query)
        else:context=self.knowledge.retrieve(task,operation)
        return dict(context,pending_projection_failures=self.knowledge.pending_projection_failures(task),
                    projection_repair_queue=self.knowledge.projection_repair_queue(task))

    async def _select(self,state):
        task=self.store.get_task(state['task_id'])
        identity=task['state'].get('operation_id')
        if identity:
            op=self.store.get_operation(identity)
            if op['status']!='cycle_complete':return {'operation_id':identity}
        preparation=await self.policy_admission.prepare(task['id'])
        knowledge=self._knowledge_context(task)
        operations=self._history_context(task['id'])
        capabilities=getattr(self.executor,'catalog',lambda **kw:{ })(workspace=Path(task['workspace']))
        if inspect.isawaitable(capabilities):capabilities=await capabilities
        correction_context=self._correction_context(task)
        source_preparation=self.source_preparation.pending(task)
        proposal_feedback=task['state'].get('proposal_revision_feedback')
        if proposal_feedback and (proposal_feedback['source_hash']!=task['source_hash'] or proposal_feedback['policy_hash']!=self.policy.hash):
            proposal_feedback=None  # The exact old record remains history, not current instructions.
        operation=await self._call(task['id'],'operation_proposal',{'plan':task['state'].get('plan'),'operations':operations,'capability_guide':capabilities,
            **({'source_preparation':source_preparation} if source_preparation['pending'] else {}),
            **({'web_correction_context':correction_context} if correction_context['pending_returns'] or correction_context['parked_parents'] or correction_context.get('original_sources') else {}),
            'engineering_preparation':self.policy_admission.context(preparation),
            'proposal_revision_feedback':proposal_feedback,
            'controller_guide':self._controller_guide(),
            'knowledge':knowledge,'required_judgment_corrections':self._pending_judgments(task['id']),
            'children':[x for x in self.store.list_tasks() if x['parent_id']==task['id']],
            'instruction':'Propose ONE next operation and enumerate each operative finalized decision in it. Never combine independent tool calls in a script. Do actual primary-goal work. exec args are {capability_id}; capability_request requires name, recipe python|pytest, entrypoint for python, files {relative_path:sha256}, arguments list, declared_effects list, resources {timeout_seconds,cpus,memory_mb,pids_limit,output_bytes,tmp_mb,rationale}. Resource values need a workload/configuration basis and later measurement. File tools use relative paths. history_read {operation_ids:[...]} retrieves full raw history when needed. reconcile {operation_id} inspects an unresolved owned execution without replaying it. knowledge_update {decisions:[...]} resolves recorded improvement IDs with reasons and real verification_operation_ids/actual_use_operation_ids; do not invent evidence. Finish only after required improvements and children are closed.'},Operation)
        if not getattr(self.gateway,'supports_semantic_wire',False):operation.id=uuid4().hex
        data=operation.model_dump()
        self._require_operation_independence(self.store.get_task(task['id']),data)
        current_preparation=self.source_preparation.pending(self.store.get_task(task['id']))
        if current_preparation['pending'] and operation.kind not in current_preparation['allowed_preparation_operations']:
            raise PolicyError('Pending source permits preparation only; correction does not waive source restrictions')
        admission_proposal=self.policy_admission.bind_proposal(task['id'],preparation,data)
        self.store.save_operation(task['id'],data,policy_hash=self.policy.hash,admission_proposal=admission_proposal)
        taskstate=dict(task['state'],operation_id=operation.id)
        self.store.update_task(task['id'],state=taskstate)
        return {'operation_id':operation.id}

    def _correction_context(self,task,*,include_sources=True):
        scoped=bool(getattr(self.policy,'role_scoped_enabled',False)) and include_sources
        sources={} if scoped else None
        frontier=(self.web_judgments.correction_frontier(task['id'],include_operations=True,source_values=sources)
            if scoped else self.web_judgments.correction_frontier(task['id'],include_operations=True))
        parents=[]
        for identity in dict.fromkeys(c['consumer'].get('parent_operation_id') or c['consumer'].get('operation_id') for c in frontier):
            if not identity:continue
            row=self.store.get_operation(identity)
            if row['task_id']!=task['id']:raise PolicyError('Pending correction has a foreign parent')
            parents.append({'operation':row['operation'],'operation_sha256':digest(row['operation']),
                'source_hash':row.get('source_hash'),'policy_hash':row.get('policy_hash'),
                'status':row['status'],'result_sha256':digest(row['result']),
                'review_ids':[c['review_id'] for c in frontier
                    if (c['consumer'].get('parent_operation_id') or c['consumer'].get('operation_id'))==identity]})
        facts=[{'review_id':entry['review_id'],
                'facts':self._knowledge_judgment_facts(task,entry['consumer'],source=(sources or {}).get(entry['review_id']))}
               for entry in frontier if entry['consumer'].get('kind')=='web_acquisition'
               and entry['consumer'].get('stage')!='uncommitted_learning'] if include_sources else []
        originals={};bindings=[]
        if scoped:
            for correction in self._pending_judgments(task['id']):
                source=sources.get(correction['id'])
                if source is None:source=self._judgment_source(task,correction)
                key=digest(source)
                originals.setdefault(key,source)
                evidence=self._judgment_evidence(task,correction)
                bindings.append({'review_id':correction['id'],'expected_hash':correction['hash'],
                    'original_source_key':key,**evidence})
        return {'pending_returns':frontier,'parent_operations':parents,'controller_facts':facts,
            **({'original_sources':originals,'correction_inputs':bindings,
                'evidence_contract':self._judgment_evidence_contract()} if scoped else {}),
            'parked_parents':task['state'].get('web_correction_returns',[]),
            'instruction':'Use actual governed diagnosis/correction and reviewed judgment_resolve for these exact consumers. A fresh operation ID or a narrative identity repair does not resolve a review or make its primary effect independent. Compare actual effect and source relationship, not merely shared paths. Reads and different corrective effects may proceed under their own full review; original obligations/returns remain. Never replay an acquired exchange or Knowledge application.'}

    def _require_operation_independence(self,task,operation):
        effect=operation_effect(Path(task['workspace']),operation)
        if effect is None:return
        for parent in self._correction_context(task,include_sources=False)['parent_operations']:
            if parent['source_hash']!=task['source_hash']:continue
            original=parent['operation']
            try:previous=operation_effect(Path(task['workspace']),original)
            except PolicyError:continue  # Invalid old args are not invented effects.
            if previous==effect:
                raise PolicyError('PENDING_CORRECTION_DEPENDENCY: planned effect recreates parent '+original['id']+
                    ' before reviewed return '+','.join(parent['review_ids'])+
                    '; select actual independent diagnosis/correction or judgment_resolve, not a new UUID for this effect')

    def _controller_guide(self):
        return {'child_integrate':'One reviewed child file: {child_id,source_path,destination_path,sha256,expected_sha256}. Child must be completed; actual bytes are bound before write.',
            **({'optional_improvements':optional_improvement_contract()} if getattr(self.policy,'role_scoped_enabled',False) else {}),
            'delegate':'{objective,acceptance,independent_scope,integration_plan,capability_requirements:[text],quality_requirements:[text],cost_considerations,model_selections:{worker,reviewer,parent}}. Each model selection is {candidate_id,configuration_hash,reasoning:{mode,effort},model_reason,reasoning_reason} from configured_model_catalog. Separate model and reasoning reasons must address this job. Child parent-role calls consume reviews; reviewer calls remain thought-only.',
            'configured_model_catalog':model_catalog(self.gateway.settings) if getattr(self.gateway,'settings',None) else {'status':'unconfigured','candidates':[]},
            'file_write':'{path:workspace_relative,text:UTF8_content} or {path,base64}; expected_sha256 is an optional exact previous-file CAS. One actual atomic file write. No automatic trailing newline.',
            'file_read':'{path:workspace_relative,offset?:0,max_bytes?:131072,encoding?:utf-8|base64}; actual UTF-8 text is in result.stdout, and result.data contains sha256,total_bytes,returned_bytes,truncated,next_offset,utf8_lossy. Base64 is populated only for encoding=base64. Preserve partial/lossy flags and retrieve remaining ranges if needed. This installed governed route cannot be replaced with a direct host read.',
            'file_list':'{path:workspace_relative_directory}, default workspace root; actual confined inventory.',
            'history_read':'{mode:index,offset:0,limit:20} returns a frozen operation index and exact next_args. For an operation use its advertised read_args. For an actual model response reference from usage/error metadata use {mode:model_response,reference,offset:0,limit:16384}; follow next_args to read retained content. Credentials and provider private reasoning are excluded before storage. A bounded diagnostic is not the full response and a lost legacy response cannot be reconstructed.',
            'source_read':'{source_id,expected_hash,offset?:0,max_bytes?:65536}; read immutable own submitted source bytes, with exact hash/range and remaining offset. UTF-8 text is a redacted view; original bytes stay private. Binary sources require an explicit extraction capability before semantic interpretation.',
            'source_prepare':'All modes require {mode,source_id,expected_hash,expected_source_hash,reason}. stage preserves the exact pending original at the returned workspace path. Prepare an extractor through file_write/capability_request/exec. accept_extraction also requires {stage_operation_id,extraction_operation_id,output_path,expected_output_sha256,method}; it captures actual reviewed UTF-8 output and adopts it only after post-review and learning. withdraw also requires {instruction_id,source_quote} from a later user instruction explicitly withdrawing this particular attachment. retain uses the same fields and a newer user instruction reversing an unresolved withdrawal confirmation; it supersedes that confirmation only after the complete reviewed cycle. Other acceptance/effects and required reading remain. No opaque attachment is adopted by a program-success label.',
            'child_scope':'{child_id,expected_lease_hash,parent_source_hash,disposition:resume|settle|retire,reason,source_id,source_quote}; reviewed response to parent source changes. Resume retains original child scope. Settle also requires settlement {objective,acceptance:[...]}, replacing a cancelled deliverable with mandatory result/review/learning/workflow closure through the child normal source-plan cycle. Retire requires all actual cycles, Web after-reviews, Skill cleanup and required improvements closed. Unknown effects first require child_reconcile. Original sources/results remain recorded.',
            'child_reconcile':'{child_id,operation_id,expected_lease_hash,expected_result_hash,reason,stop?:false}; parent reviews and inspects one stopped immediate child unknown effect without replay or child model calls. Exact old result is retained; replacement occurs only after normal parent post-review and learning. Then use child_scope resume or settle.',
            'knowledge_integrate':'For a pending child knowledge candidate visible in knowledge.candidates: {candidate_id,expected_hash:candidate_hash}. Primary parent only; exact full candidate/source/version is reviewed before shared mutation.',
            'knowledge_index':'{kind:skill|idea|episode|family|candidate|deferred,query?,cursor?,limit?:20}; exact version-bound navigation. Follow next_cursor; a preview is not a read or use. All pending counts remain binding.',
            'knowledge_projection_repair':'{skill_id,expected_skill_hash,expected_file_sha256,reason} repairs one exact DB-derived Skill file. Or {mode:"cleanup_reconcile",failure_id,expected_failure_sha256,reason} reads back the protected original cleanup scope, including an empty or legacy scope. Use exact args from pending_projection_failures. Neither mode replays cleanup; original faults remain. Both require normal actual-result review and Learning before resolution.',
            'knowledge_read':'{kind,identity,expected_hash,offset?:0,max_chars?:24000} from index.items.read_args reads the exact permitted original value in explicit ranges; {episode_id,expected_hash?} is the legacy episode route. Read all needed ranges; sharing a reference grants no private child history.',
            'candidate_disposition':'{candidate_id,expected_hash,expected_state_hash,disposition:reject|defer|rebase,reason,...}; inspect candidate and current targets first. Defer needs next_trigger; rebase needs a new exact replacement payload for full review. Preserves original candidate/history.',
            'improvement_verification':'Before executing each improvement step, bind improvement_bindings with {idea_id,candidate_sha256,role:implementation|verification|use,result_pointer,source_pointer,source_sha256,claim}. Pointers identify actual result fields and its attested candidate hash. After all steps, knowledge_update decisions may use {id,disposition:verified,reason,candidate_sha256,implementation_operation_ids,verification_operation_ids,actual_use_operation_ids}. For a controller update, implementation is prepare_update and verification is verify_update; also give activation_operation_id for the actual activate_update. Use must be an ordinary governed operation after the supervisor loaded that candidate in a different process, with source_pointer=/controller_runtime/candidate_sha256 and result_pointer pointing to its actual observed result. Activation, restart or a claimed source hash alone is not use. Execution/source/order, protected runtime records and actual Skill application evidence are validated; arbitrary previous success is not proof.',
            'judgment_resolve':self._judgment_evidence_contract()['instruction'] if getattr(self.policy,'role_scoped_enabled',False) else 'After correcting a recorded post-application review through actual governed work: {review_id,expected_hash,reason,evidence_operation_ids:[...]}. Every evidence operation must have an observed known result and must follow the reviewed outcome. The exact correction and evidence receive normal pre/post review.',
            'controller_read':'Read exact source for within-policy improvement: {path}, restricted to src/policy_harness, tests, docs, pyproject.toml, uv.lock. No secrets/runtime access.',
            'prepare_update':'{policy_hash,rationale,changes:[{path,source:workspace_relative_file_or_null,expected_sha256,sha256}]}; full old/new candidate independently reviewed. Mandatory policy changes require separate exact user approval.',
            'verify_update':'{candidate_id}; actual isolated whole-candidate verification, no model-provided PASS.',
            'activate_update':'{candidate_id}; requires real verification, unchanged sources, connected fresh-process supervisor and all tasks at safe boundaries.',
            'rollback_update':'{candidate_id}; exact preserved preimages, supervised restart required.',
            'updates_available':self.updates is not None,'restart_connected':self.on_restart is not None}

    def _model_job_context(self,task,operation,role):
        args=operation['args']
        return {'job_id':operation['id']+':'+role,'role':role,'objective':args.get('objective',''),
            'source_hash':digest({'parent_source_hash':task['source_hash'],'objective':args.get('objective'),
                'acceptance':args.get('acceptance'),'independent_scope':args.get('independent_scope'),'integration_plan':args.get('integration_plan')}),
            'policy_hash':self.policy.hash,'parent_task_id':task['id'],'parent_operation_id':operation['id'],
            'capability_requirements':args.get('capability_requirements',[]),'quality_requirements':args.get('quality_requirements',[]),
            'cost_considerations':args.get('cost_considerations','')}

    def _delegate_models(self,task,operation):
        settings=getattr(self.gateway,'settings',None)
        if settings is None or not getattr(self.gateway,'supports_model_leases',False):raise ConfigurationRequired('Configure a lease-capable model gateway before delegation')
        selections=operation['args'].get('model_selections',{})
        if set(selections)!={'worker','reviewer','parent'}:raise PolicyError('Select model and reasoning separately for each actual child job role')
        return {role:bind_selection(settings,selection,self._model_job_context(task,operation,role)) for role,selection in selections.items()}

    def _child_candidate(self,task,args):
        child=self.store.get_task(args.get('child_id'))
        if child['parent_id']!=task['id'] or child['status']!='completed':raise PolicyError('Integrate only a completed owned child result')
        source=confined(Path(child['workspace']),args['source_path'])
        data=source.read_bytes();source_hash=hashlib.sha256(data).hexdigest()
        if source_hash!=args.get('sha256'):raise PolicyError('Child artifact changed; inspect its exact current result')
        destination=confined(Path(task['workspace']),args['destination_path'],allow_missing=True)
        before=hashlib.sha256(destination.read_bytes()).hexdigest() if destination.exists() else None
        if before!=args.get('expected_sha256'):raise PolicyError('Parent destination changed after integration choice')
        return {'child_id':child['id'],'source_path':args['source_path'],'destination_path':args['destination_path'],
                'sha256':source_hash,'base64':base64.b64encode(data).decode(),'text':data.decode('utf-8',errors='replace'),
                'expected_sha256':before,'child_final':child['final']}

    async def _describe_operation(self,task,operation):
        kind,args=operation['kind'],operation['args'];workspace=Path(task['workspace'])
        if kind in {'plan_task','finish'} and self.source_preparation.pending(task)['pending']:
            raise PolicyError('Pending attachment must be read, extracted or explicitly withdrawn before adoption or completion')
        if kind=='file_write':
            path,data=file_write_input(workspace,args)
            value={'file_write':{'path':path.relative_to(workspace).as_posix(),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()},
                'meaning':'Validated actual arguments and confinement only; current bytes, permission and final CAS remain execution checks.'}
        elif kind=='plan_task':
            deferred=self.knowledge.deferred_for_planning(task)
            if args['plan'].get('deferred_index_hash')!=deferred['index_hash']:
                raise RevisionNeeded('Deferred knowledge inventory changed or is missing; regenerate the plan with all current candidate dispositions')
            value={'deferred_index':deferred}
        elif kind=='delegate':value={'model_leases':self._delegate_models(task,operation),
            'independent_scope':args.get('independent_scope'),'integration_plan':args.get('integration_plan')}
        elif kind=='capability_request' and hasattr(self.executor,'describe_capability'):value=self.executor.describe_capability(workspace,args)
        elif kind=='exec' and hasattr(self.executor,'describe_execution'):value=self.executor.describe_execution(workspace,args)
        elif kind=='child_integrate':value=self._child_candidate(task,args)
        elif kind=='knowledge_integrate':value=self.knowledge.describe_candidate(task,args['candidate_id'])
        elif kind=='knowledge_update' and getattr(self.policy,'role_scoped_enabled',False):
            value=self.knowledge.describe_idea_resolution(task,args.get('decisions',[]))
        elif kind=='knowledge_read':
            value=self.knowledge.read(task,args['kind'],args['identity'],args.get('expected_hash')) if 'kind' in args else self.knowledge.describe_episode(task,args['episode_id'],args.get('expected_hash'))
            value={'kind':args.get('kind','episode'),'identity':args.get('identity',args.get('episode_id')),'sha256':digest(value),'content':self._preview(value,24000,'knowledge_read')}
        elif kind=='candidate_disposition':value=self.knowledge.describe_candidate_disposition(task,args)
        elif kind=='knowledge_projection_repair':value=self.knowledge.describe_projection_repair(task,args)
        elif kind=='source_prepare':value=self.source_preparation.describe(task,args)
        elif kind=='child_reconcile':
            child=self.store.get_task(args.get('child_id'));previous=self.store.get_operation(args.get('operation_id'))
            lease=child['state'].get('delegation_lease')
            handle=self.running.get(child['id'])
            if child['parent_id']!=task['id'] or previous['task_id']!=child['id'] or digest(lease)!=args.get('expected_lease_hash'):
                raise PolicyError('Child reconciliation owner or lease changed')
            if handle and not handle.done():raise PolicyError('Stop the child at a safe boundary before parent reconciliation')
            if (previous.get('result') or {}).get('effect')!='unknown' or digest(previous['result'])!=args.get('expected_result_hash') or not args.get('reason'):
                raise PolicyError('Child reconciliation needs exact original unknown result and reasons')
            if type(args.get('stop',False)) is not bool:raise PolicyError('Child reconciliation stop must be boolean')
            value={'child_id':child['id'],'lease':lease,'original_operation':previous,'source_hash':child['source_hash'],
                   'recovery_protocol':'durable-observation-v1'}
        elif kind=='reconcile':
            previous=self.store.get_operation(args.get('operation_id'))
            if previous['task_id']!=task['id'] or (previous.get('result') or {}).get('effect')!='unknown':
                raise PolicyError('Reconciliation must target an actual owned unknown effect')
            if type(args.get('stop',False)) is not bool:raise PolicyError('Recovery stop must be boolean')
            value={'original_operation':previous,'recovery_protocol':'durable-observation-v1'}
        elif kind=='child_scope':
            child=self.store.get_task(args.get('child_id'))
            lease=child['state'].get('delegation_lease')
            if child['parent_id']!=task['id'] or digest(lease)!=args.get('expected_lease_hash') or task['source_hash']!=args.get('parent_source_hash'):
                raise PolicyError('Child scope disposition is foreign or stale')
            sources={s['id']:s for s in task['source_history']}
            source=sources.get(args.get('source_id'))
            texts=self.store.source_texts(task)
            if not source or not args.get('source_quote') or args['source_quote'] not in texts[source['id']] or not args.get('reason'):
                raise PolicyError('Child scope needs exact changed user source and reasons')
            if args.get('disposition') not in {'resume','settle','retire'}:raise PolicyError('Unknown child scope disposition')
            handle=self.running.get(child['id'])
            if handle and not handle.done():raise PolicyError('Child must reach a stopped safe boundary before scope changes')
            if any(o['status']=='executing' or (o.get('result') or {}).get('effect')=='unknown' for o in self.store.operations(child['id'])):
                raise PolicyError('Reconcile child effects before changing its scope')
            if args['disposition']=='retire' and any(self._mandatory_child_work(child).values()):
                raise PolicyError('Required child result cycles, Web learning, workflow improvements and cleanup must be closed before retirement; use settle for a cancelled deliverable')
            value={'child':child,'lease':lease,'actual_operations':self._history_context(child['id']),'user_source':source}
            if args['disposition'] in {'resume','settle'}:
                original=lease.get('job_operation') or self.store.get_operation(lease['parent_operation_id'])['operation']
                rebound=dict(original,args=dict(original['args'],model_selections=args.get('model_selections',original['args'].get('model_selections'))))
                if args['disposition']=='settle':
                    settlement=args.get('settlement',{})
                    if set(settlement)!={'objective','acceptance'} or not isinstance(settlement['objective'],str) or not settlement['objective'].strip() or not isinstance(settlement['acceptance'],list) or not settlement['acceptance'] or any(not isinstance(c,str) or not c.strip() for c in settlement['acceptance']):
                        raise PolicyError('Settlement needs an exact new closure objective and acceptance for review')
                    rebound=dict(rebound,id=operation['id'],args=dict(rebound['args'],**settlement,
                        independent_scope='Only close actual effects, pending reviews/learning and required workflow improvements of this cancelled child job. Preserve originals; no cancelled deliverable work.'))
                value['job_operation']=rebound
                value['model_leases']=self._delegate_models(task,rebound)
        elif kind=='judgment_resolve':
            correction=self.store.record_get('judgment_correction',args.get('review_id'))
            if not correction or correction['task_id']!=task['id'] or correction['status']!='pending' or correction['hash']!=args.get('expected_hash'):
                raise PolicyError('Judgment correction is absent, stale or foreign')
            rows=[self.store.get_operation(i) for i in args.get('evidence_operation_ids',[])]
            if (not args.get('reason') or not rows or len({r['operation']['id'] for r in rows})!=len(rows)
                    or any(not self._judgment_evidence_eligible(task,correction,r) for r in rows)):
                raise PolicyError('Judgment correction needs actual subsequent owned evidence and reasons')
            value={'correction':correction,'evidence':rows,'original_source':self._judgment_source(task,correction)}
        elif kind in {'prepare_update','verify_update','activate_update','rollback_update'}:
            if self.updates is None:raise ConfigurationRequired('Controller update manager is not connected')
            if kind in {'activate_update','rollback_update'} and self.on_restart is None:raise ConfigurationRequired('Start through the supervised entrypoint before applying controller updates')
            value=self.updates.describe(workspace,args)
        else:return None
        return await value if inspect.isawaitable(value) else value

    def _mandatory_child_work(self,child):
        identity=child['id'];snapshot=self.knowledge.snapshot(identity)
        return {'cycles':[o['operation']['id'] for o in self.store.operations(identity)
                    if o['status']=='executing' or (o.get('result') is not None and o['status']!='cycle_complete')
                    or (o.get('result') or {}).get('effect')=='unknown'],
                'web':[w['id'] for w in self.store.records('web_work') if w['task_id']==identity and w['stage']=='after' and w['status']!='complete'],
                'judgments':[j['id'] for j in self._pending_judgments(identity)],
                'workflow_ideas':[i['id'] for i in snapshot['ideas'] if i['target']=='workflow' and i['status']=='pending'],
                **({'task_ideas':[i['id'] for i in snapshot['ideas'] if i['target']=='task' and i['status']=='pending']}
                   if getattr(self.policy,'role_scoped_enabled',False) else {}),
                'skill_cleanup':[s['id'] for s in snapshot['skills'] if s.get('needs_cleanup')],
                'projections':self.knowledge.pending_projection_failures(child),
                'approvals':[a['id'] for a in self.pending_approvals(identity)],
                'children':[c['id'] for c in self.store.list_tasks() if c['parent_id']==identity
                    and (c['status'] not in {'completed','retired'} or any(self._mandatory_child_work(c).values()))]}

    def _reprepare_admission(self,task_id,identity,reason):
        def retain_and_reprepare():
            current=self.store.get_operation(identity)
            if current.get('result') is not None or self.store.operation_started_without_result(current):
                raise CycleHeld('Admission preparation cannot replace an operation with an actual or unknown effect')
            retained={'reason':reason,'prior_status':current['status'],
                'proposal_sha256':digest(current['operation']),'source_hash':current.get('source_hash'),
                'policy_hash':current.get('policy_hash')}
            self.store.update_operation(identity,status='revision_required',admission_revision=retained,revision_reason=reason)
            self.store.event(task_id,'policy_admission_reprepare','held',dict(operation_id=identity,**retained))
            current_state=dict(self.store.get_task(task_id)['state'])
            current_state['proposal_revision_feedback']={'operation_id':identity,'operation':current['operation'],
                'operation_sha256':digest(current['operation']),'reason':reason,
                'preparation_review':current.get('preparation_review'),
                'source_hash':current.get('source_hash'),'policy_hash':current.get('policy_hash'),
                'instruction':'Preserve this rejected original. Propose the actual necessary preparation or a corrected new operation; do not invent arguments from its prose or replay a prior effect.'}
            if current_state.get('operation_id')==identity:
                current_state.pop('operation_id',None);current_state.pop('cycle_id',None)
                self.store.update_task(task_id,state=current_state)
        self.store._transaction(retain_and_reprepare)

    async def _pre(self,state):
        identity=state['operation_id'];row=self.store.get_operation(identity)
        admission_fault=None
        if row['result'] is None and row['status']!='executing':
            task=self.store.get_task(state['task_id'])
            try:
                if row['operation']['kind']=='file_write':file_write_input(Path(task['workspace']),row['operation']['args'])
                self._require_operation_independence(task,row['operation'])
            except PolicyError as error:admission_fault=str(error)
        if row['status']=='reviewed' and row['operation']['kind'] in GOVERNED_KINDS:
            try:validate_admission(self.store,state['task_id'],row,self.policy.hash)
            except PolicyError as error:admission_fault=str(error)
        if row['status'] in ('reviewed','executing','result_recorded','post_reviewed','learned','cycle_complete') and admission_fault is None:return {}
        task=self.store.get_task(state['task_id']);operation=row['operation']
        if row.get('source_hash')!=task['source_hash']:
            raise RevisionNeeded('User source changed before the operation; review a newly bound proposal')
        if identity in {x['id'] for x in operation['decisions']}:raise PolicyError('Operation and finalized decision identities must be distinct')
        try:
            if admission_fault:
                raise AdmissionPreparationRequired('Unstarted legacy or stale reviewed proposal needs current source-first preparation: '+admission_fault)
            candidate=await self._describe_operation(task,operation)
            admission=await self.policy_admission.review(task,operation,candidate)
            if admission and admission.get('mechanical_missing', admission['missing']):
                # Applicability is already an independent result. Consume its
                # real opinions once; a response cannot waive its prerequisites.
                reviews=self.policy_admission.opinions(admission)
                review=Review(summary='Required preparation for this unadmitted proposal',
                    opinions=[opinion for item in reviews for opinion in item.opinions])
                disposition=await self.bounded_judgments.disposition_proposal(task['id'],operation,
                    'admission_preparation_disposition',{'operation':operation,'admission':admission,
                        'review':review.model_dump(),'sources':[],
                        'instruction':'Dispose these actual independent opinions. Mandatory missing prerequisites stay held regardless of verdict. Identify the governed preparation; this is not approval of the unexecuted operation or a replacement for its eventual Web/pre/post cycle.'})
                self.store.update_operation(identity,preparation_review={'admission':admission,
                    'review':review.model_dump(),'disposition':disposition.model_dump()})
                raise AdmissionPreparationRequired('; '.join(admission['missing']))
        except AdmissionPreparationRequired as error:
            reason=str(error)
            self._reprepare_admission(task['id'],identity,reason)
            raise RevisionNeeded(reason) from error
        web=await self._research(task['id'],operation,'pre',{'operation':operation,'decisions':operation['decisions'],'candidate':candidate})
        selection,all_ids=await self._select_skills(task,operation)
        targets=operation_targets(operation,selection)
        assessments=await self._assess_targets(task['id'],'pre_assessment',targets,{'operation':operation,'skills':selection.model_dump(),'capability_candidate':candidate,'web':web,'timing':'before the exact operation and these individual finalized choices'})
        bundle={'operation':operation,'assessments':assessments,'targets':targets,'skills':selection.model_dump(),'candidate':candidate,
                'policy_admission':admission}
        reviewed=await self._review_bundle(task['id'],operation,'pre',bundle,web,
            additional_reviews=self.policy_admission.opinions(admission))
        missing=self.policy_admission.remaining(task['id'],operation,bundle,reviewed)
        if missing:
            self.store.update_operation(identity,pre_bundle=bundle,pre_web=web,pre_review=reviewed,
                preparation_review={'admission':admission,'review':reviewed['review'],
                                    'disposition':reviewed['disposition']})
            reason='; '.join(missing)
            self._reprepare_admission(task['id'],identity,reason)
            raise RevisionNeeded('Operation held for required preparation: '+reason)
        if reviewed['disposition']['verdict']!='proceed':
            self.store.update_operation(identity,status='revision_required',pre_bundle=bundle,pre_web=web,pre_review=reviewed)
            state2=dict(task['state']);state2.pop('operation_id',None);state2.pop('cycle_id',None)
            self.store.update_task(task['id'],state=state2)
            reason=reviewed['disposition']['rationale']
            raise RevisionNeeded('Operation held for reasoned revision: '+reason)
        self.store.update_operation(identity,status='reviewed',pre_bundle=bundle,pre_web=web,pre_review=reviewed)
        self.policy_admission.confirm(task['id'],operation,admission)
        return {}

    async def _select_skills(self,task,operation):
        selection,all_ids=await self.bounded_judgments.select_skills(task,operation)
        selected={s['id'] for s in selection.selected};rejected={s['id'] for s in selection.rejected}
        if len(selected)!=len(selection.selected) or len(rejected)!=len(selection.rejected) or selected & rejected or selected|rejected!=all_ids:
            raise PolicyError('Skill selection omitted or duplicated a candidate')
        for item in selection.selected:
            actual=self.store.record_get('skill',item['id'])
            if not actual or actual['hash']!=item.get('hash') or not item.get('application'):
                raise PolicyError('Selected Skill version/application differs')
            clause=item.get('procedure_clause',actual['content'])
            if not clause or clause not in actual['content']:
                raise PolicyError('Selected procedure clause is not in the actual Skill')
            item.update(procedure_clause=clause,procedure_sha256=hashlib.sha256(clause.encode()).hexdigest())
        return selection,all_ids

    async def _execute(self,state):
        identity=state['operation_id'];row=self.store.get_operation(identity)
        if row['result'] is not None:return {}
        task=self.store.get_task(state['task_id']);operation=Operation.model_validate(row['operation'])
        self.web_judgments.require_parent_return(task['id'],identity)
        self._require_operation_independence(task,operation.model_dump())
        if operation.kind=='plan_task' and self.web_judgments.correction_frontier(task['id']):
            raise CycleHeld('Required applied Web correction must return before accepting a task plan')
        self._assert_parent_source(task)
        if row.get('source_hash')!=task['source_hash']:raise RevisionNeeded('User source changed after review')
        if row['status']=='executing':raise CycleHeld('A started operation has no result. Reconcile real effects; do not replay.')
        if row['status']!='reviewed':raise PolicyError('Required pre-execution cycle is incomplete')
        if not self._allowed_with_unknown(task,operation):
            raise CycleHeld('Workspace has an unresolved effect; dependent execution held')
        self.policy.verify_current()
        if row['policy_hash']!=self.policy.hash:raise PolicyError('Policy changed after operation review')
        bundle_hash=digest(row['pre_bundle'])
        if row['pre_review']['bundle_hash']!=bundle_hash:raise PolicyError('Reviewed bundle changed')
        for item in row['pre_bundle']['skills']['selected']:
            if self.store.record_get('skill',item['id'])['hash']!=item['hash']:raise PolicyError('Skill changed before execution')
        if row['pre_bundle'].get('candidate') is not None:
            actual=await self._describe_operation(task,operation.model_dump())
            if digest(actual)!=digest(row['pre_bundle']['candidate']):raise PolicyError('Capability source changed after review')
        lock=self.workspace_locks.setdefault(task['workspace'],asyncio.Lock())
        async with lock:
            self._assert_parent_source(self.store.get_task(task['id']))
            if row.get('source_hash')!=self.store.get_task(task['id'])['source_hash']:raise RevisionNeeded('User source changed while awaiting execution lease')
            # Repeat source/Skill checks after acquiring the actual workspace
            # lease; no awaited lock may separate the last check from dispatch.
            self.policy.verify_current()
            if row['policy_hash']!=self.policy.hash:raise RevisionNeeded('Policy changed while awaiting execution lease')
            for item in row['pre_bundle']['skills']['selected']:
                if self.store.record_get('skill',item['id'])['hash']!=item['hash']:raise RevisionNeeded('Skill changed while awaiting execution lease')
            if row['pre_bundle'].get('candidate') is not None:
                actual=await self._describe_operation(task,operation.model_dump())
                if digest(actual)!=digest(row['pre_bundle']['candidate']):raise RevisionNeeded('Candidate changed while awaiting execution lease')
            # Candidate inspection may await a subprocess; recheck synchronous
            # semantic dependencies at the final no-await admission boundary.
            self._assert_parent_source(self.store.get_task(task['id']))
            if self.policy.hash!=row['policy_hash']:raise RevisionNeeded('Policy changed during final candidate inspection')
            for item in row['pre_bundle']['skills']['selected']:
                if self.store.record_get('skill',item['id'])['hash']!=item['hash']:raise RevisionNeeded('Skill changed during final candidate inspection')
            token=self.store.issue_permit(task['id'],identity,digest(operation.model_dump()),self.policy.hash,bundle_hash)
            self.store.consume_permit(token,task_id=task['id'],operation_id=identity,action_hash=digest(operation.model_dump()),policy_hash=self.policy.hash,bundle_hash=bundle_hash)
            self.store.event(task['id'],'execution','started',{'operation':operation.model_dump()})
            started=time.monotonic()
            interrupted=False
            dispatch_started=False
            runtime_started=False
            try:
                if self.updates is not None and hasattr(self.updates,'before_execution'):
                    self.updates.before_execution(task['id'],operation.model_dump())
                    runtime_started=True
                dispatch_started=True
                result=await self._dispatch(task,operation)
            except asyncio.CancelledError:
                result=OperationResult(operation_id=identity,status='unknown',effect='unknown',stderr='Interrupted after action start; executor recovery evidence required.',elapsed_seconds=time.monotonic()-started)
                interrupted=True
            except Exception as error:
                result=OperationResult(operation_id=identity,status='unknown' if dispatch_started else 'failed',effect='unknown' if dispatch_started else 'none',stderr=type(error).__name__+': '+str(error),elapsed_seconds=time.monotonic()-started)
            result=OperationResult.model_validate(result)
            if result.operation_id!=identity:raise PolicyError('Executor returned a foreign result')
            candidate=row['pre_bundle'].get('candidate') or {}
            sources=candidate.get('sources',[])
            if sources:result.data['admitted_source_hashes']={s['path']:s['sha256'] for s in sources if 'path' in s and 'sha256' in s}
            # Only the trusted controller stamps this envelope. Executor/model
            # fields can neither choose the generation nor attest a source cut.
            result.controller_runtime={}
            if runtime_started:
                try:result.controller_runtime=self.updates.after_actual_result(task['id'],operation.model_dump(),result.model_dump())
                except Exception as error:
                    self.store.update_operation(identity,runtime_attestation_error={'type':type(error).__name__,'message':str(error)})
                    self.store.event(task['id'],'runtime_attestation','failed',{'operation_id':identity,'message':str(error),'actual_result_preserved':True})
            self.store.update_operation(identity,status='result_recorded',result=result.model_dump())
            self.store.event(task['id'],'execution',result.status,result.model_dump())
            if interrupted or task['id'] in self.stop_requested:raise asyncio.CancelledError()
            if result.status=='pending' and result.data.get('controller_quiesced'):raise Quiesced('Unstarted controller operation parked for update')
        return {}

    def _allowed_with_unknown(self,task,operation):
        unknown=[o for o in self.store.operations(task['id']) if (o.get('result') or {}).get('effect')=='unknown' or self.store.operation_started_without_result(o)]
        if not unknown:return True
        if operation.kind in {'file_read','file_list','web_fetch','reconcile','child_reconcile','history_read','controller_read','knowledge_read','knowledge_index','source_read','adopt_policy','plan_task'}:return True
        if operation.kind=='knowledge_projection_repair':
            if operation.args.get('mode')=='cleanup_reconcile':
                return all(o['operation']['kind']=='knowledge_projection_repair' and
                    o['operation']['args'].get('mode')=='cleanup_reconcile' and
                    o['operation']['args'].get('failure_id')==operation.args.get('failure_id') for o in unknown)
            return all(o['operation']['kind']=='knowledge_projection_repair' and
                not o['operation']['args'].get('mode') and
                o['operation']['args'].get('skill_id')==operation.args.get('skill_id') for o in unknown)
        if operation.kind!='rollback_update':return False
        candidate=operation.args.get('candidate_id')
        # This only allows entry to full review/CAS/preimage checks. It never
        # asserts that rollback has succeeded or clears the original unknown.
        return bool(candidate) and all(o['operation']['kind'] in {'activate_update','rollback_update'} and o['operation']['args'].get('candidate_id')==candidate for o in unknown)

    async def _dispatch(self,task,operation):
        if operation.kind=='adopt_policy':
            if operation.args['new_policy_hash']!=self.policy.hash or operation.args['original_acceptance']!=task['acceptance']:
                raise PolicyError('Policy adoption lost current source or original outcomes')
            return self._controller_commit(task,operation,lambda:OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',data={'adopted_policy_hash':self.policy.hash,'suspended_operation_id':operation.args.get('suspended_operation_id')}))
        if operation.kind=='plan_task':
            plan=TaskPlan.model_validate(operation.args['plan'])
            validate_reconciliation(task,plan.model_dump(),self.store.source_texts(task))
            if {x.get('id') for x in plan.source_coverage}!=set(self.policy.conditions)|set(self.policy.rules):raise PolicyError('Plan omitted complete source conditions')
            def save_plan():
                self.knowledge.record_deferred_selection(task,operation.id,plan.deferred_considerations,plan.deferred_index_hash)
                self.store.apply_source_plan(task['id'],task['source_hash'],plan.model_dump(),operation.id)
                return OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',data={'plan':plan.model_dump()})
            return self._controller_commit(task,operation,save_plan)
        if operation.kind=='source_read':
            args=operation.args
            raw,sha=self.store.source_bytes(task['id'],args['source_id'],args.get('expected_hash'))
            offset=args.get('offset',0);limit=args.get('max_bytes',65536)
            if type(offset) is not int or type(limit) is not int or offset<0 or offset>len(raw) or not 1<=limit<=65536:raise PolicyError('Invalid source byte range')
            end=min(offset+limit,len(raw))
            try:
                decoded=raw.decode('utf-8');binary='\x00' in decoded
            except UnicodeError:binary=True
            if not binary:
                while end<len(raw) and (raw[end]&0xC0)==0x80:end-=1
                if end<=offset and offset<len(raw):raise PolicyError('Source range is too short for one complete UTF-8 character')
                try:text=raw[offset:end].decode('utf-8')
                except UnicodeError as error:raise PolicyError('Source offset must be an observed UTF-8 boundary') from error
            else:text=''
            part=raw[offset:end]
            text,masked=redact_source_range(raw,offset,end) if not binary else ('',False)
            result=OperationResult(operation_id=operation.id,status='succeeded',stdout=redact(text),data={'source_id':args['source_id'],
                'sha256':sha,'offset':offset,'returned_bytes':len(part),'total_bytes':len(raw),'next_offset':end if end<len(raw) else None,
                'binary_or_split_encoding':binary,'text_decoded':not binary,'range_sha256':hashlib.sha256(part).hexdigest(),
                'base64':None,'private_bytes_retained':True,'extraction_required':binary,'redacted':masked})
            return result
        if operation.kind=='child_scope':
            description=await self._describe_operation(task,operation.model_dump())
            def change_child():
                child=description['child'];state=dict(child['state'])
                self.store.record('child_scope_history',operation.id,{'id':operation.id,'before':child,'disposition':operation.args})
                state['delegation_lease']=dict(description['lease'],parent_source_hash=task['source_hash'],
                    constraints=task['state'].get('plan',{}).get('constraints',[]),preservation=task['state'].get('plan',{}).get('preservation',[]),scope_disposition_operation_id=operation.id)
                if operation.args['disposition'] in {'resume','settle'}:
                    state['delegation_lease']['model_leases']=description['model_leases']
                    state['delegation_lease']['job_operation']=description['job_operation']
                    if operation.args['disposition']=='settle':
                        self.store.append_delegation_scope(child['id'],task['id'],operation.model_dump(),child['source_hash'])
                        state['delegation_lease']['settlement_only']=True
                    state.pop('plan',None)
                    identity=state.get('operation_id')
                    if identity and self.store.get_operation(identity)['result'] is None and not self.store.operation_started_without_result(self.store.get_operation(identity)):
                        self.store.update_operation(identity,status='superseded',revision_reason='Parent source/scope changed')
                        state.pop('operation_id',None);state.pop('cycle_id',None)
                self.store.update_task(child['id'],state=state,status='retired' if operation.args['disposition']=='retire' else 'source_update_required')
                return OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',data={'child_id':child['id'],'disposition':operation.args['disposition'],'preserved_history':operation.id})
            result=self._controller_commit(task,operation,change_child)
            if result.status=='succeeded' and operation.args['disposition'] in {'resume','settle'}:self.start_task(result.data['child_id'])
            return result
        if operation.kind=='history_read':
            if operation.args.get('mode')=='model_response':
                if not hasattr(self.gateway,'read_response'):raise ConfigurationRequired('Protected model-response reading is not connected')
                args=operation.args
                data=self.gateway.read_response(args.get('reference'),task_id=task['id'],offset=args.get('offset',0),
                    limit=args.get('limit',16384),expected_view_hash=args.get('expected_view_hash'))
                data['next_args']=None if data['complete'] else {'mode':'model_response','reference':data['reference'],
                    'offset':data['next_offset'],'limit':args.get('limit',16384),'expected_view_hash':data['view_sha256']}
                return OperationResult(operation_id=operation.id,status='succeeded',data=data)
            snapshot=self._history_snapshot(task['id'],operation.args.get('snapshot_id'))
            if operation.args.get('mode')=='index':
                offset=operation.args.get('offset',0);limit=operation.args.get('limit',20)
                if type(offset) is not int or type(limit) is not int or offset<0 or not 1<=limit<=100:raise PolicyError('Invalid history index range')
                index=self._history_index(task['id'],snapshot['rows'],snapshot['id']);version=digest(index)
                if operation.args.get('expected_hash') and operation.args['expected_hash']!=version:raise PolicyError('History index changed; restart the explicit lookup')
                next_offset=offset+limit if offset+limit<len(index) else None
                return OperationResult(operation_id=operation.id,status='succeeded',data={'index_hash':version,'snapshot_id':snapshot['id'],
                    'observed_at':snapshot['observed_at'],'scope':snapshot['scope'],'total':len(index),
                    'items':index[offset:offset+limit],'next_offset':next_offset,
                    'next_args':{'mode':'index','snapshot_id':snapshot['id'],'expected_hash':version,'offset':next_offset,'limit':limit} if next_offset is not None else None,
                    'full_records_read':False})
            ids=operation.args.get('operation_ids',[])
            by_id={r['operation']['id']:r for r in snapshot['rows']}
            if any(i not in by_id for i in ids):raise PolicyError('History operation is absent from this owned snapshot')
            rows=[by_id[i] for i in ids]
            if not rows or any(r['task_id']!=task['id'] for r in rows):raise PolicyError('History read must target exact owned operations')
            raw=canonical(rows);offset=operation.args.get('offset',0);limit=operation.args.get('max_chars',24000)
            if type(offset) is not int or type(limit) is not int or offset<0 or not 1<=limit<=65536:raise PolicyError('Invalid history content range')
            if operation.args.get('expected_hash') and operation.args['expected_hash']!=digest(rows):raise PolicyError('History values changed')
            next_offset=offset+limit if offset+limit<len(raw) else None
            data={'history_sha256':digest(rows),'snapshot_id':snapshot['id'],'observed_at':snapshot['observed_at'],'scope':snapshot['scope'],
                'total_chars':len(raw),'offset':offset,'next_offset':next_offset,
                'next_args':{'operation_ids':ids,'snapshot_id':snapshot['id'],'expected_hash':digest(rows),'offset':next_offset,'max_chars':limit} if next_offset is not None else None,
                'complete':offset==0 and len(raw)<=limit}
            if data['complete']:data['history']=rows
            else:data.update(json_range=raw[offset:offset+limit],full_records_read=False)
            return OperationResult(operation_id=operation.id,status='succeeded',data=data)
        if operation.kind=='knowledge_read':
            args=operation.args
            value=self.knowledge.read(task,args['kind'],args['identity'],args.get('expected_hash')) if 'kind' in args else self.knowledge.describe_episode(task,args['episode_id'],args.get('expected_hash'))
            raw=canonical(value);offset=args.get('offset',0);limit=args.get('max_chars',24000)
            if type(offset) is not int or type(limit) is not int or offset<0 or not 1<=limit<=65536:raise PolicyError('Invalid knowledge read range')
            data=value if offset==0 and len(raw)<=limit else {'json_range':raw[offset:offset+limit],'total_chars':len(raw),'next_offset':offset+limit if offset+limit<len(raw) else None,
                'offset':offset,'complete_visible_value':False,'full_value_sha256':digest(value)}
            return OperationResult(operation_id=operation.id,status='succeeded',data=data)
        if operation.kind=='knowledge_index':
            return OperationResult(operation_id=operation.id,status='succeeded',data=self.knowledge.index(task,**operation.args))
        if operation.kind=='knowledge_projection_repair':
            return self.knowledge.repair_projection(task,operation.id,operation.args)
        if operation.kind=='source_prepare':
            return await self.source_preparation.execute(task,operation)
        if operation.kind=='candidate_disposition':
            return self._controller_commit(task,operation,lambda:OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',data=self.knowledge.dispose_candidate(task,operation.id,operation.args)))
        if operation.kind=='knowledge_update':
            return self._controller_commit(task,operation,lambda:OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',data=self.knowledge.resolve_ideas(task['id'],operation.args.get('decisions',[]),operation.id)))
        if operation.kind=='knowledge_integrate':
            return self._controller_commit(task,operation,lambda:OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',data=self.knowledge.integrate_candidate(task,operation.id,operation.args['candidate_id'],operation.args['expected_hash'])))
        if operation.kind=='judgment_resolve':
            description=await self._describe_operation(task,operation.model_dump())
            def resolve_judgment():
                correction=description['correction']
                if (self.store.record_get('judgment_correction',correction['id'])!=correction
                        or self._judgment_source(task,correction)!=description['original_source']):
                    raise PolicyError('Judgment source changed before its resolution commit')
                self.store.record('judgment_correction_history',correction['id']+':'+correction['hash'],correction)
                updated=dict(correction,status='resolved',resolution_operation_id=operation.id,resolution=operation.args,resolved_at=now())
                self.store.record('judgment_correction',correction['id'],updated)
                return OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',data={
                    'resolved_review':correction['id'],'resolution':operation.args,
                    'original_consumer':description['original_source']['consumer']})
            return self._controller_commit(task,operation,resolve_judgment)
        if operation.kind=='child_reconcile':
            description=await self._describe_operation(task,operation.model_dump())
            previous=description['original_operation'];child=self.store.get_task(description['child_id'])
            return await self._observe_recovery(task,operation,child,previous)
        if operation.kind=='reconcile':
            description=await self._describe_operation(task,operation.model_dump())
            return await self._observe_recovery(task,operation,task,description['original_operation'])
        if operation.kind in ('file_read','file_write','file_list','exec','capability_request'):
            return await self.executor.execute(Path(task['workspace']),operation)
        if operation.kind=='controller_read':
            root=Path(__file__).resolve().parents[2]
            rel=operation.args.get('path','')
            if not (rel.startswith(('src/policy_harness/','tests/','docs/')) or rel in {'pyproject.toml','uv.lock'}):raise PolicyError('Controller source read is outside explicit public implementation scope')
            path=confined(root,rel);raw=path.read_bytes()
            return OperationResult(operation_id=operation.id,status='succeeded',data={'path':rel,'sha256':hashlib.sha256(raw).hexdigest(),'text':raw.decode('utf-8'),'bytes':len(raw)})
        if operation.kind=='child_integrate':
            candidate=self._child_candidate(task,operation.args)
            write=operation.model_copy(update={'kind':'file_write','args':{'path':candidate['destination_path'],'base64':candidate['base64'],'expected_sha256':candidate['expected_sha256']}})
            result=await self.executor.execute(Path(task['workspace']),write)
            result.data['integration_source']={k:candidate[k] for k in ('child_id','source_path','sha256')}
            return result
        if operation.kind in {'prepare_update','verify_update','activate_update','rollback_update'}:
            return await self._dispatch_update(task,operation)
        if operation.kind=='delegate':
            objective=operation.args.get('objective','')
            acceptance=operation.args.get('acceptance',[])
            if not operation.args.get('independent_scope') or not operation.args.get('integration_plan'):
                raise PolicyError('Delegation needs independent scope and real parent integration plan')
            models=self._delegate_models(task,operation.model_dump())
            def create_child():
                child=self.store.create_task(objective,acceptance,parent_id=task['id'])
                lease={'parent_id':task['id'],'parent_operation_id':operation.id,'parent_objective':task['objective'],'parent_acceptance':task['acceptance'],
                    'parent_source_hash':task['source_hash'],
                    'constraints':task['state'].get('plan',{}).get('constraints',[]),'preservation':task['state'].get('plan',{}).get('preservation',[]),
                    'policy_hash':self.policy.hash,'independent_scope':operation.args['independent_scope'],'integration_plan':operation.args['integration_plan'],
                    'model_leases':models,
                    'shared_knowledge_write':'candidate-only; primary parent consumes and applies shared changes','resources':operation.args.get('resources',{})}
                self.store.update_task(child['id'],state={'delegation_lease':lease})
                return OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',data={'child_id':child['id'],'workspace':child['workspace'],'lease':lease})
            result=self._controller_commit(task,operation,create_child)
            self.start_task(result.data['child_id'])
            return result
        if operation.kind=='wait_children':
            ids=operation.args.get('child_ids',[])
            if not ids or not operation.args.get('why_no_independent_work'):
                raise PolicyError('Wait requires real child IDs and why no useful independent work remains')
            handles=[]
            for identity in ids:
                child=self.store.get_task(identity)
                if child['parent_id']!=task['id']:raise PolicyError('Cannot wait on another task owner')
                if child['status']=='created':self.start_task(identity)
                handle=self.running.get(identity)
                if handle and not handle.done():handles.append(handle)
            if handles:await asyncio.gather(*(asyncio.shield(h) for h in handles))
            children=[self.snapshot(i) for i in ids]
            return OperationResult(operation_id=operation.id,status='succeeded',data={'children':children})
        if operation.kind=='web_fetch':
            web=await self.web.collect(operation.args['url'],phase='operation',task_id=task['id'],operation_id=operation.id,
                evaluate=lambda stage,detail:self._web_acquisition_evaluate(task['id'],operation.model_dump(),stage,detail))
            result=OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',data=web)
            self.store.record('web_effect',operation.id,{'id':operation.id,'task_id':task['id'],'action_hash':digest(operation.model_dump()),'result':result.model_dump()})
            return result
        if operation.kind=='policy_amendment':
            def save_amendment():
                amendment=self.policy.amendment(operation.args)
                amendment.update(id=uuid4().hex,task_id=task['id'],operation_id=operation.id,status='awaiting_user',created_at=now())
                self.store.record('policy_amendment',amendment['id'],amendment)
                return OperationResult(operation_id=operation.id,status='pending',data={'approval_id':amendment['id'],'proposal':amendment},effect='confirmed')
            return self._controller_commit(task,operation,save_amendment)
        if operation.kind=='finish':
            return OperationResult(operation_id=operation.id,status='succeeded',data={'completion_proposal':operation.args,'meaning':'Completion still requires post-review, learning, Skill cleanup and full acceptance.'})
        raise PolicyError('Unknown operation requires capability preparation')

    async def _observe_recovery(self,task,operation,owner,previous,*,resume=False):
        """Persist intent before a possible stop; continuation only reads evidence."""
        saved=self.store.record_get('controller_effect',operation.id)
        if saved:
            if saved['task_id']!=task['id'] or saved['action_hash']!=digest(operation.model_dump()):
                raise PolicyError('Recovery observation receipt changed')
            if not resume or saved['result'].get('effect')!='unknown':
                return OperationResult.model_validate(saved['result'])
            self.store.record('recovery_observation_history',operation.id+':'+digest(saved),saved)
        intent=self.store.record_get('recovery_observation',operation.id)
        if intent:
            if (intent['task_id']!=task['id'] or intent['action_hash']!=digest(operation.model_dump())
                    or intent['owner_task_id']!=owner['id'] or intent['original_operation']['operation']['id']!=previous['operation']['id']):
                raise PolicyError('Recovery intent owner or exact operation changed')
            previous=intent['original_operation']
            resume=True
        elif resume:
            return OperationResult(operation_id=operation.id,status='failed',effect='none',
                data={'reason':'Recovery observation was not started under its reviewed durable protocol',
                      'original_response_unobserved':True,'replayed':False})
        else:
            intent={'id':operation.id,'task_id':task['id'],'action_hash':digest(operation.model_dump()),
                    'owner_task_id':owner['id'],'original_operation':deepcopy(previous),
                    'stop_requested':operation.args.get('stop',False),'started_at':now()}
            self.store.record('recovery_observation',operation.id,intent)
        # Never repeat an interrupted stop. Current executor/domain records may
        # establish today's scoped effect while preserving the original unknown.
        evidence=await self._recover_effect(owner,previous,stop=False if resume else intent['stop_requested'])
        if isinstance(evidence,OperationResult):evidence=evidence.model_dump()
        data={'original_operation_id':previous['operation']['id'],
              'original_result_hash':digest(previous['result']),'reconciliation':evidence,
              'stop_requested':intent['stop_requested'],'stop_replayed':False,
              'original_observation_unobserved':resume,
              'note':'Current domain observation; original operation is never replayed. Post-review precedes current-result reconciliation.'}
        if owner['id']!=task['id']:data['child_id']=owner['id']
        effect=('unknown' if evidence.get('effect')=='unknown' else 'confirmed') if intent['stop_requested'] else 'none'
        result=OperationResult(operation_id=operation.id,status='succeeded',effect=effect,
                               started_at=intent['started_at'],data=data)
        self.store.record('controller_effect',operation.id,{'id':operation.id,'task_id':task['id'],
                          'action_hash':digest(operation.model_dump()),'result':result.model_dump()})
        return result

    async def _recover_effect(self,task,previous,stop=False):
        operation=previous['operation'];kind=operation['kind'];identity=operation['id']
        if type(stop) is not bool:raise PolicyError('Recovery stop must be a JSON boolean')
        if kind in {'file_read','file_write','file_list','exec','capability_request','child_integrate'}:
            evidence=await self.executor.reconcile(identity,stop=stop)
            if evidence.data.get('reason')=='OPERATION_NOT_FOUND':
                evidence=evidence.model_copy(update={'status':'unknown','effect':'unknown','stderr':'Executor journal missing; absence is not proof of no prior effect'})
            return evidence
        saved=self.store.record_get('controller_effect',identity)
        if saved and saved['task_id']==task['id'] and saved['action_hash']==digest(operation):
            if kind not in {'reconcile','child_reconcile'} or saved['result'].get('effect')!='unknown':
                return OperationResult.model_validate(saved['result'])
        if kind in {'reconcile','child_reconcile'}:
            candidate=previous.get('pre_bundle',{}).get('candidate') or {}
            intent=self.store.record_get('recovery_observation',identity)
            if not intent and candidate.get('recovery_protocol')!='durable-observation-v1':
                return OperationResult(operation_id=identity,status='unknown',effect='unknown',
                    data={'reason':'Legacy recovery observation has no durable protocol evidence','replayed':False})
            target=self.store.get_operation(operation['args']['operation_id'])
            owner=self.store.get_task(target['task_id'])
            if kind=='child_reconcile':
                if owner['id']!=operation['args'].get('child_id') or owner.get('parent_id')!=task['id']:
                    raise PolicyError('Recovery observation child ownership changed')
            elif owner['id']!=task['id']:raise PolicyError('Recovery observation target is foreign')
            return await self._observe_recovery(task,Operation.model_validate(operation),owner,target,resume=True)
        if kind=='web_fetch':
            observed=self.store.record_get('web_effect',identity)
            if observed and observed['task_id']==task['id'] and observed['action_hash']==digest(operation):
                return OperationResult.model_validate(observed['result'])
        failed=self.store.record_get('controller_failure',identity)
        if failed and failed['task_id']==task['id'] and failed['action_hash']==digest(operation) and failed['transaction_rolled_back']:
            projection=self.knowledge.rebuild_projections()
            unresolved=bool(projection.get('conflicts'))
            return OperationResult(operation_id=identity,status='unknown' if unresolved else 'failed',effect='unknown' if unresolved else 'none',
                stderr=failed['first_fault']['message'],data={'transaction_rolled_back':True,'first_fault':failed['first_fault'],'projection_recovery':projection,'replayed':False})
        if kind in {'prepare_update','verify_update','activate_update','rollback_update'} and self.updates is not None:
            candidate_id=operation['args'].get('candidate_id') or previous.get('pre_bundle',{}).get('candidate',{}).get('candidate_sha256')
            if candidate_id:
                data=await self.updates.reconcile(candidate_id,stop=stop)
                return OperationResult(operation_id=identity,status='unknown' if data.get('effect')=='unknown' else 'failed',effect=data.get('effect','unknown'),data=data)
        if kind=='knowledge_projection_repair':
            return self.knowledge.reconcile_projection_repair(task,identity)
        if kind=='source_prepare':
            return await self.source_preparation.reconcile(task,Operation.model_validate(operation))
        if kind in {'history_read','controller_read','knowledge_read','knowledge_index','source_read','finish','wait_children'}:
            return OperationResult(operation_id=identity,status='failed',effect='none',data={'reason':'Read-only result lost; original response unobserved, no original operation replayed'})
        # Missing domain evidence never becomes executor NOT_FOUND/no effect.
        return OperationResult(operation_id=identity,status='unknown',effect='unknown',data={'reason':'Original effect-domain evidence is required','kind':kind,'replayed':False})

    async def _dispatch_update(self,task,operation):
        if task['parent_id'] is not None:raise PolicyError('A child proposes shared controller changes; only the primary parent may apply them')
        if self.updates is None:raise ConfigurationRequired('Controller update manager is not connected')
        self.controller_waiters.add(task['id'])
        self.cycle_boundary.set()
        try:
            await self.controller_lock.acquire()
        finally:
            self.controller_waiters.discard(task['id'])
        try:
            if self.quiescing and self.quiescing!=task['id']:
                return OperationResult(operation_id=operation.id,status='pending',effect='none',
                    data={'controller_quiesced':True,'target_started':False,'owner_task_id':self.quiescing})
            if operation.kind=='prepare_update':
                row=self.store.get_operation(operation.id)
                candidate=row['pre_bundle']['candidate']
                data=self.updates.stage(Path(task['workspace']),dict(operation.args,
                    expected_candidate_sha256=candidate['candidate_sha256']))
            elif operation.kind=='verify_update':
                data=await self.updates.verify(operation.args['candidate_id'])
            else:
                if self.on_restart is None:raise ConfigurationRequired('A fresh-process supervisor must be connected before activation')
                self.quiescing=task['id']
                self.restart_resume_ids=[t['id'] for t in self.store.list_tasks() if t['status']=='running']
                try:
                    # Waiters have not started a controller effect and can be
                    # parked below this lock. Waiting for them while retaining
                    # the lock would create an unresolvable wait-for cycle.
                    while self.active_cycles-self.controller_waiters-{task['id']}:
                        self.cycle_boundary.clear()
                        if self.active_cycles-self.controller_waiters-{task['id']}:await self.cycle_boundary.wait()
                    data=(self.updates.activate if operation.kind=='activate_update' else self.updates.rollback)(operation.args['candidate_id'])
                    if data.get('effect')!='unknown' and data.get('status') in {'succeeded','activated','rolled_back'}:
                        self.restart_pending={'owner_task_id':task['id'],'operation_id':operation.id,'candidate_id':operation.args['candidate_id']}
                except BaseException:
                    self.quiescing=None
                    for identity in self.restart_resume_ids:
                        if identity!=task['id'] and self.store.get_task(identity)['status']=='running':self.start_task(identity)
                    raise
            status='succeeded' if data.get('status') in {'succeeded','activated','rolled_back'} or data.get('staged') else 'unknown' if data.get('effect')=='unknown' else 'failed'
            return OperationResult(operation_id=operation.id,status=status,effect=data.get('effect','confirmed'),data=data)
        finally:
            self.controller_lock.release()

    async def _restart_at_boundary(self,task_id):
        pending=self.restart_pending
        if not pending or pending['owner_task_id']!=task_id:return
        while self.active_cycles:
            self.cycle_boundary.clear()
            if self.active_cycles:await self.cycle_boundary.wait()
        attestation=self.updates.restart_attestation(pending['candidate_id'])
        ids=[i for i in self.restart_resume_ids if self.store.get_task(i)['status'] not in {'completed','stopped'}]
        if task_id not in ids:ids.append(task_id)
        for identity in ids:
            self.store.update_task(identity,status='restart_required')
            self.store.event(identity,'controller_restart','required',{'candidate_id':pending['candidate_id'],'original_task_preserved':True})
        self.store.record('controller_restart',pending['candidate_id'],{'id':pending['candidate_id'],**pending,**attestation,'resume_task_ids':ids,'status':'awaiting_fresh_process','requested_at':now()})
        await self.on_restart({**attestation,'resume_task_ids':ids})

    async def _consume_post_review(self,task,row,attempt,*,authenticated=False):
        """Finish the same saved verdict before any new post judgment or effect."""
        identity=row['operation']['id'];reviewed=attempt['review']
        packet=self.store.record_get('bounded_input',reviewed.get('transport_ref'))
        if (not packet or packet['id']!=digest({k:v for k,v in packet.items() if k!='id'})
                or packet['task_id']!=task['id'] or packet['source_hash']!=task['source_hash']
                or packet['policy_hash']!=self.policy.hash or packet['phase']!='post:review'):
            raise PolicyError('Saved post outcome lost its original input')
        source=packet['value'];bundle=source['bundle'];web=source['web']
        if (source['operation']!=row['operation'] or bundle.get('operation')!=row['operation']
                or bundle.get('result')!=row['result'] or bundle.get('learning')!=attempt['learning']
                or bundle.get('assessments')!=attempt['assessments']
                or bundle.get('choice_assessments')!=attempt['choice_assessments']):
            raise PolicyError('Saved post outcome composition changed')
        if not authenticated:
            # _post has already authenticated/adopted these stages before its
            # applicability decision. Only the saved verdict remains to consume.
            if bundle.get('learning_choices')!=attempt['choices']:raise PolicyError('Saved post outcome targets changed')
            await self._review_bundle(task['id'],row['operation'],'post',bundle,web,previous=attempt.get('review_input'),saved=reviewed)
        if reviewed['disposition']['verdict']=='proceed':
            self.store.update_operation(identity,status='post_reviewed',post_bundle=bundle,post_web=web,post_review=reviewed)
            return {}
        if row.get('post_draft')!=bundle or row.get('post_review')!=reviewed:
            self.store.update_operation(identity,post_draft=bundle,post_review=reviewed)
        if reviewed['disposition']['verdict']=='hold':
            raise PolicyError('Post-result judgment remains held: '+reviewed['disposition']['rationale'])
        route=(self.bounded_judgments.disposition_route(task['id'],'post_disposition',bundle,reviewed['disposition'])
               if bundle.get('learning_projection') else None)
        if route and route['scope']=='governed':
            correction=self.web_judgments.park_learning(task,row['operation'],row['result'],bundle['learning'],reviewed['review'],reviewed['disposition'],route)
            if row.get('learning_governed_correction')!=correction:
                self.store.update_operation(identity,learning_governed_correction=correction)
            self.web_judgments.learning_return(task['id'],correction)
        # This receipt records consumption of the actual verdict, not acceptance
        # of a correction. Governed revisions reach here only after real return.
        attempt['revision_consumed']=True;self.store.record('post_learning_attempt',attempt['id'],attempt)
        raise RevisionNeeded('Post-result judgment needs revision before the next operation: '+reviewed['disposition']['rationale'])

    def _post_attempt_changed(self,row,attempt,feedback):
        """A self-verdict is not new evidence; a formal rejection/observation is."""
        original=attempt['assessment_context']
        supplied=original.get('actual_revision_feedback')
        sources=[]
        if isinstance(supplied,dict) and supplied.get('version'):
            from .learning_projection import validate_feedback
            validate_feedback(self.store,row['task_id'],row['operation']['id'],supplied)
            sources=[self.store.record_get(ref['kind'],ref['id'])['value'] for ref in supplied['sources']]
        failure=(feedback or {}).get('actual_application_failure')
        if failure and not ((isinstance(supplied,dict) and supplied.get('actual_application_failure')==failure)
                or any(source.get('actual_application_failure')==failure for source in sources)):
            return True
        draft=row.get('post_draft')
        if not draft:return False
        review=attempt.get('review')
        if review and row.get('post_review')==review:
            packet=self.store.record_get('bounded_input',review.get('transport_ref'))
            if packet and packet.get('value',{}).get('bundle')==draft:
                # The complete saved verdict is still authenticated by the
                # normal consumer. Equality here only identifies self-promotion.
                return False
        if 'previous_post_draft' in original:
            return original['previous_post_draft']!=draft or supplied!=feedback
        return dict(draft,review=row.get('post_review')) not in sources

    async def _post(self,state):
        identity=state['operation_id'];row=self.store.get_operation(identity)
        if row['status'] in ('post_reviewed','learned','cycle_complete'):return {}
        if row['result'] is None:raise PolicyError('Actual result missing')
        task=self.store.get_task(state['task_id']);operation=row['operation']
        from .learning_projection import current_input,revision_feedback,merge_ideas
        old_feedback=self._learning_revision_feedback(row)
        history=sorted([a for a in self.store.records('post_learning_attempt') if a['operation_id']==identity],key=lambda a:a['created_at'])
        latest=history[-1] if history else None
        attempt=latest if latest and not latest.get('revision_consumed') and latest.get('source_hash')==task['source_hash'] and latest.get('policy_hash')==self.policy.hash and latest.get('result_sha256',digest(row['result']))==digest(row['result']) else None
        if attempt:
            attempt=self.bounded_judgments.adopt_post_attempt(task['id'],row,attempt)
            if self._post_attempt_changed(row,attempt,old_feedback):attempt=None
        if attempt and attempt.get('status')=='reviewed':return await self._consume_post_review(task,row,attempt)
        if attempt:history=history[:-1]
        if row.get('post_draft'):history.append(dict(row['post_draft'],review=row.get('post_review')))
        if old_feedback and old_feedback.get('actual_application_failure'):history.append(old_feedback)
        feedback=revision_feedback(self.store,task['id'],identity,history)
        returned=self.web_judgments.learning_return(task['id'],row['learning_governed_correction']) if row.get('learning_governed_correction') else None
        web=attempt['assessment_context']['web'] if attempt else await self._research(task['id'],operation,'post',{'result':row['result'],'decisions':operation['decisions'],'pre':row['pre_bundle']})
        targets=row['pre_bundle'].get('targets') or operation_targets(operation,row['pre_bundle']['skills'])
        old_context={'operation':operation,'pre':row['pre_bundle'],'result':row['result'],'web':web,
            'previous_post_draft':row.get('post_draft'),'actual_revision_feedback':old_feedback,'timing':'after the actual operation result; compare expected versus observed, preserve unknowns'}
        assessment_context=current_input({k:v for k,v in old_context.items() if k not in {'previous_post_draft','actual_revision_feedback'}})
        assessment_context['actual_revision_feedback']=feedback
        if attempt and attempt.get('assessments'):
            assessments=attempt['assessments']
            assessment_context=attempt['assessment_context']
        else:
            assessments=await self._assess_targets(task['id'],'post_assessment',targets,assessment_context,previous=(targets,old_context))
            assessment_context=self.bounded_judgments.assessment_context(task['id'],'post_assessment',targets,assessments)
        if attempt is None:
            attempt={'id':uuid4().hex,'task_id':task['id'],'operation_id':identity,'policy_hash':self.policy.hash,
                'source_hash':task['source_hash'],'created_at':now(),'assessments':assessments,'status':'assessed',
                'result_sha256':digest(row['result']),'assessment_context':assessment_context}
            self.store.record('post_learning_attempt',attempt['id'],attempt)
        required=self._post_required_ideas(row,assessments)
        if 'learning' not in attempt:
            if 'learning_input' not in attempt:
                attempt['learning_input']=current_input({'operation':operation,'result':row['result'],'pre':row['pre_bundle'],'post':assessments,
                    'actual_revision_feedback':feedback,'required_ideas':required,
                    **({'governed_correction_return':returned} if returned else {}),
                    'knowledge':self._knowledge_context(task,operation),'web':web,
                    'instruction':'Classify every actual result into real create/use/improve/organize/merge/retire Skill work. Propose concrete updates with source, applicability and next trigger. Unknown recurrence produces provisional knowledge. Do not claim use of an unselected Skill or verified benefit from registration. Assess/dispose all pre/post ideas; adopted feasible workflow changes need an actual next operation and verification/use, not a promise.'})
                self.store.record('post_learning_attempt',attempt['id'],attempt)
            learning=await self.bounded_judgments.learning_proposal(task['id'],'learning_proposal',attempt['learning_input'])
            self._validate_ideas(required,learning)
            attempt.update(learning=learning.model_dump(),status='learning_proposed')
            receipt=self.bounded_judgments._learning_receipts.get(digest(learning.model_dump()))
            if receipt:attempt['proposal_receipt']=self.bounded_judgments._call_ref(receipt)
            composition=self.bounded_judgments.learning_review_context(task['id'],identity,learning)
            if composition:attempt['learning_composition']=composition
            self.store.record('post_learning_attempt',attempt['id'],attempt)
        learning=Learning.model_validate(attempt['learning'])
        resumed_choices='choice_assessments' in attempt
        if not resumed_choices:
            self.bounded_judgments.authenticate_learning_attempt(task['id'],'learning_proposal',attempt,
                {'operation':operation,'result':row['result'],'pre':row['pre_bundle']},assessment_context.get('actual_revision_feedback'))
            choices=decision_targets('learning',learning,group_ideas=True)
            old_choices=decision_targets('learning',learning)
            old_choice_context={'operation':operation,'result':row['result'],'learning':learning.model_dump(),'timing':'before actual knowledge mutation'}
            choice_context=current_input(old_choice_context)
            choice_assessments=await self._assess_targets(task['id'],'learning_choices_before',choices,choice_context,
                previous=(old_choices,old_choice_context))
            choices=self.bounded_judgments.assessment_targets(task['id'],'learning_choices_before',choice_assessments,[choices,old_choices])
            choice_context=self.bounded_judgments.assessment_context(task['id'],'learning_choices_before',choices,choice_assessments)
            learning.ideas=[Idea.model_validate(i) for i in merge_ideas(learning.model_dump()['ideas'],
                [i for a in choice_assessments for i in a['assessment']['ideas']])]
            attempt.update(learning=learning.model_dump(),choices=choices,choice_assessments=choice_assessments,choice_context=choice_context,status='awaiting_review')
            self.store.record('post_learning_attempt',attempt['id'],attempt)
        choices=attempt['choices'];choice_assessments=attempt['choice_assessments']
        bundle=current_input({'operation':operation,'result':row['result'],'assessments':assessments,'learning':learning.model_dump(),'learning_choices':choices,'choice_assessments':choice_assessments})
        admission=row['pre_bundle'].get('policy_admission')
        if admission and admission.get('prerequisite_contract') and admission['decision']['required_preparation']:
            bundle['admission_obligations']={'admission':admission,'before_review':row['pre_review'],
                'meaning':'The pre-review rejects only mistaken BEFORE-action claims. It does not fulfill '
                    'or waive later checks, conditional recovery or preserved invariants. Judge these '
                    'against the actual result; still-future work remains required at completion.'}
        composition=attempt.get('learning_composition') or self.bounded_judgments.learning_review_context(task['id'],identity,learning)
        if composition:bundle['learning_composition']=composition
        old_bundle={k:v for k,v in bundle.items() if k not in {'learning_projection','learning_correction_scope'}}
        previous=attempt.get('review_input') or self.bounded_judgments.review_bundle_input(operation,old_bundle,web)
        if 'review_input' not in attempt:
            attempt['review_input']=previous;self.store.record('post_learning_attempt',attempt['id'],attempt)
        reviewed=await self._review_bundle(task['id'],operation,'post',bundle,web,previous=previous)
        attempt['review_input']=self.bounded_judgments.review_input(task['id'],'post_review',reviewed['review'])
        attempt.update(review=reviewed,status='reviewed')
        self.store.record('post_learning_attempt',attempt['id'],attempt)
        return await self._consume_post_review(task,row,attempt,authenticated=True)

    def _post_required_ideas(self,row,assessments):
        """Retain identities from every actual draft, including an interrupted one."""
        required={}
        def add(idea):
            previous=required.get(idea['id'])
            if previous and (previous['proposal'],previous['target'])!=(idea['proposal'],idea['target']):
                raise PolicyError('Prior idea identity was reused for different meaning')
            required[idea['id']]=idea
        drafts=[row.get('post_draft') or {},row.get('post_bundle') or {},
            *[a for a in self.store.records('post_learning_attempt') if a['operation_id']==row['operation']['id']]]
        # Retain each real response before a later page can fail or interrupt.
        for observation in self.store.records('bounded_model_call'):
            phase=observation.get('phase','').split(':page:',1)[0]
            if (observation.get('task_id')==row['task_id']
                    and observation.get('payload',{}).get('operation',{}).get('id')==row['operation']['id']
                    and observation.get('status')=='succeeded'):
                if phase in {'post_assessment','learning_choices_before'}:
                    drafts.append({'assessments':observation['result']['assessments']})
                elif phase=='learning_proposal':
                    drafts.append({'learning':observation['result']})
        drafts.append({'learning':{'ideas':self._learning_response_history(row['task_id'],row['operation']['id'])['ideas']}})
        for draft in [row['pre_bundle'],*drafts,{'assessments':assessments}]:
            for key in ('assessments','choice_assessments'):
                for assessment in draft.get(key,[]):
                    for idea in assessment['assessment']['ideas']:add(idea)
            for idea in draft.get('learning',{}).get('ideas',[]):add(idea)
        return list(required.values())

    def _learning_revision_feedback(self,row):
        reference=row.get('learning_contract_failure_ref')
        if not reference:return row.get('post_review')
        failure=self.store.record_get('learning_contract_failure',reference)
        if (not failure or failure['task_id']!=row['task_id']
                or failure.get('id')!=reference or digest({k:v for k,v in failure.items() if k!='id'})!=reference
                or failure['original'].get('task_id')!=row['task_id']
                or failure['original']['operation']!=row['operation']
                or failure['original']['result']!=row['result']):
            raise PolicyError('Learning correction lost its original operation/result failure binding')
        return {'review':row.get('post_review'),'actual_application_failure':failure,
            'instruction':'Produce a new complete Learning for this same actual result. Preserve prior idea identities and all actual opinions/failures. No target action is rerun; the new proposal receives full normal review.'}

    def _reject_saved_learning(self,task,row,error):
        """Return a known invalid uncommitted proposal to review, never execute."""
        failure={'task_id':task['id'],'operation_id':row['operation']['id'],
            'policy_hash':self.policy.hash,'source_hash':task['source_hash'],'original':deepcopy(row),
            'fault':{'type':type(error).__name__,'message':str(error)},
            'prior_application_failures':[x for x in self.store.records('knowledge_application_failure')
                if x['task_id']==task['id'] and x['operation_id']==row['operation']['id']],
            'phase':'preapplication-validation','target_effect':'none; pure validation',
            'observed_at':now()}
        failure['id']=digest(failure)
        def preserve():
            self.knowledge._require_clean_application(task,row['operation'])
            if self.store.get_operation(row['operation']['id'])!=row:
                raise PolicyError('Learning source changed during proposal validation')
            self.store.record('learning_contract_failure',failure['id'],failure)
            self.store.update_operation(row['operation']['id'],status='result_recorded',
                post_draft=row['post_bundle'],learning_contract_failure_ref=failure['id'])
            self.store.event(task['id'],'learning_contract','revision_required',{
                'failure_ref':failure['id'],'operation_id':row['operation']['id'],
                'fault':failure['fault'],'original_result_preserved':True,'action_replayed':False})
        self.store._transaction(preserve)

    async def _learn(self,state):
        identity=state['operation_id'];row=self.store.get_operation(identity)
        if row['status'] in ('learned','cycle_complete'):return {}
        if row['status']!='post_reviewed':raise PolicyError('Reviewed result and learning are required')
        task=self.store.get_task(state['task_id'])
        learning=Learning.model_validate(row['post_bundle']['learning'])
        learning_result=row['post_bundle']['result']
        if learning_result!=row['result']:
            observed=self.store.record_get('effect_reconciliation',row.get('reconciliation_ref',''))
            if (not observed or observed['original']['operation']!=row['operation']
                    or observed['original']['post_bundle']!=row['post_bundle']
                    or observed['original']['result']!=learning_result or observed['reconciled']!=row['result']):
                raise PolicyError('Learning result differs without an exact reviewed recovery lineage')
        selected=[x['id'] for x in row['pre_bundle']['skills']['selected']]
        # A committed input wins over current target versions and new schemas.
        # Knowledge.apply will verify/recover that exact original commit itself.
        observed=self.knowledge.application_state(task,row['operation'],learning,learning_result,selected)
        if observed['state']=='unknown':
            raise PolicyError('Knowledge episode exists without its atomic application marker; preserve the inconsistent original before further application')
        if observed['state']!='committed':
            try:self.knowledge.validate_learning_proposal(task,row['operation'],learning,learning_result,selected)
            except LearningContractError as error:
                self._reject_saved_learning(task,row,error)
                raise RevisionNeeded('Stored Learning needs a new reviewed proposal: '+str(error)) from error
            failed=[f for f in self.store.records('knowledge_application_failure') if f['task_id']==task['id']
                and f['operation_id']==identity and f['original']['post_bundle']['learning']==learning.model_dump()
                and f['original']['post_bundle']['result']==learning_result]
            if failed:
                recovery={'observation':observed,'failure_refs':[f['id'] for f in failed],
                    'action':'Recover the exact commit or apply the original accepted input after observed non-commit; no replacement proposal or target operation.'}
                self.store.record('knowledge_application_reconciliation',digest(recovery),recovery)
        if row['operation']['kind']=='source_read' and row['result']['status']=='succeeded':
            data=row['result']['data'];source_id=data['source_id']
            prior=self.store.record_get('source_read_progress',source_id) or {'next_offset':0,'operation_ids':[]}
            if data['offset']==prior['next_offset'] and identity not in prior['operation_ids']:
                self.store.record('source_read_progress',source_id,{'id':source_id,'task_id':task['id'],
                    'next_offset':data['next_offset'] if data['next_offset'] is not None else data['total_bytes'],
                    'complete':data['next_offset'] is None,'text_decoded':data.get('text_decoded',False) and prior.get('text_decoded',True),
                    'operation_ids':[*prior['operation_ids'],identity],'source_sha256':data['sha256']})
        self.store.event(task['id'],'knowledge_application','started',{'operation_id':identity})
        try:outcome=self.knowledge.apply(task,row['operation'],learning,learning_result,selected)
        except Exception as error:
            failure={'task_id':task['id'],'operation_id':identity,'original':row,
                'fault':{'type':type(error).__name__,'message':str(error)},'observed_at':now(),
                'transaction_observation':deepcopy(getattr(error,'harness_transaction_observation',None)),
                'effect':'unreconciled; inspect original commit before any retry'}
            failure['id']=digest(failure)
            self.store.record('knowledge_application_failure',failure['id'],failure)
            self.store.event(task['id'],'knowledge_application','failed',{'failure_ref':failure['id'],'fault':failure['fault']})
            raise
        choices=row['post_bundle'].get('learning_choices',[])
        if choices:
            await self._judge_applied_knowledge(task,row['operation'],'learning-outcome',choices,outcome,learning_result,row['post_web'],
                context={'learning':learning.model_dump(),'selection':row['pre_bundle']['skills'],
                         'before_review':row['pre_review'],'accepted_learning_review':row['post_review'],
                         'accepted_learning_disposition':row['post_review']['disposition'],
                         'accepted_learning_provenance':{'bundle_sha256':digest(row['post_bundle']),
                             'learning_sha256':digest(learning.model_dump()),
                             'review_id':row['post_review']['id'],
                             'application_id':task['id']+':'+identity,
                             'application_input_hash':self.store.record_get('knowledge_application',task['id']+':'+identity)['input_hash']}},
                consumer={'kind':'operation','operation_id':identity,'operation_sha256':digest(row['operation'])})
        if row['operation']['kind'] in {'reconcile','child_reconcile'} and row['result']['status']=='succeeded':
            data=row['result']['data']
            reconciled=OperationResult.model_validate(data['reconciliation'])
            original=self.store.get_operation(data['original_operation_id'])
            if reconciled.operation_id!=original['operation']['id']:raise PolicyError('Recovery returned a different operation identity')
            owner=task['id']
            already_reconciled=False
            if row['operation']['kind']=='child_reconcile':
                child=self.store.get_task(data['child_id']);owner=child['id']
                if child['parent_id']!=task['id']:raise PolicyError('Recovery child owner changed')
                if digest(original['result'])!=data['original_result_hash']:
                    already_reconciled=original.get('reconciliation_ref')==identity and original['result']==reconciled.model_dump()
                    if not already_reconciled:raise PolicyError('Original child result changed before reviewed reconciliation')
            if original['task_id']!=owner:raise PolicyError('Recovery result belongs to a different task')
            if not already_reconciled and reconciled.effect!='unknown' and reconciled.data.get('reason')!='OPERATION_NOT_FOUND':
                def record_reconciliation():
                    if self.store.record_get('effect_reconciliation',identity):return
                    history={'id':identity,'original':original,'reconciled':reconciled.model_dump(),'review_ref':row['post_review']['id'],'observed_at':now()}
                    self.store.record('effect_reconciliation',identity,history)
                    self.store.update_operation(original['operation']['id'],result=reconciled.model_dump(),reconciliation_ref=identity)
                    self.store.event(owner,'recovery','reconciled',{'operation_id':original['operation']['id'],'history_ref':identity,'result':reconciled.model_dump(),'review_owner_task_id':task['id']})
                self.store._transaction(record_reconciliation)
        self.store.update_operation(identity,status='learned',knowledge=outcome)
        self.store.event(task['id'],'knowledge_application',
            'partial' if outcome.get('projection_status',{}).get('state')=='pending' else 'succeeded',outcome)
        return {}

    def _confirm_reviewed_outcomes(self,task,row):
        """Return a resolved after-review to the exact original completion consumer.

        This consumes existing reviewed results only. It cannot rerun a tool or
        waive another pending opinion, including one on the resolution itself.
        """
        visited=set()
        while True:
            identity=row['operation']['id']
            if identity in visited:raise PolicyError('Judgment resolution lineage contains a cycle')
            visited.add(identity)
            if row['task_id']!=task['id']:raise PolicyError('Judgment completion belongs to another task')
            if row['status'] not in {'learned','cycle_complete'} or (row.get('result') or {}).get('status')!='succeeded':return
            if any(c['operation_id']==identity for c in self._pending_judgments(task['id'])):return
            kind=row['operation']['kind']
            if kind=='knowledge_projection_repair':
                self.knowledge.confirm_projection_repair(task,identity)
            if kind=='source_prepare':
                self.source_preparation.confirm(task,identity)
            if kind=='knowledge_update' and self.updates is not None and hasattr(self.updates,'consume_use_review'):
                observed=self.updates.consume_use_review(task['id'],row['operation'],row['result'])
                self.store.record('update_use_review',identity,observed)
            if kind in {'reconcile','child_reconcile'}:
                original_id=row['result']['data'].get('original_operation_id')
                original=self.store.get_operation(original_id)
                if original['operation']['kind']!='source_prepare':return
                # The recovery's full review/learning and any correction close
                # before the original preparation can consume its actual result.
                if original.get('reconciliation_ref')!=identity:
                    raise PolicyError('Source recovery completion lost its exact successor')
                if kind=='child_reconcile':
                    child=self.store.get_task(original['task_id'])
                    if child['parent_id']!=task['id'] or row['result']['data'].get('child_id')!=child['id']:
                        raise PolicyError('Source recovery completion belongs to another child')
                    task=child
                row=original
                continue
            if kind!='judgment_resolve':return
            if any(c['consumer']['parent_operation_id']==identity
                   for c in self.web_judgments.correction_frontier(task['id'])):return
            correction=self.store.record_get('judgment_correction',row['result']['data'].get('resolved_review'))
            if (not correction or correction['task_id']!=task['id'] or correction['status']!='resolved'
                    or correction.get('resolution_operation_id')!=identity
                    or correction.get('resolution')!=row['operation']['args']):
                raise PolicyError('Judgment completion lost its exact resolved source')
            original=self._judgment_source(task,correction)
            expected=row['result']['data'].get('original_consumer')
            if expected is not None and original['consumer']!=expected:
                raise PolicyError('Judgment completion differs from its exact original consumer')
            if original['consumer']['kind']=='web_acquisition' or original['consumer'].get('stage')=='uncommitted_learning':
                if self.web_judgments.has_pending_return(task['id'],identity):return
                self.web_judgments.confirm_correction(task,correction,row)
                return
            row=self.store.get_operation(correction['operation_id'])

    async def _close_cycle(self,state):
        identity=state['operation_id'];row=self.store.get_operation(identity)
        if row['status']=='cycle_complete':return {}
        if row['status']!='learned':raise PolicyError('Cannot advance without actual knowledge application')
        task=self.store.get_task(state['task_id'])
        self._confirm_reviewed_outcomes(task,row)
        if row['operation']['kind']=='adopt_policy' and row['result']['status']=='succeeded':
            self.store.update_task(task['id'],policy_hash=self.policy.hash)
            suspended=row['operation']['args'].get('suspended_operation_id')
            if suspended:
                old=self.store.get_operation(suspended)
                self.store.record('operation_policy_history',uuid4().hex,old)
                self.store.update_operation(suspended,status='result_recorded',post_policy_hash=self.policy.hash)
        if row['operation']['kind']=='finish':
            try:
                await self._finalize(task,row)
            except CycleHeld as error:
                self.store.update_operation(identity,status='cycle_complete',completion_deferred=str(error))
                current=self.store.get_task(task['id']);taskstate=dict(current['state'])
                if taskstate.get('operation_id')==identity:
                    taskstate.pop('operation_id',None);taskstate.pop('cycle_id',None)
                self.store.update_task(task['id'],state=taskstate)
                raise RevisionNeeded(str(error)) from error
        self.store.update_operation(identity,status='cycle_complete')
        current=self.store.get_task(task['id']);taskstate=dict(current['state'])
        owns_current=taskstate.get('operation_id')==identity
        if owns_current:
            taskstate.pop('operation_id',None);taskstate.pop('cycle_id',None)
        if owns_current and row['operation']['kind']=='adopt_policy' and row['operation']['args'].get('suspended_operation_id'):
            taskstate.update(operation_id=row['operation']['args']['suspended_operation_id'],cycle_id=uuid4().hex)
        self.store.update_task(task['id'],state=taskstate)
        self.store.event(task['id'],'cycle','completed',{'operation_id':identity,'result_status':row['result']['status'],'next':'Primary goal and pending required improvements'})
        return {}

    def _assert_finalization_evidence(self,task_id,row,draft):
        """Consume the original review cut, including on restart and after awaits."""
        task=self.store.get_task(task_id)
        if self.web_judgments.correction_frontier(task_id) or task['state'].get('web_correction_returns'):
            raise CycleHeld('Applied Web correction has not returned through its reviewed resolution cycle')
        obligations=self.policy_admission.completion_obligations(task_id)
        if obligations:
            raise CycleHeld('Whole-source completion has unresolved admission evidence: '+canonical(obligations))
        review=self.store.record_get('independent_review',draft.get('independent_review_id',''))
        raw=(review or {}).get('raw_evidence')
        if (draft.get('source_hash')!=task['source_hash'] or
                draft.get('acceptance_hash')!=digest(task['acceptance']) or
                draft.get('policy_hash')!=self.policy.hash or
                any(x['status']=='pending' for x in task['source_history']) or
                not review or review.get('task_id')!=task_id or
                draft.get('independent_review_sha256')!=digest(review) or
                review.get('source_hash')!=task['source_hash'] or
                review.get('acceptance_hash')!=digest(task['acceptance']) or
                review.get('policy_hash')!=self.policy.hash or not isinstance(raw,dict) or
                raw.get('task_id')!=task_id or raw.get('source_hash')!=task['source_hash'] or
                raw.get('policy_hash')!=self.policy.hash or
                draft.get('frozen_evidence_sha256')!=digest(raw) or review.get('raw_evidence_hash')!=digest(raw) or
                draft.get('history_snapshot_id')!=review.get('history_snapshot_id') or
                raw.get('snapshot_id')!=review.get('history_snapshot_id')):
            raise CycleHeld('Finalization evidence is legacy or changed; preserve its choices and cleanup, then obtain a current original-evidence review in a new finish')
        try:snapshot=self._history_snapshot(task_id,raw['snapshot_id'])
        except PolicyError as error:raise CycleHeld('Finalization evidence snapshot requires reconciliation: '+str(error)) from error
        originals=[dict(item,id=item['operation']['id']) for item in snapshot['rows']]
        original=next((item for item in originals if item['id']==row['operation']['id']),None)
        if (raw.get('rows_hash')!=snapshot['rows_hash'] or raw.get('operations')!=originals or
                not original or original['operation']!=row['operation'] or original.get('result')!=row.get('result')):
            raise CycleHeld('Finalization evidence differs from its exact reviewed operation and result snapshot')
        return raw

    async def _finalize(self,task,row):
        children=[x for x in self.store.list_tasks() if x['parent_id']==task['id']]
        if any(x['status'] not in {'completed','retired'} for x in children):raise CycleHeld('Child work is unfinished; integrate or explicitly dispose its real result before completion')
        if any(any(self._mandatory_child_work(child).values()) for child in children):raise CycleHeld('A child terminal label still has mandatory result/review/learning/cleanup work')
        if any(x['status']=='pending' for x in task['source_history']):raise CycleHeld('New user source has not been reconciled')
        if any((o.get('result') or {}).get('effect')=='unknown' or self.store.operation_started_without_result(o) for o in self.store.operations(task['id'])):raise CycleHeld('Unknown effects remain')
        if any(r['task_id']==task['id'] and r['status']=='source_revalidation_required'
               for r in self.store.records('source_preparation_confirmation_pending')):raise CycleHeld('Source confirmation still requires a current reviewed disposition')
        if self.pending_approvals(task['id']):raise CycleHeld('Exact policy amendment awaits user decision')
        if self._pending_judgments(task['id']):raise CycleHeld('Actual knowledge outcome review requires a correction and observed resolution')
        if self.web_judgments.correction_frontier(task['id']) or task['state'].get('web_correction_returns'):
            raise CycleHeld('Applied Web correction has not returned to its original parent')
        # An interrupted cleanup can predate a saved failure. Keep that original
        # intent visible to the existing governed recovery route, never replay it.
        for intent in self.store.records('cleanup_intent'):
            if (intent.get('task_id')==task['id'] and not self.store.record_get('cleanup_result',intent['id'])
                    and not self.store.record_get('cleanup_failure',intent['id'])):
                self.store.record('cleanup_failure',intent['id'],{'id':intent['id'],'task_id':task['id'],
                    'operation_id':intent['operation_id'],'status':'unknown','effect':'unknown',
                    'first_fault':{'type':'UnobservedCleanupOutcome','message':'Earlier cleanup intent has no durable outcome; reconcile its original scope without replay'},
                    'cleanup_intent_ref':{'id':intent['id'],'sha256':digest(intent)},'observed_at':now(),
                    'projection_recovery':{'state':'not_attempted','conflicts':[]},'mutation_performed':False})
        if self.knowledge.pending_projection_failures(task):raise CycleHeld('Knowledge projection failures require exact reviewed repair and confirmed readback before completion')
        if any(x['task_id']==task['id'] and x['stage']=='after' and x['status']!='complete' for x in self.store.records('web_work')):
            raise CycleHeld('Acquired Web outcomes still need classification and reviewed learning')
        pending_candidates=[x for x in self.knowledge.retrieve(task).get('candidates',[]) if not task.get('parent_id') and x.get('status','pending')=='pending']
        if pending_candidates:raise CycleHeld('Child knowledge candidates require an explicit integration or reasoned disposition')
        # A restart after cleanup must continue judging the exact committed
        # choices, not silently pair a newly generated cleanup with the old result.
        draft=row.get('finalization')
        if draft is None:
            raw_evidence=self._final_evidence(task,children)
            web=await self._research(task['id'],row['operation'],'final',{'candidate_evidence':raw_evidence,'acceptance':task['acceptance'],'closure':'Required quality, source limits, Skill cleanup, adopted workflow improvements and all child results.'})
            raw_evidence['final_web']=web
            full_knowledge=self.knowledge.snapshot(task['id'])
            knowledge=dict(self._knowledge_context(task),skills=[{k:s.get(k) for k in ('id','hash','status','uses','needs_cleanup','effect','applicability','next_use_trigger','sources')}
                for s in full_knowledge['skills'] if s['needs_cleanup']],
                cleanup_scope='All touched Skills requiring cleanup; full content is retrievable by knowledge_index/read. Other history is indexed, not claimed read.')
            independent=await self.bounded_judgments.review_proposal(task['id'],row['operation'],'independent_refutation',{'original_objective':task['objective'],'acceptance':task['acceptance'],
                'raw_evidence':raw_evidence,'children':children,
                'instruction':'Independently refute the frozen actual candidate against original acceptance, without relying on author conclusions or PASS labels. You are thought-only; distinguish review from independently executed tests. Identify missing proof, regressions, interactions, authority and remaining effects. Every supplied original belongs to this exact observation cut; a missing actual artifact read or external observation is not positive proof.'})
            independent_id=uuid4().hex
            independent=Review(summary=independent.summary,opinions=[x.model_copy(update={'id':independent_id+':'+x.id}) for x in independent.opinions])
            independent_record={'id':independent_id,'task_id':task['id'],'raw_evidence_hash':digest(raw_evidence),
                'history_snapshot_id':raw_evidence['snapshot_id'],'source_hash':task['source_hash'],'policy_hash':self.policy.hash,
                'acceptance_hash':digest(task['acceptance']),'raw_evidence':raw_evidence,
                'review':independent.model_dump(),'proof_ceiling':'All stored original evidence supplied through bounded review, with model interpretations explicit; not independently performed external tests or semantic infallibility.'}
            self.store.record('independent_review',independent_id,independent_record)
            completion=await self.bounded_judgments.completion_proposal(task['id'],{'plan':task['state']['plan'],'operations':raw_evidence['operations'],
                'frozen_evidence':raw_evidence,'acceptance':task['acceptance'],
                'children':children,'knowledge':knowledge,'independent_refutation':{'id':independent_id,'review':independent.model_dump()},
                'instruction':'Compare every original acceptance with actual artifact and independent-refutation evidence. Each acceptance has criterion, achieved, evidence_refs, independent_refs. Cite actual observed operation IDs and the supplied independent_refutation.id. No missing mandatory result is optional. Preserve false results and unknowns; an incomplete proposal returns to ordinary work.'})
            criteria={c.criterion for c in completion.acceptance}
            known={o['operation']['id'] for o in self.store.operations(task['id']) if o['result'] is not None}
            valid=bool(completion.achieved and not completion.unresolved and len(criteria)==len(completion.acceptance) and criteria==set(task['acceptance']) and all(c.achieved is True and c.evidence_refs and set(c.evidence_refs)<=known and c.independent_refs==[independent_id] for c in completion.acceptance))
            cleanup=await self._call(task['id'],'skill_cleanup_proposal',{'knowledge':knowledge,
                'instruction':'For every touched Skill with needs_cleanup=true give {id,disposition:retain|retire,reason}. Merge/organize needs ordinary work first. Retain unused provisional sources/applicability/next trigger and UNUSED_EFFECT_UNVERIFIED.'},Cleanup) if valid else Cleanup(decisions=[],rationale='Controller retains all knowledge because the completion proposal is incomplete.')
            choices=[*decision_targets('completion',completion),*decision_targets('cleanup',cleanup)]
            assessed_before=await self._assess_targets(task['id'],'completion_choices_before',choices,
                {'completion':completion.model_dump(),'cleanup':cleanup.model_dump(),'knowledge':knowledge,'web':web,'timing':'before applying the completion or continue-work decision'})
            bundle={'completion':completion.model_dump(),'cleanup':cleanup.model_dump(),'choices':choices,'assessments':assessed_before,'structural_evidence_complete':valid}
            reviewed=await self._review_bundle(task['id'],row['operation'],'final',bundle,web,[independent])
            draft={'completion':completion.model_dump(),'cleanup':cleanup.model_dump(),'choices':choices,'pre_assessments':assessed_before,'pre_review':reviewed,'web':web,'structural_evidence_complete':valid,'policy_hash':self.policy.hash,
                'source_hash':task['source_hash'],'acceptance_hash':digest(task['acceptance']),
                'independent_review_id':independent_id,'independent_review_sha256':digest(independent_record),
                'frozen_evidence_sha256':digest(raw_evidence),'history_snapshot_id':raw_evidence['snapshot_id']}
            self.store.update_operation(row['operation']['id'],finalization=draft)
        raw_evidence=self._assert_finalization_evidence(task['id'],row,draft)
        completion=Completion.model_validate(draft['completion']);cleanup=Cleanup.model_validate(draft['cleanup'])
        choices=draft['choices'];web=draft['web'];reviewed=draft['pre_review']
        criteria={c.criterion for c in completion.acceptance}
        known={o['id'] for o in raw_evidence['operations'] if o.get('result') is not None}
        evidence_complete=bool(completion.achieved and not completion.unresolved and
            len(criteria)==len(completion.acceptance) and criteria==set(task['acceptance']) and
            all(c.achieved is True and c.evidence_refs and set(c.evidence_refs)<=known and
                c.independent_refs==[draft['independent_review_id']] for c in completion.acceptance))
        cleanup_key=task['id']+':'+row['operation']['id']
        cleanup_result=self.store.record_get('cleanup_result',cleanup_key)
        cleanup_failure=self.store.record_get('cleanup_failure',cleanup_key)
        reason=None
        if not evidence_complete:
            reason='Completion evidence does not cover all original outcomes and independent refutation: '+completion.summary+'; unresolved='+canonical(completion.unresolved)
        elif reviewed['disposition']['verdict']!='proceed':reason='Final response requires revision: '+reviewed['disposition']['rationale']
        if cleanup_failure and (not cleanup_result or cleanup_failure.get('effect')!='confirmed'):
            reason='Previous cleanup failed; preserve its first fault and resolve its actual projection state through governed work.'
        if cleanup_result is None and reason is None:
            cleanup_intent=self.store.record_get('cleanup_intent',cleanup_key)
            try:
                if cleanup_intent is not None:
                    raise PolicyError('Earlier cleanup intent has no durable outcome; reconcile its original scope without replay')
                cleanup_intent=self.knowledge.capture_cleanup_intent(task,row['operation'],cleanup.decisions)
                def commit_cleanup():
                    self.knowledge.validate_cleanup_intent(task,row['operation'],cleanup.decisions,cleanup_intent)
                    before_cleanup=digest(self.knowledge.snapshot(task['id']))
                    outcome=self.knowledge.finish(task['id'],cleanup.decisions,True)
                    readback=self.knowledge.snapshot(task['id'])
                    if any(x['needs_cleanup'] for x in readback['skills']):raise PolicyError('Actual cleanup readback has unfinished Skills')
                    result={'id':cleanup_key,'operation_id':row['operation']['id'],'decisions':cleanup.decisions,'before_hash':before_cleanup,'outcome':outcome,
                        'cleanup_intent_sha256':digest(cleanup_intent),
                        'after_hash':digest(readback),'skill_readback':[{'id':s['id'],'hash':s['hash'],'status':s['status'],'needs_cleanup':s['needs_cleanup'],'effect':s['effect']} for s in readback['skills']]}
                    self.store.record('cleanup_result',cleanup_key,result)
                    return result
                cleanup_result=self.store._transaction(commit_cleanup)
            except Exception as error:
                # SQL rollback does not undo the Skill files written before a
                # later failure. Repair DB-derived projections and retain any
                # conflict/IO uncertainty before describing the actual effect.
                first_fault={'type':type(error).__name__,'message':str(error)}
                transaction=getattr(error,'harness_transaction_observation',{})
                committed=False;commit_readback={}
                try:
                    saved=self.store.record_get('cleanup_result',cleanup_key)
                    if saved is not None:
                        observed=self.knowledge.snapshot(task['id'])
                        committed=(saved.get('id')==cleanup_key and saved.get('operation_id')==row['operation']['id']
                            and saved.get('decisions')==cleanup.decisions and saved.get('after_hash')==digest(observed)
                            and not self.store.db.in_transaction)
                        commit_readback={'result_sha256':digest(saved),'observed_after_hash':digest(observed),
                                         'identity_and_state_match':committed}
                        if committed:cleanup_result=saved
                except Exception as readback_error:
                    commit_readback={'error':{'type':type(readback_error).__name__,'message':str(readback_error)}}
                try:projection=self.knowledge.rebuild_projections()
                except Exception as recovery_error:
                    projection={'conflicts':['Projection recovery could not finish'],
                        'error':{'type':type(recovery_error).__name__,'message':str(recovery_error)}}
                rolled_back=bool(transaction.get('rollback_confirmed')) and not committed
                uncertain=bool(projection.get('conflicts')) or not (committed or rolled_back)
                cleanup_failure={'id':cleanup_key,'task_id':task['id'],'operation_id':row['operation']['id'],
                    'status':'unknown' if uncertain else 'succeeded_with_commit_readback' if committed else 'failed',
                    'effect':'unknown' if uncertain else 'confirmed' if committed else 'none',
                    'transaction_rolled_back':rolled_back,'transaction_observation':transaction,
                    'commit_readback':commit_readback,'projection_recovery':projection,'first_fault':first_fault,
                    'cleanup_intent_ref':{'id':cleanup_intent['id'],'sha256':digest(cleanup_intent)} if cleanup_intent else None,
                    'mutation_performed':'committed cleanup read back without replay' if committed else 'see transaction and projection observations; absence of an active transaction is not rollback proof',
                    'observed_at':now()}
                self.store.record('cleanup_failure',cleanup_key,cleanup_failure)
                reason=None if committed and not uncertain else 'Completion cleanup has necessary work: '+str(error)
        actual_outcome=(dict(cleanup_result,commit_observation=cleanup_failure) if cleanup_result and cleanup_failure else
            cleanup_result or cleanup_failure or {'id':cleanup_key,'status':'deferred','effect':'none','reason':reason,'mutation_performed':False})
        self.store.event(task['id'],'cleanup','succeeded' if cleanup_result else 'deferred',actual_outcome)
        if not draft.get('post_review'):
            assessed_after=await self._assess_targets(task['id'],'completion_choices_after',choices,
                {'completion':completion.model_dump(),'cleanup':cleanup.model_dump(),'actual_cleanup_result':actual_outcome,'web':web,'timing':'after the actual completion/defer outcome, before publication'})
            after_review=await self._review_bundle(task['id'],row['operation'],'completion_outcome',{'assessments':assessed_after,'completion':completion.model_dump(),'actual_cleanup_result':actual_outcome},web)
            draft=dict(draft,post_assessments=assessed_after,post_review=after_review,actual_outcome=actual_outcome,deferred_reason=reason)
            self.store.update_operation(row['operation']['id'],finalization=draft)
        else:assessed_after=draft['post_assessments'];after_review=draft['post_review']
        self._journal_judgment_result(task,row['operation'],'completion-outcome',[*draft['pre_assessments'],*assessed_after],actual_outcome,after_review)
        if after_review['disposition']['verdict']!='proceed':
            self._record_judgment_correction(task,row['operation'],actual_outcome,after_review)
            reason='Actual cleanup/final evidence requires correction: '+after_review['disposition']['rationale']
        if reason:raise CycleHeld(reason)
        remaining=self.knowledge.snapshot(task['id'])
        if self._pending_judgments(task['id']) or any(i['status']=='pending' and (i['target']=='workflow'
                or getattr(self.policy,'role_scoped_enabled',False)) for i in remaining['ideas']):
            raise CycleHeld('Final review produced pending workflow improvement; return to real work before completion')
        final={'completion':completion.model_dump(),'cleanup':cleanup.model_dump(),'review':reviewed,
               'cleanup_result':cleanup_result,'individual_choices':choices,'pre_assessments':draft['pre_assessments'],'post_assessments':assessed_after,'actual_outcome_review':after_review,
               'source_hash':draft['source_hash'],'acceptance_hash':draft['acceptance_hash'],
               'independent_review_id':draft['independent_review_id'],'frozen_evidence_sha256':draft['frozen_evidence_sha256'],
               'policy_hash':self.policy.hash,'completed_at':now(),'proof_ceiling':'Actual task evidence and bounded review; no universal guarantee of correct model judgments.'}
        def commit_final():
            self._assert_finalization_evidence(task['id'],row,draft)
            self.store.update_task(task['id'],status='completed',final=final)
            self.store.event(task['id'],'final','completed',final)
        self.store._transaction(commit_final)

    def _record_judgment_correction(self,task,operation,outcome,reviewed):
        if self.store.record_get('judgment_correction',reviewed['id']):return
        correction={'id':reviewed['id'],'task_id':task['id'],'operation_id':operation['id'],'status':'pending',
            'created_at':now(),'review':reviewed,'actual_result':outcome,
            'instruction':'Use the original opinions and actual outcome in ordinary governed corrective work, then judgment_resolve with subsequent evidence. Do not replay a completed mutation.'}
        correction['hash']=digest(correction)
        self.store.record('judgment_correction',correction['id'],correction)

    def pending_approvals(self,task_id=None):
        return [x for x in self.store.records('policy_amendment') if x['status']=='awaiting_user' and (task_id is None or x['task_id']==task_id)]

    def resolve_approval(self,identity,decision,expected_hash,reason=''):
        if decision not in ('approve','reject'):raise PolicyError('Unknown approval response')
        candidate=deepcopy(self.policy)
        def commit():
            item=self.store.record_get('policy_amendment',identity)
            if not item or item['status']!='awaiting_user' or item['proposal_hash']!=expected_hash:
                raise PolicyError('Approval target changed or is not awaiting this exact decision')
            item.update(user_decision=decision,user_reason=reason,decided_at=now())
            if decision=='approve':
                item['user_approved']=True
                candidate.apply_amendment(item)
                item.update(status='applied',applied_policy_hash=candidate.hash)
                self.store.record('policy_version',candidate.hash,{'id':candidate.hash,'amendment_id':identity,
                    'previous_hash':self.policy.hash,'approved_at':item['decided_at'],'overlays':candidate.overlays})
                self.store.record('policy_current','current',{'hash':candidate.hash,'amendment_id':identity})
            else:item['status']='rejected'
            self.store.record('policy_amendment',identity,item)
            self.store.event(item['task_id'],'policy_amendment',item['status'],{'id':identity,
                'policy_hash':candidate.hash,'reconciliation_required':decision=='approve'})
            return item
        item=self.store._transaction(commit)
        if decision=='approve':
            # Publish only the committed authority. A crash before this in-memory
            # projection reloads the approved database record on restart.
            self.policy.__dict__.update(candidate.__dict__)
            self.store.policy_hash=self.policy.hash
        return redact(item)
