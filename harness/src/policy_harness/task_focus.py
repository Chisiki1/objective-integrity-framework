"""Current work and completed evidence, without rewriting original sources."""
from pathlib import Path
from .models import PolicyError
from .access_paths import artifact_path
from .store import digest


def completed(task):
    saved = dict(task['state'].get('completed_acceptance', {}))
    final = task.get('final') or {}
    for evidence in final.get('acceptance', []):
        index = evidence.get('criterion')
        if type(index) is int and 0 <= index < len(task['acceptance']):
            criterion = task['acceptance'][index]
            saved[digest(criterion)] = dict(evidence, text=criterion,
                completed_source_hash=final.get('source_hash', task['source_hash']))
    return saved


def reconcile_focus(task, plan):
    previous = completed(task)
    carried, active, requirements = {}, [], {}
    for item in plan['acceptance_dispositions']:
        if item['disposition'] == 'withdraw':
            continue
        index = len(carried) + len(active)
        key = digest(item['criterion'])
        if item['disposition'] == 'retain' and key in previous and not item.get('reopen', False):
            carried[str(index)] = dict(previous[key], criterion=index)
        else:
            active.append(index)
            requirements[str(index)] = item.get('verification', 'answer')
    return dict(source_hash=task['source_hash'], active=active, carried=carried,
                requirements=requirements)


def current_focus(task):
    value = task['state'].get('work_focus')
    if value and value.get('source_hash') == task['source_hash']:
        return value
    return dict(source_hash=task['source_hash'], active=list(range(len(task['acceptance']))),
                carried={}, requirements={})


def requirement_update(task, proposed):
    focus = current_focus(task)
    requirements = dict(focus['requirements'])
    seen = set()
    corrections=list(focus.get('interpretation_corrections',[]))
    for item in proposed:
        index = item.criterion
        if index in seen or index not in focus['active']:
            raise PolicyError('Evidence requirements must bind each active criterion at most once')
        seen.add(index)
        old = requirements.get(str(index))
        # A model's interpretation is not a new user condition. A correction
        # needs a source-based explanation and independent final review.
        if old and old != 'answer' and item.verification != old:
            if not item.reason.strip():
                raise PolicyError('Do not downgrade requested work on failure. To correct a mistaken verification classification, provide requirements with a reason grounded in the original request and observed evidence; it will be independently reviewed.')
            corrections.append(dict(criterion=index,before=old,after=item.verification,reason=item.reason))
        requirements[str(index)] = item.verification
    return dict(focus, requirements=requirements,interpretation_corrections=corrections)


def project_completion(task, answer, rows):
    """Recover presentation references from exact current recorded file proof.

    A model may cite the final read but omit its preceding write or output list.
    Link only the same path AND bytes; never infer success, intent or a new action.
    """
    result=answer.model_copy(deep=True)
    focus=current_focus(task);paths=set();workspace=Path(task['workspace'])
    def current(row):
        proof=row.get('result') or {}
        return row.get('source_hash')==task['source_hash'] and proof.get('status')=='succeeded' and proof.get('effect')!='unknown'
    def artifacts(row):
        return {(artifact_path(workspace,a['path'],full_access=True),a.get('sha256'))
                for a in (row.get('result') or {}).get('artifacts',[]) if a.get('path') and a.get('sha256')}
    selected={artifact_path(workspace,p,full_access=True) for p in result.artifacts}
    cited_reads={ordinal for evidence in result.acceptance for ordinal in evidence.operation_indices
                 if 0<=ordinal<len(rows) and current(rows[ordinal])
                 and rows[ordinal]['operation']['kind']=='file_read'}
    cited_native={ordinal for evidence in result.acceptance for ordinal in evidence.operation_indices
                  if 0<=ordinal<len(rows) and current(rows[ordinal])
                  and rows[ordinal]['operation']['kind']=='exec'
                  and rows[ordinal]['result'].get('data',{}).get('execution')=='native'}
    for evidence in result.acceptance:
        if evidence.criterion not in focus['active'] or focus['requirements'].get(str(evidence.criterion))!='artifact':continue
        cited=list(evidence.operation_indices)
        for read in cited:
            if read not in cited_reads or not selected.intersection(p for p,_ in artifacts(rows[read])):continue
            preceding=[ordinal for ordinal in cited_native if ordinal<read]
            if len(preceding)==1 and preceding[0] not in evidence.operation_indices:
                evidence.operation_indices.append(preceding[0])
        # Models often split "create output" and "read output" into different
        # criteria. Reuse their explicitly cited read of the selected output;
        # the native action and semantic code review still establish creation.
        for ordinal in cited:
            if not 0<=ordinal<len(rows) or not current(rows[ordinal]):continue
            row=rows[ordinal]
            if row['operation']['kind']!='exec' or row['result'].get('data',{}).get('execution')!='native':continue
            for read in sorted(cited_reads):
                if read>ordinal and selected.intersection(p for p,_ in artifacts(rows[read])) and read not in evidence.operation_indices:
                    evidence.operation_indices.append(read)
        for ordinal in cited:
            if not 0<=ordinal<len(rows) or not current(rows[ordinal]) or rows[ordinal]['operation']['kind']!='file_read':continue
            read=artifacts(rows[ordinal])
            for prior in range(ordinal-1,-1,-1):
                row=rows[prior]
                if current(row) and row['operation']['kind']=='file_write' and read.intersection(artifacts(row)):
                    if prior not in evidence.operation_indices:evidence.operation_indices.append(prior)
                    break
        native=False
        for ordinal in sorted(evidence.operation_indices):
            if not 0<=ordinal<len(rows) or not current(rows[ordinal]):continue
            row=rows[ordinal];kind=row['operation']['kind']
            if kind=='exec' and row['result'].get('data',{}).get('execution')=='native':native=True
            if kind=='file_write' or (kind=='file_read' and native):paths.update(p for p,_ in artifacts(row))
    # More than one candidate needs an explicit model selection. Never replace
    # an existing selection, or expose native control stream files as outputs.
    if not result.artifacts and len(paths)==1:result.artifacts=sorted(paths)
    return result


