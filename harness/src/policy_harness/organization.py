"""Presentation metadata is separate from task objectives and execution state."""
from uuid import uuid4

from .models import PolicyError, now


class Organization:
    def __init__(self, store):
        self.store = store

    def metadata(self, task_id):
        return self.store.record_get('task_organization', task_id) or {
            'id': task_id, 'revision': 0, 'title': '', 'pinned': False,
            'archived': False, 'deleted': False, 'folder_id': None,
        }

    def folders(self):
        return sorted(self.store.records('task_folder'), key=lambda x: x['name'].casefold())

    def _folder(self, identity):
        if identity is not None and not self.store.record_get('task_folder', identity):
            raise PolicyError('フォルダーが見つかりません。一覧を更新してください。')

    def update(self, task_id, changes, expected_revision):
        allowed = {'title', 'pinned', 'archived', 'deleted', 'folder_id'}
        if not changes or set(changes) - allowed:
            raise PolicyError('変更できないタスク項目です。')
        if 'title' in changes and (not isinstance(changes['title'], str)
                or not changes['title'].strip() or len(changes['title']) > 160):
            raise PolicyError('名前は1〜160文字で入力してください。')
        for key in {'pinned', 'archived', 'deleted'} & changes.keys():
            if type(changes[key]) is not bool:
                raise PolicyError('タスクの状態が正しくありません。')

        def commit():
            task = self.store.get_task(task_id)
            previous = self.metadata(task_id)
            if expected_revision != previous['revision']:
                raise PolicyError('TASK_METADATA_CONFLICT: 一覧が更新されています。もう一度操作してください。')
            if (changes.get('deleted') or changes.get('archived')) and task['status'] in {'running', 'stopping'}:
                raise PolicyError('作業中のタスクです。停止してから整理してください。')
            self._folder(changes.get('folder_id', previous['folder_id']))
            updated = dict(previous, **changes, revision=previous['revision'] + 1, updated_at=now())
            if 'title' in changes:
                updated['title'] = changes['title'].strip()
            self.store.record('task_organization_history', task_id + ':' + str(previous['revision']), previous)
            self.store.record('task_organization', task_id, updated)
            self.store.event(task_id, 'organization', 'recorded', {'changes': changes})
            return updated
        return self.store._transaction(commit)

    def create_folder(self, name, parent_id=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise PolicyError('フォルダー名は1〜80文字で入力してください。')
        def commit():
            self._folder(parent_id)
            item = {'id': uuid4().hex, 'name': name.strip(), 'parent_id': parent_id,
                    'revision': 0, 'created_at': now()}
            self.store.record('task_folder', item['id'], item)
            self.store.notify_task_list()
            return item
        return self.store._transaction(commit)

    def edit_folder(self, identity, name, expected_revision):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise PolicyError('フォルダー名は1〜80文字で入力してください。')
        def commit():
            self._folder(identity)
            previous = self.store.record_get('task_folder', identity)
            if previous['revision'] != expected_revision:
                raise PolicyError('フォルダーが変更されています。一覧を更新してください。')
            self.store.record('task_folder_history', identity + ':' + str(previous['revision']), previous)
            updated = dict(previous, name=name.strip(), revision=previous['revision'] + 1)
            self.store.record('task_folder', identity, updated)
            return updated
        return self.store._transaction(commit)

    def project(self, task):
        return dict(task, ui=self.metadata(task['id']))
