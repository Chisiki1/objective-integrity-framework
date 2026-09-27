"""User-selected tool permissions, checked at the actual execution boundary."""
from .models import PolicyError, now
from .store import digest

MODES = {'workspace', 'ask', 'read_only', 'full'}
READ_TOOLS = {'file_read', 'file_list', 'history_read', 'knowledge_read', 'attachment_read',
              'harness_info', 'harness_read'}
WRITE_TOOLS = {'file_write', 'run_python', 'run_tests', 'run_command', 'prepare_program', 'exec',
               'capability_request', 'harness_propose', 'harness_verify', 'harness_apply', 'harness_rollback'}


class PermissionRequired(PolicyError):
    pass


class Access:
    def __init__(self, store):
        self.store = store

    def get(self, task_id):
        return self.store.record_get('task_access', task_id) or {
            'task_id': task_id, 'mode': 'workspace', 'revision': 0}

    def set(self, task_id, mode, expected_revision, *, confirm_full_access=False):
        if mode not in MODES:
            raise PolicyError('アクセス権限が正しくありません。')
        def commit():
            self.store.get_task(task_id)
            previous = self.get(task_id)
            if previous['revision'] != expected_revision:
                raise PolicyError('ACCESS_CONFLICT: 権限が更新されています。画面を更新してください。')
            if mode == 'full' and previous['mode'] != 'full' and confirm_full_access is not True:
                raise PolicyError('FULL_ACCESS_CONFIRMATION_REQUIRED: フルアクセスの許可範囲を確認してください。')
            updated = dict(previous, mode=mode, revision=previous['revision'] + 1, changed_at=now())
            self.store.record('task_access_history', task_id + ':' + str(previous['revision']), previous)
            self.store.record('task_access', task_id, updated)
            self.store.event(task_id, 'permission', 'changed', {'mode': mode, 'message': '次の操作から権限を適用します。'})
            return updated
        return self.store._transaction(commit)

    def check(self, task, tool_name, arguments, operation_id, purpose):
        access = self.get(task['id'])
        if tool_name not in READ_TOOLS | WRITE_TOOLS | {'web_fetch'}:
            raise PolicyError('この操作は許可されたツールではありません。')
        if tool_name == 'run_command' and access['mode'] != 'full':
            raise PolicyError('PC上のコマンド実行にはフルアクセスを選択して確認してください。')
        if tool_name in READ_TOOLS:
            return
        if access['mode'] == 'read_only' and tool_name in WRITE_TOOLS:
            raise PolicyError('読み取り専用です。変更やプログラムの実行には、入力欄で権限を変更してください。')
        if access['mode'] != 'ask':
            return
        binding = {'task_id': task['id'], 'source_hash': task['source_hash'],
                   'access_revision': access['revision'], 'operation_id': operation_id,
                   'tool': tool_name, 'arguments': arguments}
        identity = digest(binding)
        previous = self.store.record_get('tool_approval', identity)
        if previous:
            if previous['status'] == 'approved':
                return
            if previous['status'] == 'rejected':
                raise PolicyError('この操作はユーザーが許可しませんでした。指示の範囲内で別の方法を選んでください。')
        else:
            previous = dict(binding, id=identity, proposal_hash=identity, kind='tool',
                            status='awaiting_user', reason=purpose, created_at=now())
            self.store.record('tool_approval', identity, previous)
            self.store.event(task['id'], 'permission', 'awaiting_user',
                             {'approval_id': identity, 'tool': tool_name, 'purpose': purpose})
        raise PermissionRequired('操作の確認を待っています。許可すると保存済みの続きから進めます。')

    def pending(self, task_id=None):
        result = []
        for row in self.store.records('tool_approval'):
            if row['status'] != 'awaiting_user' or (task_id and row['task_id'] != task_id):
                continue
            task = self.store.get_task(row['task_id'])
            if (task['source_hash'] == row['source_hash']
                    and self.get(task['id'])['revision'] == row['access_revision']):
                result.append(row)
        return result

    def resolve(self, identity, decision, expected_hash, reason):
        def commit():
            row = self.store.record_get('tool_approval', identity)
            if not row or row['proposal_hash'] != expected_hash or decision not in {'approve', 'reject'}:
                raise PolicyError('確認対象の操作が一致しません。')
            task = self.store.get_task(row['task_id'])
            if (row['source_hash'] != task['source_hash']
                    or row['access_revision'] != self.get(task['id'])['revision']):
                raise PolicyError('指示または権限が変わりました。現在の操作を確認してください。')
            status = 'approved' if decision == 'approve' else 'rejected'
            if row['status'] != 'awaiting_user':
                if row['status'] != status:
                    raise PolicyError('この操作への回答はすでに保存されています。')
                return row
            updated = dict(row, status=status, user_reason=reason, resolved_at=now())
            self.store.record('tool_approval', identity, updated)
            self.store.event(task['id'], 'permission', status,
                             {'approval_id': identity, 'tool': row['tool'], 'purpose': row['reason']})
            return updated
        return self.store._transaction(commit)
