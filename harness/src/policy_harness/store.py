from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
from collections import defaultdict, OrderedDict
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .models import PolicyError, now

SECRET_PATTERN=re.compile(r'(?:sk-[a-z0-9_-]{16,}|bearer\s+[a-z0-9._-]{16,})',re.I)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def redact(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if any(x in str(k).lower() for x in ('api_key', 'authorization', 'password', 'access_token')) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x) for x in value]
    if isinstance(value, str):
        return SECRET_PATTERN.sub('[REDACTED]',value)
    return value


def redact_source_range(raw,offset,end,pattern=SECRET_PATTERN):
    """Detect secrets in the full source, then mask each intersecting byte range.

    Independent public chunks cannot expose a secret split across their boundary.
    Returned mask length preserves the original byte cursor; original bytes stay
    in the private source store, never in the public/model range value.
    """
    text=raw.decode('utf-8');part=bytearray(raw[offset:end]);changed=False
    previous_char=0;previous_byte=0
    for match in pattern.finditer(text):
        start=previous_byte+len(text[previous_char:match.start()].encode('utf-8'))
        if start>=end:break
        stop=start+len(match.group().encode('utf-8'))
        previous_char=match.end();previous_byte=stop
        left=max(start,offset);right=min(stop,end)
        if left<right:
            part[left-offset:right-offset]=b'*'*(right-left);changed=True
    return bytes(part).decode('utf-8'),changed


