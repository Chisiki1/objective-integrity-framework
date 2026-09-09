#!/usr/bin/env python3
"""Run one owner-scoped ordinary operation and retain its actual process result.

No shell rewriting, automatic retry, background continuation or authority grant.
The output directory is one operation's evidence, not a learning/semantic ledger.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
import time

# A normal sibling import writes an unmanifested .pyc before even --help.
# Bootstrap the source-only loader without importing it through Python's cache.
_load_source = runpy.run_path(str(Path(__file__).with_name('source_module.py')))['load_source']
learning = _load_source(Path(__file__).with_name('in_work_learning.py'))


def canonical(value):
    return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest().upper()


def plain(value, *, kind='file', absent=False):
    p=Path(value)
    if not p.is_absolute() or '..' in p.parts or (os.name=='nt' and ':' in str(p)[len(p.drive):]):
        raise ValueError('explicit canonical absolute path required')
    for part in [*reversed(p.parents),p]:
        try:s=part.lstat()
        except FileNotFoundError:
            if part==p and absent:return p
            raise
        if stat.S_ISLNK(s.st_mode) or getattr(s,'st_file_attributes',0)&0x400:
            raise ValueError('linked/reparse path is not supported')
    s=p.stat()
    if kind=='file' and (not stat.S_ISREG(s.st_mode) or s.st_nlink!=1):
        raise ValueError('plain single-link file required')
    if kind=='dir' and not stat.S_ISDIR(s.st_mode):raise ValueError('plain directory required')
    return p


def ref(path):
    p=plain(path);return {'path':str(p),'sha256':sha(p.read_bytes())}


def load(path):
    def unique(rows):
        out={}
        for k,v in rows:
            if k in out:raise ValueError('duplicate JSON key')
            out[k]=v
        return out
    return json.loads(plain(path).read_bytes(),object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON')))


def put(path,value):
    # Exclusive result files preserve original failures and prevent silent retry.
    p=plain(path,absent=True)
    with p.open('xb') as f:
        f.write(canonical(value)+b'\n');f.flush();os.fsync(f.fileno())


def text(value):
    if not isinstance(value,str) or not value.strip():raise ValueError('nonempty text required')
    return value


def ref_shape(value):
    if (not isinstance(value,dict) or set(value)!={'path','sha256'}
        or not isinstance(value['path'],str) or not Path(value['path']).is_absolute()
        or not isinstance(value['sha256'],str) or not re.fullmatch('[A-F0-9]{64}',value['sha256'])):
        raise ValueError('absolute path and uppercase SHA256 reference required')


def validate(spec, *, current=True):
    required={'schema','operation_id','owner_chat_id','source','purpose','argv','cwd',
              'timeout_seconds','accepted_exit_codes','methods','next_consumer','effect_scope'}
    if not isinstance(spec,dict) or set(spec)!=required or spec['schema']!='work-operation-v1':
        raise ValueError('operation specification fields differ')
    for k in ['operation_id','owner_chat_id','purpose','next_consumer']:text(spec[k])
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,95}',spec['operation_id']):raise ValueError('invalid operation id')
    ref_shape(spec['source'])
    if current:learning.read_ref(spec['source']);plain(spec['cwd'],kind='dir')
    argv=spec['argv']
    if not isinstance(argv,list) or not argv or any(not isinstance(a,str) or '\0' in a for a in argv):
        raise ValueError('exact argv list required; no shell string')
    if current:plain(argv[0])
    # This small runner does not silently bypass the existing exact-shell route.
    # Such operations retain their normal tool/PowerShell preflight instead.
    if Path(argv[0]).stem.casefold() in {'powershell','pwsh','cmd','bash','sh','zsh','fish'}:
        raise ValueError('shell entrypoints use the existing exact-shell workflow')
    timeout=spec['timeout_seconds']
    if type(timeout) not in {int,float} or not math.isfinite(timeout) or not 0<timeout<=3600:
        raise ValueError('finite explicit timeout within one hour required')
    codes=spec['accepted_exit_codes']
    if not isinstance(codes,list) or not codes or any(type(v)!=int for v in codes) or len(codes)!=len(set(codes)):
        raise ValueError('explicit unique accepted exit codes required')
    if not isinstance(spec['methods'],list):raise ValueError('method references must be a list')
    ids=set()
    for method in spec['methods']:
        if not isinstance(method,dict) or set(method)!={'id','path','sha256'}:raise ValueError('method fields differ')
        text(method['id'])
        if method['id'] in ids:raise ValueError('duplicate method id')
        ids.add(method['id'])
        ref_shape({k:method[k] for k in ('path','sha256')})
        if current:learning.read_ref({k:method[k] for k in ('path','sha256')})
    if spec['effect_scope'] not in {'read-only','owned-local'}:
        raise ValueError('external/live/unknown operations retain their existing action-specific route')
    return spec


def execute(spec_path,output_root):
    spec=validate(load(spec_path));spec_ref=ref(spec_path)
    root=plain(output_root,kind='dir',absent=True)
    if root.exists():raise ValueError('operation evidence already exists; inspect it, never replay')
    source_paths=[Path(spec_ref['path']),Path(spec['source']['path']),*[Path(x['path']) for x in spec['methods']]]
    if any(root==p or root in p.parents for p in source_paths):raise ValueError('output overlaps a bound input')
    # Preserve the exact command and source before launching one process.
    root.mkdir()
    put(root/'request.json',spec)
    start=datetime.datetime.now(datetime.timezone.utc).isoformat();clock=time.monotonic()
    fault=None;code=None;status='unknown';process_started=False
    stdout=root/'stdout.bin';stderr=root/'stderr.bin'
    try:
        # Recheck bindings after creation, immediately before the only execution.
        if ref(spec_path)!=spec_ref:raise ValueError('operation spec changed before execution')
        validate(spec)
        with stdout.open('xb') as out,stderr.open('xb') as err:
            try:
                process=subprocess.Popen(spec['argv'],cwd=spec['cwd'],stdin=subprocess.DEVNULL,
                    stdout=out,stderr=err,shell=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                process_started=True
                try:
                    code=process.wait(timeout=spec['timeout_seconds'])
                    status='succeeded' if code in spec['accepted_exit_codes'] else 'failed'
                    if status=='failed':fault={'kind':'nonzero-exit','exit_code':code,'cause':'unclassified'}
                except subprocess.TimeoutExpired:
                    fault={'kind':'timeout','cause':'unclassified','owned_process_id':process.pid}
                    try:process.kill();code=process.wait(timeout=5)
                    except (OSError,subprocess.TimeoutExpired) as cleanup:
                        fault['cleanup_fault']=str(cleanup)
                    status='unknown'  # Descendants and partial effects are not inferred clean.
            except OSError as error:
                fault={'kind':'process-start-fault','type':type(error).__name__,'message':str(error),'cause':'unclassified'}
                status='failed'
    except Exception as error:
        fault={'kind':'capture-or-preexecution-fault','type':type(error).__name__,'message':str(error),'cause':'unclassified'}
        status='unknown' if process_started else 'failed'
    finally:
        streams={}
        for name,path in [('stdout',stdout),('stderr',stderr)]:
            if path.exists():streams[name]={**ref(path),'bytes':path.stat().st_size}
        changed=[]
        for item in [spec['source'],*[{k:m[k] for k in ('path','sha256')} for m in spec['methods']]]:
            try:
                if ref(item['path'])!=item:changed.append(item)
            except (OSError,ValueError):changed.append(item)
        result={'schema':'work-operation-result-v1','operation_id':spec['operation_id'],
            'owner_chat_id':spec['owner_chat_id'],'source':spec['source'],'spec_ref':spec_ref,
            'request_ref':ref(root/'request.json'),'purpose':spec['purpose'],'next_consumer':spec['next_consumer'],
            'methods':spec['methods'],'status':status,'effect_scope':spec['effect_scope'],
            'effect_state':'unknown' if status=='unknown' else 'none' if not process_started or spec['effect_scope']=='read-only' else 'partial',
            'process_started':process_started,'exit_code':code,'accepted_exit_codes':spec['accepted_exit_codes'],
            'first_fault':fault,'streams':streams,'changed_bindings':changed,
            'started_at':start,'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'elapsed_ms':round((time.monotonic()-clock)*1000,3),
            'method_use':'actual process invocation retained; declared methods are not personal reads or semantic use',
            'permission_granted':False,'proof_ceiling':'observed local process/result only; caller scope is asserted, not a sandbox or success of the source objective'}
        put(root/'result.json',result)
    return result


def _consume(result_path):
    raw_ref=ref(result_path);result=load(result_path)
    if not isinstance(result,dict) or result.get('schema')!='work-operation-result-v1':raise ValueError('ordinary operation result object required')
    ref_shape(result['request_ref']);ref_shape(result['spec_ref'])
    request=validate(json.loads(learning.read_ref(result['request_ref'])),current=False)
    for key in ['operation_id','owner_chat_id','source','methods','next_consumer','effect_scope','purpose','accepted_exit_codes']:
        if result[key]!=request[key]:raise ValueError('result/request identity differs')
    if result['status'] not in {'succeeded','failed','unknown'} or result['effect_state'] not in {'none','partial','unknown'}:
        raise ValueError('unsupported operation result')
    if type(result['process_started'])!=bool:raise ValueError('process state missing')
    if result['exit_code'] is not None and type(result['exit_code'])!=int:raise ValueError('exit status must be integer or null')
    if result['status']=='succeeded' and (not result['process_started'] or result['exit_code'] not in request['accepted_exit_codes']):
        raise ValueError('success does not match the actual declared exit criterion')
    if result['status']=='unknown' and result['effect_state']!='unknown':raise ValueError('unknown effects were discarded')
    if not isinstance(result['streams'],dict):raise ValueError('stream object required')
    if result['process_started'] and set(result['streams'])!={'stdout','stderr'}:raise ValueError('started operation streams missing')
    for stream in result['streams'].values():
        if not isinstance(stream,dict) or set(stream)!={'path','sha256','bytes'}:raise ValueError('stream reference object required')
        ref_shape({k:stream[k] for k in ('path','sha256')})
        if type(stream['bytes'])!=int or stream['bytes']<0:raise ValueError('nonnegative stream byte count required')
        body=learning.read_ref({k:stream[k] for k in ('path','sha256')})
        if len(body)!=stream['bytes']:raise ValueError('stream size differs')
    if result['first_fault'] is not None and not isinstance(result['first_fault'],dict):raise ValueError('first fault object or null required')
    if not isinstance(result['changed_bindings'],list):raise ValueError('changed binding array required')
    for binding in result['changed_bindings']:ref_shape(binding)
    if type(result['elapsed_ms']) not in {int,float} or not math.isfinite(result['elapsed_ms']) or result['elapsed_ms']<0:raise ValueError('nonnegative finite elapsed time required')
    # Historical bytes remain, even if the original spec or methods later change.
    freshness=[]
    for old in [request['source'],*[{k:m[k] for k in ('path','sha256')} for m in request['methods']]]:
        try:learning.read_ref(old)
        except (OSError,ValueError) as error:freshness.append({'ref':old,'issue':str(error)})
    handoff={'request_id':result['operation_id'],'raw_result':raw_ref,
        'reported_result':{'status':result['status'],'effect_state':result['effect_state'],
            'first_fault':result['first_fault'],'method_applications':[]},
        'validation_issues':[],
        'consumer':{'id':result['next_consumer'],'description':result['purpose']}}
    context=learning.result_context(handoff,result['methods'])
    context['operation_result']=raw_ref
    context['execution_observation']={'process_started':result['process_started'],'exit_code':result['exit_code'],
        'streams':result['streams'],'elapsed_ms':result['elapsed_ms'],'effect_state':result['effect_state'],
        'changed_bindings':result['changed_bindings'],'current_freshness_issues':freshness}
    # The same raw context is the input for the existing semantic owner decision.
    context.pop('context_sha256',None);context['context_sha256']=sha(canonical(context))
    return {'schema':'work-operation-handoff-v1','operation':result,'raw_result_ref':raw_ref,
        'in_work_learning':context,'freshness_issues':freshness,
        'effect_requires_reconciliation':result['effect_state'] in {'partial','unknown'},
        'permission_granted':False,'source_outcome_completed':False}


def consume(result_path):
    try:return _consume(result_path)
    except (OSError,ValueError,KeyError,TypeError) as error:
        try:raw=ref(result_path)
        except (OSError,ValueError):raw={'path':str(result_path),'sha256':None}
        return {'schema':'work-operation-handoff-v1','operation':None,'raw_result_ref':raw,
            'validation_issues':[str(error)],'freshness_issues':[],
            'effect_requires_reconciliation':True,'permission_granted':False,
            'source_outcome_completed':False,'proof_ceiling':'raw result retained where available; invalid/missing status and effects remain unknown'}


def candidate_input(result_path,package_root,candidate_id,operation,challenge_path=None,prior_candidate_id=None):
    """Derive repeated package/source metadata; do not author/approve the Skill."""
    skill_package = _load_source(Path(__file__).with_name('skill_package.py'))
    handoff=consume(result_path)
    if handoff['operation'] is None:raise ValueError('invalid original result needs owner reconciliation')
    result=handoff['operation'];root=plain(package_root,kind='dir')
    if operation not in {'CREATE','REVISE','MERGE','SUPERSEDE','RETIRE'}:raise ValueError('invalid candidate operation')
    if operation!='CREATE' and not prior_candidate_id:raise ValueError('non-create operation requires prior candidate lineage')
    if operation=='CREATE' and prior_candidate_id:raise ValueError('creation does not supersede a prior candidate')
    if not re.fullmatch('[a-z0-9][a-z0-9-]{0,62}',candidate_id):raise ValueError('invalid candidate id')
    members=[]
    for path in sorted(root.rglob('*')):
        plain(path,kind='dir' if path.is_dir() else 'file')
        if path.is_file():members.append({'path':path.relative_to(root).as_posix(),'sha256':ref(path)['sha256']})
    skill=ref(root/'SKILL.md')
    return {'schema_version':'mgskill-inactive-candidate-v2','operation':operation,
        'event_id':result['operation_id'],'objective_id':result['next_consumer'],
        'source_claims':[result['source']['path']+'#'+result['source']['sha256']],
        'owner':{'lane':'COORDINATED-WORK','task_id':result['owner_chat_id'],'chat_id':result['owner_chat_id'],
            'lease_id':result['operation_id'],'source_sha256':result['source']['sha256']},
        'candidate':{'candidate_id':candidate_id,'artifact_path':skill['path'],'artifact_sha256':skill['sha256'],
            'prior_candidate_id':prior_candidate_id,'equivalent_fingerprints':[],
            'package':{'root':str(root),'members':members,'manifest_sha256':skill_package.package_manifest(members)}},
        'evidence':{'source':handoff['raw_result_ref'],'next_consumer':result['next_consumer']},
        'rollback':{'owner':result['owner_chat_id'],'method':'Inactive package only; preserve source and all prior active versions. Activation needs its own exact recovery.'},
        'independent_challenge':{'status':'completed','result':ref(challenge_path)} if challenge_path else {'status':'pending'},
        'proof_ceiling':'derived complete inactive package input; challenge reference is an owner assertion, not independent approval or activation'}


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('--input',required=True);run.add_argument('--output-root',required=True)
    read=sub.add_parser('consume');read.add_argument('--result',required=True)
    package=sub.add_parser('candidate-input');package.add_argument('--result',required=True);package.add_argument('--package-root',required=True);package.add_argument('--candidate-id',required=True);package.add_argument('--operation',required=True);package.add_argument('--challenge');package.add_argument('--prior-candidate-id');package.add_argument('--output',required=True)
    args=p.parse_args()
    try:
        if args.command=='run':
            result=execute(args.input,args.output_root)
            value={'schema':'work-operation-summary-v1','result_ref':ref(Path(args.output_root)/'result.json'),
                'status':result['status'],'exit_code':result['exit_code'],'first_fault':result['first_fault'],
                'next_consumer':result['next_consumer'],'permission_granted':False}
        elif args.command=='consume':value=consume(args.result)
        else:
            value=candidate_input(args.result,args.package_root,args.candidate_id,args.operation,args.challenge,args.prior_candidate_id)
            put(args.output,value)
        print(json.dumps(value,ensure_ascii=True,sort_keys=True));return 0
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(json.dumps({'status':'ERROR','error':str(error),'permission_granted':False},ensure_ascii=True));return 2


if __name__=='__main__':raise SystemExit(main())
