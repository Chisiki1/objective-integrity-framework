"""Visible operative choices, separate from IDs and controller bookkeeping.

A list item that is itself a decision record keeps its rationale/evidence with
its choice. Private thought is not observable. New operative fields are included
by default instead of disappearing behind a hand-maintained task dictionary.
"""
from .store import canonical,digest
from .models import TaskPlan


def targets(label,value,*,group_ideas=False):
    if hasattr(value,'model_dump'):value=value.model_dump()
    rows=[]
    for field,item in value.items():
        if field in {'id','rationale','evidence_refs','hash','sha256','schema','source_coverage'}:continue
        if field=='ideas' and group_ideas:
            from .learning_projection import groups
            for group in groups(item):
                index=group['source_indexes'][0]
                meaning={k:v for k,v in group['idea'].items() if k!='id'}
                rows.append({'id':f'{label}/ideas/{index}','statement':canonical(meaning),
                    'source_path':f'/ideas/{index}','source_sha256':digest(item[index]),
                    'source_lineage':[{'id':item[i]['id'],'path':f'/ideas/{i}','sha256':digest(item[i])}
                                      for i in group['source_indexes']]})
            continue
        if isinstance(item,list):
            for index,choice in enumerate(item):
                rows.append({'id':f'{label}/{field}/{index}', 'statement':canonical(choice),
                             'source_path':f'/{field}/{index}','source_sha256':digest(choice)})
        elif isinstance(item,dict):
            for key,choice in item.items():
                rows.append({'id':f'{label}/{field}/{key}', 'statement':canonical(choice),
                             'source_path':f'/{field}/{key}','source_sha256':digest(choice)})
        elif item is not None:
            rows.append({'id':f'{label}/{field}', 'statement':canonical(item),
                         'source_path':f'/{field}','source_sha256':digest(item)})
    return rows


def task_plan_targets(plan: TaskPlan):
    # Only this typed caller owns these two controller bindings. Keep their
    # canonical values for source/deferred validation, and keep every semantic
    # disposition (including its source references) in the normal review route.
    return targets('task-plan', plan.model_dump(exclude={'source_hash', 'deferred_index_hash'}))


def operation_targets(operation,selection=None):
    rows=[{'id':operation['id'],'statement':operation['purpose']},*operation['decisions']]
    # Each exact operation argument choice is visible. Plan fields already have
    # their own decision identities; controller-generated linking IDs are not AI
    # semantic choices requiring another recursion.
    if operation['kind']!='plan_task':
        rows.extend(targets('arguments',{'args':operation['args']}))
    if selection is not None:rows.extend(targets('skill-selection',selection))
    identities=[r['id'] for r in rows]
    if len(set(identities))!=len(identities):raise ValueError('Decision identity collision')
    return rows
