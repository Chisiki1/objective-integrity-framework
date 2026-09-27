"""Governed preparation of opaque task sources; no model-owned raw-byte input.

Engine owns operation selection, permits and the complete pre/post/learning cycle.
Only ``confirm`` makes an extracted derivative or explicit withdrawal available to
source reconciliation. Merely staging bytes or running a program never adopts the
source's meaning. The original blob remains immutable in Store.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from .capabilities import confined, sha_file
from .models import Operation, OperationResult, PolicyError, now
from .store import digest, redact


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _result_hash(result: dict) -> str:
    # The trusted runtime envelope is stamped after dispatch, not supplied here.
    return digest(redact({k: v for k, v in result.items() if k != 'controller_runtime'}))


def _reviewed_row(store, task_id, operation_id, *, post=True, _visited=None):
    visited=set() if _visited is None else set(_visited)
    if operation_id in visited:
        raise PolicyError('Source preparation recovery lineage contains a cycle')
    visited.add(operation_id)
    row = store.get_operation(operation_id)
    if row['task_id'] != task_id:
        raise PolicyError('Source preparation evidence belongs to another task')
    for stage in ('pre', 'post') if post else ('pre',):
        bundle, review = row.get(stage + '_bundle'), row.get(stage + '_review')
        if not bundle or not review or review.get('bundle_hash') != digest(bundle):
            raise PolicyError('Source preparation needs the exact reviewed ' + stage + ' bundle')
        if (review.get('disposition') or {}).get('verdict') != 'proceed' or bundle.get('operation') != row['operation']:
            raise PolicyError('Source preparation evidence was not accepted unchanged')
        if stage == 'post' and bundle.get('result') != row.get('result'):
            successor_id=row.get('reconciliation_ref')
            history=store.record_get('effect_reconciliation',successor_id) if successor_id else None
            original=(history or {}).get('original',{})
            if (row['operation']['kind']!='source_prepare' or not history
                    or original.get('operation')!=row['operation'] or original.get('post_bundle')!=bundle
                    or original.get('post_review')!=review or original.get('result')!=bundle.get('result')
                    or history.get('reconciled')!=row['result']):
                raise PolicyError('Source preparation reviewed result differs without exact recovery lineage')
            recovery=store.get_operation(successor_id)
            recovery_owner=recovery['task_id']
            if recovery_owner!=task_id and (store.get_task(task_id)['parent_id']!=recovery_owner
                    or recovery['operation']['kind']!='child_reconcile'):
                raise PolicyError('Source preparation recovery belongs to another task')
            successor=_reviewed_row(store,recovery_owner,successor_id,_visited=visited)
            data=(successor.get('result') or {}).get('data',{})
            if (successor['operation']['kind'] not in {'reconcile','child_reconcile'}
                    or successor['operation']['args'].get('operation_id')!=operation_id
                    or data.get('original_operation_id')!=operation_id or data.get('reconciliation')!=row['result']
                    or successor['post_review']['id']!=history.get('review_ref')
                    or successor['result']['status']!='succeeded'):
                raise PolicyError('Source preparation recovery successor differs')
            if successor['operation']['kind']=='child_reconcile' and (data.get('child_id')!=task_id
                    or data.get('original_result_hash')!=digest(original['result'])):
                raise PolicyError('Source preparation child recovery identity differs')
    if post and row['status'] not in {'learned', 'cycle_complete'}:
        raise PolicyError('Source preparation evidence still requires its learning cycle')
    if post and any(c['task_id']==task_id and c['operation_id']==operation_id and c['status']=='pending'
                    for c in store.records('judgment_correction')):
        raise PolicyError('Source preparation evidence still requires its judgment correction')
    return row


def prepared_source_texts(store, task) -> dict[str, str]:
    """Read confirmed, source-bound derivatives without granting new authority."""
    texts = {}
    for source in task['source_history']:
        if source['kind'] != 'attachment' or source.get('classification') == 'withdraw':
            continue
        record = store.record_get('source_extraction', source['id'])
        if not record:
            continue
        if record['task_id'] != task['id'] or record['source_sha256'] != source['sha256']:
            raise PolicyError('Prepared source origin differs')
        store.source_bytes(task['id'], source['id'], source['sha256'])
        row = _reviewed_row(store, task['id'], record['operation_id'])
        if _result_hash(row['result']) != record['result_sha256']:
            raise PolicyError('Prepared source result changed')
        raw, actual = store.source_bytes(task['id'], record['raw_source_ref'], record['extracted_sha256'])
        if actual != record['extracted_sha256'] or len(raw) != record['extracted_bytes']:
            raise PolicyError('Prepared source derivative changed')
        text = raw.decode('utf-8')
        if '\x00' in text:
            raise PolicyError('Prepared source is not complete UTF-8 text')
        texts[source['id']] = redact(text)
    return texts


class SourcePreparation:
    def __init__(self, store, executor):
        self.store, self.executor = store, executor

    def _source(self, task, args, *, current=True):
        owner = self.store.get_task(task['id'])
        if current and args.get('expected_source_hash') != owner['source_hash']:
            raise PolicyError('SOURCE_HASH_CONFLICT: preparation source set changed')
        source = next((s for s in owner['source_history'] if s['id'] == args.get('source_id')), None)
        if not source or source['kind'] != 'attachment' or source['sha256'] != args.get('expected_hash'):
            raise PolicyError('Preparation needs an exact task-owned attachment')
        raw, sha = self.store.source_bytes(owner['id'], source['id'], source['sha256'])
        return owner, source, raw, sha

    @staticmethod
    def _args(args):
        common = {'mode', 'source_id', 'expected_hash', 'expected_source_hash', 'reason'}
        extra = {'stage': set(),
                 'accept_extraction': {'stage_operation_id', 'extraction_operation_id', 'output_path', 'expected_output_sha256', 'method'},
                 'withdraw': {'instruction_id', 'source_quote'},
                 'retain': {'instruction_id', 'source_quote'}}
        mode = args.get('mode')
        if mode not in extra or set(args) != common | extra[mode]:
            raise PolicyError('Invalid source_prepare fields for mode')
        if any(not isinstance(args[k], str) or not args[k].strip() for k in args):
            raise PolicyError('Source preparation arguments must be nonempty strings')
        return mode

    def _protected(self, task_id, operation_id):
        row = _reviewed_row(self.store, task_id, operation_id)
        record = self.store.record_get('source_preparation', operation_id)
        if not record or record['task_id'] != task_id or record['operation_sha256'] != digest(row['operation']):
            raise PolicyError('Source preparation record is missing or foreign')
        # Validation of a persisted result normalizes default 0 to float 0.0.
        # Compare the typed result values; retain both original serialized cuts
        # and all stored historical hashes instead of rewriting their lineage.
        observed=OperationResult.model_validate(row['result']).model_dump(exclude={'controller_runtime'})
        actual=OperationResult.model_validate(record['result']).model_dump(exclude={'controller_runtime'}) if record.get('result') else None
        if actual is None or digest(redact(observed))!=digest(redact(actual)):
            raise PolicyError('Source preparation result differs from protected actual result')
        if row['result']['status'] != 'succeeded' or row['result']['effect'] != 'confirmed':
            raise PolicyError('Source preparation did not confirm its actual effect')
        return row, record

    def _extraction(self, task, args, source, raw):
        stage, staged = self._protected(task['id'], args['stage_operation_id'])
        if staged['candidate']['mode'] != 'stage' or staged['candidate']['source_id'] != source['id'] or staged['candidate']['source_sha256'] != source['sha256']:
            raise PolicyError('Extraction is bound to a different staged original')
        row = _reviewed_row(self.store, task['id'], args['extraction_operation_id'])
        if row['operation']['kind'] != 'exec' or row['result']['status'] != 'succeeded' or row['result']['effect'] != 'confirmed':
            raise PolicyError('Extraction needs an actual successful reviewed Executor operation')
        actual = self.executor._read_record(row['operation']['id'])
        if (not actual or actual.get('owner') != self.executor.owner or actual.get('workspace') != task['workspace']
                or actual.get('operation') != row['operation'] or not actual.get('result')):
            raise PolicyError('Extraction lacks its protected Executor record')
        saved_result = dict(row['result'])
        saved_result['data'] = {k: v for k, v in saved_result['data'].items() if k != 'admitted_source_hashes'}
        if _result_hash(saved_result) != _result_hash(actual['result']):
            raise PolicyError('Extraction result differs from actual Executor outcome')
        candidate = (row.get('pre_bundle') or {}).get('candidate') or {}
        staged_path = staged['candidate']['path']
        before = actual.get('before_manifest', {})
        after = actual.get('after_manifest', {})
        original = {'type': 'file', 'bytes': len(raw), 'sha256': source['sha256']}
        if before.get(staged_path) != original or after.get(staged_path) != original:
            raise PolicyError('Extraction did not preserve the exact original input')
        if candidate.get('workspace_manifest') != before:
            raise PolicyError('Extraction workspace differs from its reviewed input manifest')
        output = confined(Path(task['workspace']), args['output_path'])
        relative = output.relative_to(Path(task['workspace'])).as_posix()
        data = output.read_bytes()
        expected = {'type': 'file', 'bytes': len(data), 'sha256': _sha(data)}
        if (relative == staged_path or after.get(relative) != expected or relative not in actual['result']['data'].get('changed_paths', [])
                or _sha(data) != args['expected_output_sha256']):
            raise PolicyError('Extraction output differs from the actual changed file')
        try:
            text = data.decode('utf-8')
        except UnicodeError as error:
            raise PolicyError('Extraction output must be complete UTF-8 text') from error
        if '\x00' in text or not text.strip():
            raise PolicyError('Extraction produced no usable text')
        proof = {'stage_operation_id': stage['operation']['id'], 'stage_operation_sha256': digest(stage['operation']),
                 'stage_result_sha256': _result_hash(stage['result']), 'extraction_operation_id': row['operation']['id'],
                 'extraction_operation_sha256': digest(row['operation']), 'extraction_result_sha256': _result_hash(row['result']),
                 'executor_record_sha256': digest(actual), 'output_path': relative, 'extracted_sha256': _sha(data),
                 'extracted_bytes': len(data), 'method': args['method'], 'text': redact(text),
                 'fidelity': 'Actual reviewed program/input/output lineage. Faithfulness requires the parent and reviewer to inspect the extraction method and complete output; program success alone is not semantic fidelity.'}
        return proof, data

    def describe(self, task, args):
        mode = self._args(args)
        task, source, raw, sha = self._source(task, args)
        attempts=self._pending_confirmations(task['id'],source['id'])
        if source['status'] != 'pending' and not (mode=='retain' and source['status']=='applied' and attempts):
            raise PolicyError('Source preparation is only for a pending attachment')
        base = {'mode': mode, 'task_id': task['id'], 'source_id': source['id'], 'source_sha256': sha,
                'source_set_hash': task['source_hash'], 'bytes': len(raw), 'filename': source.get('filename'),
                'reason': args['reason'], 'original_private_bytes_retained': True, 'source_semantics_adopted': False}
        if mode == 'stage':
            # Source identity is already unique and the immutable blob hash is
            # bound in this candidate. Duplicating both in nested path names can
            # exceed Windows path limits in an otherwise valid owned workspace.
            path = f'source_inputs/{source["id"]}.bin'
            target = confined(Path(task['workspace']), path, allow_missing=True)
            if target.exists():
                raise PolicyError('Staged source path already exists; consume or reconcile its original operation')
            return {**base, 'path': path, 'expected_before_sha256': None,
                    'capability_route': 'Prepare extractor source using file_write; bind its exact UTF-8 source in capability_request, then exec. The raw binary input is pinned by the reviewed complete workspace_manifest, not decoded as executable source.'}
        if mode == 'accept_extraction':
            proof, _ = self._extraction(task, args, source, raw)
            return {**base, 'extraction': proof}
        history = task['source_history']
        instruction = next((s for s in history if s['id'] == args['instruction_id']), None)
        if (not instruction or instruction['kind'] != 'instruction' or history.index(instruction) <= history.index(source)
                or not args['source_quote'].strip() or args['source_quote'] not in instruction['text']):
            raise PolicyError('Source disposition needs an exact quote from a later user instruction')
        if mode=='retain':
            if not attempts:raise PolicyError('Retention requires an unresolved prior confirmation')
            for prior in attempts:
                original=self.store.get_operation(prior['id'])['operation']['args']
                earlier=next((s for s in history if s['id']==original.get('instruction_id')),None)
                if not earlier or history.index(instruction)<history.index(earlier) or (
                        original.get('mode')=='withdraw' and history.index(instruction)==history.index(earlier)):
                    raise PolicyError('Retention needs a user instruction later than the prior withdrawal decision')
        return {**base, 'instruction_id': instruction['id'], 'instruction_sha256': instruction['sha256'],
                'source_quote': args['source_quote'], 'unchanged_acceptance': task['acceptance'],
                'prior_confirmation_attempts':attempts,
                'meaning': ('Review whether this later user quote explicitly reverses the prior withdrawal and retains the attachment. Only the pending confirmation is superseded; reading and source adoption still have their normal requirements.' if mode=='retain' else 'Review whether the actual later user quote explicitly withdraws this particular attachment. Do not infer withdrawal from silence, extraction failure or a model proposal. No other source, acceptance or unknown effect is removed.')}

    def _pending_confirmations(self, task_id, source_id):
        return [r for r in self.store.records('source_preparation_confirmation_pending')
                if r['task_id']==task_id and r['source_id']==source_id and r['status']=='source_revalidation_required']

    def pending(self, task):
        ready = prepared_source_texts(self.store, task)
        items = []
        for source in task['source_history']:
            if source['kind'] != 'attachment':
                continue
            confirmations=self._pending_confirmations(task['id'],source['id'])
            if not confirmations and (source['status'] != 'pending' or source['id'] in ready):continue
            progress = self.store.record_get('source_read_progress', source['id']) or {}
            if not confirmations and progress.get('text_decoded') and progress.get('complete'):
                continue
            stages = [record for record in self.store.records('source_preparation')
                      if record['task_id'] == task['id'] and record['candidate']['source_id'] == source['id']]
            items.append({'source_id': source['id'], 'expected_hash': source['sha256'], 'expected_source_hash': task['source_hash'],
                          'filename': source.get('filename'), 'read_progress': progress,
                          'preparations': [{'operation_id': p['id'], 'mode': p['candidate']['mode'], 'result': p.get('result'),
                                            'path': p['candidate'].get('path')} for p in stages],
                          'confirmation_pending':confirmations,
                          'status': 'confirmation_required' if confirmations else 'preparation_required', 'source_semantics_adopted': False})
        return {'pending': items, 'allowed_preparation_operations': ['source_prepare', 'source_read', 'file_write', 'file_read', 'file_list',
                'capability_request', 'exec', 'web_fetch', 'history_read', 'knowledge_read', 'knowledge_index', 'knowledge_update',
                'knowledge_projection_repair', 'reconcile', 'judgment_resolve', 'controller_read', 'prepare_update',
                'verify_update', 'activate_update', 'rollback_update', 'adopt_policy', 'policy_amendment'],
                'instruction': 'Choose the next necessary reviewed preparation operation. Stage exact private input, research/prepare and validate the needed extraction method, then accept the actual reviewed output. A later explicit user withdrawal is reachable with its exact quote. A pending confirmation remains mandatory even if the attachment is decoded: use a current reviewed withdrawal, or mode=retain with a newer exact user quote reversing it. Opaque source content remains unadopted and all other obligations/effects remain open.'}

    async def execute(self, task, operation):
        existing = self.store.record_get('source_preparation', operation.id)
        if existing:
            if existing['task_id'] != task['id'] or existing['operation_sha256'] != digest(operation.model_dump()):
                raise PolicyError('Source preparation operation identity changed')
            return await self.reconcile(task, operation)
        row = _reviewed_row(self.store, task['id'], operation.id, post=False)
        candidate = self.describe(task, operation.args)
        if digest(row['pre_bundle'].get('candidate')) != digest(candidate):
            raise PolicyError('Source preparation candidate changed after review')
        record = {'id': operation.id, 'task_id': task['id'], 'operation_sha256': digest(operation.model_dump()),
                  'candidate': candidate, 'created_at': now(), 'phase': 'prepared'}
        self.store.record('source_preparation', operation.id, record)
        if candidate['mode'] == 'stage':
            raw, _ = self.store.source_bytes(task['id'], candidate['source_id'], candidate['source_sha256'])
            internal_id = _sha((operation.id + ':source-stage').encode())[:32]
            record['executor_operation_id'] = internal_id
            self.store.record('source_preparation', operation.id, record)
            actual = await self.executor.execute(Path(task['workspace']), Operation(id=internal_id, kind='file_write',
                args={'path': candidate['path'], 'base64': base64.b64encode(raw).decode('ascii'), 'expected_sha256': None},
                purpose=operation.purpose, decisions=operation.decisions, expected_result=operation.expected_result))
            return self._save_stage_result(record, actual)
        def capture():
            data = {'source_id': candidate['source_id'], 'source_sha256': candidate['source_sha256'],
                    'mode': candidate['mode'], 'original_private_bytes_retained': True, 'confirmation_pending': True}
            if candidate['mode'] == 'accept_extraction':
                _, source, raw, _ = self._source(task, operation.args)
                proof, extracted = self._extraction(task, operation.args, source, raw)
                if digest(proof) != digest(candidate['extraction']):
                    raise PolicyError('Extraction evidence changed before capture')
                derivative = self.store.store_source_derivative(task['id'], source['id'], source['sha256'],
                                                               operation.id + ':extracted', extracted)
                data.update(derivative)
            else:
                data.update(instruction_id=candidate['instruction_id'], instruction_sha256=candidate['instruction_sha256'],
                            source_quote=candidate['source_quote'])
            result = OperationResult(operation_id=operation.id, status='succeeded', effect='confirmed', data=data)
            record.update(result=result.model_dump(), phase='finished')
            self.store.record('source_preparation', operation.id, record)
            return result
        return self.store._transaction(capture)

    def _save_stage_result(self, record, actual):
        candidate = record['candidate']
        result = OperationResult(operation_id=record['id'], status=actual.status, effect=actual.effect,
            stderr=actual.stderr, data={'mode': 'stage', 'source_id': candidate['source_id'], 'source_sha256': candidate['source_sha256'],
                'path': candidate['path'], 'bytes': candidate['bytes'], 'executor_operation_id': record['executor_operation_id'],
                'executor_result_sha256': _result_hash(actual.model_dump()), 'original_private_bytes_retained': True,
                'source_semantics_adopted': False, 'confirmation_pending': True})
        record.update(result=result.model_dump(), phase='unresolved' if actual.effect == 'unknown' or actual.status == 'pending' else 'finished')
        self.store.record('source_preparation', record['id'], record)
        return result

    async def reconcile(self, task, operation):
        record = self.store.record_get('source_preparation', operation.id)
        if not record or record['task_id'] != task['id'] or record['operation_sha256'] != digest(operation.model_dump()):
            return OperationResult(operation_id=operation.id, status='unknown', effect='unknown', data={'reason': 'SOURCE_PREPARATION_RECORD_REQUIRED', 'replayed': False})
        if record.get('result') and record['phase'] == 'finished':
            return OperationResult.model_validate(record['result'])
        if record['candidate']['mode'] == 'stage' and record.get('executor_operation_id'):
            actual = await self.executor.reconcile(record['executor_operation_id'])
            return self._save_stage_result(record, actual)
        # Capture and its terminal record share one DB transaction; no filesystem
        # extraction is performed here. An absent terminal record proves that
        # transaction did not commit, not that the prior extractor should rerun.
        if record['candidate']['mode'] != 'stage' or not record.get('executor_operation_id'):
            result = OperationResult(operation_id=operation.id, status='failed', effect='none',
                data={'reason': 'SOURCE_PREPARATION_CAPTURE_NOT_COMMITTED', 'replayed': False,
                      'original_prepared_record_sha256': digest(record)})
            record.update(result=result.model_dump(), phase='finished')
            self.store.record('source_preparation', operation.id, record)
            return result
        return OperationResult(operation_id=operation.id, status='unknown', effect='unknown', data={'reason': 'SOURCE_PREPARATION_CAPTURE_INCOMPLETE', 'replayed': False})

    def confirm(self, task, operation_id):
        """Engine calls after learning and resolution of all after-learning opinions."""
        row, record = self._protected(task['id'], operation_id)
        existing = self.store.record_get('source_preparation_confirmation', operation_id)
        result_sha = _result_hash(row['result'])
        if existing:
            if existing['result_sha256'] != result_sha:
                raise PolicyError('Source preparation confirmation result changed')
            return existing
        candidate = record['candidate']
        # Confirming an already observed copy/derivative is factual bookkeeping;
        # a later instruction does not erase its unchanged original-byte lineage.
        # Withdrawal changes source disposition and retains the current-set CAS.
        owner, source, _, _ = self._source(task, row['operation']['args'], current=False)
        if candidate['mode'] in {'withdraw','retain'} and row['operation']['args']['expected_source_hash']!=owner['source_hash']:
            prior=self.store.record_get('source_preparation_confirmation_pending',operation_id)
            if prior and prior['status'] in {'superseded_by_reviewed_confirmation','superseded_by_reviewed_retention'}:return prior
            pending={'id':operation_id,'task_id':task['id'],'source_id':source['id'],'source_sha256':source['sha256'],
                     'result_sha256':result_sha,'status':'source_revalidation_required',
                     'reviewed_source_hash':row['operation']['args']['expected_source_hash'],
                     'current_source_hash':owner['source_hash'],
                     'next':'Propose a current source_prepare withdrawal or retention under the latest actual instruction, with exact user quote and full pre/post/learning review. The captured old operation does not decide the current source disposition.'}
            if prior!=pending:
                if prior:self.store.record('source_preparation_confirmation_history',digest(prior),prior)
                self.store.record('source_preparation_confirmation_pending',operation_id,pending)
                self.store.event(task['id'],'source_preparation','revalidation_required',pending)
            return pending
        confirmed = {'id': operation_id, 'task_id': task['id'], 'source_id': source['id'], 'source_sha256': source['sha256'],
                     'result_sha256': result_sha, 'mode': candidate['mode'], 'confirmed_at': now()}
        def commit():
            if candidate['mode'] in {'withdraw','retain'} and self.store.get_task(task['id'])['source_hash']!=owner['source_hash']:
                raise PolicyError('SOURCE_HASH_CONFLICT: source disposition changed before confirmation')
            if candidate['mode'] == 'accept_extraction':
                data = row['result']['data']
                raw, sha = self.store.source_bytes(task['id'], data['raw_source_ref'], data['extracted_sha256'])
                if sha != candidate['extraction']['extracted_sha256'] or len(raw) != candidate['extraction']['extracted_bytes']:
                    raise PolicyError('Captured extraction differs before confirmation')
                prior = self.store.record_get('source_extraction', source['id'])
                if prior:
                    self.store.record('source_extraction_history', digest(prior), prior)
                self.store.record('source_extraction', source['id'], {**confirmed, 'id': source['id'], 'operation_id': operation_id,
                    'raw_source_ref': data['raw_source_ref'], 'extracted_sha256': sha, 'extracted_bytes': len(raw),
                    'lineage': candidate['extraction']})
            elif candidate['mode'] == 'withdraw':
                self.store.withdraw_source(task['id'], owner['source_hash'], source['id'], candidate['instruction_id'],
                                           candidate['source_quote'], operation_id)
            if candidate['mode'] in {'withdraw','retain'}:
                for prior in candidate.get('prior_confirmation_attempts',[]):
                    current=self.store.record_get('source_preparation_confirmation_pending',prior['id'])
                    if current!=prior:raise PolicyError('Prior withdrawal confirmation changed before reviewed adoption')
                    self.store.record('source_preparation_confirmation_history',digest(prior),prior)
                    self.store.record('source_preparation_confirmation_pending',prior['id'],
                        {**prior,'status':'superseded_by_reviewed_retention' if candidate['mode']=='retain' else 'superseded_by_reviewed_confirmation','confirmation_operation_id':operation_id,
                         'confirmed_result_sha256':result_sha})
            self.store.record('source_preparation_confirmation', operation_id, confirmed)
            return confirmed
        return self.store._transaction(commit)
