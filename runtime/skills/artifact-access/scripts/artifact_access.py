#!/usr/bin/env python3
"""Read-only exact artifact access. Inventories are disposable, never authority."""
from __future__ import annotations
import argparse
import difflib
import hashlib
import json
import os
import re
from pathlib import Path,PurePosixPath
import stat


def digest(raw):return hashlib.sha256(raw).hexdigest().upper()


def plain(path,kind='file'):
    p=Path(path)
    if not p.is_absolute() or '..' in p.parts or (os.name=='nt' and ':' in str(p)[len(p.drive):]):raise ValueError('canonical absolute path required')
    for ancestor in [*reversed(p.parents),p]:
        s=ancestor.lstat()
        if stat.S_ISLNK(s.st_mode) or getattr(s,'st_file_attributes',0)&0x400:raise ValueError('linked/reparse input')
    s=p.stat()
    if kind=='file' and (not stat.S_ISREG(s.st_mode) or s.st_nlink!=1):raise ValueError('plain single-link file required')
    if kind=='dir' and not stat.S_ISDIR(s.st_mode):raise ValueError('plain directory required')
    return p


def file_ref(p,raw=None):
    p=plain(p);raw=p.read_bytes() if raw is None else raw
    return {'path':str(p),'sha256':digest(raw),'bytes':len(raw)}


def relative(value):
    if not isinstance(value,str) or not value or '\\' in value or ':' in value:
        raise ValueError('portable relative member path required')
    p=PurePosixPath(value)
    if p.is_absolute() or str(p)!=value or any(x in {'','..','.'} or x.endswith((' ','.')) for x in value.split('/')):
        raise ValueError('noncanonical member path')
    return value


def parse(raw):
    def pairs(items):
        d={}
        for k,v in items:
            if k in d:raise ValueError('duplicate JSON key')
            d[k]=v
        return d
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON')))


def inventory(root,output):
    root=plain(root,'dir');out=Path(output)
    if not out.is_absolute() or out==root or root in out.parents:raise ValueError('inventory output must be outside its source tree')
    plain(out.parent,'dir')
    if out.exists():raise ValueError('existing inventory is not overwritten')
    rows=[]
    for base,dirs,files in os.walk(root,followlinks=False):
        for name in dirs:plain(Path(base)/name,'dir')
        for name in files:
            path=plain(Path(base)/name);raw=path.read_bytes()
            rows.append({'path':relative(path.relative_to(root).as_posix()),'sha256':digest(raw)})
    value={'schema':'artifact-inventory-v1','root':str(root),'files':sorted(rows,key=lambda r:r['path']),
        'proof_ceiling':'enumerated paths and bytes only; no semantic or later freshness guarantee'}
    with out.open('xb') as f:f.write(json.dumps(value,ensure_ascii=True,sort_keys=True).encode()+b'\n')
    return {'inventory':file_ref(out),'members':len(rows)}


def read_member(manifest_path,path,root=None,start=1,lines=80):
    p=plain(manifest_path);raw=p.read_bytes();manifest=parse(raw)
    if not isinstance(manifest,dict):raise ValueError('manifest object required')
    if root is None:
        if manifest.get('schema')!='artifact-inventory-v1':raise ValueError('existing manifests need an explicit root')
        root=manifest['root']
    root=plain(root,'dir');path=relative(path)
    rows=manifest.get('files',manifest.get('members'))
    if not isinstance(rows,list):raise ValueError('manifest files/members array required')
    selected=[];names=set()
    for row in rows:
        if not isinstance(row,dict):raise ValueError('manifest member object required')
        if not isinstance(row.get('sha256'),str) or not re.fullmatch('[a-fA-F0-9]{64}',row['sha256']):raise ValueError('member SHA256 string required')
        name=relative(row['path']);fold=name.casefold()
        if fold in names:raise ValueError('duplicate or case-alias member')
        names.add(fold)
        if name==path:selected.append(row)
    if len(selected)!=1:raise FileNotFoundError('exact manifest member absent: '+path)
    if type(start)!=int or start<1 or type(lines)!=int or not 1<=lines<=500:
        raise ValueError('positive start and 1..500 lines required')
    source=plain(root/Path(path));body=source.read_bytes()
    if digest(body)!=selected[0]['sha256'].upper():raise ValueError('STALE_MEMBER: original file changed; select a reviewed current cut')
    values=body.decode('utf-8-sig').splitlines(keepends=True)
    if start>len(values)+1:raise ValueError('start is beyond EOF')
    end=min(len(values),start-1+lines);view=''.join(values[start-1:end])
    if len(view.encode('utf8'))>131072:raise ValueError('selected lines exceed 128KiB; choose a smaller range')
    return {'manifest':file_ref(p,raw),'source':file_ref(source,body),'start_line':start,'end_line':end,
        'total_lines':len(values),'text':view,'next_line':end+1 if end<len(values) else None,
        'complete_file_in_this_view':start==1 and end==len(values)}


