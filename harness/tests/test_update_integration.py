"""Opt-in real supervised update integration on a disposable project.

AI/Web judgments are labelled fixtures. Docker verifies ONE immutable regression
in the copied project, not this repository's complete acceptance suite. The
baseline cannot recurse into these integration tests.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import pytest

from policy_harness.capabilities import canonical, digest, sha_file
from policy_harness.cli import _supervise
from policy_harness.models import PolicyError
from policy_harness.policy import PolicyCatalog
from policy_harness.server import source_manifest, validate_restart_marker
from policy_harness.store import Store
from policy_harness.updates import UpdateManager


pytestmark = pytest.mark.skipif(
    os.getenv('HARNESS_RUN_SUPERVISED_UPDATE_TESTS') != '1',
    reason='Explicit opt-in: real supervised Python processes and Docker on an owned disposable project',
)

PROBE_TEXT = 'new controller source consumed by the same task'
PROBE_CODE = '\n\ndef integration_probe():\n    return ' + repr(PROBE_TEXT) + '\n'
IDEA = 'Connect this exact controller candidate to a new process and actual normal file delivery'
TARGET = 'src/policy_harness/decisions.py'


WORKER = r'''
import argparse, asyncio, importlib.util, json, os, sys, traceback
from pathlib import Path
import uvicorn

project = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(project / 'src'))
sys.path.insert(0, str(project))
import policy_harness.decisions as decisions
import policy_harness.server as server_module
from fixture_core import FixtureGateway, FixtureWeb
from policy_harness.capabilities import atomic_json, digest
from policy_harness.cli import _serve
from policy_harness.engine import Engine
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.models import Assessment, Idea, Operation, TaskPlan
from policy_harness.policy import PolicyCatalog
from policy_harness.store import Store
from policy_harness.updates import UpdateManager

context = json.loads(os.environ['HERMES_POLICY_WORKER'])
data = Path(context['data_dir'])
fixture = json.loads((project / 'integration-fixture.json').read_text(encoding='utf-8'))
store = Store(data)
policy = PolicyCatalog(project / 'policy/complete-policy-v3.json')
updates = UpdateManager(data, project, policy, verification_timeout=90, setup_timeout=180)
task_id = fixture['task_id']
task = store.get_task(task_id)
mode = fixture['mode']

class Gateway(FixtureGateway):
    async def generate(self, role, phase, payload, schema):
        if schema is Operation:
            rows = store.operations(task_id)
            done = [row for row in rows if row.get('result')]
            def one(kind, path=None):
                return next((row for row in done if row['operation']['kind'] == kind and
                             (path is None or row['operation']['args'].get('path') == path)), None)
            staged, verified, activated = one('prepare_update'), one('verify_update'), one('activate_update')
            bound = []
            if one('file_write', 'proposal.py') is None:
                kind, args = 'file_write', {'path':'proposal.py', 'text':fixture['candidate_text']}
            elif staged is None:
                kind, args = 'prepare_update', fixture['public_update_args']
                assert 'expected_candidate_sha256' not in args
                if mode == 'normal':
                    candidate = updates.describe(Path(task['workspace']), args)['candidate_sha256']
                    bound = [self.binding(candidate, 'implementation', '/data/candidate_sha256', '/data')]
            elif verified is None:
                kind, args = 'verify_update', {'candidate_id':staged['result']['data']['candidate_id']}
                if mode == 'normal':
                    bound = [self.binding(args['candidate_id'], 'verification', '/data/candidate_id', '/data/test_report')]
            elif activated is None:
                assert verified['result']['status'] == 'succeeded', verified['result']
                kind, args = 'activate_update', {'candidate_id':staged['result']['data']['candidate_id']}
            elif mode == 'rollback' and one('rollback_update') is None:
                assert activated['result']['effect'] == 'unknown', activated['result']
                # Different-candidate recovery must not enter the effect boundary.
                wrong = Operation(kind='rollback_update', args={'candidate_id':'0'*64},
                    purpose='Negative fixture: foreign recovery', expected_result='held',
                    decisions=[{'id':'negative','statement':'Reject foreign candidate','rationale':'Exact recovery ownership'}])
                assert not engine._allowed_with_unknown(store.get_task(task_id), wrong)
                atomic_json(data / 'foreign-rollback-held.json', {'operation_id':activated['operation']['id'],
                    'foreign_candidate':'0'*64, 'allowed':False, 'effect':'none'})
                kind, args = 'rollback_update', {'candidate_id':staged['result']['data']['candidate_id']}
            elif mode == 'rollback' and activated['result']['effect'] == 'unknown':
                assert context['resume'] is not None
                kind, args = 'reconcile', {'operation_id':activated['operation']['id']}
            elif one('file_write', 'answer.txt') is None:
                assert context['resume'] is not None, 'No normal use before supervised replacement'
                actual = decisions.integration_probe() if mode == 'normal' else 'restored original source'
                assert (hasattr(decisions, 'integration_probe')) == (mode == 'normal')
                kind, args = 'file_write', {'path':'answer.txt', 'text':actual}
                if mode == 'normal':
                    bound = [self.binding(staged['result']['data']['candidate_id'], 'use',
                                          '/controller_runtime/candidate_sha256', '/data/sha256')]
            elif one('file_read', 'answer.txt') is None:
                kind, args = 'file_read', {'path':'answer.txt'}
            elif mode == 'normal' and one('knowledge_update') is None:
                idea = self.update_idea()
                kind, args = 'knowledge_update', {'decisions':[{'id':idea['id'], 'disposition':'verified',
                    'reason':'Fixture source, stage, real Docker report, activation and new-process actual file output matched',
                    'candidate_sha256':staged['result']['data']['candidate_id'],
                    'implementation_operation_ids':[staged['operation']['id']],
                    'verification_operation_ids':[verified['operation']['id']],
                    'activation_operation_id':activated['operation']['id'],
                    'actual_use_operation_ids':[one('file_write','answer.txt')['operation']['id']]}]}
            else:
                bad = [row for row in done if row['result']['status'] != 'succeeded' and
                       not (mode == 'rollback' and row['operation']['kind'] == 'activate_update' and row.get('reconciliation_ref'))]
                assert not bad, bad
                kind, args = 'finish', {'summary':'Bounded actual update consumer observed; judgments are fixtures'}
            operation = Operation(kind=kind, args=args, purpose='Deliver the source-bound integration fixture',
                expected_result='Exact observed operation result', improvement_bindings=bound,
                decisions=[{'id':'scope','statement':'Use only the disposable task/project', 'rationale':'Bounded fixture ownership'},
                           {'id':'evidence','statement':'Preserve actual result and incomplete proof', 'rationale':'No fixture-as-semantic claim'}])
            return operation, {'fixture':True,'usage':None,'cost':None}
        value, usage = await super().generate(role, phase, payload, schema)
        if schema is TaskPlan:
            value.phases = ['prepare source','stage','verify','activate','supervised restart','normal use','knowledge resolution','finish']
        if (schema is Assessment and phase == 'pre_assessment' and mode == 'normal' and
                payload.get('operation',{}).get('kind') == 'file_write' and
                payload['operation']['args'].get('path') == 'proposal.py' and
                payload.get('target',{}).get('id') == payload['operation']['id']):
            value.ideas = [Idea(id='update-consumer', target='workflow', proposal=fixture['idea'],
                disposition='adopt', rationale='Test the complete governed update consumer on this isolated project',
                next_operation={'kind':'prepare_update','purpose':'Stage after the actual proposal write'})]
        return value, usage

    def update_idea(self):
        ideas = [item for item in store.records('idea') if item['task_id'] == task_id and item['proposal'] == fixture['idea']]
        assert len(ideas) == 1, ideas
        return ideas[0]

    def binding(self, candidate, role, source, result):
        return {'idea_id':self.update_idea()['id'], 'candidate_sha256':candidate, 'role':role,
                'source_pointer':source, 'source_sha256':candidate, 'result_pointer':result,
                'claim':'Bound exact '+role+' output to this controller candidate; semantic benefit is not inferred'}

gateway = Gateway(policy)
engine = Engine(store, policy, Executor(data), gateway, FixtureWeb(), Knowledge(store), updates=updates)
if mode == 'rollback' and context['resume'] is None:
    original_apply = updates._apply_files
    def partial(candidate, **kwargs):
        first = candidate['changes'][0]
        original_apply(dict(candidate, changes=[first]), **kwargs)
        atomic_json(data / 'injected-partial-activation.json', {'candidate_id':digest(__import__('policy_harness.capabilities',fromlist=['canonical']).canonical(candidate)),
            'path':first['path'], 'observed_sha256':digest((project/first['path']).read_bytes()),
            'expected_after_sha256':first['new']['sha256'], 'fault':'injected before second file', 'effect':'partial'})
        raise OSError('fixture injected interruption after first exact file activation')
    updates._apply_files = partial

class Settings:
    def public(self): return {'fixture':True}

server_module.ROOT = project
server_module.create_runtime = lambda path:(store, Settings(), policy, engine)
errors = []

async def advance(app):
    try:
        handle = engine.running.get(task_id) if context['resume'] else engine.start_task(task_id)
        if handle is not None:
            await asyncio.wait_for(asyncio.shield(handle), timeout=330)
        current = store.get_task(task_id)
        observed = {'pid':os.getpid(), 'generation_id':context['generation_id'], 'task_id':task_id,
            'status':current['status'], 'loaded_probe':decisions.integration_probe() if hasattr(decisions,'integration_probe') else None,
            'runtime':updates.runtime_identity(), 'normal_use':updates.status()['normal_use'],
            'operation_ids':[row['operation']['id'] for row in store.operations(task_id)]}
        atomic_json(data / ('observed-'+context['generation_id']+'.json'), observed)
        if context['resume'] is None:
            assert current['status'] == 'restart_required', current
            assert observed['normal_use']['count'] == 0
            assert app.state.shutting_down
        else:
            assert current['status'] == 'completed', {'task':current,'last_events':store.events(task_id)[-5:]}
            if mode == 'normal':
                assert gateway.update_idea()['status'] == 'verified'
                assert store.records('update_use_review')
            app.state.request_shutdown()
    except BaseException as error:
        errors.append(type(error).__name__)
        atomic_json(data / ('worker-fault-'+context['generation_id']+'.json'),
                    {'type':type(error).__name__, 'message':str(error), 'traceback':traceback.format_exc()})
        traceback.print_exc()
        await engine.stop_task(task_id)
        app.state.request_shutdown()

class ObservedServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets)
        if self.started:
            self.observer = asyncio.create_task(advance(self.config.app))
uvicorn.Server = ObservedServer
args = argparse.Namespace(host='127.0.0.1', port=0, data_dir=str(data),
                          launch_id=context['launch_id'], internal_worker=True)
code = asyncio.run(_serve(args))
sys.exit(1 if errors else code)
'''


def disposable_project(tmp_path: Path, mode: str):
    original = Path(__file__).resolve().parents[1]
    project, data = tmp_path / 'p', tmp_path / 'd'
    (project / 'src').mkdir(parents=True)
    shutil.copytree(original / 'src/policy_harness', project / 'src/policy_harness',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo', '.pytest_cache'))
    for name in ('pyproject.toml', 'uv.lock'):
        shutil.copyfile(original / name, project / name)
    (project / 'policy').mkdir()
    for name in ('complete-policy-v3.json', 'answer-Q18.json'):
        shutil.copyfile(original / 'policy' / name, project / 'policy' / name)
    (project / 'tests').mkdir()
    (project / 'tests/test_fixed_candidate.py').write_text(
        'def test_exact_new_probe():\n'
        '    from policy_harness.decisions import integration_probe\n'
        '    assert integration_probe() == ' + repr(PROBE_TEXT) + '\n', encoding='utf-8')
    # Fixture behavior is frozen outside the writable controller tree/baseline.
    shutil.copyfile(original / 'tests/test_core.py', project / 'fixture_core.py')
    (project / 'worker.py').write_text(WORKER, encoding='utf-8')
    policy = PolicyCatalog(project / 'policy/complete-policy-v3.json')
    store = Store(data)
    expected = PROBE_TEXT if mode == 'normal' else 'restored original source'
    task = store.create_task('Deliver the governed update integration fixture', ['answer.txt contains '+expected])
    store.close()
    old = (project / TARGET).read_bytes()
    candidate = old.decode('utf-8') + PROBE_CODE
    changes = [{'path':TARGET, 'source':'proposal.py', 'expected_sha256':digest(old),
                'sha256':digest(candidate.encode('utf-8'))}]
    if mode == 'rollback':
        changes.append({'path':'src/policy_harness/integration_second.py', 'source':'proposal.py',
                        'expected_sha256':None, 'sha256':digest(candidate.encode('utf-8'))})
    config = {'mode':mode, 'task_id':task['id'], 'candidate_text':candidate, 'idea':IDEA,
              'public_update_args':{'policy_hash':policy.hash, 'rationale':'Explicit bounded update integration fixture', 'changes':changes}}
    (project / 'integration-fixture.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    manager = UpdateManager(data, project, policy)
    manager.enroll_baseline_epoch(reason='Trusted isolated one-regression fixture; not the parent whole-project baseline')
    (tmp_path / 'source-before.json').write_text(json.dumps(source_manifest(project), indent=2), encoding='utf-8')
    return project, data, task, manager, old


@pytest.mark.parametrize('mode', ['normal', 'rollback'])
def test_actual_engine_update_supervised_restart_and_same_task_consumer(tmp_path_factory, mode):
    # Short owned paths also exercise Windows without inheriting pytest's long
    # human-readable test name in every staged verification member.
    tmp_path = tmp_path_factory.mktemp('su')
    project, data, task, setup_manager, old_source = disposable_project(tmp_path, mode)
    args = argparse.Namespace(host='127.0.0.1', port=0, data_dir=str(data), launch_id=uuid4().hex)
    code = _supervise(args, worker_command=lambda context:[sys.executable, '-B', str(project/'worker.py'), str(project)],
                      project_root=project)
    assert code == 0, [json.loads(path.read_text(encoding='utf-8')) for path in data.glob('worker-fault-*.json')]
    directory = data / '.controller-restarts'
    processes = [json.loads(path.read_text(encoding='utf-8')) for path in directory.glob('process-*.json')]
    assert sorted(item['exit_code'] for item in processes) == [0, 75]
    assert sorted(item['status'] for item in processes) == ['exited', 'restart_accepted']
    observed = sorted((json.loads(path.read_text(encoding='utf-8')) for path in data.glob('observed-*.json')),
                      key=lambda item:item['status'] == 'completed')
    assert len(observed) == 2 and observed[0]['pid'] != observed[1]['pid']
    assert observed[0]['runtime']['process_id'] != observed[1]['runtime']['process_id']
    assert {item['task_id'] for item in observed} == {task['id']}
    assert observed[0]['normal_use']['count'] == 0
    store = Store(data)
    try:
        final = store.get_task(task['id'])
        assert final['status'] == 'completed'
        assert final['objective'] == task['objective'] and final['acceptance'] == task['acceptance']
        rows = store.operations(task['id'])
        def one(kind, path=None):
            return next(row for row in rows if row['operation']['kind'] == kind and
                        (path is None or row['operation']['args'].get('path') == path))
        stage = one('prepare_update')
        candidate = stage['result']['data']['candidate_id']
        assert 'expected_candidate_sha256' not in stage['operation']['args']
        assert stage['pre_bundle']['candidate']['candidate_sha256'] == candidate
        assert stage['status'] == 'cycle_complete' and stage['result']['status'] == 'succeeded'
        verification = one('verify_update')['result']['data']
        assert verification['status'] == 'succeeded' and verification['test_report']['tests'] == 1
        assert verification['container_id'] and verification['image_id']
        manager = UpdateManager(data, project, PolicyCatalog(project/'policy/complete-policy-v3.json'))
        with pytest.raises((FileNotFoundError, PolicyError)):
            manager.restart_attestation('0'*64)
        use = one('file_write', 'answer.txt')
        record = manager.runtime_record(task['id'], use['operation']['id'])
        assert record['controller_runtime'] == use['result']['controller_runtime']
        assert use['result']['data']['sha256'] == sha_file(Path(task['workspace'])/'answer.txt')
        assert store.verify_events()
        if mode == 'normal':
            assert observed[1]['loaded_probe'] == PROBE_TEXT
            assert (Path(task['workspace'])/'answer.txt').read_text(encoding='utf-8') == PROBE_TEXT
            assert record['normal_use']['status'] == 'observed'
            idea = next(item for item in store.records('idea') if item['proposal'] == IDEA)
            assert idea['status'] == 'verified'
            proof = idea['evidence']['controller_update']
            assert proof['candidate_sha256'] == candidate
            assert proof['actual_use'][0]['pid'] == observed[1]['pid']
            assert proof['empirical_benefit'] == 'UNVERIFIED_UNTIL_MEASURED_COMPARISON'
            assert store.records('update_use_review')[0]['observations']
            assert manager.status()['normal_use']['reviewed_resolutions']
        else:
            assert observed[1]['loaded_probe'] is None
            assert (project/TARGET).read_bytes() == old_source
            assert not (project/'src/policy_harness/integration_second.py').exists()
            assert one('rollback_update')['result']['data']['status'] == 'rolled_back'
            assert one('activate_update')['reconciliation_ref']
            original = store.records('effect_reconciliation')[0]['original']
            assert original['result']['effect'] == 'unknown'
            assert 'UPDATE_ACTIVATION_INTERRUPTED' in original['result']['stderr']
            activation_record = json.loads((data/'.updates/candidates'/candidate/'activation.json').read_text(encoding='utf-8'))
            assert activation_record['first_fault']['type'] == 'OSError'
            assert 'fixture injected interruption' in activation_record['first_fault']['reason']
            partial = json.loads((data/'injected-partial-activation.json').read_text(encoding='utf-8'))
            assert partial['candidate_id'] == candidate and partial['effect'] == 'partial'
            assert partial['observed_sha256'] == partial['expected_after_sha256']
            assert json.loads((data/'foreign-rollback-held.json').read_text())['allowed'] is False
            assert record['normal_use']['status'] == 'not-established'
    finally:
        store.close()
    # Actual accepted marker still rejects a changed controller source before
    # checking that its legitimate original resumption was already consumed.
    context = json.loads((directory/'supervisor.json').read_text(encoding='utf-8'))
    accepted = directory/context['resume']['name']
    reference = dict(context['resume'], sha256=sha_file(accepted))
    saved = (project/TARGET).read_bytes()
    try:
        (project/TARGET).write_bytes(saved + b'\n# unreviewed source counterexample\n')
        with pytest.raises(PolicyError, match='RESTART_SOURCE_CHANGED'):
            validate_restart_marker(data, context, reference=reference, project_root=project)
    finally:
        (project/TARGET).write_bytes(saved)
    with pytest.raises(PolicyError, match='RESTART_ALREADY_CONSUMED'):
        validate_restart_marker(data, context, reference=reference, project_root=project)
    assert not (directory/'pending.json').exists()
