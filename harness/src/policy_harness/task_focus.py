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
    pending = {s['id'] for s in task['source_history'] if s['status'] == 'pending'}
    carried, active, requirements = {}, [], {}
    for item in plan['acceptance_dispositions']:
        if item['disposition'] == 'withdraw':
            continue
        index = len(carried) + len(active)
        key = digest(item['criterion'])
        touched = bool(pending.intersection(item.get('source_ids', [])))
        if item['disposition'] == 'retain' and key in previous and not touched:
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


def completion_evidence(task, answer, rows):
    focus = current_focus(task)
    supplied = {e.criterion: e for e in answer.acceptance}
    required = set(focus['active'])
    if any(str(i) not in focus['requirements'] for i in required):
        raise PolicyError('Classify every active criterion in requirements before completing: answer, read, artifact, run_code or browser. Missing classification is not a text-only request. Preserve successful work and supply its operation_indices; do not repeat tools merely to repair this response.')
    if len(supplied) != len(answer.acceptance) or set(supplied) not in (required, set(range(len(task['acceptance'])))):
        raise PolicyError('Completion needs evidence for every active criterion exactly once; completed background is retained by the controller')
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
            raise PolicyError('Requested file creation needs a declared deliverable produced by its cited current action; an unrelated old file is not evidence')
    merged = {int(i): e for i,e in focus['carried'].items()}
    merged.update({i:e.model_dump() for i,e in supplied.items() if i in required})
    return [merged[i] for i in range(len(task['acceptance']))]
