"""Real Store/API/CLI creation consumers with isolated, non-executing engines.

These cases never run a model, service, executor or original runtime. The one
real Engine constructor checks its existing abandoned-task recovery connection.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from policy_harness.engine import Engine
from policy_harness.models import PolicyError
from policy_harness.store import Store, canonical
from tests.test_source_input_ui import FixturePolicy, FixtureSettings, cli, server


class CreationEngine:
    policy = FixturePolicy()

    def __init__(self, store):
        self.store = store
        self.starts = []

    def start_task(self, identity):
        self.starts.append(identity)
        self.store.update_task(identity, status='running')

    def snapshot(self, identity):
        return {'task': self.store.get_task(identity), 'events': self.store.events(identity),
                'operations': self.store.operations(identity)}


@pytest.fixture
def creation(tmp_path):
    store = Store(tmp_path / 'runtime')
    engine = CreationEngine(store)
    app = server.create_app(store.data_dir, engine=engine, store=store, settings=FixtureSettings())
    with TestClient(app, base_url='http://127.0.0.1:8765',
                    client=('127.0.0.1', 41235), raise_server_exceptions=False) as client:
        assert client.get('/').status_code == 200
        session = client.get('/api/session')
        assert session.status_code == 200
        client.headers['X-CSRF-Token'] = session.json()['csrf_token']
        yield client, store, engine, app
    store.close()


def submission(objective='  Build one result.\nKeep the original input.  ', **changes):
    return {'objective': objective, 'acceptance': [], 'submission_id': uuid4().hex, **changes}


def test_committed_response_loss_retry_recovers_one_task_and_exact_source(creation):
    client, store, engine, _ = creation
    body = submission()
    first = client.post('/api/tasks', json=body)
    assert first.status_code == 201
    identity = first.json()['task']['id']
    # The receiver did not observe the first response; its original ID survives.
    second = client.post('/api/tasks', json=body)
    assert second.status_code == 201
    assert second.json()['task']['id'] == identity
    assert second.json()['submission_id'] == body['submission_id']
    assert engine.starts == [identity]
    assert len(store.list_tasks()) == len(list(store.workspaces.iterdir())) == 1
    raw, _ = store.source_bytes(identity, identity + ':initial')
    assert raw == canonical({'objective': body['objective'], 'acceptance': []}).encode('utf-8')
    assert len([e for e in store.events(identity) if e['stage'] == 'task' and e['status'] == 'created']) == 1


@pytest.mark.parametrize('change', [
    {'objective': 'original '}, {'acceptance': ['keep one condition']},
])
def test_same_submission_rejects_changed_original_payload(creation, change):
    client, store, engine, _ = creation
    body = submission('original')
    created = client.post('/api/tasks', json=body).json()
    rejected = client.post('/api/tasks', json={**body, **change})
    assert rejected.status_code == 409
    assert rejected.json()['detail'].startswith('SUBMISSION_PAYLOAD_CONFLICT:')
    assert len(store.list_tasks()) == len(engine.starts) == 1
    assert store.get_task(created['task']['id'])['source_history'][0]['text'] == 'original'


def test_collision_compares_private_originals_not_equal_redacted_views(creation):
    client, store, engine, _ = creation
    body = submission('synthetic sk-' + 'a' * 24)
    identity = client.post('/api/tasks', json=body).json()['task']['id']
    assert 'sk-' not in store.get_task(identity)['objective']
    response = client.post('/api/tasks', json={**body, 'objective': 'synthetic sk-' + 'b' * 24})
    assert response.status_code == 409
    assert engine.starts == [identity]


def test_intentional_new_identity_can_repeat_same_prompt(creation):
    client, store, engine, _ = creation
    first, second = submission('same prompt'), submission('same prompt')
    ids = [client.post('/api/tasks', json=body).json()['task']['id'] for body in (first, second)]
    assert ids[0] != ids[1]
    assert engine.starts == ids and len(store.list_tasks()) == 2


@pytest.mark.parametrize('status', ['held', 'completed', 'recovery_required', 'unknown', 'stopped'])
def test_creation_retry_never_restarts_existing_task_disposition(creation, status):
    client, store, engine, _ = creation
    body = submission()
    identity = client.post('/api/tasks', json=body).json()['task']['id']
    store.update_task(identity, status=status, state={'unknown_effect': 'original effect'}, final={'original': True})
    before = store.get_task(identity)
    receipt = client.post('/api/tasks', json=body).json()
    assert receipt['task'] == before
    assert store.get_task(identity) == before and engine.starts == [identity]


def test_commit_before_start_claim_is_recovered_once_but_lookup_is_read_only(creation):
    client, store, engine, _ = creation
    body = submission()
    task = store.create_task(body['objective'], [], submission_id=body['submission_id'])
    receipt = client.get('/api/submissions/' + body['submission_id'])
    assert receipt.status_code == 200 and receipt.json()['task']['status'] == 'created'
    assert receipt.json()['start_claimed'] is False and engine.starts == []
    for _ in range(2):
        assert client.post('/api/tasks', json=body).json()['task']['id'] == task['id']
    assert engine.starts == [task['id']]


def test_claim_before_dispatch_crash_enters_real_engine_recovery_without_replay(tmp_path):
    directory = tmp_path / 'runtime'
    store = Store(directory)
    identity = uuid4().hex
    task = store.create_task('single work', [], submission_id=identity)
    assert store.claim_submission_start(identity)
    assert store.get_task(task['id'])['status'] == 'running'
    store.close()
    reopened = Store(directory)
    try:
        engine = Engine(reopened, FixturePolicy(), SimpleNamespace(), SimpleNamespace(),
                        SimpleNamespace(), SimpleNamespace())
        assert reopened.get_task(task['id'])['status'] == 'recovery_required'
        assert engine.running == {} and reopened.operations(task['id']) == []
        assert reopened.claim_submission_start(identity) is False
        assert reopened.create_task('single work', [], submission_id=identity)['id'] == task['id']
        assert any(e['stage'] == 'recovery' and e['detail'].get('automatic_replay') is False
                   for e in reopened.events(task['id']))
    finally:
        reopened.close()


def test_dispatch_failure_retains_first_fault_and_does_not_reinvoke(creation, monkeypatch):
    client, store, engine, _ = creation
    def lost_dispatch(identity):
        engine.starts.append(identity)
        raise RuntimeError('synthetic dispatch boundary')
    monkeypatch.setattr(engine, 'start_task', lost_dispatch)
    body = submission()
    assert client.post('/api/tasks', json=body).status_code == 500
    task = store.list_tasks()[0]
    assert task['status'] == 'recovery_required'
    second = client.post('/api/tasks', json=body)
    assert second.status_code == 201 and second.json()['task']['id'] == task['id']
    assert engine.starts == [task['id']]
    faults = [e for e in store.events(task['id']) if e['status'] == 'dispatch_unknown']
    assert len(faults) == 1 and faults[0]['detail'] == {'error_type': 'RuntimeError', 'automatic_replay': False}


def test_submission_lookup_keeps_protected_records_withheld(creation):
    client, store, engine, app = creation
    body = submission('private task body')
    identity = client.post('/api/tasks', json=body).json()['task']['id']
    class Unavailable(FixtureSettings):
        def redaction_secrets(self):
            return (), ('history:synthetic',)
    app.state.settings = Unavailable()
    result = client.get('/api/submissions/' + body['submission_id'])
    assert result.status_code == 200
    receipt = result.json()
    assert receipt['accepted'] and receipt['records_withheld'] and receipt['action'] == 'create'
    assert receipt['submission_id'] == body['submission_id']
    assert receipt['task'] == {'id': identity, 'status': 'running'}
    assert 'private task body' not in result.text and engine.starts == [identity]


def test_concurrent_store_creation_and_claim_share_one_commit(tmp_path):
    store = Store(tmp_path / 'runtime')
    identity = uuid4().hex
    def create_and_claim(_):
        task = store.create_task('one concurrent request', [], submission_id=identity)
        return task['id'], store.claim_submission_start(identity)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create_and_claim, range(4)))
        assert len({task for task, _ in results}) == 1
        assert sum(claimed for _, claimed in results) == 1
        assert len(store.list_tasks()) == len(list(store.workspaces.iterdir())) == 1
        store.verify_events()
    finally:
        store.close()


def test_cli_response_loss_reuses_durable_id_then_allows_intentional_new_work(creation, tmp_path, monkeypatch, capsys):
    client, store, engine, _ = creation
    client_state = tmp_path / 'client'
    monkeypatch.setenv('HERMES_HARNESS_CLIENT_STATE', str(client_state))
    requests, printed = [], []
    class Bridge:
        def __init__(self, url): pass
        def post(self, path, body):
            requests.append(body)
            response = client.post(path, json=body)
            assert response.status_code == 201
            if len(requests) == 1:
                raise httpx.ReadError('synthetic committed response loss')
            return response.json()
        def close(self): pass
    monkeypatch.setattr(cli, 'Client', Bridge)
    monkeypatch.setattr(cli, '_print', printed.append)
    command = ['run', '  private prompt\n preserve whitespace  ']
    assert cli.main(command) == 1
    records = list(client_state.glob('*.json'))
    assert len(records) == 1
    saved = json.loads(records[0].read_text(encoding='utf-8'))
    assert set(saved) == {'schema', 'id', 'payload_sha256', 'origin_sha256'}
    assert command[1] not in records[0].read_text(encoding='utf-8')
    assert cli.main(command) == 0
    assert requests[0] == requests[1]
    assert len(engine.starts) == len(store.list_tasks()) == 1
    assert printed[0]['task_id'] == engine.starts[0]
    assert list(client_state.glob('*.json')) == []
    assert cli.main(command) == 0
    assert requests[2]['submission_id'] != requests[1]['submission_id']
    assert len(engine.starts) == len(store.list_tasks()) == 2
    assert 'synthetic committed' not in capsys.readouterr().err