def difference(left,right,lines=120):
    if type(lines)!=int or not 1<=lines<=500:raise ValueError('1..500 output lines required')
    a=plain(left);b=plain(right);ar=a.read_bytes();br=b.read_bytes()
    full=list(difflib.unified_diff(ar.decode('utf-8-sig').splitlines(True),br.decode('utf-8-sig').splitlines(True),fromfile=str(a),tofile=str(b),n=3))
    view=''.join(full[:lines])
    if len(view.encode('utf8'))>131072:raise ValueError('selected diff exceeds 128KiB')
    return {'left':file_ref(a,ar),'right':file_ref(b,br),'text':view,'diff_lines':len(full),'truncated':len(full)>lines,
        'proof_ceiling':'this displayed diff only; follow original sources when truncated'}


def json_view(path,pointer='',keys=False,chars=8192):
    if type(chars)!=int or not 128<=chars<=131072:raise ValueError('128..131072 output characters required')
    p=plain(path);raw=p.read_bytes();value=parse(raw)
    if pointer and not pointer.startswith('/'):raise ValueError('JSON pointer must be empty or start with /')
    for token in pointer.split('/')[1:] if pointer else []:
        if any(token[i]=='~' and (i+1==len(token) or token[i+1] not in '01') for i in range(len(token))):
            raise ValueError('invalid JSON pointer escape')
        token=token.replace('~1','/').replace('~0','~')
        if isinstance(value,list):
            if not token.isdigit() or str(int(token))!=token:raise ValueError('canonical array index required')
            value=value[int(token)]
        elif isinstance(value,dict):value=value[token]
        else:raise ValueError('pointer traverses a scalar')
    if keys:
        if not isinstance(value,dict):raise ValueError('keys requires an object')
        value=list(value)
    rendered=json.dumps(value,ensure_ascii=False,sort_keys=True)
    if len(rendered)>chars:raise ValueError('JSON selection exceeds output bound; choose a narrower pointer or keys')
    return {'source':file_ref(p,raw),'pointer':pointer,'keys_only':keys,'value':value,
        'proof_ceiling':'exact selected JSON projection only; not complete source reading'}


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    index=sub.add_parser('inventory');index.add_argument('--root',required=True);index.add_argument('--output',required=True)
    read=sub.add_parser('read');source=read.add_mutually_exclusive_group(required=True);source.add_argument('--inventory');source.add_argument('--manifest');read.add_argument('--root');read.add_argument('--path',required=True);read.add_argument('--start',type=int,default=1);read.add_argument('--lines',type=int,default=80)
    diff=sub.add_parser('diff');diff.add_argument('--left',required=True);diff.add_argument('--right',required=True);diff.add_argument('--lines',type=int,default=120)
    view=sub.add_parser('json');view.add_argument('--file',required=True);view.add_argument('--pointer',default='');view.add_argument('--keys',action='store_true');view.add_argument('--chars',type=int,default=8192)
    args=p.parse_args()
    try:
        if args.command=='inventory':data=inventory(args.root,args.output)
        elif args.command=='read':data=read_member(args.inventory or args.manifest,args.path,args.root,args.start,args.lines)
        elif args.command=='diff':data=difference(args.left,args.right,args.lines)
        else:data=json_view(args.file,args.pointer,args.keys,args.chars)
        result={'schema':'artifact-access-result-v1','status':'succeeded','operation':args.command,**data};code=0
    except (OSError,ValueError,KeyError,TypeError,IndexError) as error:
        result={'schema':'artifact-access-result-v1','status':'failed','operation':args.command,'first_fault':{'type':type(error).__name__,'message':str(error)},'fallback_used':False};code=2
    print(json.dumps(result,ensure_ascii=True,sort_keys=True));return code


if __name__=='__main__':raise SystemExit(main())
