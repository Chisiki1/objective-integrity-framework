"""Durable finite acquisition judgments, independent of network replay."""
from copy import deepcopy
from .models import AssessmentBatch,Learning,LearningContractError,Review,Disposition,Idea,Operation,OperationResult,PolicyError,now
from .decisions import targets as decision_targets
from .store import canonical,digest
from .learning_projection import current_input,revision_feedback,merge_ideas,VERSION as PROJECTION_VERSION


class WebSelectionChanged(PolicyError):
    """A pre-exchange selection became stale while its judgment was pending."""


class WebCorrectionNeeded(PolicyError):
    """An acquired result needs governed correction, not another acquisition."""
    def __init__(self, parent_operation_id, corrections):
        super().__init__('Applied Web review requires actual correction and reviewed return before its parent continues')
        self.parent_operation_id=parent_operation_id
        self.corrections=deepcopy(corrections)


class WebJudgments:
    def __init__(self,engine):self.e=engine

    def _observed_ideas(self,task_id,acquisition_id):
        """Every successful partial response is an actual source for later learning."""
        ideas=[]
        for observed in self.e.store.records('bounded_model_call'):
            if observed.get('task_id')!=task_id or observed.get('status')!='succeeded':continue
            payload=observed.get('payload',{});phase=observed.get('phase','').split(':page:',1)[0]
            if acquisition_id not in {payload.get('operation',{}).get('id'),payload.get('acquisition',{}).get('id')}:continue
            if phase in {'web_acquisition_before','web_acquisition_after','web_learning_choices_before'}:
                ideas.extend(i for a in observed['result']['assessments'] for i in a['assessment']['ideas'])
            elif phase=='web_acquisition_learning':
                ideas.extend(observed['result']['ideas'])
        ideas.extend(self.e._learning_response_history(task_id,acquisition_id)['ideas'])
        return ideas

    def _binding(self, task):
        return {'policy_hash': self.e.policy.hash, 'source_hash': task['source_hash']}

    @staticmethod
    def _saved_binding(saved):
        return {name: saved.get(name) for name in ('policy_hash', 'source_hash')}

    def _assert_binding(self, task_id, binding):
        if self._binding(self.e.store.get_task(task_id)) != binding:
            raise PolicyError('Web judgment policy or task source changed; resume the stored acquisition under the current source')

    def _save(self, saved):
        self.e.store.record('web_work', saved['id'], saved)

    def _advance_pending_analysis(self,saved,task):
        """Replace only a known failed pending result judgment, never its HTTP."""
        if (saved.get('status')=='complete' or saved.get('analysis_transition')
                or saved['detail'].get('response_analysis_version')
                or self._saved_binding(saved)!=self._binding(task)):
            return saved
        if self._restore_applied(saved,task):return saved
        if saved.get('application_intent'):
            # The exact accepted-input/application consumer owns this recovery.
            # An optional analysis upgrade cannot replace its original intent.
            return saved
        calls=[row for row in self.e.store.records('bounded_model_call') if row.get('task_id')==task['id']
            and row.get('payload',{}).get('operation',{}).get('id')==saved['detail']['id']]
        learning_calls=[row for row in calls if row.get('phase','').startswith('web_acquisition_learning')
            and row.get('learning_schema')]
        failed=[row for row in learning_calls if row.get('status')=='failed'
            and row['payload'].get('operation')==saved.get('acquisition_operation')
            and row['payload'].get('result')==saved.get('acquisition_result')]
        if not any(self.e.bounded_judgments.observed_retryable_failure(row) for row in failed):
            return saved  # Normal saved review/output recovery remains its owner.
        if any(row.get('status') not in {'succeeded','failed'} for row in calls):
            raise PolicyError('Unobserved Web judgment must remain held; no analysis transition or resend')
        from . import models
        # Record insertion/update order is not event chronology. Authenticate
        # each applicable terminal before choosing the latest failed stage.
        terminals=[]
        for row in failed:
            schema=getattr(models,row['learning_schema'])
            bound=self.e.bounded_judgments._failed_learning_feedback(task['id'],row,schema,task['actor'],partition=True)
            terminals.append((bound['terminal_seq'],row))
        latest=max(seq for seq,row in terminals)
        matches=[row for seq,row in terminals if seq==latest]
        if len(matches)!=1:raise PolicyError('Failed Web judgment descendants have no unique terminal order')
        call=matches[0];schema=getattr(models,call['learning_schema'])
        if not self.e.bounded_judgments.observed_retryable_failure(call):return saved
        restart=self.e.bounded_judgments.failed_learning_restart(task['id'],call,schema=schema)
        if (call['payload'].get('operation')!=saved.get('acquisition_operation')
                or call['payload'].get('result')!=saved.get('acquisition_result')):
            raise PolicyError('Failed Web Learning belongs to a different original result; preserve its own recovery')
        if not hasattr(self.e.web,'saved_response_analysis'):return saved
        detail=self.e.web.saved_response_analysis(saved['detail'])
        original=deepcopy(saved);history_id=saved['id']+':'+digest(original)
        self.e.store.record('web_work_history',history_id,original)
        result=dict(saved['acquisition_result'],data=detail)
        transition={'version':'web-analysis-transition-v1',
            'original_work_ref':{'id':history_id,'sha256':digest(original)},
            'original_detail_sha256':digest(original['detail']),'current_detail_sha256':digest(detail),
            'original_result_sha256':digest(original['acquisition_result']),'current_result_sha256':digest(result),
            'failed_restart':restart,
            'meaning':'Same saved HTTP exchange, newly observed local analysis. Original before/transport '
                'judgments and failed calls remain immutable. The added result requires current after '
                'judgment and independent review; old output is not promoted to the new result.'}
        current={key:value for key,value in saved.items() if key not in
            {'assessments','review','review_input','review_input_sha256','disposition','outcome','completed_at'}}
        current.update(detail=detail,acquisition_result=result,context_contract_version='web-identity-v4',
            analysis_transition=transition,status='pending')
        self._save(current)
        return current

    def validate_analysis_transition(self,task_id,payload,failed_ref):
        """Read-only bridge used by the existing failed-Learning restoration."""
        transition=payload.get('analysis_transition')
        if transition is None:return False
        def invalid():raise PolicyError('Web analysis transition source/result/failure binding differs')
        if not isinstance(transition,dict) or transition.get('version')!='web-analysis-transition-v1':invalid()
        operation=payload.get('operation',{});identity=operation.get('id')
        saved=self.e.store.record_get('web_work',str(identity)+':after')
        ref=transition.get('original_work_ref',{})
        original=self.e.store.record_get('web_work_history',ref.get('id'))
        task=self.e.store.get_task(task_id)
        if (not saved or not original or saved.get('task_id')!=task_id or original.get('task_id')!=task_id
                or saved.get('analysis_transition')!=transition or digest(original)!=ref.get('sha256')
                or self._saved_binding(saved)!=self._binding(task) or self._saved_binding(original)!=self._binding(task)
                or original.get('learning_applied') or original.get('application_intent') or original.get('status')=='complete'
                or digest(original['detail'])!=transition.get('original_detail_sha256')
                or digest(saved['detail'])!=transition.get('current_detail_sha256')
                or digest(original['acquisition_result'])!=transition.get('original_result_sha256')
                or digest(saved['acquisition_result'])!=transition.get('current_result_sha256')
                or saved['acquisition_operation']!=operation or payload.get('result')!=saved['acquisition_result']
                or original['acquisition_result'].get('data')!=original['detail']
                or saved['acquisition_result'].get('data')!=saved['detail']
                or original['detail'].get('id')!=identity or saved['detail'].get('id')!=identity):invalid()
        if not self.e.web.validate_saved_response_analysis(original['detail'],saved['detail']):invalid()
        analysis=saved['detail'].get('response_analysis',{})
        if (analysis.get('response_sha256')!=original['detail'].get('sha256')
                or saved['detail'].get('exchange_id')!=original['detail'].get('exchange_id')):invalid()
        restart=transition.get('failed_restart',{});bound=restart.get('source_receipt')
        if failed_ref!=bound:return False
        call=self.e.store.record_get('bounded_model_call',bound.get('id')) if isinstance(bound,dict) else None
        if not call or self.e.bounded_judgments._call_ref(call)!=bound:invalid()
        from . import models
        checked=self.e.bounded_judgments.failed_learning_restart(task_id,call,schema=getattr(models,call['learning_schema']))
        if checked!=restart or call['payload'].get('operation')!=original['acquisition_operation'] or call['payload'].get('result')!=original['acquisition_result']:invalid()
        return True

    def applied_source(self,task,acquisition_id):
        """Read the exact committed Web consumer without changing or replaying it."""
        try:self.e.store.get_operation(acquisition_id)
        except KeyError:pass
        else:raise PolicyError('Web acquisition identity conflicts with a task operation')
        saved=self.e.store.record_get('web_work',acquisition_id+':after')
        if (not saved or saved.get('id')!=acquisition_id+':after' or saved.get('stage')!='after'
                or saved.get('task_id')!=task['id'] or saved.get('detail',{}).get('id')!=acquisition_id
                or not saved.get('learning_applied')):
            raise PolicyError('Web judgment has no exact owned committed acquisition')
        restored=deepcopy(saved)
        # A committed saved outcome is required above, so this validates the
        # existing episode/selection without the recovery writer being invoked.
        self._restore_applied(restored,task)
        if (restored['acquisition_operation']!=saved.get('acquisition_operation')
                or restored['acquisition_result']!=saved.get('acquisition_result')
                or restored['applied_learning']!=saved.get('applied_learning')):
            raise PolicyError('Web judgment source differs from its committed input')
        app_id=task['id']+':'+acquisition_id
        app=self.e.store.record_get('knowledge_application',app_id)
        episode=self.e.store.record_get('episode',app['outcome']['episode_id'])
        consumer={'kind':'web_acquisition','task_id':task['id'],'web_work_id':saved['id'],
            'acquisition_id':acquisition_id,'parent_operation_id':saved['operation']['id'],
            'operation_sha256':digest(saved['acquisition_operation']),
            'result_sha256':digest(saved['acquisition_result']),
            'application_id':app_id,'application_input_hash':app['input_hash'],
            'episode_id':episode['id'],'episode_sha256':digest(episode),
            'outcome_sha256':digest(app['outcome']),
            'application_binding':saved.get('learning_application_binding')}
        return {'consumer':consumer,'operation':saved['acquisition_operation'],
            'result':saved['acquisition_result'],'outcome':saved['learning_applied'],
            'learning':saved['applied_learning'],'selection':saved.get('selection'),
            'parent_operation':saved['operation'],'acquisition':saved['detail']}

    def correction_source(self,task,correction):
        if correction.get('consumer',{}).get('stage')=='uncommitted_learning':
            return self.learning_correction_source(task,correction)
        source=self.applied_source(task,correction['operation_id'])
        supplied=correction.get('consumer')
        if supplied is not None and supplied!=source['consumer']:
            raise PolicyError('Web judgment correction consumer changed')
        reviews=[r for r in self.e.store.records('applied_knowledge_review')
                 if r.get('review',{}).get('id')==correction['id']]
        if (len(reviews)!=1 or reviews[0].get('review')!=correction['review']
                or not reviews[0]['id'].startswith(correction['operation_id']+':web-learning-outcome')
                or reviews[0].get('outcome_hash')!=digest(source['outcome'])
                or correction.get('actual_result')!=source['outcome']):
            raise PolicyError('Web judgment correction lost its actual applied review')
        actual=self.e.store.record_get('review',correction['id'])
        if (not actual or actual.get('task_id')!=task['id']
                or any(actual.get(k)!=v for k,v in correction['review'].items())):
            raise PolicyError('Web judgment correction review is absent or foreign')
        if supplied is None:
            # Recover only the old exact hash-bound application review. A missing
            # normal operation must never become a guessed Web consumer.
            saved=self.e.store.record_get('web_work',source['consumer']['web_work_id'])
            prior=reviews[0]
            legacy=digest({'choices':saved['applied_choices'],'outcome':source['outcome'],
                'result':source['result'],'web':{'sources':[]},
                'policy_hash':prior.get('policy_hash'),'source_hash':prior.get('source_hash')})
            if prior.get('input_hash')!=legacy:
                raise PolicyError('Legacy Web correction has no exact original review binding')
        return source

    def park_learning(self,task,operation,result,learning,review,disposition,route,*,saved=None):
        """Use the existing governed correction/return graph before regeneration."""
        if not route or route['scope']!='governed':raise PolicyError('Learning correction has no governed semantic decision')
        parent=saved['operation'] if saved else operation
        consumer={'kind':'web_acquisition' if saved else 'operation','stage':'uncommitted_learning',
            'operation_id':operation['id'],'operation_sha256':digest(operation),'result_sha256':digest(result),
            'parent_operation_id':parent['id'],'parent_operation_sha256':digest(parent)}
        if saved:consumer.update(web_work_id=saved['id'],acquisition_id=operation['id'])
        original={'task_id':task['id'],'operation':deepcopy(operation),'result':deepcopy(result),
            'learning':deepcopy(learning),'review':deepcopy(review),'disposition':deepcopy(disposition),
            'route':deepcopy(route),'consumer':consumer}
        identity=digest(original);original['id']=identity
        existing=self.e.store.record_get('learning_correction',identity)
        if existing is not None and existing!=original:raise PolicyError('Learning correction original changed')
        if existing is None:
            reviewed={'id':identity,'review':deepcopy(review),'disposition':deepcopy(disposition),
                'source_learning_sha256':digest(learning),'policy_hash':self.e.policy.hash}
            correction={'id':identity,'task_id':task['id'],'operation_id':operation['id'],'status':'pending',
                'created_at':now(),'review':reviewed,'actual_result':deepcopy(result),'consumer':consumer,
                'instruction':'This Learning is uncommitted. Perform actual independent diagnosis/correction of the separately identified target, then reviewed judgment_resolve with subsequent evidence. Preserve this operation/result and the pending Learning; never rewrite an acquisition or replay an effect.'}
            correction['hash']=digest(correction)
            def commit():
                self.e.store.record('learning_correction',identity,original)
                self.e.store.record('review',identity,dict(reviewed,task_id=task['id'],phase='uncommitted_learning'))
                self.e.store.record('judgment_correction',identity,correction)
            self.e.store._transaction(commit)
        return identity

    def learning_correction_source(self,task,correction):
        original=self.e.store.record_get('learning_correction',correction['id'])
        if (not original or original['id']!=correction['id'] or original['id']!=digest({k:v for k,v in original.items() if k!='id'})
                or original['task_id']!=task['id'] or original['consumer']!=correction.get('consumer')
                or correction.get('actual_result')!=original['result']
                or correction['review']['review']!=original['review']
                or correction['review']['disposition']!=original['disposition']):
            raise PolicyError('Uncommitted Learning correction lost its exact reviewed original')
        consumer=original['consumer'];parent=self.e.store.get_operation(consumer['parent_operation_id'])
        actual_review=self.e.store.record_get('review',correction['id'])
        if (not actual_review or actual_review.get('task_id')!=task['id']
                or any(actual_review.get(k)!=v for k,v in correction['review'].items())
                or consumer['operation_id']!=original['operation']['id']
                or consumer['operation_sha256']!=digest(original['operation'])
                or consumer['result_sha256']!=digest(original['result'])):
            raise PolicyError('Learning correction original or review identity changed')
        if parent['task_id']!=task['id'] or digest(parent['operation'])!=consumer['parent_operation_sha256']:
            raise PolicyError('Learning correction parent changed')
        if consumer['kind']=='web_acquisition':
            saved=self.e.store.record_get('web_work',consumer['web_work_id'])
            self._context(saved)  # Existing exact acquisition/parent/result guard.
            if saved['acquisition_operation']!=original['operation'] or saved['acquisition_result']!=original['result']:
                raise PolicyError('Learning correction acquired result changed')
        elif parent['operation']!=original['operation'] or parent.get('result')!=original['result']:
            raise PolicyError('Learning correction operation result changed')
        route=original['route'];packet=self.e.store.record_get('bounded_input',route['source_packet']['id'])
        if not packet:raise PolicyError('Learning correction route input is missing')
        proposal=packet['value'].get('learning',packet['value'].get('bundle',{}).get('learning'))
        if proposal!=original['learning']:raise PolicyError('Learning correction points to another operative proposal')
        checked=self.e.bounded_judgments.disposition_route(task['id'],route['phase'],packet['value'],original['disposition'])
        if checked!=route:raise PolicyError('Learning correction semantic routing changed')
        return {'consumer':consumer,'operation':original['operation'],'result':original['result'],
            'learning':original['learning'],'parent_operation':parent['operation']}

    def learning_return(self,task_id,identity):
        """Resolved prose is insufficient; require the completed reviewed return."""
        task=self.e.store.get_task(task_id);correction=self.e.store.record_get('judgment_correction',identity)
        if not correction:raise PolicyError('Learning correction is missing')
        self.learning_correction_source(task,correction)
        pending=[c for c in self.correction_frontier(task_id) if c['review_id']==identity]
        if pending:raise WebCorrectionNeeded(correction['consumer']['parent_operation_id'],pending)
        receipt=self.e.store.record_get('web_judgment_resolution',identity)
        if not receipt:raise PolicyError('Learning correction lacks its reviewed return receipt')
        row=self.e.store.get_operation(receipt['resolution_operation_id'])
        return {'correction_id':identity,'receipt':receipt,'operation':row['operation'],
            'result':row['result'],'review':row['post_review']}

    def confirm_correction(self,task,correction,resolution):
        source=self.correction_source(task,correction)
        expected=resolution['result']['data'].get('original_consumer')
        if expected is not None and expected!=source['consumer']:
            raise PolicyError('Resolved Web consumer differs from its reviewed action')
        record={'id':correction['id'],'task_id':task['id'],'consumer':source['consumer'],
            'resolution_operation_id':resolution['operation']['id'],
            'resolution_result_sha256':digest(resolution['result']),
            'meaning':'The governed resolution and its own review/learning returned to this exact committed Web result. Other pending judgments remain; no acquisition or application was replayed.'}
        def commit():
            previous=self.e.store.record_get('web_judgment_resolution',record['id'])
            if previous:
                if previous!=record:raise PolicyError('Web resolution completion identity changed')
                return
            if source['consumer'].get('web_work_id'):
                saved=self.e.store.record_get('web_work',source['consumer']['web_work_id'])
                self.e.store.record('web_work_history',saved['id']+':'+digest(saved),saved)
                updated=dict(saved,judgment_resolutions=[*saved.get('judgment_resolutions',[]),record['id']])
                self._save(updated)
            self.e.store.record('web_judgment_resolution',record['id'],record)
        self.e.store._transaction(commit)

    def correction_frontier(self,task_id,*,include_operations=False,source_values=None):
        """Read unresolved returns, including old complete acquisitions.

        Dispatch changes a correction to resolved before its own review. Only
        the existing reviewed-return receipt AND the completed resolution cycle
        close this frontier. No old application or review is rewritten here.
        """
        e=self.e;task=e.store.get_task(task_id);entries={};children={}
        for correction in e.store.records('judgment_correction'):
            if correction.get('task_id')!=task_id:continue
            consumer=correction.get('consumer')
            if consumer is not None and consumer.get('kind')!='web_acquisition' and consumer.get('stage')!='uncommitted_learning':continue
            if consumer is None:
                try:e.store.get_operation(correction['operation_id'])
                except KeyError:pass
                else:continue
            source=self.correction_source(task,correction)
            if source_values is not None:source_values[correction['id']]=source
            receipt=e.store.record_get('web_judgment_resolution',correction['id'])
            closed=False
            if receipt is not None:
                resolution=e.store.get_operation(receipt['resolution_operation_id'])
                if (receipt.get('task_id')!=task_id or receipt.get('consumer')!=source['consumer']
                        or correction.get('status')!='resolved'
                        or correction.get('resolution_operation_id')!=receipt['resolution_operation_id']
                        or resolution.get('task_id')!=task_id
                        or resolution['operation']['kind']!='judgment_resolve'
                        or resolution['operation']['args'].get('review_id')!=correction['id']
                        or resolution['operation']['args'].get('expected_hash')!=correction['hash']
                        or (resolution.get('result') or {}).get('status')!='succeeded'
                        or (resolution.get('result') or {}).get('effect')=='unknown'
                        or (resolution.get('result') or {}).get('data',{}).get('resolved_review')!=correction['id']
                        or receipt.get('resolution_result_sha256')!=digest(resolution.get('result'))):
                    raise PolicyError('Web correction return lost its exact resolution identity')
                closed=(resolution['status']=='cycle_complete'
                    and not any(c.get('operation_id')==receipt['resolution_operation_id']
                        for c in e._pending_judgments(task_id)))
            entry={'review_id':correction['id'],'expected_hash':correction['hash'],
                'status':correction['status'],'consumer':source['consumer'],
                'resolution_operation_id':correction.get('resolution_operation_id')}
            entries[correction['id']]=(entry,closed)
            children.setdefault(source['consumer']['parent_operation_id'],[]).append(correction['id'])
        # Normal applied reviews also have a dispatch/review gap. Include them
        # in dependency closure so a resolved nested opinion cannot release its
        # parent until its actual resolution cycle (and own reviews) completes.
        remaining=[c for c in e.store.records('judgment_correction')
                   if c.get('task_id')==task_id and c['id'] not in entries]
        owners={entry['resolution_operation_id'] for entry,_ in entries.values()}
        while True:
            linked=[c for c in remaining if c['operation_id'] in owners]
            if not linked:break
            remaining=[c for c in remaining if c['operation_id'] not in owners]
            for correction in linked:
                source=e._judgment_source(task,correction)
                if source_values is not None:source_values[correction['id']]=source
                closed=False
                if correction['status']=='resolved':
                    resolution=e.store.get_operation(correction['resolution_operation_id'])
                    if (resolution['task_id']!=task_id or resolution['operation']['kind']!='judgment_resolve'
                            or resolution['operation']['args']!=correction.get('resolution')
                            or resolution['operation']['args'].get('review_id')!=correction['id']
                            or resolution['operation']['args'].get('expected_hash')!=correction['hash']):
                        raise PolicyError('Nested correction return lost its exact resolution identity')
                    result=resolution.get('result')
                    if result and result.get('effect')!='unknown':
                        if (result.get('status')!='succeeded' or result.get('data',{}).get('resolved_review')!=correction['id']
                                or result['data'].get('original_consumer',source['consumer'])!=source['consumer']):
                            raise PolicyError('Nested correction return differs from its actual resolved result')
                        closed=resolution['status']=='cycle_complete'
                entry={'review_id':correction['id'],'expected_hash':correction['hash'],
                    'status':correction['status'],'consumer':source['consumer'],
                    'resolution_operation_id':correction.get('resolution_operation_id')}
                entries[correction['id']]=(entry,closed)
                children.setdefault(correction['operation_id'],[]).append(correction['id'])
                owners.add(correction.get('resolution_operation_id'))
        # A historical completed resolution can itself have a still-open Web
        # review. Acquisition IDs differ from ordinary operation IDs, so follow
        # the exact parent consumer instead of only pending operation_id matches.
        resolved={};visiting=set()
        def returned(review_id):
            if review_id in resolved:return resolved[review_id]
            if review_id in visiting:raise PolicyError('Web correction return lineage contains a cycle')
            visiting.add(review_id)
            entry,own_complete=entries[review_id]
            descendants=[returned(child) for child in children.get(entry['resolution_operation_id'],[])]
            visiting.remove(review_id)
            resolved[review_id]=own_complete and all(descendants)
            return resolved[review_id]
        return [entry for review_id,(entry,_) in entries.items() if not returned(review_id)
                and (include_operations or entry['consumer']['kind']=='web_acquisition' or entry['consumer'].get('stage')=='uncommitted_learning')]

    def has_pending_return(self,task_id,operation_id):
        return any((item['consumer'].get('parent_operation_id') or item['consumer'].get('operation_id'))==operation_id
                   for item in self.correction_frontier(task_id,include_operations=True))

    def recover_reviewed_returns(self,task_id):
        """Complete missing return receipts from exact old reviewed commits only.

        No operation, model, exchange or Knowledge application is executed. Each
        iteration can only confirm an existing eligible resolution; unfinished
        own reviews stay on the frontier and siblings never erase each other.
        """
        e=self.e;task=e.store.get_task(task_id)
        while True:
            frontier=self.correction_frontier(task_id)
            blocked={c['consumer']['parent_operation_id'] for c in frontier}
            recovered=False
            for item in frontier:
                identity=item.get('resolution_operation_id')
                if (item['status']!='resolved' or not identity or identity in blocked
                        or e.store.record_get('web_judgment_resolution',item['review_id'])):continue
                row=e.store.get_operation(identity)
                if row['status']!='cycle_complete':continue
                if self.has_pending_return(task_id,identity):continue
                # Reuse the actual pre/post review validator and the original
                # committed episode; a saved phase label alone is insufficient.
                e.knowledge._reviewed_row(task_id,identity,post=True)
                selected=[s['id'] for s in row['pre_bundle']['skills']['selected']]
                payload={'operation':row['operation'],'learning':row['post_bundle']['learning'],
                    'result':row['result'],'selected':selected}
                app=e.store.record_get('knowledge_application',task_id+':'+identity)
                episode=e.store.record_get('episode',(app or {}).get('outcome',{}).get('episode_id',''))
                legacy=digest({k:v for k,v in payload.items() if k!='operation'})
                if (not app or app.get('id')!=task_id+':'+identity
                        or app.get('input_hash') not in {digest(payload),legacy}
                        or row.get('knowledge')!=app.get('outcome') or not episode
                        or episode.get('task_id')!=task_id or episode.get('operation_id')!=identity
                        or episode.get('operation')!=payload['operation'] or episode.get('result')!=payload['result']
                        or episode.get('learning')!=payload['learning'] or episode.get('selected_skills')!=selected):
                    raise PolicyError('Historical Web resolution lacks its exact reviewed Knowledge commit')
                e._confirm_reviewed_outcomes(task,row)
                recovered=bool(e.store.record_get('web_judgment_resolution',item['review_id'])) or recovered
            if not recovered:return

    def require_parent_return(self,task_id,parent_operation_id):
        pending=[c for c in self.correction_frontier(task_id)
                 if c['consumer']['parent_operation_id']==parent_operation_id]
        if pending:raise WebCorrectionNeeded(parent_operation_id,pending)

    def _current_outcome(self,saved):
        outcome=deepcopy(saved['outcome'])
        if saved['stage']=='after':
            pending=[c for c in self.correction_frontier(saved['task_id'])
                     if c['consumer']['acquisition_id']==saved['detail']['id']]
            if pending:outcome['pending_applied_reviews']=pending
        return outcome

    def _restore_applied(self, saved, task):
        """Recover a committed input from its actual episode, never a new draft.

        Knowledge's input hash remains strict. The legacy hash is accepted only
        when it binds the full original episode's learning/result/selection;
        this reader neither changes that receipt nor reruns its application.
        """
        identity = task['id'] + ':' + saved['detail']['id']
        application = self.e.store.record_get('knowledge_application', identity)
        if not application:
            if saved.get('learning_applied'):
                raise PolicyError('Saved Web knowledge outcome has no committed application')
            return False
        episode = self.e.store.record_get('episode', application['outcome'].get('episode_id'))
        if (not episode or episode.get('task_id') != task['id'] or
                episode.get('operation_id') != saved['detail']['id'] or
                episode.get('operation', {}).get('kind') != 'web_fetch' or
                episode.get('result', {}).get('operation_id') != saved['detail']['id'] or
                episode['result'].get('data') != saved['detail']):
            raise PolicyError('Committed Web knowledge episode differs from its original acquisition')
        payload = {'operation': episode['operation'], 'learning': episode['learning'],
                   'result': episode['result'], 'selected': episode['selected_skills']}
        original_hash = digest(payload)
        legacy_hash = digest({k: v for k, v in payload.items() if k != 'operation'})
        if application['input_hash'] not in {original_hash, legacy_hash}:
            raise PolicyError('Committed Web knowledge input hash differs from its source episode')
        selected=episode['selected_skills']
        if selected:
            before=self.e.store.record_get('web_work',saved['detail']['id']+':before')
            if (not before or before.get('task_id')!=task['id'] or
                    [s['id'] for s in before.get('selection',{}).get('selected',[])]!=selected):
                raise PolicyError('Committed Web Skill use lost its original pre-exchange selection')
            if saved.get('selection') is not None and saved['selection']!=before['selection']:
                raise PolicyError('Committed Web selection differs from its original acquisition')
            saved['selection']=deepcopy(before['selection'])
            saved['selection_binding']=deepcopy(before.get('selection_binding'))
        elif saved.get('selection',{}).get('selected'):
            raise PolicyError('Stored Web selection differs from its committed empty selection')
        newly_recovered = not saved.get('learning_applied')
        for name, expected in [('acquisition_operation', episode['operation']), ('acquisition_result', episode['result'])]:
            if name in saved and saved[name] != expected:
                raise PolicyError('Stored Web acquisition input changed after application')
            saved[name] = deepcopy(expected)
        intent = saved.get('application_intent')
        if intent and intent['input_hash'] != original_hash:
            raise PolicyError('Web application intent differs from its actual committed input')
        if saved.get('learning_applied') and saved['learning_applied'] != application['outcome']:
            raise PolicyError('Stored Web knowledge outcome changed after application')
        matches = [i for i, attempt in enumerate(saved['learning_attempts'])
                   if attempt['learning'] == episode['learning'] and
                   attempt.get('disposition', {}).get('verdict') == 'proceed']
        accepted = saved['learning_attempts'][matches[-1]] if matches else None
        saved.setdefault('learning_application_binding',
                         (intent or {}).get('binding') or (accepted or {}).get('binding') or self._saved_binding(saved))
        saved.update(learning_applied=deepcopy(application['outcome']),
                     applied_learning=deepcopy(episode['learning']),
                     application_recovery={'application_id': identity, 'input_hash': application['input_hash'],
                         'episode_id': episode['id'], 'episode_sha256': digest(episode),
                         'accepted_attempt_index': matches[-1] if matches else None,
                         'review_reconstructed': False, 'application_replayed': False})
        if 'applied_choices' not in saved:
            saved['applied_choices'] = deepcopy(accepted['choices']) if accepted else decision_targets('web-learning', episode['learning'])
        if newly_recovered:
            def record_recovery():
                self._save(saved)
                projection = application['outcome'].get('projection_status', {}).get('state', 'unobserved')
                self.e.store.event(task['id'], 'web_knowledge_application',
                    'recovered' if projection == 'projected' else 'partial',
                    {**saved['application_recovery'], 'outcome': application['outcome'],
                     'projection_observation': projection, 'network_replayed': False})
            self.e.store._transaction(record_recovery)
        return True

    def _ensure_acquisition_input(self, saved):
        """Persist exactly once, before Learning or any knowledge write.

        Missing legacy transport timestamps use the first controller observation
        and are labelled as such; an existing committed episode always wins.
        """
        detail = saved['detail']
        if 'acquisition_operation' not in saved:
            saved['acquisition_operation'] = Operation(kind='web_fetch', id=detail['id'], args={'url': detail.get('url', '')},
                purpose='Evaluate this exact public acquisition outcome', expected_result='Actual source transport and extraction evidence',
                decisions=[{'id':'acquisition','statement':'Acquire declared public evidence',
                            'rationale':'Original target research under finite R16 boundary'}]).model_dump()
        if 'acquisition_result' not in saved:
            saved['acquisition_result'] = OperationResult(operation_id=detail['id'],
                status=detail.get('status') if detail.get('status') in {'succeeded','failed','unknown'} else 'pending',
                effect='unknown' if detail.get('status')=='unknown' else 'none', data=detail,
                started_at=detail.get('started_at') or detail.get('prepared_at') or saved['created_at'],
                finished_at=detail.get('finished_at') or saved['created_at'],
                elapsed_seconds=detail.get('elapsed_seconds',0)).model_dump()
            saved['acquisition_time_basis'] = {
                'started_at': 'transport' if detail.get('started_at') else 'request_preparation' if detail.get('prepared_at') else 'first_controller_observation',
                'finished_at': 'transport' if detail.get('finished_at') else 'first_controller_observation',
                'missing_transport_timestamps_reconstructed': False}
        self._save(saved)

    def _context(self,saved):
        """Keep old Learning transport bytes; clarify newly constructed work.

        Legacy pending exchanges retain the exact v1 caller representation. New
        judgment content has a distinct request identity, never a cache alias.
        """
        e=self.e;task_id=saved['task_id'];detail=saved['detail']
        acquisition=saved['acquisition_operation'];result=saved['acquisition_result']
        if (detail['id']==saved['operation']['id'] or acquisition['id']!=detail['id']
                or result['operation_id']!=detail['id'] or result['data']!=detail
                or detail.get('task_id',task_id)!=task_id
                or detail.get('operation_id',saved['operation']['id'])!=saved['operation']['id']):
            raise PolicyError('Web acquisition/parent/result identity mismatch')
        context={'operation':acquisition,'result':result,'pre':{'skills':saved['selection']},
            'selection_binding':saved['selection_binding'],
            'parent_operation':saved['operation'],'acquisition':detail,'timing':saved['stage'],'research_choice':saved['research_choice'],
            'collection_context':detail.get('collection_context',{}),
            'phase_contract':'This is ONE HTTP exchange within the declared collection. Before permits a prepared request and claims no measured result. After judges actual transport/response metadata, not yet extracted semantic page content. Redirect success is not acquired-body evidence. Multiple declared URLs are collected separately. Do not fetch other information recursively to review this exchange.',
            'prior_acquisition_evidence':[{'id':x['id'],'acquisition':x['acquisition'],'disposition':x['disposition']}
                for x in e.store.records('web_review') if x['task_id']==task_id and x['stage']=='after'
                and x['id']!=detail['id']+':after'][-8:]}
        if saved.get('context_contract_version')=='web-identity-v4':
            analysis=detail.get('response_analysis')
            context['phase_contract']='This is ONE HTTP exchange within the declared collection. Before has no measured response. After includes the saved transport and its safe in-process analysis when response_analysis is present. Extraction is not content adoption; this callback and required independent review must finish before handing the source onward or following a redirect. Do not fetch other information recursively to review this exchange. Legacy snapshots without analysis retain their own historical meaning.'
            completed=detail.get('collection_context',{}).get('completed_targets',[])
            completed_ids={target.get('source_id') for target in completed}
            prior=[]
            for row in e.store.records('web_review'):
                if (row['task_id']!=task_id or row['stage']!='after' or row['id']==detail['id']+':after'
                        or (row['acquisition'].get('operation_id')!=saved['operation']['id']
                            and row['acquisition']['id'] not in completed_ids)):
                    continue
                entry={key:deepcopy(row[key]) for key in ('id','acquisition','disposition')}
                work=e.store.record_get('web_work',row['id'])
                if (work and work.get('status')=='complete' and self._saved_binding(work)==self._saved_binding(saved)
                        and row.get('disposition',{}).get('verdict')=='proceed'):
                    entry['acquisition']=self._acquisition_summary(row['acquisition'])
                    entry['reused_judgment']={'work_id':work['id'],'work_sha256':digest(work),
                        'source_hash':work['source_hash'],'policy_hash':work['policy_hash'],
                        'acquisition_sha256':digest(row['acquisition']),
                        'basis':'Completed unchanged applicable exchange judgment; original content/history retained. '
                            'No new fact here reopens it. Current or unresolved source content is supplied in full.'}
                prior.append(entry)
            context['prior_acquisition_evidence']=prior
            # The complete new content appears once in result.data. Its other
            # same-input occurrences use a controller-owned exact reference.
            context['acquisition']=self._acquisition_summary(detail,source_pointer='/result/data/response_analysis/source')
            context['controller_facts']={
                'version':'web-progression-v1',
                'current_exchange':{'id':detail['id'],'evaluation_stage':saved['stage'],
                    'transport_status':detail.get('status'),'analysis_state':analysis.get('state') if analysis else 'not_recorded',
                    'content_adopted':False,'current_source_handoff':'not_yet_performed'},
                'completed_before_this_exchange':completed,
                'prior_snapshot_interpretation':[{'snapshot_id':row['id'],
                    'acquisition_id':row['acquisition']['id'],
                    'snapshot_sha256':row.get('reused_judgment',{}).get('acquisition_sha256',digest(row['acquisition'])),
                    'subsequent_completed_targets':[target for target in completed
                        if target.get('source_id')==row['acquisition']['id']]}
                    for row in context['prior_acquisition_evidence']],
                'next_parent_stage':'pre_collection_return' if detail.get('phase')=='pre' else
                    'post_collection_return' if detail.get('phase')=='post' else 'not_recorded',
                'meaning':'The prior snapshot stays immutable. A later completed target records successful '
                    'extraction and reviewed handoff at a later time; it does not rewrite the earlier '
                    'transport snapshot. No completion entry means no completion evidence here. '
                    'Current analysis and remaining installed parent duties do not grant permission or '
                    'establish successful future work.'}
            if saved.get('analysis_transition'):
                context['analysis_transition']=deepcopy(saved['analysis_transition'])
                previous=e.store.record_get('web_work_history',saved['analysis_transition']['original_work_ref']['id'])
                context['analysis_revision']={
                    'change':'Add the actual local extraction to this same retained HTTP response; no network replay.',
                    'original_result_sha256':saved['analysis_transition']['original_result_sha256'],
                    'original_disposition':deepcopy(previous.get('disposition')),
                    'unresolved_review':deepcopy(previous.get('review'))
                        if previous.get('disposition',{}).get('verdict')!='proceed' else None,
                    'reuse_basis':'The original transport and before judgments remain historical facts. '
                        'A proceed disposition already resolved its listed opinions under that input. '
                        'Reconsider only the added result and affected conclusions; unresolved opinions '
                        'remain supplied here and in the current Learning revision feedback.'}
        if saved.get('context_contract_version') in {'web-identity-v2','web-identity-v3','web-identity-v4'}:
            context['identity_and_stage']=self._identity_context(saved)
        if saved.get('context_contract_version') in {'web-identity-v3','web-identity-v4'}:
            # Only newly issued contexts use the shared reference representation.
            # Old successful/partial/unknown calls keep their exact caller bytes.
            before=e.store.record_get('web_work',detail['id']+':before') if saved['stage']=='after' else None
            if before and self._saved_binding(before)==self._saved_binding(saved):
                if (before.get('task_id')!=task_id or before.get('operation')!=saved['operation']
                        or before.get('detail',{}).get('id')!=detail['id']
                        or before['detail'].get('url')!=detail.get('url')):
                    raise PolicyError('Acquisition candidates belong to another original exchange')
                assessments=before.get('assessments',[])
                if assessments and e._wire_recovery(AssessmentBatch) is not None:
                    e.bounded_judgments._retained_assessment_parts(task_id,'web_acquisition_before',assessments,
                        subset={'operation':before['acquisition_operation'],'acquisition':before['detail'],
                                'research_choice':before['research_choice'],'timing':'before'})
                context['required_ideas']=merge_ideas([], [idea for row in assessments for idea in row['assessment']['ideas']])
        return context

    @staticmethod
    def _acquisition_summary(detail,*,source_pointer=None):
        value=deepcopy(detail);analysis=value.get('response_analysis',{})
        source=analysis.get('source')
        if source is not None:
            analysis['source']={key:item for key,item in source.items() if key!='text'}
            analysis['source']['text_reference']={'source_sha256':digest(source),
                **({'pointer':source_pointer} if source_pointer else
                   {'retained_exchange_id':detail.get('exchange_id'),'current_input_use':'already resolved historical result'})}
        return value

    @staticmethod
    def _identity_context(saved):
        detail=saved['detail']
        result={'parent_operation_id':saved['operation']['id'],'acquisition_operation_id':detail['id'],
            'exchange_id':detail.get('exchange_id'), 'collection_phase':detail.get('phase'),
            'evaluation_stage':saved['stage'],'time_basis':saved.get('acquisition_time_basis'),
            'meaning':'acquisition.operation_id names the parent owning this collection; acquisition.id is the distinct exchange/judgment OperationResult identity. Collection phase pre/post is relative to that parent, not the transport evaluation stage. The collection_context snapshot predates extraction of this exchange; completed_targets excludes it until successful extraction. A failed response is not an extracted source. Prepared-to-start delay includes governance; elapsed time/status alone does not establish a network cause. Preserve actual mismatches and unknowns; these definitions do not dismiss valid criticism.'}
        if saved.get('context_contract_version')=='web-identity-v4':
            result['meaning']='acquisition.operation_id identifies the parent; acquisition.id identifies this exchange and its OperationResult. Collection phase pre/post is relative to the parent. The collection_context snapshot precedes handoff of this exchange. response_analysis, when present, records safe parsing of the saved response before after-judgment. It is not content adoption or destination communication. completed_targets contains only earlier reviewed source handoffs. Failed or unknown transport is not a successful source. Prepared-to-start delay includes governance; timing/status alone does not prove network causality. Preserve actual mismatches and unknowns.'
        return result

    def _accepted_learning_context(self,saved):
        learning=saved['applied_learning'];attempts=saved['learning_attempts']
        matches=[i for i,a in enumerate(attempts) if a['learning']==learning
                 and a.get('disposition',{}).get('verdict')=='proceed']
        intent=saved.get('application_intent',{})
        index=intent.get('accepted_attempt_index')
        if index is not None:
            if type(index) is not int or index not in matches:
                raise PolicyError('Accepted Web disposition differs from the committed application intent')
        elif len(matches)==1:index=matches[0]
        elif len(matches)>1:raise PolicyError('Legacy accepted Web disposition is ambiguous')
        accepted=attempts[index] if index is not None else None
        for key,value in [('accepted_review_sha256',accepted.get('review') if accepted else None),
                          ('accepted_disposition_sha256',accepted.get('disposition') if accepted else None)]:
            if key in intent and intent[key]!=digest(value):
                raise PolicyError('Accepted Web response differs from its immutable application intent')
        if accepted and (accepted.get('review_input',{}).get('learning',learning)!=learning
                or (accepted.get('review_input_sha256') is not None
                    and digest(accepted.get('review_input'))!=accepted['review_input_sha256'])):
            raise PolicyError('Accepted Web Learning review input changed')
        if accepted and accepted.get('disposition_input_sha256') is not None:
            actual=accepted.get('disposition_input',{})
            if (digest(actual)!=accepted['disposition_input_sha256'] or actual.get('learning')!=learning
                    or actual.get('review')!=accepted.get('review')):
                raise PolicyError('Accepted Web disposition input changed')
        return {'accepted_learning_review':deepcopy(accepted.get('review')) if accepted else None,
            'accepted_learning_disposition':deepcopy(accepted.get('disposition')) if accepted else None,
            'accepted_learning_provenance':{'accepted_attempt_index':index,'learning_sha256':digest(learning),
                'review_input_sha256':accepted.get('review_input_sha256') if accepted else None,
                'disposition_input_sha256':accepted.get('disposition_input_sha256') if accepted else None,
                'application_intent':deepcopy(intent),
                'limit':'Exact retained accepted response; missing legacy provenance is not reconstructed. Opinion responses are context for the committed-result review, not another recursively reviewed final response.'}}

    async def _selection(self,saved,task):
        """Select before the actual exchange; after and recovery retain that cut."""
        e=self.e;stage=saved['stage'];before=e.store.record_get('web_work',saved['detail']['id']+':before')
        if 'selection' not in saved:
            if stage=='before':
                selection,_=await e._select_skills(task,saved['acquisition_operation'])
                self._assert_binding(task['id'],self._saved_binding(saved))
                saved['selection']=selection.model_dump()
                saved['selection_binding']=self._binding(task)
                # A pre-exchange legacy proposal may be rejudged without HTTP.
                # Its earlier assessments cannot attest this new selection.
                for name in ('assessments','review','disposition','outcome','completed_at'):
                    if name in saved:
                        saved.setdefault('preselection_history',{})[name]=saved.pop(name)
                saved['status']='pending'
            elif before and before.get('selection'):
                if (before.get('task_id')!=task['id'] or before.get('operation')!=saved['operation'] or
                    before['detail'].get('url')!=saved['detail'].get('url') or
                    before.get('disposition',{}).get('verdict')!='proceed'):
                    raise PolicyError('Web outcome differs from its reviewed pre-exchange selection')
                saved['selection']=deepcopy(before['selection'])
                saved['selection_binding']=deepcopy(before.get('selection_binding'))
                if saved.get('learning_applied') and saved['acquisition_operation']!=before['acquisition_operation']:
                    raise PolicyError('Applied Web operation differs from the actual pre-exchange input')
                saved['acquisition_operation']=deepcopy(before['acquisition_operation'])
            else:
                saved['selection']={'selected':[],'rejected':[],'new_knowledge_needed':[],
                    'rationale':'No retained pre-exchange selection; past Skill use remains unobserved. No retrospective selection or HTTP replay.'}
                saved['selection_binding']=None
            self._save(saved)
        if stage=='before':self._assert_selection(saved)
        return [s['id'] for s in saved['selection']['selected']]

    def _assert_selection(self,saved):
        for item in saved['selection']['selected']:
            actual=self.e.store.record_get('skill',item['id'])
            if not actual or actual['hash']!=item['hash'] or actual['status']=='retired':
                raise WebSelectionChanged('Selected Web Skill changed before dispatch; prepare a new reviewed acquisition')

    def prepared_exchange(self,task_id,operation,detail):
        """Only the actual collector journal can establish an unstarted request."""
        rows=[row for row in self.e.store.records('web_exchange')
              if row.get('record',{}).get('id')==detail['id']]
        return (len(rows)==1 and rows[0].get('stage')=='prepared'
            and rows[0].get('record')==detail
            and detail.get('task_id')==task_id
            and detail.get('operation_id')==operation['id']
            and detail.get('status')=='prepared'
            and detail.get('network_dispatched') is False
            and not detail.get('http_dispatched'))

    def _refresh_prepared_selection(self,saved):
        if saved['stage']!='before' or 'selection' not in saved:return saved
        try:self._assert_selection(saved)
        except WebSelectionChanged:
            if not self.prepared_exchange(saved['task_id'],saved['operation'],saved['detail']):
                raise PolicyError('Changed Web selection has no confirmed unstarted exchange; preserve its original history')
            previous=deepcopy(saved)
            saved={k:v for k,v in saved.items() if k not in {
                'selection','selection_binding','assessments','review','review_input',
                'review_input_sha256','disposition','outcome','completed_at'}}
            saved['status']='pending'
            def retain_and_refresh():
                self.e.store.record('web_work_history',previous['id']+':'+digest(previous),previous)
                self._save(saved)
                self.e.store.event(saved['task_id'],'web_selection','reprepare',{
                    'acquisition_id':saved['detail']['id'],'prior_judgment_sha256':digest(previous),
                    'network_dispatched':False,'reason':'Actual exchange remains prepared; select and review the current procedure before dispatch.'})
            self.e.store._transaction(retain_and_refresh)
        return saved

    def recover_legacy(self):
        """Project existing legacy actual results; never claim missing body bytes.

        Old versions persisted the after-review but could omit learning. Recover
        that mandatory frontier from its original record, not from new HTTP.
        """
        e=self.e
        for reviewed in e.store.records('web_review'):
            if reviewed.get('stage')!='after' or e.store.record_get('web_work',reviewed['id']):continue
            try:row=e.store.get_operation(reviewed['operation_id'])
            except KeyError:continue
            detail=reviewed['acquisition'];identity=detail['id']
            application=e.store.record_get('knowledge_application',reviewed['task_id']+':'+identity)
            applied_review=e.store.record_get('applied_knowledge_review',identity+':web-learning-outcome')
            saved={'id':reviewed['id'],'task_id':reviewed['task_id'],'operation':row['operation'],'stage':'after','detail':detail,
                'research_choice':None,'status':'pending','created_at':detail.get('finished_at',now()),'learning_attempts':[],
                'assessments':reviewed['assessments'],'review':reviewed['review'],'disposition':reviewed['disposition'],
                'policy_hash':row.get('policy_hash'), 'source_hash':row.get('source_hash'),
                'legacy_recovery':'Original metadata and review retained. Missing raw response bytes remain unobserved; no network replay establishes past content.'}
            events=e.store.events(reviewed['task_id'])
            observed=next((i for i,event in enumerate(events) if event['stage']=='web_acquisition' and event['status']=='observed_after' and event['detail'].get('id')==identity),None)
            if observed is not None:
                earlier=[event for event in events[:observed] if event['stage'].endswith('_research_query') and event['status']=='succeeded' and 'query' in event['detail'].get('result',{})]
                if earlier:saved['research_choice']=earlier[-1]['detail']['result']
                segment=[]
                for event in events[observed+1:]:
                    if event['stage']=='web_acquisition' and event['status'] in {'observed_before','observed_after'}:break
                    if event['status']=='succeeded':segment.append(event)
                draft=None
                for event in segment:
                    value=event['detail'].get('result')
                    if not isinstance(value,dict):continue
                    if event['stage']=='web_acquisition_learning':
                        draft={'learning':value,'choices':decision_targets('web-learning',value),'assessments':[],
                            'created_at':event['created_at'],'legacy_event_refs':[{'seq':event['seq'],'hash':event['hash']}]}
                    elif draft and event['stage']=='web_learning_choices_before':
                        draft['assessments'].extend(value.get('assessments',[]))
                        draft['learning']=dict(draft['learning'],ideas=[*draft['learning']['ideas'],
                            *[idea for a in value.get('assessments',[]) for idea in a['assessment']['ideas']]])
                    elif draft and event['stage']=='web_learning_review':draft['review']=value
                    elif draft and event['stage']=='web_learning_disposition' and 'review' in draft:
                        draft['disposition']=value;draft['legacy_event_refs'].append({'seq':event['seq'],'hash':event['hash']})
                        saved['learning_attempts'].append(draft);draft=None
            if application:
                self._restore_applied(saved,e.store.get_task(reviewed['task_id']))
                outcome=saved['learning_applied']
                if applied_review:
                    saved.update(status='complete',outcome={'assessments':reviewed['assessments'],'review':reviewed['review'],
                        'disposition':reviewed['disposition'],'knowledge':outcome})
            e.store.record('web_work',saved['id'],saved)
            e.store.event(reviewed['task_id'],'web_recovery','projected',{'web_work_id':saved['id'],'status':saved['status'],
                'source_review_id':reviewed['id'],'network_replayed':False,'response_bytes_reconstructed':False})

    async def evaluate(self,task_id,operation,stage,detail,research_choice=None):
        e=self.e;key=detail['id']+':'+stage
        if stage not in {'before','after'}:raise PolicyError('Unknown Web acquisition judgment stage')
        saved=e.store.record_get('web_work',key)
        task=e.store.get_task(task_id)
        binding=self._binding(task)
        if saved and stage=='after':
            incoming=detail
            saved=self._advance_pending_analysis(saved,task)
            transition=saved.get('analysis_transition')
            if transition and digest(incoming)==transition['original_detail_sha256']:
                detail=saved['detail']
        if not saved:
            saved={'id':key,'task_id':task_id,'operation':operation,'stage':stage,'detail':detail,
                'research_choice':research_choice,'status':'pending','created_at':now(),'learning_attempts':[],
                'context_contract_version':'web-identity-v4',**binding}
            self._save(saved)
        else:
            if saved['task_id']!=task_id or saved['operation']!=operation or saved['detail']!=detail:
                raise PolicyError('Web continuation differs from its stored task, operation or acquired result')
            # Keep original acquired bytes/measurement identity, even after a
            # restart. This continuation cannot become another acquisition.
            operation=saved['operation'];detail=saved['detail'];research_choice=saved['research_choice']
        if stage=='after':self._restore_applied(saved,task)
        else:saved=self._refresh_prepared_selection(saved)
        if self._saved_binding(saved)!=binding:
            # Preserve the old review/outcome and every attempt before replacing
            # only the current judgment. The acquisition and commit stay fixed.
            old_binding=self._saved_binding(saved)
            e.store.record('web_work_history',key+':'+digest(saved),saved)
            for attempt in saved['learning_attempts']:attempt.setdefault('binding',old_binding)
            saved={k:v for k,v in saved.items() if k not in {'assessments','review','disposition','outcome','completed_at'}}
            if stage=='before':
                saved.pop('selection',None);saved.pop('selection_binding',None)
            if not saved.get('learning_applied'):saved.pop('application_intent',None)
            saved.update(status='pending',**binding)
            self._save(saved)
        self._ensure_acquisition_input(saved)
        selected=await self._selection(saved,task)
        context=self._context(saved)
        judgment_context=(current_input(context,identity=self._identity_context(saved),editable_learning=False)
            if saved.get('context_contract_version') in {'web-identity-v3','web-identity-v4'} else context)
        learning_context=current_input(context,identity=self._identity_context(saved))
        if saved.get('status')=='complete':return self._current_outcome(saved)
        acquisition=saved['acquisition_operation'];result=saved['acquisition_result']
        if 'assessments' not in saved:
            e.store.event(task_id,'web_acquisition','observed_'+stage,detail)
            target_detail=context['acquisition'] if saved.get('context_contract_version')=='web-identity-v4' else detail
            targets=[{'id':'acquisition','statement':canonical(target_detail)},*decision_targets('research-choice',research_choice or {}),
                     *decision_targets('skill-selection',saved['selection'])]
            saved['assessments']=await e._assess_targets(task_id,'web_acquisition_'+stage,targets,judgment_context)
            self._assert_binding(task_id,binding)
            e.store.record('web_work',key,saved)
            e.store.record('web_assessment',key,{'id':key,'assessments':saved['assessments']})
        if 'review' not in saved:
            saved['review_input']={**judgment_context,'assessments':saved['assessments']}
            saved['review']=(await e.bounded_judgments.review_proposal(task_id,acquisition,'web_acquisition_review_'+stage,
                saved['review_input'])).model_dump()
            saved['review_input_sha256']=digest(saved['review_input'])
            self._assert_binding(task_id,binding)
            e.store.record('web_work',key,saved)
        if 'disposition' not in saved:
            saved['disposition']=(await e.bounded_judgments.disposition_proposal(task_id,acquisition,'web_acquisition_disposition_'+stage,
                {**judgment_context,'assessments':saved['assessments'],'review':saved['review'],'sources':[]})).model_dump()
            self._assert_binding(task_id,binding)
            e.store.record('web_work',key,saved)
        reviewed={'id':key,'task_id':task_id,'operation_id':operation['id'],'stage':stage,
            'acquisition':detail,'assessments':saved['assessments'],'review':saved['review'],'disposition':saved['disposition'],**binding}
        e.store.record('web_review',key,reviewed)
        learned=None
        if stage=='after':
            before=e.store.record_get('web_assessment',detail['id']+':before') or {}
            # Historical identities precede their later decisions. They remain
            # required originals, not peer judgments of the current proposal.
            required=list(saved.get('analysis_transition',{}).get('failed_restart',{}).get('required_ideas',[]))
            required.extend(idea for a in [*before.get('assessments',[]),*saved['assessments']] for idea in a['assessment']['ideas'])
            required.extend(self._observed_ideas(task_id,detail['id']))
            if not saved.get('learning_applied'):
                while True:
                    previous=saved['learning_attempts'][-1] if saved['learning_attempts'] else None
                    returned=self.learning_return(task_id,previous['governed_correction']) if previous and previous.get('governed_correction') else None
                    observed=e.knowledge.application_state(task,acquisition,
                        Learning.model_validate(previous['learning']) if previous else None,result,selected)
                    if observed['state']!='not_committed':
                        raise PolicyError('Web knowledge application state changed or is inconsistent; reconcile its exact original commit before continuing')
                    required_by_id={}
                    for idea in [*required,*(i for a in saved['learning_attempts'] for i in a['learning']['ideas'])]:
                        prior=required_by_id.get(idea['id'])
                        if prior and (prior['proposal'],prior['target'])!=(idea['proposal'],idea['target']):
                            raise PolicyError('Prior idea identity was reused for different meaning')
                        required_by_id[idea['id']]=idea
                    failed_uncommitted=False
                    if previous and previous.get('application_failure'):
                        try:e.knowledge.validate_learning_proposal(task,acquisition,Learning.model_validate(previous['learning']),result,selected)
                        except LearningContractError:
                            pass  # Known invalid proposal: new reviewed input, same result.
                        else:
                            recovery={'observation':observed,'original_failure':deepcopy(previous['application_failure']),
                                'action':'Apply the same accepted input after observed non-commit; no new Learning or acquisition.'}
                            e.store.record('knowledge_application_reconciliation',digest(recovery),recovery)
                            failed_uncommitted=True
                    resumable=previous and previous.get('binding')==binding and (not previous.get('application_failure') or failed_uncommitted) and not previous.get('proposal_failure')
                    if saved.get('analysis_transition') and previous and previous.get('result_sha256')!=digest(result):resumable=False
                    # A saved verdict is still work to consume. In particular a
                    # governed revise without its later pointer is not a new
                    # Learning request after interruption.
                    attempt=previous if resumable and not previous.get('revision_consumed') else None
                    if attempt is None:
                        feedback=revision_feedback(e.store,task_id,acquisition['id'],saved['learning_attempts'])
                        proposal_input={**learning_context,'operation':acquisition,'result':result,
                            'assessments':saved['assessments'],'acquisition_review':reviewed,'required_ideas':list(required_by_id.values()),
                            'knowledge':e._knowledge_context(task),'actual_revision_feedback':feedback,
                            **({'governed_correction_return':returned} if returned else {}),
                            'instruction':'Classify this actual outcome even when acquisition review requires revision or transport failed. Revise only the learning proposal using every actual opinion; never refetch to repair this record. Preserve all idea identities, dispose redundant ideas with reasons. Current collection may still have remaining URLs; this exchange is not the completed collection. Keep measured/unobserved facts distinct. Q04 provisional knowledge handles unknown recurrence.'}
                        if saved.get('context_contract_version')=='web-identity-v4':
                            proposal_input['acquisition_review']=dict(reviewed,
                                acquisition=self._acquisition_summary(detail,source_pointer='/result/data/response_analysis/source'))
                        learning=await e.bounded_judgments.learning_proposal(task_id,'web_acquisition_learning',proposal_input)
                        self._assert_binding(task_id,binding)
                        e._validate_ideas(list(required_by_id.values()),learning)
                        attempt={'learning':learning.model_dump(),'choices':decision_targets('web-learning',learning,group_ideas=True),
                                 'binding':binding,'created_at':now(),'projection_version':PROJECTION_VERSION,'result_sha256':digest(result)}
                        receipt=e.bounded_judgments._learning_receipts.get(digest(learning.model_dump()))
                        if receipt:attempt['proposal_receipt']=e.bounded_judgments._call_ref(receipt)
                        composition=e.bounded_judgments.learning_review_context(task_id,acquisition['id'],learning)
                        if composition:attempt['learning_composition']=composition
                        saved['learning_attempts'].append(attempt);self._save(saved)
                    # Recovery may reuse the latest attempt itself. Its actual
                    # predecessor stays the preceding retained attempt, not the
                    # current proposal or its first newly received review.
                    attempt_index=next(i for i,item in enumerate(saved['learning_attempts']) if item is attempt)
                    predecessor=saved['learning_attempts'][attempt_index-1] if attempt_index else None
                    learning=Learning.model_validate(attempt['learning']);choices=attempt['choices']
                    feedback=revision_feedback(e.store,task_id,acquisition['id'],saved['learning_attempts'][:attempt_index])
                    try:e.knowledge.validate_learning_proposal(task,acquisition,learning,result,selected)
                    except LearningContractError as error:
                        # A legacy accepted proposal is retained byte-for-byte;
                        # reject only its admission, then generate/review anew.
                        fault={'type':type(error).__name__,'message':str(error),
                            'learning_sha256':digest(attempt['learning']),
                            'operation_sha256':digest(acquisition),'result_sha256':digest(result),
                            'observed_at':now(),'target_effect':'none; pure validation'}
                        attempt['proposal_failure']=fault
                        saved.setdefault('proposal_failures',[]).append(fault);self._save(saved)
                        continue
                    if 'assessments' not in attempt:
                        e.bounded_judgments.authenticate_learning_attempt(task_id,'web_acquisition_learning',attempt,context,predecessor)
                        old_context={**context,'learning':learning.model_dump(),'actual_revision_feedback':predecessor,'timing':'before knowledge application'}
                        current_choices=decision_targets('web-learning',learning,group_ideas=True)
                        assessed=await e._assess_targets(task_id,'web_learning_choices_before',current_choices,
                            {**learning_context,'learning':learning.model_dump(),'actual_revision_feedback':feedback,'timing':'before knowledge application'},
                            previous=(choices,old_context) if not attempt.get('projection_version') else None)
                        self._assert_binding(task_id,binding)
                        choices=e.bounded_judgments.assessment_targets(task_id,'web_learning_choices_before',assessed,[current_choices,choices])
                        choice_context=e.bounded_judgments.assessment_context(task_id,'web_learning_choices_before',choices,assessed)
                        learning.ideas=[Idea.model_validate(i) for i in merge_ideas(learning.model_dump()['ideas'],
                            [i for a in assessed for i in a['assessment']['ideas']])]
                        # Preserve the original target definition as history. The
                        # reviewed current target projection has a separate slot.
                        attempt.update(assessments=assessed,learning=learning.model_dump(),current_choices=choices,choice_context=choice_context);self._save(saved)
                    else:
                        choices,choice_context=e.bounded_judgments.authenticate_learning_choices(task_id,'web_acquisition_learning',
                            'web_learning_choices_before',attempt,context,predecessor,assessment_field='assessments',
                            target_field='current_choices' if 'current_choices' in attempt else 'choices')
                        attempt.update(current_choices=choices,choice_context=choice_context);self._save(saved)
                    if 'review' not in attempt:
                        review_input={**learning_context,'learning':deepcopy(attempt['learning']),
                            'choice_assessments':deepcopy(attempt['assessments']),'actual_revision_feedback':feedback,
                            **({'learning_composition':deepcopy(attempt['learning_composition'])} if attempt.get('learning_composition') else {})}
                        previous_input=attempt.get('review_input') or {**context,'learning':deepcopy(attempt['learning']),
                            'choice_assessments':deepcopy(attempt['assessments']),'actual_revision_feedback':predecessor,
                            **({'learning_composition':deepcopy(attempt['learning_composition'])} if attempt.get('learning_composition') else {})}
                        attempt['review']=(await e.bounded_judgments.review_proposal(task_id,acquisition,'web_learning_review',review_input,
                            previous=previous_input)).model_dump()
                        attempt['review_input']=e.bounded_judgments.review_input(task_id,'web_learning_review',attempt['review'])
                        attempt['review_input_sha256']=digest(attempt['review_input'])
                        self._assert_binding(task_id,binding);self._save(saved)
                    else:
                        if digest(attempt['review_input'])!=attempt['review_input_sha256']:
                            raise PolicyError('Saved Web Learning review input changed')
                        actual=(await e.bounded_judgments.review_proposal(task_id,acquisition,'web_learning_review',
                            attempt['review_input'],retained_only=True)).model_dump()
                        if actual!=attempt['review']:raise PolicyError('Saved Web Learning review differs from its actual return')
                    if 'disposition' in attempt and not attempt.get('disposition_input_sha256'):
                        # A legacy accepted response remains evidence of its old
                        # request, not of the new unchanged-payload contract.
                        # Reconsider only this uncommitted decision. Learning,
                        # review, acquired result and old response stay retained.
                        history={'disposition':deepcopy(attempt['disposition']),
                            'learning_sha256':digest(attempt['learning']),
                            'review_sha256':digest(attempt['review']),
                            'reason':'Explicit unchanged operative payload contract before first application'}
                        attempt.setdefault('prior_dispositions',[]).append(history)
                        attempt.pop('disposition');self._save(saved)
                    if 'disposition' not in attempt:
                        attempt['disposition_input']={**learning_context,'learning':deepcopy(attempt['learning']),
                            'review':deepcopy(attempt['review']),'sources':[],
                            'identity_and_stage':self._identity_context(saved)}
                        self._save(saved)
                        attempt['disposition']=(await e.bounded_judgments.disposition_proposal(task_id,acquisition,
                            'web_learning_disposition',attempt['disposition_input'])).model_dump()
                        attempt['disposition_input_sha256']=digest(attempt['disposition_input'])
                        self._assert_binding(task_id,binding);self._save(saved)
                    else:
                        if digest(attempt['disposition_input'])!=attempt['disposition_input_sha256']:
                            raise PolicyError('Saved Web Learning disposition input changed')
                        actual=(await e.bounded_judgments.disposition_proposal(task_id,acquisition,'web_learning_disposition',
                            attempt['disposition_input'],retained_only=True)).model_dump()
                        if actual!=attempt['disposition']:raise PolicyError('Saved Web Learning disposition differs from its actual return')
                    disposition=attempt['disposition']
                    if disposition['verdict']=='proceed':break
                    if disposition['verdict']=='hold':raise PolicyError('Web learning held at its original acquired result: '+disposition['rationale'])
                    if attempt['disposition_input'].get('learning_projection'):
                        route=e.bounded_judgments.disposition_route(task_id,'web_learning_disposition',attempt['disposition_input'],disposition)
                        if route and route['scope']=='governed':
                            attempt['governed_correction']=self.park_learning(task,acquisition,result,attempt['learning'],attempt['review'],disposition,route,saved=saved)
                            self._save(saved)
                            self.learning_return(task_id,attempt['governed_correction'])
                    if predecessor and digest(predecessor['learning'])==digest(attempt['learning']):raise PolicyError('Web learning repeated unchanged after exact review feedback')
                    attempt['revision_consumed']=True;self._save(saved)
                self._assert_binding(task_id,binding)
                # Commit the accepted input identity before entering Knowledge.
                # On restart an accepted attempt is reused, never regenerated.
                saved['application_intent']={'input_hash':digest({'operation':acquisition,'learning':learning.model_dump(),'result':result,'selected':selected}),
                    'accepted_attempt_index':len(saved['learning_attempts'])-1,
                    'accepted_review_sha256':digest(attempt['review']),
                    'accepted_disposition_sha256':digest(attempt['disposition']),
                    'binding':binding,'started_at':now()}
                self._save(saved)
                e.store.event(task_id,'web_knowledge_application','started',{'acquisition_id':detail['id']})
                try:learned=e.knowledge.apply(task,acquisition,learning,result,selected)
                except Exception as error:
                    fault={'type':type(error).__name__,'message':str(error),'input_hash':saved['application_intent']['input_hash'],'observed_at':now(),
                        'transaction_observation':deepcopy(getattr(error,'harness_transaction_observation',None))}
                    if attempt.get('application_failure'):
                        attempt.setdefault('application_failure_history',[]).append(deepcopy(attempt['application_failure']))
                    attempt['application_failure']=fault
                    saved.setdefault('application_failures',[]).append(fault);self._save(saved)
                    raise
                saved.update(learning_applied=learned,applied_choices=choices,applied_learning=learning.model_dump(),learning_application_binding=binding)
                e.store.record('web_work',key,saved)
                e.store.event(task_id,'web_knowledge_application',
                    'partial' if learned.get('projection_status',{}).get('state')=='pending' else 'succeeded',learned)
            else:learned=saved['learning_applied']
            phase='web-learning-outcome'
            if saved.get('learning_application_binding')!=binding:
                phase+=':'+digest(binding)
                # New acquisition-review ideas remain ordinary governed work;
                # an old committed Learning is not edited to include them.
                e._journal_judgment_result(task,acquisition,'web-acquisition-reassessment:'+digest(binding),
                    [*before.get('assessments',[]),*saved['assessments']],result,reviewed)
            applied_review=await e._judge_applied_knowledge(task,acquisition,phase,saved['applied_choices'],learned,result,{'sources':[]},
                context={**{k:v for k,v in context.items() if k not in {'operation','result'}},
                    'learning':saved['applied_learning'],'acquisition_review':reviewed,
                    'identity_and_stage':self._identity_context(saved),
                    **self._accepted_learning_context(saved)},
                consumer=self.applied_source(task,detail['id'])['consumer'])
            if 'review' in applied_review:saved['applied_review_id']=applied_review['review']['id']
            self._assert_binding(task_id,binding)
        elif saved['disposition']['verdict']!='proceed':
            e._journal_judgment_result(task,acquisition,'web-before-unexecuted',saved['assessments'],
                {'status':'pending','effect':'none','data':{'request_not_dispatched':True,'disposition':saved['disposition']}},reviewed)
        outcome={'assessments':saved['assessments'],'review':saved['review'],'disposition':saved['disposition'],'knowledge':learned}
        self._assert_binding(task_id,binding)
        # All awaited judgments have finished. DNS/HTTP has not started yet;
        # an acquired response instead retains its original selected versions.
        if stage=='before':self._assert_selection(saved)
        saved.update(status='complete',outcome=outcome,completed_at=now());self._save(saved)
        return self._current_outcome(saved)

    async def drain(self,task_id):
        # Before-only proposals can safely be abandoned. Actual after-results
        # must finish their judgment/knowledge handling. Surface old mandatory
        # corrections first; do not spend the resumed parent on a later FAQ
        # before its already committed result has received reviewed correction.
        blocked={c['consumer']['parent_operation_id'] for c in self.correction_frontier(task_id)}
        for item in self.e.store.records('web_work'):
            if (item['task_id']==task_id and item['stage']=='after' and
                    item['operation']['id'] not in blocked and
                    (item['status']!='complete' or self._saved_binding(item)!=self._binding(self.e.store.get_task(task_id)))):
                outcome=await self.evaluate(task_id,item['operation'],'after',item['detail'],item.get('research_choice'))
                if outcome.get('pending_applied_reviews'):
                    raise WebCorrectionNeeded(item['operation']['id'],outcome['pending_applied_reviews'])