def completion_evidence(task, answer, rows):
    focus = current_focus(task)
    supplied = {e.criterion: e for e in answer.acceptance}
    required = set(focus['active'])
    if any(str(i) not in focus['requirements'] for i in required):
        raise PolicyError('Classify every active criterion in requirements before completing: answer, read, artifact, run_code or browser. Missing classification is not a text-only request. Preserve successful work and supply its operation_indices; do not repeat tools merely to repair this response.')
    if len(supplied) != len(answer.acceptance) or set(supplied) not in (required, set(range(len(task['acceptance'])))):
        missing = sorted(required - set(supplied))
        raise PolicyError(f'Completion needs evidence for active criteria {sorted(required)} exactly once; missing {missing}. Completed background is retained by the controller. Use saved successful operation_indices and repair the response; do not rerun successful tools merely to fill this evidence list.')
    kinds = {'read': {'file_read','file_list','web_fetch','browser_inspect','history_read','attachment_read','knowledge_read','harness_read','harness_info'},
             'artifact': {'file_write','exec'}, 'run_code': {'exec'}, 'browser': {'browser_inspect'}}
    for index in required:
        evidence = supplied[index]
        verified = []
        delivered = set()
        native_outputs = set()
        mode = focus['requirements'][str(index)]
        for ordinal in sorted(evidence.operation_indices):
            if type(ordinal) is not int or not 0 <= ordinal < len(rows):
                raise PolicyError('Completion refers to an operation that does not exist')
            row = rows[ordinal];result = row.get('result') or {}
            if result.get('status') != 'succeeded' or result.get('effect') == 'unknown':
                raise PolicyError('Failed or unknown operations cannot establish completed work')
            if mode in {'artifact','run_code','browser'} and row.get('source_hash') != task['source_hash']:
                raise PolicyError('New requested actions need results bound to the current instruction; completed historical work is retained separately')
            verified.append(row['operation']['kind'])
            if row['operation']['kind'] == 'exec' and result.get('data',{}).get('execution') == 'native':
                native_outputs.add(ordinal)
            if row['operation']['kind'] in {'file_write','exec'}:
                delivered.update(artifact_path(Path(task['workspace']),a['path'],full_access=True)
                                 for a in result.get('artifacts',[]))
            elif row['operation']['kind'] == 'file_read' and native_outputs:
                # Native programs register their output through subsequent
                # verified reads. Both actions must be cited and current; the
                # existing final code review checks their semantic relationship.
                delivered.update(artifact_path(Path(task['workspace']),a['path'],full_access=True)
                                 for a in result.get('artifacts',[]))
        if mode in kinds and not kinds[mode].intersection(verified):
            raise PolicyError(f'Criterion {index} requires observed {mode} evidence. Supply successful operation_indices; explaining that work was not performed cannot complete it.')
        if mode == 'artifact' and not delivered.intersection(
                artifact_path(Path(task['workspace']),p,full_access=True) for p in answer.artifacts):
            raise PolicyError('Requested file creation needs a declared deliverable produced by its cited current action. Include the output path in artifacts and cite its write/execution plus readback indices. Preserve successful files; correct this response without repeating the tools.')
    merged = {int(i): e for i,e in focus['carried'].items()}
    merged.update({i:e.model_dump() for i,e in supplied.items() if i in required})
    return [merged[i] for i in range(len(task['acceptance']))]
