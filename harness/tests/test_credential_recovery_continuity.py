"""Credential transitions through real ASGI/Store; explicit non-executing fixtures.

Only synthetic keys and a reversible cipher are used. These author tests are not
DPAPI, live browser/model, independent-review or whole-task compliance evidence.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import policy_harness

import pytest
from fastapi.testclient import TestClient

from policy_harness.cli import Client
from policy_harness.server import create_app
from policy_harness.settings import SettingsError, SettingsManager
from policy_harness.store import Store
from tests.test_whole_ui_repairs import (CipherFixture, MODEL_KEY, WEB_KEY, NEW_MODEL_KEY,
                                  NEW_WEB_KEY, NODE_UI, break_credentials)


class Engine:
    policy = None

    def __init__(self, store, settings, cipher):
        self.store, self.settings, self.cipher = store, settings, cipher
        self.executor = self.gateway = self
        self.calls = []
        self.break_during = None

    def snapshot(self, identity):
        return {'task': self.store.get_task(identity), 'events': self.store.events(identity)}

    async def health(self):
        return {'status': 'explicit-non-executing-fixture'}

    def after_effect(self, action, identity):
        self.calls.append((action, identity))
        if self.break_during == action:
            break_credentials(self.settings, self.cipher, ('model_api_key',))

    async def start_task(self, identity):
        self.store.update_task(identity, status='running')
        self.after_effect('create', identity)

    async def resume_task(self, identity):
        self.store.update_task(identity, status='running')
        self.after_effect('resume', identity)

    async def stop_task(self, identity):
        self.store.update_task(identity, status='stopped')
        self.after_effect('stop', identity)

    async def submit_instruction(self, identity, text, expected):
        self.store.append_instruction(identity, text, expected)
        self.after_effect('instruction', identity)
        return self.snapshot(identity)

    async def submit_attachment(self, identity, filename, raw, expected):
        self.store.append_instruction(identity, 'Attached source: ' + filename, expected,
                                      attachment={'filename': filename, 'bytes': raw})
        self.after_effect('attachment', identity)
        return self.snapshot(identity)

    async def resolve_approval(self, identity, **values):
        self.store.record('fixture_approval', identity, {'id': identity, **values})
        self.after_effect('approval', identity)
        return {'id': identity, 'status': values['decision']}


@contextmanager
def harness_at(directory, cipher, initialize=False):
    store = Store(directory)
    settings = SettingsManager(directory, protector=cipher)
    if initialize:
        settings.update({'model': 'explicit-fixture', 'model_api_key': MODEL_KEY, 'web_api_key': WEB_KEY})
    engine = Engine(store, settings, cipher)
    app = create_app(directory, engine=engine, settings=settings, store=store)
    try:
        with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41701)) as client:
            assert client.get('/').status_code == 200
            client.headers['X-CSRF-Token'] = client.get('/api/session').json()['csrf_token']
            yield client, store, engine, settings
    finally:
        store.close()


def request_for(action, task):
    path = '/api/tasks/' + task['id']
    if action == 'create':
        return '/api/tasks', {'objective': 'A separately admitted fixture task', 'acceptance': []}
    if action == 'instruction':
        return path + '/instructions', {'text': 'Exact new source', 'expected_source_hash': task['source_hash']}
    if action == 'attachment':
        return path + '/attachments', {'filename': 'source.txt', 'base64': base64.b64encode(b'Exact bytes').decode(), 'expected_source_hash': task['source_hash']}
    if action == 'approval':
        return '/api/approvals/fixture-amendment', {'decision': 'reject', 'expected_hash': 'fixture-hash', 'reason': 'Keep the existing conditions'}
    return path + '/' + action, {}


@pytest.mark.parametrize('name,old,new', [('model_api_key', MODEL_KEY, NEW_MODEL_KEY), ('web_api_key', WEB_KEY, NEW_WEB_KEY)])
def test_history_survives_unreadable_replacement_restart_and_real_recovery(tmp_path, name, old, new):
    cipher = CipherFixture()
    with harness_at(tmp_path, cipher, True) as (client, store, engine, settings):
        task = store.create_task('Keep the exact original ' + old, [])
        store.event(task['id'], 'fixture', 'unknown', {'message': old})
        original = store.get_task(task['id'])
        events = store.events(task['id'])
        response = client.get('/api/tasks/' + task['id'])
        assert response.status_code == 200 and old not in response.text
        break_credentials(settings, cipher, (name,))
        before = settings.path.read_bytes()
        prior_ciphertext = json.loads(before)['secrets'][name]
        assert client.get('/api/tasks/' + task['id']).status_code == 409
        response = client.post('/api/settings', json={name: new})
        assert response.status_code == 200 and old not in response.text and new not in response.text
        assert response.json()['redaction_status'] == 'historical_recovery_required'
        assert settings.secret(name) == new
        document = json.loads(settings.path.read_bytes())
        assert document['version'] == 2 and prior_ciphertext in document['redaction_history'][name]
        backup = tmp_path / 'settings-history' / (hashlib.sha256(before).hexdigest() + '.json')
        assert backup.read_bytes() == before
        assert old.encode() not in settings.path.read_bytes() and new.encode() not in settings.path.read_bytes()
        held = client.get('/api/tasks/' + task['id'])
        assert held.status_code == 409 and '新しい接続キーの保存だけでは解決しません' in held.text
        controls = client.get('/api/recovery/tasks')
        assert controls.status_code == 200 and controls.json()['tasks'] == [{'id': task['id'], 'status': 'created'}]
        assert old not in controls.text and 'objective' not in controls.text
        stopped = client.post('/api/tasks/' + task['id'] + '/stop', json={})
        assert stopped.status_code == 200 and stopped.json()['accepted'] is True
        assert stopped.json()['task'] == {'id': task['id'], 'status': 'stopped'}
        assert engine.calls == [('stop', task['id'])]
        assert store.get_task(task['id'])['source_history'] == original['source_history']
        assert store.events(task['id']) == events
    # New manager, Store and app; no in-memory secret or task cache is reused.
    with harness_at(tmp_path, cipher) as (client, store, engine, settings):
        assert settings.secret(name) == new and engine.calls == []
        assert client.get('/api/tasks/' + task['id']).status_code == 409
        assert client.get('/api/settings').json()['redaction_status'] == 'historical_recovery_required'
        assert client.get('/api/status').status_code == 200
        assert store.get_task(task['id'])['source_history'] == original['source_history']
        # Restoring the same protection context makes the original ciphertext
        # readable. It is not a user override that discards a redaction hold.
        cipher.unreadable.clear()
        restored = client.get('/api/tasks/' + task['id'])
        assert restored.status_code == 200 and old not in restored.text and new not in restored.text
        assert restored.json()['task']['source_hash'] == original['source_hash']
        assert store.events(task['id']) == events
        assert client.get('/api/settings').json()['redaction_status'] == 'ready'
        resumed = client.post('/api/tasks/' + task['id'] + '/resume', json={})
        assert resumed.status_code == 200 and resumed.json()['task']['id'] == task['id']
        assert engine.calls == [('resume', task['id'])]


@pytest.mark.parametrize('name,old,new', [('model_api_key', MODEL_KEY, NEW_MODEL_KEY), ('web_api_key', WEB_KEY, NEW_WEB_KEY)])
def test_readable_v1_rotation_and_removal_retain_masking_and_exact_preimage(tmp_path, name, old, new):
    cipher = CipherFixture()
    settings = SettingsManager(tmp_path, protector=cipher)
    settings.update({name: old})
    legacy = json.loads(settings.path.read_bytes())
    legacy['version'] = 1
    del legacy['redaction_history']
    settings.path.write_text(json.dumps(legacy), encoding='utf-8')
    before = settings.path.read_bytes()
    settings.update({name: new})
    settings.update({name: None})
    recovered = SettingsManager(tmp_path, protector=cipher)
    values, unavailable = recovered.redaction_secrets()
    assert old in values and new in values and unavailable == () and recovered.secret(name) is None
    assert (tmp_path / 'settings-history' / (hashlib.sha256(before).hexdigest() + '.json')).read_bytes() == before
    for path in tmp_path.rglob('*.json'):
        assert old.encode() not in path.read_bytes() and new.encode() not in path.read_bytes()


@pytest.mark.parametrize('name', ['model_api_key', 'web_api_key'])
@pytest.mark.parametrize('action', ['create', 'resume', 'instruction', 'attachment', 'approval'])
def test_known_unreadable_preflight_has_no_mutation_and_historical_hold_persists(tmp_path, name, action):
    cipher = CipherFixture()
    with harness_at(tmp_path, cipher, True) as (client, store, engine, settings):
        task = store.create_task('Original source', [])
        initial = store.get_task(task['id'])
        break_credentials(settings, cipher, (name,))
        path, payload = request_for(action, task)
        for state in ('current-unreadable', 'historical-unreadable'):
            if state == 'historical-unreadable':
                settings.update({name: NEW_MODEL_KEY if name == 'model_api_key' else NEW_WEB_KEY})
            response = client.post(path, json=payload)
            assert response.status_code == 409 and response.json()['kind'] == 'ConfigurationRequired'
            assert len(store.list_tasks()) == 1 and engine.calls == []
            assert store.get_task(task['id']) == initial
            assert store.records('fixture_approval') == []


@pytest.mark.parametrize('action', ['create', 'resume', 'instruction', 'attachment', 'approval'])
def test_after_admission_key_failure_returns_exact_accepted_effect(tmp_path, action):
    cipher = CipherFixture()
    with harness_at(tmp_path, cipher, True) as (client, store, engine, settings):
        task = store.create_task('Original source', [])
        engine.break_during = action
        path, payload = request_for(action, task)
        response = client.post(path, json=payload)
        assert response.status_code == (201 if action == 'create' else 200)
        result = Client._result(response)  # The real CLI HTTP-result consumer.
        assert result['accepted'] is True and result['records_withheld'] is True
        assert result['action'] == action and len(engine.calls) == 1
        if action == 'approval':
            assert result['approval_identity_sha256'] == hashlib.sha256(b'fixture-amendment').hexdigest()
            assert len(store.records('fixture_approval')) == 1
        else:
            assert result['task']['id'] == engine.calls[0][1]
        assert MODEL_KEY not in response.text and WEB_KEY not in response.text
        assert len(store.list_tasks()) == (2 if action == 'create' else 1)


def test_healthy_asgi_and_cli_response_contract_and_stop_preserve_unknown_state(tmp_path):
    with harness_at(tmp_path, CipherFixture(), True) as (client, store, engine, settings):
        response = client.post('/api/tasks', json={'objective': 'Normal task', 'acceptance': ['Exact output']})
        task = Client._result(response)['task']
        assert response.status_code == 201 and task['objective'] == 'Normal task'
        store.update_task(task['id'], state={'unknown_effect': {'original': 'Unresolved fixture evidence'}})
        for action in ('instruction', 'attachment', 'stop', 'resume'):
            current = store.get_task(task['id'])
            path, payload = request_for(action, current)
            response = client.post(path, json=payload)
            assert response.status_code == 200 and 'records_withheld' not in response.json()
            assert Client._result(response)['task']['id'] == task['id']
        assert store.get_task(task['id'])['state']['unknown_effect'] == {'original': 'Unresolved fixture evidence'}


def test_settings_write_failure_preserves_current_and_history_without_replay(tmp_path, monkeypatch):
    import policy_harness.settings as module
    cipher = CipherFixture()
    settings = SettingsManager(tmp_path, protector=cipher)
    settings.update({'model_api_key': MODEL_KEY})
    before = settings.path.read_bytes()
    calls = []
    def failed_replace(source, destination):
        calls.append((str(source), str(destination)))
        raise OSError('Explicit failure before current-file replacement')
    monkeypatch.setattr(module.os, 'replace', failed_replace)
    with pytest.raises(OSError, match='Explicit failure'):
        settings.update({'model_api_key': NEW_MODEL_KEY})
    assert len(calls) == 1 and settings.path.read_bytes() == before
    assert (tmp_path / 'settings-history' / (hashlib.sha256(before).hexdigest() + '.json')).read_bytes() == before


def test_v2_missing_history_cannot_be_treated_as_ready_and_safe_stop_remains_available(tmp_path):
    with harness_at(tmp_path, CipherFixture(), True) as (client, store, engine, settings):
        task = store.create_task('Original private source', [])
        document = json.loads(settings.path.read_bytes())
        del document['redaction_history']
        settings.path.write_text(json.dumps(document), encoding='utf-8')
        before = settings.path.read_bytes()
        with pytest.raises(SettingsError):
            settings.redaction_secrets()
        assert client.get('/api/tasks').status_code == 409
        assert client.post('/api/tasks', json={'objective': 'Do not admit', 'acceptance': []}).status_code == 409
        assert len(store.list_tasks()) == 1 and engine.calls == []
        controls = client.get('/api/recovery/tasks')
        assert controls.status_code == 200 and controls.json()['tasks'][0]['id'] == task['id']
        stopped = client.post('/api/tasks/' + task['id'] + '/stop', json={})
        assert stopped.status_code == 200 and stopped.json()['accepted'] is True
        assert engine.calls == [('stop', task['id'])] and settings.path.read_bytes() == before


@pytest.mark.parametrize('switch_task', [False, True])
def test_full_app_recovery_list_and_stop_request_reach_real_asgi(tmp_path, switch_task):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is unavailable; full-script DOM consumer not observed')
    cipher = CipherFixture()
    with harness_at(tmp_path, cipher, True) as (client, store, engine, settings):
        task = store.create_task('Never show original ' + MODEL_KEY, [])
        break_credentials(settings, cipher, ('model_api_key',))
        responses = {}
        for route in ['/api/session', '/api/tasks', '/api/recovery/tasks', '/api/tasks/' + task['id'] + '?view=conversation']:
            response = client.get(route)
            responses['GET ' + route] = {'status': response.status_code, 'body': response.json()}
        stop_path = '/api/tasks/' + task['id'] + '/stop'
        # The JS emits the exact request; Python consumes it once below.
        responses['POST ' + stop_path] = {'status': 200, 'body': {'accepted': True, 'records_withheld': True,
            'task': {'id': task['id'], 'status': 'stopped'}, 'detail': 'Explicit fixture acknowledgement; actual request is checked below.'}}
        script = NODE_UI.split('(async()=>{', 1)[0].replace('hooks={state,renderSnapshot,artifactLink}', 'hooks={state,renderSnapshot,artifactLink,selectTask}')
        script += r'''(async()=>{await bootReady;const h=context.hooks;
          try{await h.selectTask(input.task_id);}catch(error){if(error.kind!=='ConfigurationRequired')throw error;}
              const target=document.getElementById('loading-message');const button=target.children.find(x=>x.tag==='button'&&typeof x.onclick==='function');
          if(!button)throw new Error('Recovery stop control missing');const stopped=button.onclick();
          if(input.switch_task){h.state.taskId=input.other_task_id;h.state.generation++;}
          await stopped;
          process.stdout.write(JSON.stringify({calls,task_id:h.state.taskId,snapshot:h.state.snapshot,
            loading:!document.getElementById('loading').hidden,notice:document.getElementById('notice').textContent}));
        })().catch(error=>{console.error(error);process.exitCode=1;});'''
        payload = {'script': str(Path(policy_harness.__file__).parent / 'static/app.js'),
                   'i18n': str(Path(policy_harness.__file__).parent / 'static/i18n.js'),
                   'responses': responses, 'task_id': task['id'], 'switch_task': switch_task, 'other_task_id': 'b'*32}
        completed = subprocess.run([node, '-e', script], input=json.dumps(payload), capture_output=True,
                                   text=True, encoding='utf-8', timeout=15)
        (tmp_path / 'node.stdout').write_text(completed.stdout, encoding='utf-8')
        (tmp_path / 'node.stderr').write_text(completed.stderr, encoding='utf-8')
        assert completed.returncode == 0, completed.stderr
        observed = json.loads(completed.stdout)
        assert observed['task_id'] == ('b'*32 if switch_task else task['id'])
        assert observed['snapshot'] is None and observed['loading']
        assert MODEL_KEY not in completed.stdout and engine.calls == []
        posts = [call for call in observed['calls'] if call['options'].get('method') == 'POST']
        assert len(posts) == 1 and posts[0]['path'] == stop_path
        real = client.post(posts[0]['path'], json=json.loads(posts[0]['options']['body']))
        assert real.status_code == 200 and real.json()['accepted'] is True
        assert real.json()['task'] == {'id': task['id'], 'status': 'stopped'}
        assert engine.calls == [('stop', task['id'])]
