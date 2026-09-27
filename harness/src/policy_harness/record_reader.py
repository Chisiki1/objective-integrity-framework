"""Read-only, request-scoped SQLite views for UI pages and record exports.

The separate WAL read transaction keeps each response consistent without holding
the controller's lock while a client reads. Cancellation closes the connection.
"""
from contextlib import contextmanager
from copy import copy
import json
import sqlite3
import threading

from .store import Store


class RecordReader(Store):
    def __init__(self, original, task_id, *, display=False, after=0, before=0, source_before=0, operation_before=0, artifact_page=0):
        self.data_dir, self.workspaces, self.path = original.data_dir, original.workspaces, original.path
        self.db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute('BEGIN')
        self.target, self.display = task_id, display
        self.after, self.before, self.source_before = after, before, source_before
        self.operation_before, self.artifact_page = operation_before, artifact_page
        self.page = {}
        self.metadata_only = False
        self._tasks = {}
        self.transform_event = lambda row: row

    def get_task(self, identity):
        if identity in self._tasks:
            return self._tasks[identity]
        if not self.display or identity != self.target:
            task = super().get_task(identity)
            self._tasks[identity] = task
            return task
        row = self.db.execute("SELECT json_remove(body,'$.source_history','$.state') AS body, "
                              "json_array_length(body,'$.source_history') AS count, "
                              "json_extract(body,'$.state') AS state FROM tasks WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        task = json.loads(row['body'])
        # State is one record, scrubbed before its display preview is produced.
        task['state'] = json.loads(row['state'] or '{}')
        count = row['count'] or 0
        end = min(self.source_before or count, count)
        start = max(0, end-20)
        sources = []
        for r in self.db.execute("SELECT j.value, j.key FROM tasks, json_each(body,'$.source_history') j "
                                 "WHERE tasks.id=? AND j.key>=? AND j.key<? ORDER BY j.key", (identity,start,end)):
            source = json.loads(r['value'])
            source['_index'] = r['key']
            source['_record_url'] = f'/api/tasks/{identity}/records/sources/{source["id"]}'
            receipt = self.db.execute("SELECT seq FROM events WHERE task_id=? AND stage='source' AND status='received' "
                                      "AND json_extract(detail,'$.source.id')=? ORDER BY seq LIMIT 1", (identity,source['id'])).fetchone()
            first = self.db.execute('SELECT min(seq) n FROM events WHERE task_id=?',(identity,)).fetchone() if r['key']==0 and not receipt else None
            source['_event_seq'] = receipt['seq'] if receipt else first['n'] if first else None
            sources.append(source)
        if sources:
            task['source_history'] = sources
        task = self._source_view(task)
        self.page['source_before'] = start
        self.page['source_count'] = count or len(task['source_history'])
        self.page['pending_source_count'] = self.db.execute("SELECT count(*) FROM tasks,json_each(body,'$.source_history') j WHERE tasks.id=? AND json_extract(j.value,'$.status')='pending'",(identity,)).fetchone()[0]
        # Completed results are small per-instruction records, obtained even when
        # their progress is outside the current event page.
        for i, source in enumerate(sources):
            lower = source['_event_seq']
            upper_row = self.db.execute("SELECT min(seq) n FROM events WHERE task_id=? AND stage='source' AND status='received' AND seq>?", (identity,lower or 0)).fetchone()
            upper = upper_row['n'] if upper_row else None
            row = self.db.execute("SELECT seq,detail FROM events WHERE task_id=? AND stage='task' AND status='completed' "
                                  "AND seq>=? AND (? IS NULL OR seq<?) ORDER BY seq DESC LIMIT 1", (identity,lower or 0,upper,upper)).fetchone()
            if row:
                source['_final'] = json.loads(row['detail'])
                source['_final_record_url'] = f'/api/tasks/{identity}/records/events/{row["seq"]}'
        self._tasks[identity] = task
        return task

    def records(self, kind):
        if self.metadata_only:return iter(())
        # Narrow snapshot-only reads to this task before JSON decoding. Original
        # execution and global learning selection keep their existing Store API.
        sql='SELECT body FROM records WHERE kind=?'
        args=[kind]
        if kind in {'practical_skill_use','practical_idea','idea','episode','skill_application'}:
            sql+=" AND json_extract(body,'$.task_id')=?";args.append(self.target)
        elif kind in {'skill','practical_skill'}:
            sql+=" AND (EXISTS(SELECT 1 FROM json_each(body,'$.touched_tasks') WHERE value=?)";args.append(self.target)
            if kind=='practical_skill':
                sql+=" OR id IN (SELECT json_extract(u.body,'$.skill_id') FROM records u WHERE u.kind='practical_skill_use' AND json_extract(u.body,'$.task_id')=?)";args.append(self.target)
            sql+=')'
        elif kind=='knowledge_candidate':
            sql+=" AND (json_extract(body,'$.payload.source_task_id')=? OR json_extract(body,'$.payload.primary_task_id')=?)";args.extend([self.target,self.target])
        if self.display:
            sql+=' ORDER BY rowid DESC LIMIT 81'
            values=[json.loads(row['body']) for row in self.db.execute(sql,args)]
            if len(values)>80:self.page['knowledge_more']=True
            return iter(values[:80])
        sql+=' ORDER BY rowid'
        return (json.loads(row['body']) for row in self.db.execute(sql,args))

    def task_records(self, kind, task_id, *, limit=-1, source_hash=None, newest_first=False):
        if self.metadata_only:return []
        if not self.display:
            return super().task_records(kind,task_id,limit=limit,source_hash=source_hash,newest_first=newest_first)
        count=min(limit,80) if limit>=0 else 80
        values=super().task_records(kind,task_id,limit=count+1,source_hash=source_hash,newest_first=True)
        if len(values)>count:self.page['knowledge_more']=True
        return values[:count]

    def task_skills(self, task_id, used_ids):
        if self.metadata_only:return []
        if not self.display:return super().task_skills(task_id,used_ids)
        rows=self.db.execute("""SELECT body FROM records WHERE kind='practical_skill' AND (
            json_extract(body,'$.owner_task_id')=? OR EXISTS(
                SELECT 1 FROM json_each(records.body,'$.touched_tasks') WHERE value=?) OR
            id IN (SELECT value FROM json_each(?))) ORDER BY rowid DESC LIMIT 81""",
            (task_id,task_id,json.dumps(list(used_ids))))
        values=[json.loads(row['body']) for row in rows]
        if len(values)>80:self.page['knowledge_more']=True
        return values[:80]

    def events(self, task_id, after=0):
        if not self.display:
            return []  # The export streams original rows separately.
        cursor = self.after or after
        order = 'ASC' if cursor and not self.before else 'DESC'
        rows=[];more=False
        for row in self.db.execute('SELECT * FROM events WHERE task_id=? AND seq>? AND (?=0 OR seq<?) ORDER BY seq '+order+' LIMIT 161',
                                   (task_id,cursor,self.before,self.before)):
            if len(rows)==160:
                more=True;break
            rows.append(self.transform_event(dict(row,detail=json.loads(row['detail']))))
        self.page['event_before'] = rows[-1]['seq'] if more and order=='DESC' else 0
        self.page['event_more_after'] = more and order=='ASC'
        return list(reversed(rows)) if order=='DESC' else rows

    def operations(self, task_id):
        if not self.display:
            return []
        # SQLite extracts metadata before Python sees the large tool results.
        fields = ['operation','tool_name','status','started_at','source_hash','result.status','result.effect','result.artifacts']
        # Arguments are intentionally absent; the exact operation has a raw link.
        sql = "SELECT rowid,id,json_array(json_extract(body,'$.operation.id'),json_extract(body,'$.operation.kind'),json_extract(body,'$.operation.purpose')," + ','.join("json_extract(body,'$."+f+"')" for f in fields[1:]) + ") data FROM operations WHERE task_id=? AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT 81"
        result=[]
        last=0;more=False
        for row in self.db.execute(sql,(task_id,self.operation_before,self.operation_before)):
            if len(result)==80:
                more=True;break
            last=row['rowid']
            v=json.loads(row['data'])
            result.append({'task_id':task_id,'operation':dict(zip(('id','kind','purpose'),v[:3])),
                           **dict(zip(fields[1:5],v[3:7])), 'result':dict(zip(('status','effect','artifacts'),v[7:]))})
        self.page['operation_before']=last if more else 0
        return list(reversed(result))

    def latest_artifacts(self, eligible):
        # Deduplicate versions in SQLite. Later readbacks of an old version do
        # not outrank the write that produced a newer file. Python receives at
        # most one small page, regardless of tool output/history size.
        self.db.create_function('oif_artifact_eligible', 3, eligible)
        sql="""WITH artifacts AS (
          SELECT o.rowid position,o.id,json_extract(o.body,'$.operation.kind') kind,
            coalesce(json_extract(o.body,'$.tool_name'),json_extract(o.body,'$.operation.kind')) tool,
            json_extract(o.body,'$.started_at') started,json_extract(o.body,'$.source_hash') source_hash,
            coalesce(json_extract(a.value,'$.path'),json_extract(a.value,'$.relative_path')) path,
            json_extract(a.value,'$.sha256') hash,a.value artifact
          FROM operations o,json_each(o.body,'$.result.artifacts') a
          WHERE o.task_id=? AND a.type='object' AND o.id=json_extract(o.body,'$.operation.id')
            AND coalesce(json_extract(a.value,'$.channel'),'') NOT IN ('stdout','stderr')
            AND oif_artifact_eligible(
              coalesce(json_extract(a.value,'$.path'),json_extract(a.value,'$.relative_path')),
              json_extract(a.value,'$.sha256'),o.id)
        ), versions AS (
          SELECT *,row_number() OVER(PARTITION BY path,hash ORDER BY (tool IN ('file_write','file_edit')) DESC,position DESC) version_rank FROM artifacts
        ), latest AS (
          SELECT *,row_number() OVER(PARTITION BY path ORDER BY position DESC) path_rank FROM versions WHERE version_rank=1
        ) SELECT * FROM latest WHERE path_rank=1 AND path IS NOT NULL ORDER BY position DESC,path LIMIT 33 OFFSET ?"""
        rows=self.db.execute(sql,(self.target,self.artifact_page*32)).fetchall()
        self.page['artifact_page']=self.artifact_page
        self.page['artifact_more']=len(rows)>32
        return [{'operation':{'id':r['id'],'kind':r['kind']},'tool_name':r['tool'],'started_at':r['started'],
                 'source_hash':r['source_hash'],'result':{'artifacts':[json.loads(r['artifact'])]}} for r in reversed(rows[:32])]

    def list_tasks(self):
        # Snapshot consumers only request children of this task.
        return [self._source_view(json.loads(r['body'])) for r in self.db.execute(
            "SELECT body FROM tasks WHERE json_extract(body,'$.parent_id')=? ORDER BY rowid DESC",(self.target,))]

    def iter_events(self):
        for row in self.db.execute('SELECT * FROM events WHERE task_id=? ORDER BY seq',(self.target,)):
            yield dict(row,detail=json.loads(row['detail']))

    def iter_operations(self):
        for row in self.db.execute('SELECT body FROM operations WHERE task_id=? ORDER BY rowid',(self.target,)):
            yield json.loads(row['body'])

    def knowledge_records(self, engine):
        """Stream original scoped knowledge once; use links stay separate."""
        from itertools import chain
        task=self.get_task(self.target)
        legacy=getattr(engine,'knowledge',None)
        learning=getattr(engine,'learning',None)
        def skills():
            if legacy:
                for row in self.records('skill'):
                    if self.target in row['touched_tasks'] and legacy._writable(task,row):yield row
            if learning:
                for row in self.records('practical_skill'):
                    yield row if row['owner_task_id']==self.target else learning.public_lesson(row)
        def candidates():
            if legacy:
                for row in self.records('knowledge_candidate'):
                    if row['payload']['source_task_id']==self.target or (not task.get('parent_id') and row['payload']['primary_task_id']==self.target):
                        yield legacy._describe_record(row)
        return {'skills':skills(), 'ideas':chain(self.records('idea'),self.records('practical_idea')),
                'episodes':self.records('episode'),
                'applications':chain(self.records('skill_application'),self.records('practical_skill_use')),
                'candidates':candidates()}


@contextmanager
def snapshot_reader(original, engine, task_id, **options):
    reader = RecordReader(original,task_id,**options)
    try:
        local = copy(engine)
        local.store = reader
        # Constructors would perform startup reconciliation; shallow copies only
        # rebind read-only snapshot collaborators to the same transaction.
        for name in ('knowledge','learning','access'):
            component=getattr(engine,name,None)
            if component is not None:
                component=copy(component);component.store=reader;setattr(local,name,component)
        yield reader,local
    finally:
        reader.db.close()
