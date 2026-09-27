"""Derived task headers for bounded lists; original tasks remain unchanged."""
import json
from .models import PolicyError


def ensure_index(store):
    fields = ('objective', 'status', 'parent_id', 'created_at', 'version', 'source_version', 'source_hash')
    def projection(body):
        # Keep the objective for redaction before display truncation. Unlike a
        # task body it contains no accumulated history, results or model state.
        pairs = [f"'{key}',json_extract({body},'$.{key}')" for key in fields]
        return "json_object('id',NEW.id," + ','.join(pairs) + ')'
    with store.lock:
        if store.record_get('controller_schema', 'task_headers') == {'version': 1}:
            return
        body = projection('NEW.body')
        store.db.executescript(f'''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS task_headers(id TEXT PRIMARY KEY, sequence INTEGER NOT NULL,
                parent_id TEXT, status TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS task_header_status ON task_headers(status);
            CREATE INDEX IF NOT EXISTS task_header_order ON task_headers(parent_id,sequence DESC);
            CREATE TRIGGER IF NOT EXISTS task_header_insert AFTER INSERT ON tasks BEGIN
                INSERT INTO task_headers VALUES(NEW.id,NEW.rowid,json_extract(NEW.body,'$.parent_id'),
                    json_extract(NEW.body,'$.status'),{body});
            END;
            CREATE TRIGGER IF NOT EXISTS task_header_update AFTER UPDATE OF body ON tasks BEGIN
                UPDATE task_headers SET parent_id=json_extract(NEW.body,'$.parent_id'),
                    status=json_extract(NEW.body,'$.status'),body={body} WHERE id=NEW.id;
            END;
            CREATE TRIGGER IF NOT EXISTS task_header_delete AFTER DELETE ON tasks BEGIN
                DELETE FROM task_headers WHERE id=OLD.id;
            END;
            DELETE FROM task_headers;
            INSERT INTO task_headers SELECT NEW.id,NEW.rowid,json_extract(NEW.body,'$.parent_id'),
                json_extract(NEW.body,'$.status'),{body} FROM tasks AS NEW;
            INSERT OR REPLACE INTO records VALUES('controller_schema','task_headers','{{"version":1}}');
            COMMIT;
        ''')


def page(store, *, cursor='', limit=60, view='active', folder=''):
    if view not in {'active', 'archive', 'trash', 'all'} or not 1 <= limit <= 100:
        raise PolicyError('タスク一覧の表示範囲が正しくありません。')
    args = []
    conditions = ['h.parent_id IS NULL']
    deleted = "coalesce(json_extract(o.body,'$.deleted'),0)"
    archived = "coalesce(json_extract(o.body,'$.archived'),0)"
    pinned = "coalesce(json_extract(o.body,'$.pinned'),0)"
    if view != 'all':
        conditions += [f'{deleted}=1'] if view == 'trash' else [f'{deleted}=0', f'{archived}=' + ('1' if view == 'archive' else '0')]
    if folder:
        conditions.append("json_extract(o.body,'$.folder_id')=?");args.append(folder)
    if cursor:
        try:
            pin, sequence = (int(x) for x in cursor.split(':'))
            if pin not in {0, 1} or sequence < 1: raise ValueError()
        except (ValueError, TypeError):
            raise PolicyError('タスク一覧のページ番号が正しくありません。') from None
        conditions.append(f'({pinned}<? OR ({pinned}=? AND h.sequence<?))');args += [pin, pin, sequence]
    with store.lock:
        rows = store.db.execute(f'''SELECT h.body,h.sequence,o.body AS organization,{pinned} AS pinned
            FROM task_headers h LEFT JOIN records o ON o.kind='task_organization' AND o.id=h.id
            WHERE {' AND '.join(conditions)} ORDER BY pinned DESC,h.sequence DESC LIMIT ?''', (*args, limit+1)).fetchall()
    tasks = []
    for row in rows[:limit]:
        task = json.loads(row['body'])
        task['ui'] = json.loads(row['organization']) if row['organization'] else {
            'id': task['id'], 'revision': 0, 'title': '', 'pinned': False, 'archived': False, 'deleted': False, 'folder_id': None}
        tasks.append(task)
    tail = rows[limit-1] if len(rows) > limit else None
    return {'tasks': tasks, 'next_cursor': f"{tail['pinned']}:{tail['sequence']}" if tail else None}