class Store:
    """Trusted-controller persistence. This database is never mounted in a workspace.

    SQLite transactions protect action identity and permit consumption. Hashes
    detect accidental history changes; they are not a same-user security boundary.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).absolute()
        if self.data_dir.is_symlink():
            raise PolicyError('Runtime directory cannot be a link')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces = self.data_dir / 'workspaces'
        self.workspaces.mkdir(exist_ok=True)
        self.path = self.data_dir / 'control.sqlite3'
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.listeners = defaultdict(set)
        self.task_list_listeners = set()
        self.task_list_revision = 0
        self._operation_index_cache = OrderedDict()
        self.policy_hash = ''
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL REFERENCES tasks(id),stage TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL,prev_hash TEXT NOT NULL,hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS permits(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, operation_id TEXT NOT NULL UNIQUE, action_hash TEXT NOT NULL, policy_hash TEXT NOT NULL, bundle_hash TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS records(kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS source_blobs(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),sha256 TEXT NOT NULL,body BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS task_submissions(id TEXT PRIMARY KEY,task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),payload_sha256 TEXT NOT NULL,start_claimed INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS operations_task ON operations(task_id);
            CREATE INDEX IF NOT EXISTS records_kind_task ON records(kind,json_extract(body,'$.task_id'));
            CREATE INDEX IF NOT EXISTS skills_scope ON records(json_extract(body,'$.scope'),json_extract(body,'$.updated_at')) WHERE kind='practical_skill';
            CREATE INDEX IF NOT EXISTS skills_owner ON records(json_extract(body,'$.owner_task_id'),json_extract(body,'$.updated_at')) WHERE kind='practical_skill';
            CREATE INDEX IF NOT EXISTS pending_calls ON records(json_extract(body,'$.task_id'),json_extract(body,'$.phase')) WHERE kind='practical_call' AND json_extract(body,'$.status')='responded' AND json_extract(body,'$.consumed')=0;
        ''')
        self._ensure_operation_headers()
        from .task_list import ensure_index as ensure_task_index
        ensure_task_index(self)
        from .skill_search import ensure_index
        ensure_index(self)
        self.verify_events()

    def _ensure_operation_headers(self):
        """Derived small headers; triggers also cover other/older connections.

        Migration reads original bodies once. Subsequent decisions never parse
        compressed historical stdout/Web bodies just to find state and tools.
        Original records and their order remain unchanged.
        """
        with self.lock:
            version = self.record_get('controller_schema', 'operation_headers')
            present = self.db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('operation_headers','operation_header_insert','operation_header_update')").fetchone()[0]
            if version == {'version': 2} and present == 3:return
        def header(body):
            return f"""json_object('operation',json_object(
                'id',json_extract({body},'$.operation.id'),
                'kind',json_extract({body},'$.operation.kind'),
                'purpose',json_extract({body},'$.operation.purpose'),
                'args',json_object('path',json_extract({body},'$.operation.args.path'))),
                'tool_name',coalesce(json_extract({body},'$.tool_name'),json_extract({body},'$.operation.kind')),
                'status',json_extract({body},'$.status'),'started_at',json_extract({body},'$.started_at'),
                'source_hash',json_extract({body},'$.source_hash'),
                'request_fingerprint',json_extract({body},'$.request_fingerprint'),
                'result',json(CASE WHEN json_type({body},'$.result')='object' THEN json_object(
                    'status',json_extract({body},'$.result.status'),'effect',json_extract({body},'$.result.effect'),
                    'data',json_object('bytes',coalesce(json_extract({body},'$.result.data.bytes'),0),
                                      'sha256',json_extract({body},'$.result.data.sha256')),
                    'artifacts',json(coalesce(json_extract({body},'$.result.artifacts'),'[]'))) ELSE 'null' END))"""
        def unresolved(body):
            return f"""(json_extract({body},'$.status')!='superseded' AND
                (coalesce(json_type({body},'$.result'),'null')='null' OR
                 json_extract({body},'$.result.effect')='unknown' OR
                 json_extract({body},'$.result.status') IN ('pending','unknown')))"""
        with self.lock:
            self.db.executescript(f'''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS operation_headers(
                    id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL,
                    unresolved INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS header_order ON operation_headers(task_id,sequence);
                CREATE INDEX IF NOT EXISTS header_unresolved ON operation_headers(task_id,unresolved,sequence);
                DROP TRIGGER IF EXISTS operation_header_insert;
                DROP TRIGGER IF EXISTS operation_header_update;
                CREATE TRIGGER IF NOT EXISTS operation_header_insert AFTER INSERT ON operations BEGIN
                    INSERT INTO operation_headers VALUES(NEW.id,NEW.task_id,NEW.rowid,{header('NEW.body')},coalesce({unresolved('NEW.body')},0));
                END;
                CREATE TRIGGER IF NOT EXISTS operation_header_update AFTER UPDATE OF body ON operations BEGIN
                    UPDATE operation_headers SET body={header('NEW.body')},unresolved=coalesce({unresolved('NEW.body')},0) WHERE id=NEW.id;
                END;
                INSERT OR REPLACE INTO operation_headers SELECT o.id,o.task_id,o.rowid,{header('o.body')},coalesce({unresolved('o.body')},0)
                    FROM operations o;
                INSERT OR REPLACE INTO records VALUES('controller_schema','operation_headers','{{"version":2}}');
                COMMIT;
            ''')

    def _transaction(self, fn):
        with self.lock:
            nested=self.db.in_transaction
            savepoint='nested_'+uuid4().hex
            self.db.execute('SAVEPOINT '+savepoint if nested else 'BEGIN IMMEDIATE')
            try:
                result = fn()
                self.db.execute('RELEASE '+savepoint if nested else 'COMMIT')
                return result
            except BaseException as original:
                # SQLite may already have aborted the transaction (for example
                # SQLITE_FULL). Cleanup must never hide that original failure.
                transaction_observation={'nested':nested,'rollback_confirmed':False,
                                         'inactive_after_failure':not self.db.in_transaction}
                try:
                    if self.db.in_transaction:
                        if nested:
                            self.db.execute('ROLLBACK TO '+savepoint)
                            self.db.execute('RELEASE '+savepoint)
                        else:self.db.execute('ROLLBACK')
                        transaction_observation['rollback_confirmed']=True
                    else:
                        original.add_note('Transaction inactive after failure; no cleanup attempted. This is not a successful-effect or rollback attestation.')
                except BaseException as cleanup:
                    transaction_observation['cleanup_fault']={'type':type(cleanup).__name__,'message':str(cleanup)}
                    original.add_note('Transaction cleanup also failed: '+type(cleanup).__name__+': '+str(cleanup))
                original.harness_transaction_observation=transaction_observation
                raise

    def create_task(self, objective, acceptance, parent_id=None, *, submission_id=None, attachments=None, options=None):
        if not isinstance(objective, str) or not objective.strip():
            raise PolicyError('目的を入力してください')
        if not isinstance(acceptance, list) or not all(isinstance(x, str) and x.strip() for x in acceptance):
            raise PolicyError('完了条件は文字列の一覧で指定してください')
        if parent_id is not None:
            self.get_task(parent_id)
        if submission_id is not None and (not isinstance(submission_id,str) or not re.fullmatch('[a-f0-9]{32}',submission_id)):
            raise PolicyError('SUBMISSION_ID_INVALID')
        raw=canonical({'objective':objective,'acceptance':acceptance}).encode('utf-8')
        attachments = attachments or []
        options = options or {}
        if options.get('access_mode') == 'full' and options.get('confirm_full_access') is not True:
            raise PolicyError('FULL_ACCESS_CONFIRMATION_REQUIRED: フルアクセスの許可範囲を確認してください。')
        submission_payload = {'objective': objective, 'acceptance': acceptance}
        if attachments or options:
            submission_payload.update(attachments=[{'filename': a['filename'], 'sha256': hashlib.sha256(a['bytes']).hexdigest()} for a in attachments], options=options)
        submission_hash = digest(submission_payload)
        def create():
            if submission_id is not None:
                previous=self.db.execute('SELECT * FROM task_submissions WHERE id=?',(submission_id,)).fetchone()
                if previous is not None:
                    task=self.get_task(previous['task_id'])
                    original,original_hash=self.source_bytes(task['id'],task['id']+':initial')
                    if original!=raw or task['parent_id']!=parent_id or submission_hash!=previous['payload_sha256']:
                        raise PolicyError('SUBMISSION_PAYLOAD_CONFLICT: this submission already has different original content')
                    return task
            identity=uuid4().hex
            workspace=self.workspaces/identity
            workspace.mkdir()
            item=dict(id=identity,parent_id=parent_id,objective=redact(objective.strip()),
                acceptance=redact(acceptance or [objective.strip()]),status='created',
                actor='worker' if parent_id else 'parent',policy_hash=self.policy_hash,
                created_at=now(),workspace=str(workspace),state={},final=None,version=1)
            source={'id':identity+':initial','kind':'delegation' if parent_id else 'initial','text':redact(objective),
                'acceptance':redact(acceptance),'created_at':item['created_at'],'status':'applied','classification':'initial',
                'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'raw_source_ref':identity+':initial',
                'redacted':redact(objective)!=objective or redact(acceptance)!=acceptance}
            item.update(source_history=[source],source_version=1,source_hash=digest([{'id':source['id'],'sha256':source['sha256']}]))
            self.db.execute('INSERT INTO tasks VALUES(?,?)', (identity, canonical(item)))
            self.db.execute('INSERT INTO source_blobs VALUES(?,?,?,?)',(source['id'],identity,source['sha256'],raw))
            if submission_id is not None:
                self.db.execute('INSERT INTO task_submissions VALUES(?,?,?,0)',(submission_id,identity,submission_hash))
            self.event(identity,'task','created',{'objective':item['objective'],'acceptance':item['acceptance']})
            from .organization import Organization
            from .access import Access
            if options.get('folder_id'):
                Organization(self).update(identity, {'folder_id': options['folder_id']}, 0)
            if options.get('access_mode'):
                Access(self).set(identity, options['access_mode'], 0,
                                 confirm_full_access=options.get('confirm_full_access', False))
            for attachment in attachments:
                item = self.append_instruction(identity, 'Reference attachment: ' + attachment['filename'], item['source_hash'], attachment=attachment)
            if attachments:
                item = self.update_task(identity, status='created')
            return self.get_task(identity)
        return self._transaction(create)

    def get_submission(self, submission_id):
        if not isinstance(submission_id,str) or not re.fullmatch('[a-f0-9]{32}',submission_id):
            raise KeyError(submission_id)
        with self.lock:
            row=self.db.execute('SELECT * FROM task_submissions WHERE id=?',(submission_id,)).fetchone()
        if row is None:raise KeyError(submission_id)
        return {'submission_id':submission_id,'task':self.get_task(row['task_id']),
                'start_claimed':bool(row['start_claimed'])}

    def claim_submission_start(self, submission_id):
        """Only an untouched, durably created submission can be started once.

        Commit-before-claim is retryable. Claim-before-dispatch is deliberately
        not replayable: the persisted running state enters normal controller
        restart recovery even if the process dies before scheduling the engine.
        """
        def claim():
            receipt=self.get_submission(submission_id)
            task=receipt['task']
            if receipt['start_claimed'] or task['status']!='created':
                return False
            self.db.execute('UPDATE task_submissions SET start_claimed=1 WHERE id=?',(submission_id,))
            task.update(status='running',version=task['version']+1)
            self.db.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(task),task['id']))
            self.event(task['id'],'creation','start_claimed',{'submission_id':submission_id,
                'meaning':'Initial start claimed; engine dispatch and execution are not yet observed.'})
            return True
        return self._transaction(claim)

    def get_task(self, identity):
        with self.lock:
            row = self.db.execute('SELECT body FROM tasks WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return self._source_view(json.loads(row['body']))

    def list_tasks(self):
        with self.lock:
            return [self._source_view(json.loads(r['body'])) for r in self.db.execute('SELECT body FROM tasks ORDER BY rowid DESC')]

    def task_page(self, **kwargs):
        from .task_list import page
        return page(self, **kwargs)

    def active_task_ids(self, *, statuses=('running', 'stopping'), exclude=None):
        with self.lock:
            return [r[0] for r in self.db.execute('SELECT id FROM task_headers WHERE status IN (' +
                ','.join('?' for _ in statuses) + ') AND id!=?', (*statuses, exclude or ''))]

    def notify_task_list(self):
        self.task_list_revision += 1
        for loop, notifier in tuple(self.task_list_listeners):
            if not loop.is_closed():loop.call_soon_threadsafe(notifier.set)

    async def subscribe_task_list(self):
        notifier = asyncio.Event();pair = (asyncio.get_running_loop(), notifier)
        self.task_list_listeners.add(pair)
        try:
            while True:
                notifier.clear()
                yield self.task_list_revision
                await notifier.wait()
        finally:
            self.task_list_listeners.discard(pair)

    @staticmethod
    def _source_view(item):
        if 'source_history' not in item:
            source={'id':item['id']+':initial','kind':'delegation' if item['parent_id'] else 'initial',
                'text':item['objective'],'acceptance':item['acceptance'],'created_at':item['created_at'],
                'status':'applied','classification':'initial',
                'provenance':'Original stored task fields; older imports may already be trimmed/redacted.'}
            source['sha256']=digest({'objective':source['text'],'acceptance':source['acceptance']})
            item=dict(item,source_history=[source],source_version=1,source_hash=digest([{'id':source['id'],'sha256':source['sha256']}]))
        return item

    def append_instruction(self,task_id,text,expected_source_hash,*,attachment=None):
        """Trusted user entry only. Raw source bytes never enter a workspace.

        Public/model views redact secrets; the immutable private blob preserves
        exact submitted bytes and the displayed digest is of those original bytes.
        """
        if attachment is None and (not isinstance(text,str) or not text.strip()):
            raise PolicyError('Instruction must contain text')
        raw=attachment['bytes'] if attachment else text.encode('utf-8')
        if not isinstance(raw,bytes):raise PolicyError('Attachment must be bytes')
        def append():
            item=self.get_task(task_id)
            if item['source_hash']!=expected_source_hash:raise PolicyError('SOURCE_HASH_CONFLICT: current instructions changed')
            identity=uuid4().hex;sha=hashlib.sha256(raw).hexdigest()
            source={'id':identity,'kind':'attachment' if attachment else 'instruction',
                'text':redact(text),'created_at':now(),'status':'pending','classification':None,
                'sha256':sha,'bytes':len(raw),'raw_source_ref':identity,'redacted':redact(text)!=text}
            if attachment:source['filename']=attachment['filename']
            history=[*item['source_history'],source]
            item.update(source_history=history,source_version=item['source_version']+1,
                source_hash=digest([{'id':x['id'],'sha256':x['sha256']} for x in history]),
                status='source_update_required',version=item['version']+1)
            self.db.execute('INSERT INTO source_blobs VALUES(?,?,?,?)',(identity,task_id,sha,raw))
            self.db.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(item),task_id))
            self.event(task_id,'source','received',{'source':source,'source_hash':item['source_hash'],'previous_source_hash':expected_source_hash})
            return item
        return self._transaction(append)

    def source_bytes(self,task_id,source_id,expected_hash=None):
        with self.lock:
            row=self.db.execute('SELECT * FROM source_blobs WHERE id=? AND task_id=?',(source_id,task_id)).fetchone()
        if row is None:raise PolicyError('Source is absent or belongs to another task')
        raw=bytes(row['body'])
        if hashlib.sha256(raw).hexdigest()!=row['sha256'] or (expected_hash and expected_hash!=row['sha256']):
            raise PolicyError('Source bytes changed')
        return raw,row['sha256']

    def store_source_derivative(self,task_id,source_id,expected_origin_hash,derivative_id,raw):
        if not isinstance(raw,bytes) or not isinstance(derivative_id,str) or not derivative_id:
            raise PolicyError('Source derivative needs exact bytes and an immutable identity')
        sha=hashlib.sha256(raw).hexdigest()
        def capture():
            task=self.get_task(task_id)
            origin=next((s for s in task['source_history'] if s['id']==source_id),None)
            if not origin or origin['kind']!='attachment' or origin['sha256']!=expected_origin_hash:
                raise PolicyError('Source derivative origin is absent, changed or foreign')
            self.source_bytes(task_id,source_id,expected_origin_hash)
            lineage={'id':derivative_id,'task_id':task_id,'source_id':source_id,
                     'source_sha256':expected_origin_hash,'extracted_sha256':sha,'extracted_bytes':len(raw)}
            existing=self.db.execute('SELECT * FROM source_blobs WHERE id=?',(derivative_id,)).fetchone()
            if existing:
                if (existing['task_id']!=task_id or existing['sha256']!=sha or bytes(existing['body'])!=raw
                        or self.record_get('source_derivative',derivative_id)!=lineage):
                    raise PolicyError('Immutable source derivative identity changed')
            else:
                self.db.execute('INSERT INTO source_blobs VALUES(?,?,?,?)',(derivative_id,task_id,sha,raw))
                self.record('source_derivative',derivative_id,lineage)
            return {'raw_source_ref':derivative_id,'extracted_sha256':sha,'extracted_bytes':len(raw)}
        return self._transaction(capture)

    def withdraw_source(self,task_id,expected_source_hash,source_id,instruction_id,source_quote,operation_id):
        def withdraw():
            task=self.get_task(task_id);history=task['source_history']
            if task['source_hash']!=expected_source_hash:raise PolicyError('SOURCE_HASH_CONFLICT: withdrawal source changed')
            source=next((s for s in history if s['id']==source_id),None)
            instruction=next((s for s in history if s['id']==instruction_id),None)
            if (not source or source['kind']!='attachment' or source['status']!='pending'
                    or not instruction or instruction['kind']!='instruction' or history.index(instruction)<=history.index(source)
                    or not isinstance(source_quote,str) or not source_quote.strip() or source_quote not in instruction['text']):
                raise PolicyError('Withdrawal needs an exact later instruction for the pending owned attachment')
            self.source_bytes(task_id,source_id,source['sha256'])
            self.source_bytes(task_id,instruction_id,instruction['sha256'])
            operation=self.get_operation(operation_id)
            args=operation['operation']['args']
            if (operation['task_id']!=task_id or operation['operation']['kind']!='source_prepare' or args.get('mode')!='withdraw'
                    or args.get('source_id')!=source_id or args.get('instruction_id')!=instruction_id or args.get('source_quote')!=source_quote):
                raise PolicyError('Withdrawal operation source differs')
            provenance={'source_id':source_id,'classification':'withdraw','instruction_id':instruction_id,
                        'instruction_sha256':instruction['sha256'],'source_quote':source_quote,
                        'reason':args['reason'],'operation_id':operation_id}
            self.record('task_source_history',operation_id+':source-withdrawal',
                        {'id':operation_id+':source-withdrawal','task_id':task_id,'before':task,'disposition':provenance})
            task['source_history']=[dict(s,status='withdrawn',classification='withdraw',disposition=provenance,
                applied_operation_id=operation_id) if s['id']==source_id else s for s in history]
            task['version']+=1
            self.db.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(task),task_id))
            self.event(task_id,'source','withdrawn',provenance)
            return task
        return self._transaction(withdraw)

    def append_delegation_scope(self,child_id,parent_id,operation,expected_source_hash):
        """A reviewed parent's new job scope, explicitly distinct from user input.

        The caller owns admission. Preserve original delegation and acceptance;
        normal child source reconciliation must consume this pending source.
        """
        args=operation['args'];text=canonical({'settlement':args['settlement'],
            'meaning':'Replace the cancelled deliverable with closure of all actual pending results, required reviews, learning and workflow improvements. Do not resume the cancelled deliverable.',
            'parent_source_id':args['source_id'],'parent_source_quote':args['source_quote'],
            'parent_reason':args['reason']})
        raw=text.encode('utf-8');identity=operation['id']+':delegation-scope'
        def append():
            item=self.get_task(child_id)
            if item['parent_id']!=parent_id or item['source_hash']!=expected_source_hash:
                raise PolicyError('Delegation scope owner/source changed')
            source={'id':identity,'kind':'delegation_update','text':redact(text),'created_at':now(),
                'status':'pending','classification':None,'sha256':hashlib.sha256(raw).hexdigest(),
                'bytes':len(raw),'raw_source_ref':identity,'parent_id':parent_id,
                'parent_operation_id':operation['id'],'parent_source_hash':args['parent_source_hash'],
                'authority':'Reviewed parent job scope; not a new user instruction or policy amendment'}
            history=[*item['source_history'],source]
            item.update(source_history=history,source_version=item['source_version']+1,
                source_hash=digest([{'id':s['id'],'sha256':s['sha256']} for s in history]),
                status='source_update_required',version=item['version']+1)
            self.db.execute('INSERT INTO source_blobs VALUES(?,?,?,?)',(identity,child_id,source['sha256'],raw))
            self.db.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(item),child_id))
            self.event(child_id,'delegation_scope','received',{'source':source,'source_hash':item['source_hash']})
            return item
        return self._transaction(append)

    def apply_source_plan(self,task_id,expected_source_hash,plan,operation_id, *, reference_attachment_ids=()):
        from .sources import validate_reconciliation
        from .source_preparation import prepared_source_texts
        def apply():
            item=self.get_task(task_id)
            if item['source_hash']!=expected_source_hash:raise PolicyError('SOURCE_HASH_CONFLICT: plan source changed')
            if any(r['task_id']==task_id and r['status']=='source_revalidation_required'
                   for r in self.records('source_preparation_confirmation_pending')):
                raise PolicyError('Source confirmation requires a current reviewed withdrawal or retention before adoption')
            prepared=prepared_source_texts(self,item)
            references = set(reference_attachment_ids)
            pending_attachments = {s['id'] for s in item['source_history'] if s['kind']=='attachment' and s['status']=='pending'}
            if references - pending_attachments:
                raise PolicyError('Reference attachment identity is outside the pending source set')
            for disposition in plan.get('acceptance_dispositions', []):
                if disposition.get('disposition') != 'retain' and references.intersection(disposition.get('source_ids', [])):
                    raise PolicyError('Reference attachment content cannot change user requirements')
            for source in item['source_history']:
                if source['kind']!='attachment' or source['status']!='pending':continue
                if source['id'] in references:
                    continue  # Classified as reference only, never asserted fully read.
                progress=self.record_get('source_read_progress',source['id']) or {}
                if source['id'] not in prepared and not (progress.get('complete') and progress.get('text_decoded')):
                    raise PolicyError('Pending attachment requires complete source reading or confirmed extraction before adoption')
            validate_reconciliation(item,plan,self.source_texts(item))
            self.record('task_source_history',operation_id,{'id':operation_id,'task_id':task_id,'before':item,'plan':plan})
            dispositions={x['source_id']:x for x in plan.get('source_dispositions',[])}
            history=[dict(x,status='applied',classification=dispositions[x['id']]['classification'],
                **({'reference_only': True} if x['id'] in references else {}),
                disposition=dispositions[x['id']],applied_operation_id=operation_id) if x['id'] in dispositions else x for x in item['source_history']]
            state=dict(item['state'],plan=plan)
            state.pop('source_update_pending',None)
            item.update(objective=redact(plan['objective']),acceptance=redact(plan['acceptance']),source_history=history,state=state,version=item['version']+1)
            self.db.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(item),task_id))
            self.event(task_id,'source','reconciled',{'source_hash':item['source_hash'],'operation_id':operation_id,
                'dispositions':plan.get('source_dispositions',[]),'acceptance_dispositions':plan.get('acceptance_dispositions',[])})
            return item
        return self._transaction(apply)

    def source_texts(self,task):
        from .source_preparation import prepared_source_texts
        texts={s['id']:s['text'] for s in task['source_history']}
        for source in task['source_history']:
            if source['kind']!='attachment':continue
            raw,_=self.source_bytes(task['id'],source['id'],source['sha256'])
            try:texts[source['id']]=redact(raw.decode('utf-8'))
            except UnicodeError:pass
        texts.update(prepared_source_texts(self,task))
        return texts

    def update_task(self, identity, **changes):
        allowed = {'status','state','final','policy_hash','acceptance','objective'}
        if set(changes)-allowed:
            raise PolicyError('Task identity/ownership cannot be rewritten')
        def edit():
            item = self.get_task(identity)
            item.update(redact(changes))
            item['version'] += 1
            self.db.execute('UPDATE tasks SET body=? WHERE id=?', (canonical(item), identity))
            if set(changes) & {'status', 'objective'}:self.notify_task_list()
            return item
        return self._transaction(edit)

    def event(self, task_id, stage, status, detail):
        self.get_task(task_id)
        item = dict(task_id=task_id, stage=stage, status=status, detail=redact(detail), created_at=now())
        def append():
            previous = self.db.execute('SELECT hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
            item['prev_hash'] = previous['hash'] if previous else '0'*64
            item['hash'] = digest(item)
            cursor = self.db.execute('INSERT INTO events(task_id,stage,status,detail,created_at,prev_hash,hash) VALUES(?,?,?,?,?,?,?)',
                (task_id,stage,status,canonical(item['detail']),item['created_at'],item['prev_hash'],item['hash']))
            return dict(seq=cursor.lastrowid, **item)
        event = self._transaction(append)
        if stage in {'task', 'source', 'organization', 'recovery'} and status != 'progress':
            self.notify_task_list()
        for loop, notifier in tuple(self.listeners[task_id]):
            if not loop.is_closed():
                loop.call_soon_threadsafe(notifier.set)
        return event

    def events(self, task_id, after=0, limit=None):
        self.get_task(task_id)
        with self.lock:
            sql='SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq'
            args=(task_id,int(after))
            if limit is not None:
                sql+=' LIMIT ?';args=(*args,max(1,int(limit)))
            rows=self.db.execute(sql,args).fetchall()
        return [dict(r, detail=json.loads(r['detail'])) for r in rows]

    async def subscribe(self, task_id, after=0):
        self.get_task(task_id)
        notifier = asyncio.Event()
        pair = (asyncio.get_running_loop(), notifier)
        self.listeners[task_id].add(pair)
        try:
            while True:
                notifier.clear()
                batch=self.events(task_id,after,limit=160)
                for event in batch:
                    after=event['seq']
                    yield event
                if len(batch)<160:
                    await notifier.wait()
        finally:
            self.listeners[task_id].discard(pair)

    def verify_events(self):
        prior='0'*64
        with self.lock:
            for row in self.db.execute('SELECT * FROM events ORDER BY seq'):
                item=dict(row);item.pop('seq');expected=item.pop('hash');item['detail']=json.loads(item['detail'])
                if item['prev_hash']!=prior or digest(item)!=expected:
                    raise PolicyError('Event history integrity failure; preserve database for recovery')
                prior=expected
        return prior

    def save_operation(self, task_id, operation_dict, **fields):
        self.get_task(task_id)
        identity=operation_dict['id']
        fields.setdefault('source_hash',self.get_task(task_id)['source_hash'])
        fields.setdefault('delegation_hash',digest(self.get_task(task_id)['state'].get('delegation_lease')))
        item=dict(task_id=task_id, operation=operation_dict, status='proposed', receipts={}, result=None, **fields)
        with self.lock:
            try:
                self.db.execute('INSERT INTO operations VALUES(?,?,?)',(identity,task_id,canonical(redact(item))))
            except sqlite3.IntegrityError as e:
                raise PolicyError('Operation identity already exists; replay rejected') from e
        return item

    def get_operation(self, identity):
        with self.lock:
            row=self.db.execute('SELECT body FROM operations WHERE id=?',(identity,)).fetchone()
        if row is None: raise KeyError(identity)
        return json.loads(row['body'])

    def update_operation(self, identity, **fields):
        if set(fields) & {'task_id','operation'}:
            raise PolicyError('Frozen operation cannot be rewritten')
        def edit():
            item=self.get_operation(identity);item.update(redact(fields))
            self.db.execute('UPDATE operations SET body=? WHERE id=?',(canonical(item),identity))
            return item
        return self._transaction(edit)

    def operations(self, task_id):
        with self.lock:
            return [json.loads(r['body']) for r in self.db.execute('SELECT body FROM operations WHERE task_id=? ORDER BY rowid',(task_id,))]

    def operation_window(self, task_id, start=0, count=100):
        with self.lock:
            return [json.loads(r['body']) for r in self.db.execute(
                'SELECT body FROM operations WHERE task_id=? ORDER BY rowid LIMIT ? OFFSET ?',
                (task_id, count, start))]

    def operation_count(self, task_id):
        with self.lock:
            return self.db.execute('SELECT count(*) FROM operation_headers WHERE task_id=?',(task_id,)).fetchone()[0]

    def operation_results_hash(self, task_id):
        """Bind every result for review reuse without materializing the history."""
        value = hashlib.sha256()
        with self.lock:
            for row in self.db.execute("SELECT id,json_extract(body,'$.result') FROM operations WHERE task_id=? ORDER BY rowid", (task_id,)):
                for part in row:
                    raw = str(part).encode('utf-8')
                    value.update(len(raw).to_bytes(8, 'big'));value.update(raw)
        return value.hexdigest()

    def practical_metrics(self, task_id):
        """Aggregate counters in SQLite; full provider replies stay on disk."""
        with self.lock:
            row = self.db.execute("""SELECT count(*),coalesce(sum(json_extract(body,'$.elapsed_seconds')),0),
                coalesce(sum(json_extract(body,'$.phase')='context_compaction'),0)
                FROM records WHERE kind='practical_call' AND json_extract(body,'$.task_id')=?""", (task_id,)).fetchone()
            web = self.db.execute("SELECT count(*) FROM operation_headers WHERE task_id=? AND json_extract(body,'$.operation.kind')='web_fetch'", (task_id,)).fetchone()[0]
            compressed = self.db.execute("SELECT count(*) FROM records WHERE kind='practical_context' AND json_extract(body,'$.task_id')=?", (task_id,)).fetchone()[0]
        return dict(model_calls=row[0],model_elapsed_seconds=row[1],compaction_model_calls=row[2],
                    web_operations=web,context_compactions=compressed,learning_model_calls=0)

    def operation_headers(self, task_id, start=0, count=-1):
        with self.lock:
            return [json.loads(r['body']) for r in self.db.execute(
                'SELECT body FROM operation_headers WHERE task_id=? ORDER BY sequence LIMIT ? OFFSET ?',
                (task_id,count,start))]

    def operation_position(self, task_id, identity):
        with self.lock:
            sequence=self.db.execute('SELECT sequence FROM operation_headers WHERE task_id=? AND id=?',(task_id,identity)).fetchone()
            if sequence is None:raise KeyError(identity)
            return self.db.execute('SELECT count(*) FROM operation_headers WHERE task_id=? AND sequence<?',(task_id,sequence[0])).fetchone()[0]

    def unresolved_operations(self, task_id):
        with self.lock:
            rows=self.db.execute('SELECT id,body FROM operation_headers WHERE task_id=? AND unresolved=1 ORDER BY sequence',(task_id,)).fetchall()
            return [(self.operation_position(task_id,r['id']),json.loads(r['body'])) for r in rows]

    def operation_started_without_result(self, row):
        """A consumed permit/result gap is not an unstarted operation."""
        if row.get('result') is not None:return False
        if row['status']=='executing' or row.get('started_at'):return True
        with self.lock:
            permit=self.db.execute('SELECT consumed FROM permits WHERE operation_id=?',
                                   (row['operation']['id'],)).fetchone()
        return bool(permit and permit['consumed'])

    def operation_index(self, task_id):
        """Current exact index; reuse computation only for identical stored bytes.

        Every call reads the current rows, including other connections' changes.
        The bounded cache contains metadata only; full history stays in SQLite.
        """
        with self.lock:
            rows=self.db.execute('SELECT id,body FROM operations WHERE task_id=? ORDER BY rowid',(task_id,)).fetchall()
            result=[]
            for stored in rows:
                key=(task_id,stored['id'])
                version=hashlib.sha256(stored['body'].encode('utf-8')).digest()
                cached=self._operation_index_cache.get(key)
                if cached is None or cached[0]!=version:
                    row=json.loads(stored['body'])
                    encoded=canonical(row).encode('utf-8')
                    item={'id':row['operation']['id'],'kind':row['operation']['kind'],'status':row['status'],
                        'effect':(row.get('result') or {}).get('effect') or ('unknown' if self.operation_started_without_result(row) else None),
                        'sha256':hashlib.sha256(encoded).hexdigest(),
                        'read_args':{'operation_ids':[row['operation']['id']],
                            'expected_hash':hashlib.sha256(b'['+encoded+b']').hexdigest(),
                            'offset':0,'max_chars':24000}}
                    self._operation_index_cache[key]=(version,item)
                self._operation_index_cache.move_to_end(key)
                result.append(deepcopy(self._operation_index_cache[key][1]))
                while len(self._operation_index_cache)>4096:
                    self._operation_index_cache.popitem(last=False)
            return result

    def record(self, kind, identity, body):
        with self.lock:
            self.db.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',
                (kind,identity,canonical(redact(body))))
        return body

    def records(self, kind):
        with self.lock:
            return [json.loads(r['body']) for r in self.db.execute('SELECT body FROM records WHERE kind=? ORDER BY rowid',(kind,))]

    def task_records(self, kind, task_id, *, limit=-1, source_hash=None, newest_first=False):
        with self.lock:
            predicate = " AND json_extract(body,'$.source_hash')=?" if source_hash is not None else ''
            values = [kind, task_id] + ([source_hash] if source_hash is not None else []) + [limit]
            return [json.loads(r['body']) for r in self.db.execute(
                "SELECT body FROM records WHERE kind=? AND json_extract(body,'$.task_id')=?" + predicate
                + (' ORDER BY rowid DESC' if newest_first else ' ORDER BY rowid') + ' LIMIT ?', values)]

    def record_get(self, kind, identity):
        with self.lock:
            row=self.db.execute('SELECT body FROM records WHERE kind=? AND id=?',(kind,identity)).fetchone()
        return json.loads(row['body']) if row else None

    def matching_outcomes(self, task_id, query):
        with self.lock:
            # SQLite filters before Python decodes; retain the same literal,
            # Unicode-insensitive search without loading every old outcome.
            self.db.create_function('oif_casefold', 1, lambda value: value.casefold())
            rows=self.db.execute("""SELECT body FROM records WHERE kind='practical_outcome'
                AND json_extract(body,'$.task_id')=? AND instr(oif_casefold(body),?)>0
                ORDER BY rowid DESC LIMIT 10""",(task_id,query.casefold()))
            return list(reversed([json.loads(row[0]) for row in rows]))

    def web_operation_exchanges(self, task_id, operation_id):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("""SELECT body FROM records
                WHERE kind='web_exchange' AND json_extract(body,'$.record.task_id')=?
                AND json_extract(body,'$.record.operation_id')=? ORDER BY rowid""",(task_id,operation_id))]

    def pending_calls(self, task_id, phase):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("""SELECT body FROM records
                WHERE kind='practical_call' AND json_extract(body,'$.task_id')=? AND json_extract(body,'$.phase')=?
                AND json_extract(body,'$.status')='responded' AND json_extract(body,'$.consumed')=0 ORDER BY rowid DESC""",(task_id,phase))]

    def available_skills(self, task_id, scope, *, query='', tools=(), limit=32):
        from .skill_search import search
        return search(self, task_id, scope, query, tools, limit)

    def pending_skill_uses(self, task_id):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("""SELECT body FROM records
                WHERE kind='practical_skill_use' AND json_extract(body,'$.task_id')=?
                AND json_extract(body,'$.status')='result_observed' AND json_extract(body,'$.assessment') IS NULL
                ORDER BY rowid LIMIT 8""",(task_id,))]

    def pending_update_uses(self, task_id):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("""SELECT body FROM records
                WHERE kind='maintenance_ordinary_use' AND json_extract(body,'$.task_id')=?
                AND json_extract(body,'$.assessment_call') IS NULL
                ORDER BY rowid LIMIT 8""",(task_id,))]

    def task_skills(self, task_id, used_ids):
        with self.lock:
            # A detail-panel query, never part of each model decision. Include
            # refinements made here even if they have not yet been used here.
            rows={r['id']:json.loads(r['body']) for r in self.db.execute("""SELECT id,body FROM records
                WHERE kind='practical_skill' AND (json_extract(body,'$.owner_task_id')=? OR
                EXISTS(SELECT 1 FROM json_each(records.body,'$.touched_tasks') WHERE value=?))""",(task_id,task_id))}
            for identity in used_ids:
                skill=self.record_get('practical_skill',identity)
                if skill:rows[identity]=skill
            return list(rows.values())

    def _permit_dependencies(self, task_id, op):
        task=self.get_task(task_id);lease=task['state'].get('delegation_lease')
        if op.get('source_hash')!=task['source_hash']:raise PolicyError('SOURCE_HASH_CONFLICT: reviewed operation source changed')
        if op.get('delegation_hash')!=digest(lease):raise PolicyError('Child lease changed after review')
        dependencies={'task_id':task_id,'source_hash':task['source_hash'],'delegation_hash':digest(lease),'parent':None}
        if task['parent_id']:
            parent=self.get_task(task['parent_id'])
            if not lease or lease.get('parent_source_hash')!=parent['source_hash']:
                raise PolicyError('Parent source changed; child requires a reviewed scope disposition')
            dependencies['parent']={'id':parent['id'],'source_hash':parent['source_hash']}
        return dependencies

    def issue_permit(self, task_id, operation_id, action_hash, policy_hash, bundle_hash):
        """Internal only; Engine is the only permit issuer and HTTP has no route here."""
        def issue():
            identity=uuid4().hex
            op=self.get_operation(operation_id)
            if op['task_id']!=task_id or op['status']!='reviewed' or digest(op['operation'])!=action_hash:
                raise PolicyError('Permit target does not match reviewed operation')
            from .policy_admission import validate_admission
            validate_admission(self,task_id,op,policy_hash)
            dependencies=self._permit_dependencies(task_id,op)
            existing=self.db.execute('SELECT * FROM permits WHERE operation_id=?',(operation_id,)).fetchone()
            if existing:
                if existing['consumed'] or any(existing[k]!=v for k,v in {'task_id':task_id,'action_hash':action_hash,'policy_hash':policy_hash,'bundle_hash':bundle_hash}.items()):
                    raise PolicyError('Existing execution permit is consumed or bound to a different review')
                if self.record_get('permit_dependencies',existing['id'])!=dependencies:
                    raise PolicyError('Existing execution permit lacks the exact current source dependencies')
                return existing['id']
            self.db.execute('INSERT INTO permits VALUES(?,?,?,?,?,?,0)',(identity,task_id,operation_id,action_hash,policy_hash,bundle_hash))
            self.record('permit_dependencies',identity,dependencies)
            return identity
        return self._transaction(issue)

    def consume_permit(self, identity, *, task_id, operation_id, action_hash, policy_hash, bundle_hash):
        def consume():
            row=self.db.execute('SELECT * FROM permits WHERE id=?',(identity,)).fetchone()
            if row is None or row['consumed'] or any(row[k]!=v for k,v in dict(task_id=task_id,operation_id=operation_id,action_hash=action_hash,policy_hash=policy_hash,bundle_hash=bundle_hash).items()):
                raise PolicyError('Missing, stale or consumed execution permit')
            op=self.get_operation(operation_id)
            if op['status']!='reviewed' or digest(op['operation'])!=action_hash:
                raise PolicyError('Reviewed action changed')
            if self.record_get('permit_dependencies',identity)!=self._permit_dependencies(task_id,op):
                raise PolicyError('Permit source dependencies changed before execution')
            from .policy_admission import validate_admission
            validate_admission(self,task_id,op,policy_hash)
            op['status']='executing';op['started_at']=now()
            self.db.execute('UPDATE permits SET consumed=1 WHERE id=?',(identity,))
            self.db.execute('UPDATE operations SET body=? WHERE id=?',(canonical(op),operation_id))
        self._transaction(consume)

    def backup(self, path: Path):
        with self.lock, sqlite3.connect(path) as target:
            self.db.backup(target)

    def close(self):
        with self.lock:
            self.db.close()
