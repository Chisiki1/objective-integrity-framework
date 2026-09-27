"""Source-bound task reconciliation; classifications remain reviewed judgments."""
from collections import Counter
from .models import PolicyError
from .store import digest


def contract(task):
    return {'source_hash':task['source_hash'], 'source_version':task['source_version'],
        'history':task['source_history'],
        'pending_ids':[x['id'] for x in task['source_history'] if x['status']=='pending'],
        'prior_acceptance':[{'index':i,'criterion':c,'hash':digest(c)} for i,c in enumerate(task['acceptance'])],
        'instruction':'Preserve all prior open outcomes and unknown effects. Classify each pending source exactly once in source_dispositions as {source_id,classification:add|clarify|correct|replace|withdraw,reason}. Bind source_hash. For each prior acceptance supply acceptance_dispositions {index,old_hash,disposition:retain|replace|withdraw,criterion,source_ids,source_quote,reason}; index=null/disposition=add for new criteria. Retain uses the unchanged criterion. A removed/replaced criterion requires an exact quote from an explicitly correct/replace/withdraw source. New criteria require an exact source quote. Fully read any attachment with source_read before classifying it. Textual classification is reviewed, not proof of meaning or permission to amend policy.'}


def validate_reconciliation(task,plan,source_texts=None):
    pending={s['id']:s for s in task['source_history'] if s['status']=='pending'}
    if not pending:
        if not set(task['acceptance'])<=set(plan['acceptance']):raise PolicyError('Plan dropped original acceptance')
        return
    if plan.get('source_hash')!=task['source_hash']:raise PolicyError('SOURCE_HASH_CONFLICT: plan must bind current source_hash')
    dispositions=plan.get('source_dispositions',[])
    if len(dispositions)!=len(pending) or {x.get('source_id') for x in dispositions}!=set(pending):
        raise PolicyError('Classify every exact pending source once')
    classes={}
    for item in dispositions:
        if item.get('classification') not in {'add','clarify','correct','replace','withdraw'} or not item.get('reason','').strip():
            raise PolicyError('Source classification and reason are required')
        classes[item['source_id']]=item['classification']
    seen=set();accepted=[]
    for item in plan.get('acceptance_dispositions',[]):
        index=item.get('index');choice=item.get('disposition');criterion=item.get('criterion')
        if not item.get('reason','').strip():raise PolicyError('Each acceptance disposition needs a reason')
        if index is None:
            if choice!='add':raise PolicyError('New acceptance must use add')
        else:
            if type(index) is not int or index in seen or not 0<=index<len(task['acceptance']):raise PolicyError('Prior acceptance identity missing or duplicated')
            seen.add(index)
            old=task['acceptance'][index]
            if item.get('old_hash')!=digest(old):raise PolicyError('Prior acceptance source hash changed')
            if choice not in {'retain','replace','withdraw'}:raise PolicyError('Unknown acceptance disposition')
            if choice=='retain' and criterion!=old:raise PolicyError('Retained acceptance was changed')
        if choice!='retain':
            refs=item.get('source_ids',[]);quote=item.get('source_quote','')
            texts=source_texts or {r:s['text'] for r,s in pending.items()}
            if not refs or not set(refs)<=set(pending) or not quote or not any(quote in texts[r] for r in refs):
                raise PolicyError('Changed acceptance needs an exact quote from the current user source')
            if choice in {'replace','withdraw'} and not any(classes[r] in {'correct','replace','withdraw'} for r in refs):
                raise PolicyError('A mere addition cannot silently remove prior acceptance')
        if choice!='withdraw':
            if not isinstance(criterion,str) or not criterion.strip():raise PolicyError('Nonempty acceptance required')
            accepted.append(criterion)
    if seen!=set(range(len(task['acceptance']))) or Counter(accepted)!=Counter(plan['acceptance']):
        raise PolicyError('Every prior and proposed acceptance must have exactly one source-bound disposition')
