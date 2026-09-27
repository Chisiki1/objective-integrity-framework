"""Source-bound controller updates; Engine owns semantic review and authority.

Candidate code runs only in a Docker container. This module never imports,
compiles, evaluates, or executes candidate Python on the host.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import xml.etree.ElementTree as ET
from uuid import uuid4

from .capabilities import atomic_json, canonical, check_node, confined, digest, process_lock, relative_path, sha_file
from .models import PolicyError, now


_TOKENIZER_URL = 'https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken'
_TOKENIZER_SHA256 = '223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7'
_TOKENIZER_CACHE = '/opt/hph-tiktoken'
_VERIFIER_CONTRACT = 'fixed-collection-and-result-v2'
_CHILD_MARKER = b'HPH_PYTEST_OBSERVATION_V2 '
_DRIVER_MARKER = b'HPH_VERIFIER_RESULT_V2 '
_VERIFIER_CEILING = ('Fixed before-source collection, candidate process, complete reports and exact case membership; '
    'candidate and pytest share a Python process. Neither case reports nor this same-container driver prove '
    'hostile code cannot influence reported execution, or prove semantic policy preservation.')

_TOKENIZER_SETUP = r'''import hashlib, os, pathlib, urllib.request
url = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
expected = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
cache = pathlib.Path(os.environ["TIKTOKEN_CACHE_DIR"])
with urllib.request.urlopen(url, timeout=60) as response:
    raw = response.read()
if hashlib.sha256(raw).hexdigest() != expected:
    raise RuntimeError("TOKENIZER_ASSET_HASH_MISMATCH")
cache.mkdir(parents=True, exist_ok=True)
path = cache / hashlib.sha1(url.encode()).hexdigest()
path.write_bytes(raw)
# This check must use the fixed cache, never an unnoticed second download.
import tiktoken, tiktoken.load
def no_download(*args, **kwargs):
    raise RuntimeError("TOKENIZER_OFFLINE_CACHE_MISSING")
tiktoken.load.read_file = no_download
encoding = tiktoken.get_encoding("cl100k_base")
assert encoding.decode(encoding.encode("offline tokenizer 検証")) == "offline tokenizer 検証"
path.chmod(0o444)
cache.chmod(0o555)
print("TOKENIZER_READY", expected)
'''

# Trusted image content, never imported from a candidate tree. Reference collection
# and candidate tests run in separate children; neither child is imported by this
# driver's parent process. Case statements remain subject to the ceiling above.
_VERIFIER_DRIVER = Path(__file__).with_name('verification_driver.py').read_bytes()

_NODE_IMAGE = 'node:24-bookworm-slim'
_IMAGE_BODY = ('COPY --from=hph_node /usr/local/bin/node /usr/local/bin/node\n'
    'RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 libatomic1 && rm -rf /var/lib/apt/lists/* && node --version\n'
    'WORKDIR /baseline\nCOPY requirements.txt /requirements.txt\n'
    'RUN python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes --only-binary=:all: --no-deps -r /requirements.txt\n'
    'ENV TIKTOKEN_CACHE_DIR=/opt/hph-tiktoken\n'
    'COPY tokenizer_setup.py /hph-tokenizer-setup.py\nRUN python /hph-tokenizer-setup.py\n'
    'COPY verifier.py /hph-verifier.py\nRUN chmod 0444 /hph-verifier.py\n'
    'COPY baseline/ /baseline/\nCOPY baseline/ /candidate/\nCOPY baseline/ /reference/\n'
    'ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/candidate/src HOME=/tmp\n')


class UpdateError(PolicyError):
    pass


def _strict_json(raw: bytes) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('nonfinite JSON value: ' + value)
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError('JSON object required')
    return value


def _junit(raw: bytes) -> dict:
    """Preserve valid failed reports too; structural acceptance is a later step."""
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise UpdateError('UPDATE_TEST_REPORT_EXTERNAL_DECLARATION')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise UpdateError('UPDATE_TEST_REPORT_MALFORMED') from error
    suites = [root] if root.tag == 'testsuite' else list(root) if root.tag == 'testsuites' else []
    if not suites or any(s.tag != 'testsuite' for s in suites):
        raise UpdateError('UPDATE_TEST_REPORT_SUITE_SHAPE')
    names = ('tests', 'errors', 'failures', 'skipped')
    totals = {name: 0 for name in names}
    cases, issues = [], []
    for suite in suites:
        if suite.findall('testsuite'):
            issues.append('nested suite')
        try:
            counts = {name: int(suite.attrib[name]) for name in names}
        except (KeyError, ValueError) as error:
            raise UpdateError('UPDATE_TEST_REPORT_COUNTS_INVALID') from error
        if any(value < 0 for value in counts.values()):
            issues.append('negative count')
        found = suite.findall('testcase')
        actual = {'tests': len(found), 'errors': sum(bool(c.findall('error')) for c in found),
                  'failures': sum(bool(c.findall('failure')) for c in found),
                  'skipped': sum(bool(c.findall('skipped')) for c in found)}
        if counts != actual:
            issues.append('suite counts disagree with cases')
        for name in names:
            totals[name] += counts[name]
        for case in found:
            ids = [p.get('value') for p in case.findall('./properties/property') if p.get('name') == 'hph_node_id']
            outcomes = [item for item in case if item.tag in ('error', 'failure', 'skipped')]
            if len(ids) != 1 or not ids[0]:
                issues.append('exact case node identity missing')
            if len(outcomes) > 1:
                issues.append('multiple case outcomes')
            outcome = outcomes[0].tag if outcomes else 'passed'
            detail = '\n'.join((item.get('message', '') + '\n' + ''.join(item.itertext())).strip() for item in outcomes)
            if outcome == 'skipped' and not detail.strip():
                issues.append('skip reason missing')
            cases.append({'node_id': ids[0] if len(ids) == 1 else None, 'name': case.get('name'),
                'classname': case.get('classname'), 'outcome': outcome, 'detail': detail})
    return {**totals, 'cases': cases, 'integrity_errors': issues}


def _driver_observation(stdout: bytes) -> dict:
    lines = [line[len(_DRIVER_MARKER):] for line in stdout.splitlines() if line.startswith(_DRIVER_MARKER)]
    if len(lines) != 1:
        raise UpdateError('UPDATE_DRIVER_REPORT_MISSING_OR_AMBIGUOUS')
    try:
        return _strict_json(base64.b64decode(lines[0], validate=True))
    except (ValueError, TypeError) as error:
        raise UpdateError('UPDATE_DRIVER_REPORT_MALFORMED') from error


def _report_validation(report: dict, driver: dict) -> dict:
    """Compare fixed reference collection, execution observations and full JUnit."""
    issues = list(report.get('integrity_errors', []))
    if driver.get('schema') != 'verifier-driver-v2' or driver.get('driver_sha256') != digest(_VERIFIER_DRIVER):
        issues.append('driver identity mismatch')
    phases = driver.get('phases', {})
    if not isinstance(phases, dict) or set(phases) != {'reference', 'candidate'}:
        return {'valid': False, 'issues': issues + ['both reference and candidate phases required']}
    for name, phase in phases.items():
        if not isinstance(phase, dict):
            issues.append(name + ' phase malformed')
            continue
        seen = phase.get('observation', {})
        if not isinstance(seen, dict):
            issues.append(name + ' observation malformed')
            continue
        if (seen.get('schema') != 'pytest-observation-v2' or seen.get('collection_complete') is not True or
                seen.get('session_finished') is not True):
            issues.append(name + ' incomplete collection/session')
        if any(type(value) is not int or value != 0 for value in (
                phase.get('exit_code'), seen.get('exit_code'), seen.get('returned_exit_code'))):
            issues.append(name + ' unsuccessful exit')
        if seen.get('collection_events') != []:
            issues.append(name + ' collection did not pass completely')
        ids = seen.get('collected_node_ids')
        if (not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x for x in ids) or
                len(set(ids)) != len(ids)):
            issues.append(name + ' missing or duplicate collected node IDs')
    if issues:
        return {'valid': False, 'issues': issues}
    expected = phases['reference']['observation']['collected_node_ids']
    candidate = phases['candidate']['observation']
    actual = candidate['collected_node_ids']
    case_ids = [case.get('node_id') for case in report['cases']]
    if set(expected) != set(actual):
        issues.append('candidate collected set differs from fixed before-source collection')
    if len(set(case_ids)) != len(case_ids) or set(case_ids) != set(expected):
        issues.append('JUnit case set differs from expected or contains duplicates')
    reports = candidate.get('reports')
    if not isinstance(reports, list):
        issues.append('runtime reports missing')
        reports = []
    by_id = {node: {} for node in expected}
    for item in reports:
        if not isinstance(item, dict) or item.get('node_id') not in by_id or item.get('when') not in ('setup', 'call', 'teardown'):
            issues.append('unknown runtime report')
            continue
        bucket = by_id[item['node_id']]
        if item['when'] in bucket:
            issues.append('duplicate runtime phase')
        bucket[item['when']] = item
    for case in report['cases']:
        phase = by_id.get(case.get('node_id'), {})
        setup, call, teardown = (phase.get(name, {}) for name in ('setup', 'call', 'teardown'))
        if teardown.get('outcome') != 'passed':
            issues.append('missing or unsuccessful teardown')
        if case['outcome'] == 'passed':
            if setup.get('outcome') != 'passed' or call.get('outcome') != 'passed':
                issues.append('passing JUnit without complete passing phases')
        elif case['outcome'] == 'skipped':
            skipped = setup if setup.get('outcome') == 'skipped' else call
            if skipped.get('outcome') != 'skipped' or not str(skipped.get('detail', '')).strip():
                issues.append('skipped JUnit without runtime skip reason')
            if setup.get('outcome') == 'skipped' and call:
                issues.append('setup skip with unexpected call')
            if setup.get('outcome') != 'skipped' and setup.get('outcome') != 'passed':
                issues.append('call skip without passing setup')
        else:
            issues.append('failed test outcome')
    if report['tests'] <= report['skipped'] or report['failures'] or report['errors']:
        issues.append('baseline has failures, errors or no passing case')
    return {'valid': not issues, 'issues': sorted(set(issues)),
        'expected_node_ids': expected, 'expected_set_sha256': digest(canonical(sorted(expected))),
        'observed_cases': len(case_ids), 'proof_ceiling': _VERIFIER_CEILING}


def _bytes(data: bytes | None) -> dict:
    if data is None:
        return {'sha256': None, 'bytes': 0, 'base64': None, 'text': None, 'absent': True}
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        text = None
    return {'sha256': digest(data), 'bytes': len(data), 'base64': base64.b64encode(data).decode('ascii'), 'text': text, 'absent': False}


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        check_node(path)
    # Keep sibling staging bounded even when the destination name/path is long.
    temporary = path.with_name('.u-' + uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _tree(root: Path) -> dict[str, bytes]:
    """Read a complete plain-file tree, with no link-following or cache inputs."""
    check_node(root)
    result = {}
    queue = [root]
    while queue:
        parent = queue.pop()
        for path in sorted(parent.iterdir()):
            if path.name in ('__pycache__', '.pytest_cache'):
                continue
            check_node(path)
            if path.is_dir():
                queue.append(path)
            elif path.suffix not in ('.pyc', '.pyo'):
                data = path.read_bytes()
                if sha_file(path) != digest(data):
                    raise UpdateError('UPDATE_SOURCE_CHANGED_DURING_READ')
                result[path.relative_to(root).as_posix()] = data
    return result


def _manifest(files: dict[str, bytes]) -> dict[str, str]:
    return {name: digest(data) for name, data in sorted(files.items())}


def _identity(manifest: dict) -> str:
    return digest(canonical(manifest))


_PROCESS_PID = os.getpid()
_PROCESS_ID = uuid4().hex
_NORMAL_USE_KINDS = frozenset({'file_read', 'file_write', 'file_list', 'exec', 'web_fetch', 'child_integrate',
                             'knowledge_read', 'history_read'})
_READ_ONLY_USE_KINDS = frozenset({'file_read', 'file_list', 'history_read', 'knowledge_read'})
_BINDING_FIELDS = ('expected_source_version', 'activation_candidate_id', 'candidate_sha256',
                   'activation_process_id', 'activation_pid', 'activation_sha256', 'activation_status')


def ordinary_use_result_observed(kind: str, result: dict) -> bool:
    """Known ordinary outcomes, not an authority or a claim of improvement benefit.

    A successful read can honestly have no modification. Only the four explicit
    read operations admit effect=none; unknown/unstarted/bookkeeping never do.
    The protected runtime still checks the actual start, source and new process.
    """
    if kind not in _NORMAL_USE_KINDS:
        return False
    return ((result.get('status') in {'succeeded', 'failed'} and result.get('effect') == 'confirmed') or
            (kind in _READ_ONLY_USE_KINDS and result.get('status') == 'succeeded' and result.get('effect') == 'none'))


def _process_identity() -> tuple[int, str]:
    # Reconstructing a manager is not a process restart. A fork has a new PID.
    global _PROCESS_PID, _PROCESS_ID
    if _PROCESS_PID != os.getpid():
        _PROCESS_PID, _PROCESS_ID = os.getpid(), uuid4().hex
    return _PROCESS_PID, _PROCESS_ID


class UpdateManager:
    """Only the trusted controller constructs this service and invokes its methods.

    Constructor options are deployment configuration, never model operation args.
    The baseline is enrolled once from independently owned project files. A later
    change to those files invalidates dependent verification instead of silently
    replacing the baseline with weaker tests.
    """
    def __init__(self, data_dir: Path, project_root: Path, policy, *, docker_bin='docker', uv_bin='uv',
                 base_image='python:3.12-slim', verification_timeout=1800, setup_timeout=900,
                 memory_bytes=4 * 1024**3, tmpfs_bytes=2 * 1024**3, cpus=2, pids_limit=128):
        for name, value in (('memory_bytes', memory_bytes), ('tmpfs_bytes', tmpfs_bytes), ('pids_limit', pids_limit)):
            if type(value) is not int or value <= 0:
                raise UpdateError('UPDATE_RESOURCE_INVALID: ' + name)
        for name, value in (('cpus', cpus), ('verification_timeout', verification_timeout), ('setup_timeout', setup_timeout)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise UpdateError('UPDATE_RESOURCE_INVALID: ' + name)
        if tmpfs_bytes > memory_bytes:
            raise UpdateError('UPDATE_TMPFS_EXCEEDS_MEMORY')
        self.memory_bytes, self.tmpfs_bytes = memory_bytes, tmpfs_bytes
        self.cpus, self.pids_limit = cpus, pids_limit
        self.data_dir = Path(data_dir).absolute()
        self.project_root = Path(project_root).absolute()
        self.policy = policy
        self.control = self.data_dir / '.updates'
        self.workspaces = self.data_dir / 'workspaces'
        self.control.mkdir(parents=True, exist_ok=True)
        self.workspaces.mkdir(parents=True, exist_ok=True)
        for path in (self.project_root, self.control, self.workspaces):
            for node in reversed([path, *path.parents]):
                check_node(node)
        self.candidates = self.control / 'candidates'
        self.runs = self.control / 'runs'
        self.stages = self.control / 'stages'
        self.baselines = self.control / 'baselines'
        self.runtime_operations = self.control / 'runtime-operations'
        self.use_reviews = self.control / 'use-reviews'
        self.candidates.mkdir(exist_ok=True)
        self.runs.mkdir(exist_ok=True)
        self.stages.mkdir(exist_ok=True)
        self.baselines.mkdir(exist_ok=True)
        self.runtime_operations.mkdir(exist_ok=True)
        self.use_reviews.mkdir(exist_ok=True)
        for path in (self.candidates, self.runs, self.stages, self.baselines, self.runtime_operations, self.use_reviews):
            check_node(path)
        self.docker_bin = shutil.which(docker_bin) or docker_bin
        self.uv_bin = shutil.which(uv_bin) or uv_bin
        self.base_image = base_image
        self.verification_timeout = verification_timeout
        self.setup_timeout = setup_timeout
        self._host = None
        owner = self.control / 'owner.json'
        with process_lock(self.control / 'owner.lock'):
            if not owner.exists():
                atomic_json(owner, {'owner': uuid4().hex}, exclusive=True)
            check_node(owner)
            self.owner = json.loads(owner.read_text())['owner']
        if not re.fullmatch('[0-9a-f]{32}', self.owner):
            raise UpdateError('UPDATE_OWNER_INVALID')
        self._loaded_manifest = _manifest(self._code())
        self._loaded_source = _identity(self._loaded_manifest)
        pid, process_id = _process_identity()
        self._runtime = {'loaded_source_version': self._loaded_source, 'pid': pid,
                         'process_id': process_id, 'captured_at': now()}

    def resource_envelope(self) -> dict:
        """Trusted deployment limits, never inferred from model operation arguments."""
        return {'memory_bytes': self.memory_bytes, 'memory_swap_bytes': self.memory_bytes,
            'tmpfs_bytes': self.tmpfs_bytes, 'cpus': self.cpus, 'pids_limit': self.pids_limit,
            'verification_timeout_seconds': self.verification_timeout,
            'network': 'none', 'read_only': True, 'uid_gid': '65534:65534',
            'tmpfs_options': 'rw,noexec,nosuid,nodev', 'cap_drop': 'ALL', 'no_new_privileges': True}

    def runtime_identity(self) -> dict:
        """Construction-time source cut and actual process; never refresh from disk."""
        if self._runtime['pid'] != os.getpid():
            raise UpdateError('UPDATE_MANAGER_CROSSED_PROCESS_BOUNDARY')
        return dict(self._runtime)

    def source_status(self) -> dict:
        """Compare the current files with the original loaded cut, never rebase it."""
        self.runtime_identity()
        return {'restart_required': _manifest(self._code()) != self._loaded_manifest}

    def _active_binding(self) -> dict:
        path = self.control / 'active.json'
        if not path.exists():
            return {}
        check_node(path)
        active = json.loads(path.read_text(encoding='utf-8'))
        if active.get('owner') != self.owner:
            raise UpdateError('UPDATE_ACTIVE_OWNER_MISMATCH')
        identity = active.get('candidate_id')
        candidate = self._candidate(identity)['candidate']
        activation = self._activation(identity)
        if not activation or activation.get('effect') != 'confirmed' or activation.get('status') not in ('activated', 'rolled_back'):
            return {}
        # The active pointer cannot silently describe a different activation cut.
        if any(active.get(key) != value for key, value in activation.items()):
            raise UpdateError('UPDATE_ACTIVE_ACTIVATION_MISMATCH')
        reverse = activation['status'] == 'rolled_back'
        actor = activation.get('rollback_runtime' if reverse else 'activator_runtime', {})
        return {'expected_source_version': candidate['before_version' if reverse else 'after_version'],
                'activation_candidate_id': identity, 'candidate_sha256': _identity(candidate),
                'activation_process_id': actor.get('process_id'), 'activation_pid': actor.get('pid'),
                'activation_sha256': _identity(activation), 'activation_status': activation['status']}

    @staticmethod
    def _runtime_record_id(task_id: str, operation_id: str) -> str:
        if not isinstance(task_id, str) or not task_id or not isinstance(operation_id, str) or not operation_id:
            raise UpdateError('UPDATE_RUNTIME_OPERATION_ID_REQUIRED')
        return _identity({'task_id': task_id, 'operation_id': operation_id})

    def runtime_record(self, task_id: str, operation_id: str) -> dict | None:
        """Protected exact record for Knowledge, including records from older processes."""
        identity = self._runtime_record_id(task_id, operation_id)
        path = confined(self.runtime_operations, identity + '.json', allow_missing=True)
        if not path.exists():
            return None
        check_node(path)
        row = json.loads(path.read_text(encoding='utf-8'))
        checksum = row.pop('record_sha256', None)
        if (row.get('owner') != self.owner or row.get('record_id') != identity or row.get('task_id') != task_id or
                row.get('operation_id') != operation_id or checksum != _identity(row) or
                row.get('operation_sha256') != _identity(row.get('operation', {}))):
            raise UpdateError('UPDATE_RUNTIME_RECORD_IDENTITY_CHANGED')
        row['record_sha256'] = checksum
        runtime = row.get('controller_runtime', {})
        if runtime and any(runtime.get(key) != row.get(key) for key in ('record_id', 'operation_sha256', 'result_sha256')):
            raise UpdateError('UPDATE_RUNTIME_RESULT_BINDING_CHANGED')
        return row

    def _save_runtime_record(self, row: dict) -> None:
        clean = {key: value for key, value in row.items() if key != 'record_sha256'}
        atomic_json(self.runtime_operations / (clean['record_id'] + '.json'),
                    {**clean, 'record_sha256': _identity(clean)})

    def before_execution(self, task_id: str, operation: dict) -> dict:
        """Called after review/permit consumption, immediately before real dispatch."""
        runtime = self.runtime_identity()
        operation_id = operation.get('id')
        identity = self._runtime_record_id(task_id, operation_id)
        operation_hash = _identity(operation)
        with process_lock(self.control / 'runtime.lock'):
            existing = self.runtime_record(task_id, operation_id)
            if existing is not None:
                raise UpdateError('UPDATE_RUNTIME_OPERATION_ALREADY_STARTED: reconcile; do not replay')
            binding = self._active_binding()
            source_stable = _manifest(self._code()) == self._loaded_manifest
            if operation.get('kind') in _NORMAL_USE_KINDS and not source_stable:
                raise UpdateError('UPDATE_LOADED_SOURCE_DIFFERS_FROM_DISK: restart required before ordinary execution')
            runtime.update(binding)
            runtime.update(record_id=identity, operation_sha256=operation_hash)
            row = {'schema': 'controller-runtime-operation-v1', 'owner': self.owner, 'record_id': identity,
                   'task_id': task_id, 'operation_id': operation_id, 'operation_sha256': operation_hash,
                   'operation': operation, 'status': 'started', 'started_at': now(),
                   'runtime_before': runtime, 'source_stable_before': source_stable,
                   'result_sha256': None, 'controller_runtime': {}}
            self._save_runtime_record(row)
            return runtime

    def after_actual_result(self, task_id: str, operation: dict, result: dict) -> dict:
        """Stamp a dispatch result. Hash omits the controller_runtime KEY entirely.

        Results never provide process, source, activation identity or success flags
        for this service. Those values come from the recorded start and manager.
        """
        operation_id = operation.get('id')
        raw = {key: value for key, value in result.items() if key != 'controller_runtime'}
        result_hash = _identity(raw)
        with process_lock(self.control / 'runtime.lock'):
            row = self.runtime_record(task_id, operation_id)
            if row is None or row['operation_sha256'] != _identity(operation) or result.get('operation_id') != operation_id:
                raise UpdateError('UPDATE_RUNTIME_START_OR_RESULT_MISMATCH')
            if row['status'] == 'recorded':
                if row['result_sha256'] != result_hash:
                    raise UpdateError('UPDATE_RUNTIME_RESULT_CHANGED')
                return dict(row['controller_runtime'])
            current = self.runtime_identity()
            before = row['runtime_before']
            if any(before.get(key) != current.get(key) for key in ('loaded_source_version', 'pid', 'process_id')):
                raise UpdateError('UPDATE_RUNTIME_RESULT_FROM_DIFFERENT_PROCESS')
            binding = self._active_binding()
            runtime = {**before, 'result_sha256': result_hash}
            if operation.get('kind') in {'activate_update', 'rollback_update', 'harness_apply', 'harness_rollback'}:
                runtime = {key: value for key, value in runtime.items() if key not in _BINDING_FIELDS}
                if (result.get('status') == 'succeeded' and result.get('effect') == 'confirmed' and
                        result.get('data', {}).get('candidate_id') == binding.get('activation_candidate_id')):
                    runtime.update(binding)
            stable = row['source_stable_before'] and _manifest(self._code()) == self._loaded_manifest
            ordinary = operation.get('kind') in _NORMAL_USE_KINDS
            known = ordinary_use_result_observed(operation.get('kind'), result)
            same_binding = bool(binding) and all(before.get(key) == binding.get(key) for key in _BINDING_FIELDS)
            # PID inequality is deliberately conservative if the OS reuses a PID.
            restarted = (isinstance(binding.get('activation_pid'), int) and
                         binding.get('activation_pid') != current['pid'] and
                         isinstance(binding.get('activation_process_id'), str) and
                         binding.get('activation_process_id') != current['process_id'])
            observed = bool(ordinary and known and stable and same_binding and restarted and
                            binding.get('activation_status') == 'activated' and
                            current['loaded_source_version'] == binding.get('expected_source_version'))
            runtime['normal_use'] = 'observed' if observed else 'not-established'
            row.update(status='recorded', finished_at=now(), result_sha256=result_hash, controller_runtime=runtime,
                       result_status=result.get('status'), effect=result.get('effect'),
                       result_observation={'stdout_sha256': digest(str(raw.get('stdout', '')).encode('utf-8')),
                           'stderr_sha256': digest(str(raw.get('stderr', '')).encode('utf-8')),
                           'data_sha256': _identity(raw.get('data', {})), 'artifacts_sha256': _identity(raw.get('artifacts', [])),
                           'elapsed_seconds': raw.get('elapsed_seconds'), 'exit_code': raw.get('exit_code')},
                       normal_use={'status': runtime['normal_use'], 'ordinary': ordinary, 'known_effect': known,
                           'source_stable': stable, 'same_activation': same_binding, 'new_process': bool(restarted),
                           'empirical_benefit': 'UNVERIFIED_UNTIL_MEASURED_COMPARISON'})
            self._save_runtime_record(row)
            return runtime

    def consume_use_review(self, task_id: str, operation: dict, result: dict) -> dict:
        """Record a completed, reviewed Knowledge resolution without inferring benefit.

        The trusted Engine calls this only after the operation's post/learning
        reviews. A matching actual runtime result and existing observed use are
        mandatory; caller-provided effect/benefit claims have no authority here.
        """
        raw = {key: value for key, value in result.items() if key != 'controller_runtime'}
        row = self.runtime_record(task_id, operation.get('id'))
        if (row is None or row['status'] != 'recorded' or operation.get('kind') != 'knowledge_update' or
                row['operation_sha256'] != _identity(operation) or row['result_sha256'] != _identity(raw) or
                result.get('status') != 'succeeded' or result.get('effect') != 'confirmed'):
            raise UpdateError('UPDATE_USE_REVIEW_ACTUAL_RESULT_REQUIRED')
        resolved = set(result.get('data', {}).get('resolved_idea_ids', []))
        observations = []
        for decision in operation.get('args', {}).get('decisions', []):
            if decision.get('id') not in resolved or decision.get('disposition') != 'verified':
                continue
            for operation_id in decision.get('actual_use_operation_ids', []):
                use = self.runtime_record(task_id, operation_id)
                if not use or use.get('normal_use', {}).get('status') != 'observed':
                    continue
                runtime = use['controller_runtime']
                if decision.get('candidate_sha256') != runtime.get('candidate_sha256'):
                    continue
                observations.append({'idea_id': decision['id'], 'candidate_id': runtime['activation_candidate_id'],
                                     'use_operation_id': operation_id, 'use_record_sha256': use['record_sha256']})
        record = {'schema': 'controller-use-review-v1', 'owner': self.owner, 'task_id': task_id,
                  'operation_id': operation['id'], 'operation_sha256': row['operation_sha256'],
                  'result_sha256': row['result_sha256'], 'observations': observations,
                  'empirical_benefit': 'UNVERIFIED_UNTIL_MEASURED_COMPARISON'}
        with process_lock(self.control / 'runtime.lock'):
            path = self.use_reviews / (row['record_id'] + '.json')
            if path.exists():
                check_node(path)
                prior = json.loads(path.read_text(encoding='utf-8'))
                if prior != record:
                    raise UpdateError('UPDATE_USE_REVIEW_CHANGED')
                return prior
            atomic_json(path, record, exclusive=True)
        return record

    def review_ordinary_use(self, task_id, operation, result, call_id, source_hash, assessment):
        """Practical-loop judgment, bound to an actual post-restart observation.

        Uses the existing protected review store without another model call.
        A model's helpful judgment is not a measured before/after comparison.
        """
        row = self.runtime_record(task_id, operation['id'])
        raw = {k: v for k, v in result.items() if k != 'controller_runtime'}
        if (not row or row['status'] != 'recorded' or row['operation_sha256'] != _identity(operation)
                or row['result_sha256'] != _identity(raw)
                or row.get('normal_use', {}).get('status') != 'observed'):
            raise UpdateError('UPDATE_REVIEW_REQUIRES_OBSERVED_ORDINARY_USE')
        if assessment['judgment'] == 'helpful' and result.get('status') != 'succeeded':
            raise UpdateError('UPDATE_FAILED_USE_CANNOT_ESTABLISH_BENEFIT')
        runtime = row['controller_runtime']
        record = {'schema': 'controller-use-review-v1', 'owner': self.owner, 'task_id': task_id,
            'call_id': call_id, 'source_hash': source_hash, 'assessment': assessment,
            'observations': [{'candidate_id': runtime['activation_candidate_id'],
                              'use_operation_id': operation['id'], 'use_record_sha256': row['record_sha256']}],
            'empirical_benefit': 'MODEL_JUDGMENT_OF_OBSERVED_USE; NOT_A_CONTROLLED_COMPARISON'}
        with process_lock(self.control / 'runtime.lock'):
            path = self.use_reviews / (_identity({'task': task_id, 'call': call_id, 'use': operation['id']}) + '.json')
            if path.exists():
                check_node(path)
                if json.loads(path.read_text(encoding='utf-8')) != record:
                    raise UpdateError('UPDATE_USE_REVIEW_CHANGED')
            else:
                atomic_json(path, record, exclusive=True)
        return record

    def _policy_current(self):
        verify = getattr(self.policy, 'verify_current', None)
        if verify is not None:
            verify()

    def _workspace(self, workspace: Path) -> Path:
        value = Path(workspace).absolute()
        if value.parent != self.workspaces:
            raise UpdateError('UPDATE_WORKSPACE_NOT_OWNED')
        for path in reversed([value, *value.parents]):
            check_node(path)
        if not value.is_dir():
            raise UpdateError('UPDATE_WORKSPACE_NOT_DIRECTORY')
        return value

    @staticmethod
    def _target(value: str) -> str:
        value = relative_path(value)
        parts = value.split('/')
        if parts[:2] != ['src', 'policy_harness'] or len(parts) < 3:
            raise UpdateError('UPDATE_TARGET_OUTSIDE_CONTROLLER_SOURCE')
        if any(part.startswith('.') or part == '__pycache__' for part in parts):
            raise UpdateError('UPDATE_TARGET_ALIAS_OR_CACHE')
        if parts[2] != 'static' and not value.endswith('.py'):
            raise UpdateError('UPDATE_TARGET_NOT_PYTHON_OR_STATIC')
        return value

    def _code(self) -> dict[str, bytes]:
        root = confined(self.project_root, 'src/policy_harness')
        return {'src/policy_harness/' + name: data for name, data in _tree(root).items()}

    def _baseline_files(self) -> dict[str, bytes]:
        files = {}
        for name in ('pyproject.toml', 'uv.lock'):
            files[name] = confined(self.project_root, name).read_bytes()
        tests = _tree(confined(self.project_root, 'tests'))
        if not any(name.endswith('.py') for name in tests):
            raise UpdateError('UPDATE_BASELINE_TESTS_MISSING')
        files.update({'tests/' + name: data for name, data in tests.items()})
        # Product policy is evidence inside the test container, not controller data.
        for name, data in _tree(confined(self.project_root, 'policy')).items():
            files['policy/' + name] = data
        return files

    def enroll_baseline_epoch(self, *, reason: str) -> dict:
        """Trusted root maintenance only; never exposed as model operation args.

        The root calls this after independent baseline changes/freeze. Every
        previous epoch and snapshot remains available; old candidates become stale.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise UpdateError('UPDATE_BASELINE_EPOCH_REASON_REQUIRED')
        self._policy_current()
        current = self._baseline_files()
        manifest = _manifest(current)
        identity = _identity(manifest)
        path = self.control / 'baseline.json'
        with process_lock(self.control / 'baseline.lock'):
            previous = None
            if path.exists():
                check_node(path)
                previous = json.loads(path.read_text(encoding='utf-8'))
            directory = self.baselines / identity
            if not directory.exists():
                temporary = self.baselines / ('.staging-' + uuid4().hex)
                temporary.mkdir()
                for name, data in current.items():
                    _write(confined(temporary, name, allow_missing=True), data)
                os.replace(temporary, directory)
            if _manifest(_tree(directory)) != manifest:
                raise UpdateError('UPDATE_BASELINE_SNAPSHOT_CHANGED')
            if _manifest(self._baseline_files()) != manifest:
                raise UpdateError('UPDATE_BASELINE_CHANGED_DURING_ENROLLMENT')
            row = {'sha256': identity, 'manifest': manifest, 'policy_hash': self.policy.hash,
                'epoch_id': uuid4().hex, 'previous_epoch_id': previous.get('epoch_id') if previous else None,
                'reason': reason, 'enrolled_at': now()}
            atomic_json(self.baselines / ('epoch-' + row['epoch_id'] + '.json'), row, exclusive=True)
            atomic_json(path, row)
            return row

    def _baseline(self) -> dict:
        self._policy_current()
        path = self.control / 'baseline.json'
        if not path.exists():
            self.enroll_baseline_epoch(reason='Initial enrollment of independently owned root baseline')
        check_node(path)
        row = json.loads(path.read_text(encoding='utf-8'))
        manifest = _manifest(self._baseline_files())
        identity = _identity(manifest)
        if row.get('sha256') != identity or row.get('manifest') != manifest or row.get('policy_hash') != self.policy.hash:
            raise UpdateError('UPDATE_TRUSTED_BASELINE_CHANGED: independent baseline maintenance is required')
        if _manifest(_tree(confined(self.baselines, identity))) != manifest:
            raise UpdateError('UPDATE_BASELINE_SNAPSHOT_CHANGED')
        return row

    def _candidate_path(self, candidate_id: str) -> Path:
        if not isinstance(candidate_id, str) or not re.fullmatch('[0-9a-f]{64}', candidate_id):
            raise UpdateError('UPDATE_CANDIDATE_ID_INVALID')
        return confined(self.candidates, candidate_id)

    def _candidate(self, candidate_id: str) -> dict:
        directory = self._candidate_path(candidate_id)
        path = confined(directory, 'candidate.json')
        row = json.loads(path.read_text(encoding='utf-8'))
        if row.get('owner') != self.owner or _identity(row.get('candidate', {})) != candidate_id:
            raise UpdateError('UPDATE_CANDIDATE_IDENTITY_CHANGED')
        candidate = row['candidate']
        if _manifest(_tree(confined(directory, 'tree'))) != candidate['after_manifest']:
            raise UpdateError('UPDATE_CANDIDATE_BYTES_CHANGED')
        if _manifest(_tree(confined(directory, 'before'))) != candidate['before_manifest']:
            raise UpdateError('UPDATE_PREIMAGE_BYTES_CHANGED')
        return row

    def describe(self, workspace: Path, args: dict) -> dict:
        workspace = self._workspace(workspace)
        if not isinstance(args, dict):
            raise UpdateError('UPDATE_ARGS_INVALID')
        if set(args) == {'candidate_id'}:
            candidate_id = args['candidate_id']
            row = self._candidate(candidate_id)
            if row['candidate']['workspace'] != str(workspace):
                raise UpdateError('UPDATE_CANDIDATE_WORKSPACE_MISMATCH')
            return {**row['candidate'], 'candidate_id': candidate_id,
                    'verification': self._verification(candidate_id), 'activation': self._activation(candidate_id)}
        if set(args) - {'policy_hash', 'rationale', 'changes', 'expected_candidate_sha256'}:
            raise UpdateError('UPDATE_ARGS_UNSUPPORTED: no tests, command, policy, approval or receipt overrides')
        if args.get('policy_hash') != self.policy.hash or not isinstance(args.get('rationale'), str) or not args['rationale'].strip():
            raise UpdateError('UPDATE_SOURCE_AND_RATIONALE_REQUIRED')
        self._policy_current()
        baseline = self._baseline()
        before = self._code()
        after = dict(before)
        changes = args.get('changes')
        if not isinstance(changes, list) or not changes:
            raise UpdateError('UPDATE_CHANGES_REQUIRED')
        seen, descriptions = set(), []
        for change in changes:
            if not isinstance(change, dict) or set(change) != {'path', 'source', 'expected_sha256', 'sha256'}:
                raise UpdateError('UPDATE_CHANGE_SCHEMA_INVALID')
            target = self._target(change['path'])
            alias = target.casefold()
            if alias in seen:
                raise UpdateError('UPDATE_DUPLICATE_TARGET')
            seen.add(alias)
            # Windows spelling aliases may otherwise target an existing file twice.
            if any(name.casefold() == alias and name != target for name in before):
                raise UpdateError('UPDATE_TARGET_CASE_ALIAS')
            old = before.get(target)
            if change['expected_sha256'] != (digest(old) if old is not None else None):
                raise UpdateError('UPDATE_PREIMAGE_CONFLICT: ' + target)
            source = change['source']
            new = None if source is None else confined(workspace, source).read_bytes()
            if change['sha256'] != (digest(new) if new is not None else None):
                raise UpdateError('UPDATE_WORKSPACE_SOURCE_CHANGED: ' + target)
            if new == old:
                raise UpdateError('UPDATE_NO_CHANGE: ' + target)
            if new is None:
                after.pop(target, None)
            else:
                after[target] = new
            descriptions.append({'path': target, 'source': source, 'old': _bytes(old), 'new': _bytes(new)})
        candidate = {'schema': 'controller-update-v1', 'workspace': str(workspace), 'policy_hash': self.policy.hash,
            'source_conditions': ['R15', 'N-06'], 'rationale': args['rationale'],
            'baseline_sha256': baseline['sha256'], 'baseline_epoch_id': baseline['epoch_id'], 'baseline_manifest': baseline['manifest'],
            'before_manifest': _manifest(before), 'after_manifest': _manifest(after),
            'before_version': _identity(_manifest(before)), 'after_version': _identity(_manifest(after)),
            'changes': sorted(descriptions, key=lambda item: item['path']),
            'semantic_permission': 'Engine parent/reviewer gates required; no approval is created by this description',
            'effect_measurement': 'pending-next-use'}
        identity = _identity(candidate)
        expected = args.get('expected_candidate_sha256')
        if expected is not None and expected != identity:
            raise UpdateError('UPDATE_REVIEWED_CANDIDATE_CHANGED')
        return {**candidate, 'candidate_sha256': identity}

    def stage(self, workspace: Path, args: dict) -> dict:
        if not isinstance(args, dict) or 'expected_candidate_sha256' not in args:
            raise UpdateError('UPDATE_REVIEWED_DESCRIPTION_HASH_REQUIRED')
        with process_lock(self.control / 'update.lock'):
            description = self.describe(workspace, args)
            identity = description.pop('candidate_sha256')
            directory = self.candidates / identity
            if directory.exists():
                self._candidate(identity)
                return {'candidate_id': identity, 'staged': True, 'reused': True, 'executed': False}
            before = self._code()
            if _manifest(before) != description['before_manifest']:
                raise UpdateError('UPDATE_SOURCE_CHANGED_BEFORE_STAGE')
            after = dict(before)
            for change in description['changes']:
                if change['new']['absent']:
                    after.pop(change['path'], None)
                else:
                    after[change['path']] = base64.b64decode(change['new']['base64'], validate=True)
            temporary = self.candidates / ('.staging-' + uuid4().hex)
            journal = {'owner': self.owner, 'candidate_id': identity, 'workspace': str(workspace),
                'phase': 'staging', 'started_at': now(), 'temporary_directory': temporary.name,
                'effect': 'unknown', 'candidate_committed': False}
            atomic_json(self.stages / (identity + '.json'), journal)
            temporary.mkdir()
            for branch, files in (('before', before), ('tree', after)):
                root = temporary / branch
                root.mkdir()
                for name, data in files.items():
                    _write(confined(root, name, allow_missing=True), data)
            atomic_json(temporary / 'candidate.json', {'owner': self.owner, 'candidate': description, 'created_at': now()}, exclusive=True)
            os.replace(temporary, directory)
            self._candidate(identity)
            journal.update(phase='staged', effect='confirmed', candidate_committed=True, finished_at=now())
            atomic_json(self.stages / (identity + '.json'), journal)
            return {'candidate_id': identity, 'candidate_sha256': identity, 'staged': True,
                    'after_version': description['after_version'], 'executed': False, 'effect_measurement': 'pending-next-use'}

    @staticmethod
    def _environment() -> dict:
        # No model/search keys or arbitrary agent environment reaches a subprocess.
        names = ('PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'HOME',
                 'LOCALAPPDATA', 'APPDATA', 'ProgramData', 'COMSPEC', 'PATHEXT')
        return {name: os.environ[name] for name in names if name in os.environ}

    async def _process(self, executable: str, args: list[str], *, cwd: Path, timeout: float,
                       input_bytes: bytes | None = None) -> tuple[int, bytes, bytes]:
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        process = await asyncio.create_subprocess_exec(executable, *args, cwd=cwd, env=self._environment(),
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options)
        chunks = [bytearray(), bytearray()]
        async def read(stream, index):
            while block := await stream.read(65536):
                chunks[index].extend(block)
                if len(chunks[index]) > 8 * 1024 * 1024:
                    raise UpdateError('UPDATE_PROCESS_OUTPUT_LIMIT')
        async def communicate():
            if input_bytes is not None:
                process.stdin.write(input_bytes)
                await process.stdin.drain()
                process.stdin.close()
            await asyncio.gather(read(process.stdout, 0), read(process.stderr, 1))
            return await process.wait()
        try:
            code = await asyncio.wait_for(communicate(), timeout)
            return code, bytes(chunks[0]), bytes(chunks[1])
        except BaseException as error:
            if process.returncode is None:
                process.kill()
            await process.wait()
            # The Docker target may outlive its client. Its owned identity is
            # reconciled separately; retain the first client fault and bytes.
            error.update_streams = {'stdout': bytes(chunks[0]), 'stderr': bytes(chunks[1])}
            raise

    async def _docker(self, args: list[str], *, timeout=60, input_bytes=None):
        if self._host is None:
            code, out, _ = await self._process(self.docker_bin, ['context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}'], cwd=self.control, timeout=60)
            if code:
                raise UpdateError('UPDATE_DOCKER_CONTEXT_UNAVAILABLE')
            self._host = json.loads(out)
            if not isinstance(self._host, str) or not self._host.startswith(('npipe://', 'unix://')):
                raise UpdateError('UPDATE_LOCAL_DOCKER_REQUIRED')
        return await self._process(self.docker_bin, ['--host', self._host, *args], cwd=self.control,
                                   timeout=timeout, input_bytes=input_bytes)

    async def _image(self, reference: str) -> dict:
        code, out, _ = await self._docker(['image', 'inspect', reference])
        if code:
            raise UpdateError('UPDATE_IMAGE_UNAVAILABLE')
        info = json.loads(out)[0]
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', info.get('Id', '')) or info.get('Os') != 'linux':
            raise UpdateError('UPDATE_IMAGE_ID_OR_PLATFORM_INVALID')
        return info

    async def _prepare(self, baseline: dict) -> dict:
        recipe_hash = digest(canonical({'dockerfile_body': _IMAGE_BODY,
            'tokenizer_setup_sha256': digest(_TOKENIZER_SETUP.encode()), 'driver_sha256': digest(_VERIFIER_DRIVER),
            'contract': _VERIFIER_CONTRACT, 'node_image': _NODE_IMAGE}))
        path = self.control / 'environment.json'
        if path.exists():
            check_node(path)
            saved = json.loads(path.read_text())
            if saved.get('baseline_sha256') == baseline['sha256'] and saved.get('recipe_sha256') == recipe_hash:
                image = await self._image(saved['image_id'])
                if image.get('Config', {}).get('Labels', {}).get('policy-harness.update-baseline') != baseline['sha256']:
                    raise UpdateError('UPDATE_IMAGE_BASELINE_LABEL_CHANGED')
                return saved
        setup_id = uuid4().hex
        setup = {'id': setup_id, 'started_at': now(), 'status': 'started', 'baseline_sha256': baseline['sha256'], 'steps': []}
        setup_path = self.control / ('setup-' + setup_id + '.json')
        atomic_json(setup_path, setup)
        try:
            base = None
            try:
                base = await self._image(self.base_image)
            except UpdateError as error:
                if str(error) != 'UPDATE_IMAGE_UNAVAILABLE':
                    raise
            if base is None:
                code, out, err = await self._docker(['pull', self.base_image], timeout=self.setup_timeout)
                setup['steps'].append({'action': 'pull', 'exit_code': code, 'stdout': out.decode(errors='replace'), 'stderr': err.decode(errors='replace')})
                atomic_json(setup_path, setup)
                if code:
                    raise UpdateError('UPDATE_BASE_IMAGE_PULL_FAILED')
                base = await self._image(self.base_image)
            base_ref = next((item for item in base.get('RepoDigests', []) if re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}', item)), None)
            if base_ref is None:
                raise UpdateError('UPDATE_BASE_REPOSITORY_DIGEST_REQUIRED')
            try:
                node = await self._image(_NODE_IMAGE)
            except UpdateError as error:
                if str(error) != 'UPDATE_IMAGE_UNAVAILABLE':
                    raise
                code, out, err = await self._docker(['pull', _NODE_IMAGE], timeout=self.setup_timeout)
                setup['steps'].append({'action': 'pull-node', 'exit_code': code,
                    'stdout': out.decode(errors='replace'), 'stderr': err.decode(errors='replace')})
                atomic_json(setup_path, setup)
                if code:
                    raise UpdateError('UPDATE_NODE_IMAGE_PULL_FAILED')
                node = await self._image(_NODE_IMAGE)
            node_ref = next((item for item in node.get('RepoDigests', [])
                if re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}', item)), None)
            if node_ref is None:
                raise UpdateError('UPDATE_NODE_REPOSITORY_DIGEST_REQUIRED')
            # The root lockfile is exported by a trusted CLI; no candidate source
            # exists in this working directory and no project code is installed.
            code, requirements, err = await self._process(self.uv_bin,
                ['export', '--offline', '--frozen', '--all-groups', '--no-emit-project', '--no-header', '--format', 'requirements-txt'],
                cwd=confined(self.baselines, baseline['sha256']), timeout=60)
            setup['steps'].append({'action': 'locked-export', 'exit_code': code, 'requirements_sha256': digest(requirements), 'stderr': err.decode(errors='replace')})
            atomic_json(setup_path, setup)
            if code:
                raise UpdateError('UPDATE_LOCK_EXPORT_FAILED')
            if not requirements.strip() or b'--hash=sha256:' not in requirements or b'-e ' in requirements:
                raise UpdateError('UPDATE_HASH_LOCKED_DEPENDENCIES_REQUIRED')
            dockerfile = (f'FROM {node_ref} AS hph_node\nFROM {base_ref}\n' + _IMAGE_BODY).encode()
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w') as tar:
                inputs = {'Dockerfile': dockerfile, 'requirements.txt': requirements,
                    'tokenizer_setup.py': _TOKENIZER_SETUP.encode(), 'verifier.py': _VERIFIER_DRIVER}
                inputs.update({'baseline/' + name: data for name, data in _tree(confined(self.baselines, baseline['sha256'])).items()})
                for name, data in inputs.items():
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    entry.mode = 0o444
                    tar.addfile(entry, io.BytesIO(data))
            tag = 'policy-harness-controller-check:' + digest(dockerfile + requirements + baseline['sha256'].encode() + recipe_hash.encode())[:24]
            code, out, err = await self._docker(['build', '--label', 'policy-harness.update-baseline=' + baseline['sha256'],
                '--label', 'policy-harness.update-owner=' + self.owner, '--tag', tag, '-'], timeout=self.setup_timeout, input_bytes=archive.getvalue())
            setup['steps'].append({'action': 'build', 'exit_code': code, 'dockerfile_sha256': digest(dockerfile),
                'base_digest': base_ref, 'node_digest': node_ref,
                'stdout': out.decode(errors='replace'), 'stderr': err.decode(errors='replace')})
            atomic_json(setup_path, setup)
            if code:
                raise UpdateError('UPDATE_VERIFICATION_IMAGE_BUILD_FAILED')
            image = await self._image(tag)
            saved = {'image_id': image['Id'], 'baseline_sha256': baseline['sha256'], 'setup_id': setup_id,
                'requirements_sha256': digest(requirements), 'recipe_sha256': recipe_hash,
                'driver_sha256': digest(_VERIFIER_DRIVER), 'verifier_contract': _VERIFIER_CONTRACT,
                'tokenizer': {'url': _TOKENIZER_URL, 'sha256': _TOKENIZER_SHA256, 'cache_dir': _TOKENIZER_CACHE,
                    'offline_checked_during_build': True, 'runtime_cache_read_only': True},
                'dockerfile_sha256': digest(dockerfile), 'base_digest': base_ref,
                'node_digest': node_ref, 'prepared_at': now()}
            atomic_json(path, saved)
            setup.update(status='succeeded', environment=saved)
            return saved
        except BaseException as error:
            setup.update(status='unknown' if isinstance(error, (asyncio.CancelledError, asyncio.TimeoutError)) else 'failed', first_fault=type(error).__name__ + ': ' + str(error), effects='image build/pull may remain; no candidate was run by setup')
            raise
        finally:
            setup['finished_at'] = now()
            atomic_json(setup_path, setup)

    def _verification(self, identity: str) -> dict | None:
        path = self._candidate_path(identity) / 'verification.json'
        if not path.exists():
            return None
        check_node(path)
        row = json.loads(path.read_text(encoding='utf-8'))
        if row.get('owner') != self.owner or row.get('candidate_id') != identity:
            raise UpdateError('UPDATE_VERIFICATION_IDENTITY_INVALID')
        run_id = row.get('run_id', '')
        if not re.fullmatch('[0-9a-f]{32}', run_id):
            raise UpdateError('UPDATE_VERIFICATION_RUN_INVALID')
        run_path = confined(self.runs, run_id + '.json')
        if json.loads(run_path.read_text(encoding='utf-8')) != row:
            raise UpdateError('UPDATE_VERIFICATION_POINTER_MISMATCH')
        return row

    async def _owned(self, row: dict) -> dict:
        identity = row.get('container_id') or row.get('container_name')
        code, out, _ = await self._docker(['container', 'inspect', identity])
        if code:
            raise UpdateError('UPDATE_CONTAINER_OBSERVATION_UNAVAILABLE')
        info = json.loads(out)[0]
        labels = info.get('Config', {}).get('Labels', {}) or {}
        if (labels.get('policy-harness.update-owner') != self.owner or
            labels.get('policy-harness.update-run') != row['run_id'] or
            labels.get('policy-harness.update-candidate') != row['candidate_id'] or
            info.get('Name') != '/' + row['container_name'] or
            not re.fullmatch('[0-9a-f]{64}', info.get('Id', '')) or
            row.get('container_id', info['Id']) != info['Id']):
            raise UpdateError('UPDATE_CONTAINER_OWNERSHIP_MISMATCH')
        row['container_id'] = info['Id']
        return info

    async def _stop(self, row):
        info = await self._owned(row)
        if info['State'].get('Running'):
            code, _, _ = await self._docker(['container', 'kill', info['Id']])
            if code:
                raise UpdateError('UPDATE_OWNED_STOP_FAILED')
            info = await self._owned(row)
        return info

    def _save_verification(self, row: dict):
        atomic_json(self._candidate_path(row['candidate_id']) / 'verification.json', row)
        atomic_json(self.runs / (row['run_id'] + '.json'), row)

    async def verify(self, candidate_id: str) -> dict:
        # An OS lease spans awaits; competing processes fail explicitly, without
        # polling or silently running two tests against one controller version.
        with process_lock(self.control / 'update.lock'), process_lock(self.control / 'verification.lock'):
            row = self._candidate(candidate_id)
            candidate = row['candidate']
            baseline = self._baseline()
            if candidate['baseline_sha256'] != baseline['sha256'] or candidate['baseline_epoch_id'] != baseline['epoch_id'] or candidate['policy_hash'] != self.policy.hash:
                raise UpdateError('UPDATE_CANDIDATE_BASELINE_OR_POLICY_STALE')
            previous = self._verification(candidate_id)
            if previous is not None:
                if previous['status'] == 'succeeded' and previous.get('verifier_contract') == _VERIFIER_CONTRACT:
                    self._check_verified(candidate_id, previous)
                    return {**previous, 'reused': True}
                if previous['status'] in ('started', 'unknown'):
                    return {**previous, 'reconciliation_required': True, 'replayed': False}
                # A retry is a new explicitly gated verify action, retaining the
                # immutable earlier run and its first fault in the run journal.
            image = await self._prepare(baseline)
            run_id = uuid4().hex
            row = {'owner': self.owner, 'run_id': run_id, 'candidate_id': candidate_id,
                'baseline_sha256': baseline['sha256'], 'policy_hash': self.policy.hash,
                'image_id': image['image_id'], 'after_version': candidate['after_version'],
                'status': 'started', 'effect': 'unknown', 'started_at': now(), 'first_fault': None,
                'container_name': 'hph-update-' + self.owner[:10] + '-' + run_id,
                'previous_run_id': previous.get('run_id') if previous else None,
                'verifier_contract': _VERIFIER_CONTRACT, 'driver_sha256': digest(_VERIFIER_DRIVER),
                'before_version': candidate['before_version'],
                'resource_envelope': self.resource_envelope(), 'proof_ceiling': _VERIFIER_CEILING}
            self._save_verification(row)
            started = time.monotonic()
            tree = self._candidate_path(candidate_id) / 'tree' / 'src'
            reference = self._candidate_path(candidate_id) / 'before' / 'src'
            if any(',' in str(path) or '"' in str(path) for path in (tree, reference)):
                raise UpdateError('UPDATE_MOUNT_PATH_ENCODING')
            args = ['container', 'create', '--name', row['container_name'],
                '--label', 'policy-harness.update-owner=' + self.owner,
                '--label', 'policy-harness.update-candidate=' + candidate_id,
                '--label', 'policy-harness.update-run=' + run_id,
                '--network', 'none', '--user', '65534:65534', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', '--pids-limit', str(self.pids_limit), '--cpus', str(self.cpus),
                '--memory', str(self.memory_bytes), '--memory-swap', str(self.memory_bytes), '--init',
                '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=' + str(self.tmpfs_bytes), '--workdir', '/baseline',
                '--env', 'PYTHONPATH=/candidate/src', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                '--env', 'HOME=/tmp', '--env', 'LANGCHAIN_TRACING_V2=false', '--env', 'LANGSMITH_TRACING=false',
                '--env', 'HARNESS_RUN_DOCKER_TESTS=0', '--env', 'HARNESS_RUN_UPDATE_DOCKER_TESTS=0',
                '--mount', 'type=bind,src=' + str(tree) + ',dst=/candidate/src,readonly',
                '--mount', 'type=bind,src=' + str(reference) + ',dst=/reference/src,readonly',
                '--entrypoint', 'python', image['image_id'], '-B', '/hph-verifier.py',
                '--reference-src', '/reference/src', '--candidate-src', '/candidate/src',
                '--tests', '/baseline/tests', '--config', '/baseline/pyproject.toml']
            row['create_argv'] = args
            self._save_verification(row)
            try:
                code, out, err = await self._docker(args)
                row['create_exit_code'] = code
                if code:
                    raise UpdateError('UPDATE_CONTAINER_CREATE_FAILED: ' + err.decode(errors='replace'))
                row['container_id'] = out.decode().strip()
                await self._owned(row)
                self._candidate(candidate_id)
                row['phase'] = 'starting'
                self._save_verification(row)
                code, out, err = await self._docker(['container', 'start', '--attach', row['container_id']], timeout=self.verification_timeout)
                self._capture(row, out, err)
                self._capture_reports(row, out)
                info = await self._owned(row)
                row['container_state'] = info['State']
                row['client_exit_code'] = code
                row['exit_code'] = info['State'].get('ExitCode')
                if info['State'].get('Running') or info['State'].get('OOMKilled') or code != 0 or row['exit_code'] != 0:
                    raise UpdateError('UPDATE_BASELINE_PROCESS_FAILED')
                if row.get('report_capture_errors'):
                    raise UpdateError(row['report_capture_errors'][0]['reason'])
                if not row.get('report_validation', {}).get('valid'):
                    raise UpdateError('UPDATE_BASELINE_REPORTS_NOT_ACCEPTED')
                self._candidate(candidate_id)
                self._baseline()
                row.update(status='succeeded', effect='confirmed')
            except BaseException as error:
                row['first_fault'] = row.get('first_fault') or {'type': type(error).__name__, 'reason': str(error), 'at': now()}
                streams = getattr(error, 'update_streams', None)
                if streams:
                    self._capture(row, streams['stdout'], streams['stderr'])
                    self._capture_reports(row, streams['stdout'])
                    row['capture_complete'] = False
                row.update(status='unknown', effect='unknown')
                try:
                    info = await asyncio.shield(self._stop(row))
                    row['container_state'] = info['State']
                    if not info['State'].get('Running'):
                        row.update(status='failed', effect='confirmed', exit_code=info['State'].get('ExitCode'))
                except BaseException as cleanup_error:
                    row['recovery_error'] = type(cleanup_error).__name__ + ': ' + str(cleanup_error)
                # Cancellation returns a durable unknown/failed result. Never turn
                # an interrupted client into a successful or replayable test.
            row.update(finished_at=now(), elapsed_seconds=time.monotonic() - started)
            self._save_verification(row)
            if row['status'] != 'unknown' and row.get('container_id'):
                try:
                    info = await self._owned(row)
                    if not info['State'].get('Running'):
                        code, _, _ = await self._docker(['container', 'rm', info['Id']])
                        row['container_removed'] = code == 0
                except BaseException as error:
                    row['cleanup_error'] = type(error).__name__ + ': ' + str(error)
                self._save_verification(row)
            return row

    def _capture(self, row: dict, stdout: bytes, stderr: bytes):
        row['streams'] = []
        for name, data in (('stdout', stdout), ('stderr', stderr)):
            path = self.runs / (row['run_id'] + '.' + name)
            _write(path, data)
            row['streams'].append({'kind': name, 'path': str(path), 'sha256': digest(data), 'bytes': len(data)})
            row[name] = data.decode('utf-8', errors='replace')
        row['capture_complete'] = True

    def _capture_reports(self, row: dict, stdout: bytes) -> None:
        """Capture first, classify later: nonzero exits still retain full JUnit."""
        row['report_capture_errors'] = []
        parsed = driver = None
        try:
            reports = re.findall(rb'<\?xml\s[^>]*\?>\s*<testsuites(?:\s[^>]*)?>.*?</testsuites>', stdout, flags=re.DOTALL)
            if len(reports) != 1:
                raise UpdateError('UPDATE_TEST_REPORT_MISSING_OR_AMBIGUOUS')
            path = self.runs / (row['run_id'] + '.xml')
            _write(path, reports[0])
            row['test_report'] = {'path': str(path), 'sha256': sha_file(path)}
            parsed = _junit(reports[0])
            row['test_report'].update(parsed)
        except (OSError, ValueError, PolicyError) as error:
            row['report_capture_errors'].append({'kind': 'junit', 'type': type(error).__name__, 'reason': str(error)})
        try:
            driver = _driver_observation(stdout)
            path = self.runs / (row['run_id'] + '.observation.json')
            atomic_json(path, driver)
            row['driver_report'] = {'path': str(path), 'sha256': sha_file(path), 'observation': driver}
        except (OSError, ValueError, PolicyError) as error:
            row['report_capture_errors'].append({'kind': 'driver', 'type': type(error).__name__, 'reason': str(error)})
        if parsed is not None and driver is not None:
            row['report_validation'] = _report_validation(parsed, driver)
        else:
            row['report_validation'] = {'valid': False, 'issues': ['complete JUnit and driver observations required']}

    async def reconcile(self, candidate_id: str, *, stop=False) -> dict:
        if not isinstance(candidate_id, str) or not re.fullmatch('[0-9a-f]{64}', candidate_id):
            raise UpdateError('UPDATE_CANDIDATE_ID_INVALID')
        with process_lock(self.control / 'update.lock'), process_lock(self.control / 'verification.lock'):
            stage_path = self.stages / (candidate_id + '.json')
            stage = None
            if stage_path.exists():
                check_node(stage_path)
                stage = json.loads(stage_path.read_text())
                if stage.get('owner') != self.owner or stage.get('candidate_id') != candidate_id:
                    raise UpdateError('UPDATE_STAGE_OWNERSHIP_MISMATCH')
            candidate = None
            try:
                candidate = self._candidate(candidate_id)['candidate']
            except FileNotFoundError:
                if stage is None:
                    return {'candidate_id': candidate_id, 'status': 'observed', 'effect': 'none', 'candidate_state': 'absent', 'replayed': False,
                        'original_operation_success': 'not-inferred', 'note': 'No committed candidate or owned stage journal was found'}
            row = self._verification(candidate_id) if candidate is not None else None
            if row and row['status'] in ('started', 'unknown'):
                try:
                    info = await self._stop(row) if stop else await self._owned(row)
                    row['container_state'] = info['State']
                    if not info['State'].get('Running'):
                        row.update(status='failed', effect='confirmed', recovered=True, exit_code=info['State'].get('ExitCode'),
                            recovery_note='Observed termination only; missing complete test capture is not success')
                    else:
                        row.update(status='unknown', effect='unknown')
                except (OSError, ValueError, PolicyError, asyncio.TimeoutError) as error:
                    row.update(status='unknown', effect='unknown', recovery_error=str(error))
                self._save_verification(row)
            activation = self._activation(candidate_id) if candidate is not None else None
            current = _manifest(self._code())
            source_state = None if candidate is None else ('before' if current == candidate['before_manifest'] else 'after' if current == candidate['after_manifest'] else 'mixed-or-foreign')
            unresolved = bool(row and row['status'] in ('started', 'unknown')) or bool(activation and activation.get('effect') == 'unknown') or bool(stage and candidate is None and stage.get('effect') == 'unknown')
            return {'candidate_id': candidate_id, 'status': 'unknown' if unresolved else 'observed',
                'effect': 'unknown' if unresolved else 'confirmed' if stage or row or activation else 'none',
                'candidate_state': 'complete' if candidate else 'incomplete', 'stage': stage, 'verification': row,
                'activation': activation, 'current_source_state': source_state, 'current_source_version': _identity(current),
                'replayed': False, 'original_operation_success': 'not-inferred',
                'next_action': 'explicit owned rollback/recovery decision' if unresolved else 'consume observations through normal parent/reviewer gates'}

    def _check_verified(self, identity: str, row: dict) -> None:
        candidate = self._candidate(identity)['candidate']
        baseline = self._baseline()
        if (row.get('status') != 'succeeded' or row.get('effect') != 'confirmed' or row.get('exit_code') != 0 or
            row.get('verifier_contract') != _VERIFIER_CONTRACT or row.get('driver_sha256') != digest(_VERIFIER_DRIVER) or
            row.get('before_version') != candidate['before_version'] or row.get('resource_envelope') != self.resource_envelope() or
            row.get('candidate_id') != identity or row.get('after_version') != candidate['after_version'] or
            candidate.get('baseline_epoch_id') != baseline['epoch_id'] or
            row.get('baseline_sha256') != baseline['sha256'] or row.get('policy_hash') != self.policy.hash or not row.get('capture_complete') or
            row.get('client_exit_code') != 0 or row.get('container_state', {}).get('Running') is not False or
            row.get('container_state', {}).get('ExitCode') != 0 or not re.fullmatch(r'sha256:[0-9a-f]{64}', row.get('image_id', ''))):
            raise UpdateError('UPDATE_SUCCESSFUL_BOUND_VERIFICATION_REQUIRED')
        if {item.get('kind') for item in row.get('streams', [])} != {'stdout', 'stderr'}:
            raise UpdateError('UPDATE_VERIFICATION_STREAMS_MISSING')
        for item in [*row.get('streams', []), row.get('test_report', {}), row.get('driver_report', {})]:
            if not item.get('path'):
                raise UpdateError('UPDATE_VERIFICATION_ARTIFACT_MISSING')
            path = Path(item['path']).absolute()
            if path.parent != self.runs:
                raise UpdateError('UPDATE_VERIFICATION_ARTIFACT_NOT_OWNED')
            check_node(path)
            if sha_file(path) != item['sha256']:
                raise UpdateError('UPDATE_VERIFICATION_ARTIFACT_CHANGED')
        report = _junit(Path(row['test_report']['path']).read_bytes())
        driver = _strict_json(Path(row['driver_report']['path']).read_bytes())
        stdout = Path(next(item['path'] for item in row['streams'] if item['kind'] == 'stdout')).read_bytes()
        report_bytes = Path(row['test_report']['path']).read_bytes()
        reports = re.findall(rb'<\?xml\s[^>]*\?>\s*<testsuites(?:\s[^>]*)?>.*?</testsuites>', stdout, flags=re.DOTALL)
        if reports != [report_bytes] or _driver_observation(stdout) != driver:
            raise UpdateError('UPDATE_VERIFICATION_REPORT_STREAM_MISMATCH')
        validation = _report_validation(report, driver)
        if (not validation['valid'] or any(row['test_report'].get(name) != value for name, value in report.items()) or
                validation != row.get('report_validation') or driver != row['driver_report'].get('observation') or
                row.get('report_capture_errors')):
            raise UpdateError('UPDATE_VERIFICATION_TEST_REPORT_INVALID')

    def _activation(self, identity: str) -> dict | None:
        path = self._candidate_path(identity) / 'activation.json'
        if not path.exists():
            return None
        check_node(path)
        row = json.loads(path.read_text())
        if row.get('owner') != self.owner or row.get('candidate_id') != identity:
            raise UpdateError('UPDATE_ACTIVATION_IDENTITY_INVALID')
        return row

    def _save_activation(self, row: dict):
        atomic_json(self._candidate_path(row['candidate_id']) / 'activation.json', row)

    def _apply_files(self, candidate: dict, *, reverse=False):
        for change in candidate['changes']:
            target = confined(self.project_root, self._target(change['path']), allow_missing=True)
            old, new = (change['new'], change['old']) if reverse else (change['old'], change['new'])
            current = sha_file(target) if target.exists() else None
            if current != old['sha256']:
                raise UpdateError('UPDATE_WRITE_CAS_CONFLICT: ' + change['path'])
            if new['absent']:
                target.unlink()
            else:
                _write(target, base64.b64decode(new['base64'], validate=True))

    def activate(self, candidate_id: str) -> dict:
        with process_lock(self.control / 'update.lock'):
            candidate = self._candidate(candidate_id)['candidate']
            verification = self._verification(candidate_id)
            self._check_verified(candidate_id, verification or {})
            previous = self._activation(candidate_id)
            if previous:
                if previous['status'] == 'activated' and _manifest(self._code()) == candidate['after_manifest']:
                    return {**previous, 'reused': True}
                raise UpdateError('UPDATE_PRIOR_ACTIVATION_REQUIRES_RECONCILIATION')
            if _manifest(self._code()) != candidate['before_manifest']:
                raise UpdateError('UPDATE_ACTIVE_SOURCE_CAS_CONFLICT')
            row = {'owner': self.owner, 'candidate_id': candidate_id, 'status': 'activating', 'effect': 'unknown',
                'before_version': candidate['before_version'], 'after_version': candidate['after_version'],
                'verification_run_id': verification['run_id'], 'verification_sha256': _identity(verification),
                'started_at': now(), 'first_fault': None, 'restart_required': True, 'effect_measurement': 'pending-next-use',
                'activator_runtime': self.runtime_identity()}
            self._save_activation(row)
            try:
                self._apply_files(candidate)
                if _manifest(self._code()) != candidate['after_manifest']:
                    raise UpdateError('UPDATE_ACTIVATION_READBACK_MISMATCH')
                row.update(status='activated', effect='confirmed', finished_at=now())
                self._save_activation(row)
                atomic_json(self.control / 'active.json', {**row, 'policy_hash': self.policy.hash})
                return row
            except BaseException as error:
                row['first_fault'] = {'type': type(error).__name__, 'reason': str(error), 'at': now()}
                row.update(status='interrupted', effect='unknown')
                self._save_activation(row)
                raise UpdateError('UPDATE_ACTIVATION_INTERRUPTED: exact preimages retained; explicit rollback/reconciliation required') from error

    def rollback(self, candidate_id: str) -> dict:
        with process_lock(self.control / 'update.lock'):
            candidate = self._candidate(candidate_id)['candidate']
            row = self._activation(candidate_id)
            if row is None:
                raise UpdateError('UPDATE_NO_OWNED_ACTIVATION')
            current = self._code()
            # A crash may leave a mix of this candidate's exact old/new images.
            # Any third-party bytes hold rollback rather than being overwritten.
            before, after = candidate['before_manifest'], candidate['after_manifest']
            for name in set(current) | set(before) | set(after):
                actual = digest(current[name]) if name in current else None
                if actual not in (before.get(name), after.get(name)):
                    raise UpdateError('UPDATE_ROLLBACK_CAS_CONFLICT: ' + name)
            row.update(status='rolling_back', effect='unknown', rollback_started_at=now(), restart_required=True,
                       rollback_runtime=self.runtime_identity())
            self._save_activation(row)
            try:
                for change in candidate['changes']:
                    target = confined(self.project_root, self._target(change['path']), allow_missing=True)
                    actual = sha_file(target) if target.exists() else None
                    if actual == change['old']['sha256']:
                        continue
                    if actual != change['new']['sha256']:
                        raise UpdateError('UPDATE_ROLLBACK_WRITE_CONFLICT')
                    if change['old']['absent']:
                        target.unlink()
                    else:
                        _write(target, base64.b64decode(change['old']['base64'], validate=True))
                if _manifest(self._code()) != before:
                    raise UpdateError('UPDATE_ROLLBACK_READBACK_MISMATCH')
                row.update(status='rolled_back', effect='confirmed', rollback_finished_at=now(), active_version=candidate['before_version'])
                self._save_activation(row)
                atomic_json(self.control / 'active.json', row)
                return row
            except BaseException as error:
                row.update(status='rollback_interrupted', effect='unknown', rollback_error=type(error).__name__ + ': ' + str(error))
                self._save_activation(row)
                raise

    def status(self) -> dict:
        path = self.control / 'active.json'
        active = None
        if path.exists():
            check_node(path)
            active = json.loads(path.read_text())
        binding = self._active_binding()
        observations, prior_observations, pending, reviews = [], [], [], []
        for record_path in sorted(self.runtime_operations.glob('*.json')):
            check_node(record_path)
            identity = json.loads(record_path.read_text(encoding='utf-8'))
            record = self.runtime_record(identity.get('task_id'), identity.get('operation_id'))
            runtime = record.get('controller_runtime') or record['runtime_before']
            if runtime.get('activation_candidate_id') != binding.get('activation_candidate_id'):
                continue
            brief = {key: record.get(key) for key in ('task_id', 'operation_id', 'operation_sha256',
                      'result_sha256', 'record_sha256', 'started_at', 'finished_at', 'result_status', 'effect')}
            brief['controller_runtime'] = runtime
            if record['status'] == 'started' or record.get('effect') == 'unknown':
                pending.append(brief)
            elif record.get('normal_use', {}).get('status') == 'observed':
                collection = observations if runtime.get('activation_sha256') == binding.get('activation_sha256') else prior_observations
                collection.append(brief)
        observations.sort(key=lambda item: item['finished_at'])
        for review_path in sorted(self.use_reviews.glob('*.json')):
            check_node(review_path)
            review = json.loads(review_path.read_text(encoding='utf-8'))
            if review.get('owner') != self.owner:
                raise UpdateError('UPDATE_USE_REVIEW_OWNER_MISMATCH')
            if any(item.get('candidate_id') == binding.get('activation_candidate_id') for item in review.get('observations', [])):
                reviews.append(review)
        use = {'status': 'observed' if observations else 'pending-next-use', 'count': len(observations),
               'first': observations[0] if observations else None, 'latest': observations[-1] if observations else None,
               'observations': observations, 'prior_activation_observations': prior_observations, 'pending': pending,
               'reviewed_resolutions': reviews, 'empirical_benefit': 'UNVERIFIED_UNTIL_MEASURED_COMPARISON'}
        return {'active': active, 'source_version_at_manager_construction': self._loaded_source,
            'runtime_identity': self.runtime_identity(), 'active_binding': binding,
            'disk_version': _identity(_manifest(self._code())), 'normal_use': use,
            'effect_measurement': 'normal-use-observed-benefit-unverified' if observations else 'pending-next-use',
            'proof_ceiling': 'Trusted construction/process and actual dispatch-result records; observed normal use is not measured benefit or universal policy compliance'}

    def restart_attestation(self, candidate_id: str) -> dict:
        """Verify the disk/activation binding; the supervisor proves new process use."""
        with process_lock(self.control / 'update.lock'):
            candidate = self._candidate(candidate_id)['candidate']
            activation = self._activation(candidate_id)
            if activation is None or activation.get('status') not in ('activated', 'rolled_back') or activation.get('effect') != 'confirmed':
                raise UpdateError('UPDATE_TERMINAL_ACTIVATION_REQUIRED_FOR_RESTART')
            self._policy_current()
            if candidate['policy_hash'] != self.policy.hash:
                raise UpdateError('UPDATE_RESTART_POLICY_CHANGED')
            reverse = activation['status'] == 'rolled_back'
            manifest = candidate['before_manifest' if reverse else 'after_manifest']
            version = candidate['before_version' if reverse else 'after_version']
            if _manifest(self._code()) != manifest:
                raise UpdateError('UPDATE_RESTART_SOURCE_CHANGED')
            return {'candidate_id': candidate_id, 'source_version': version, 'source_hashes': manifest,
                'activation_hash': _identity(activation), 'activation_status': activation['status'],
                'policy_hash': self.policy.hash, 'restart_required': True, 'effect_measurement': 'pending-next-use'}

    def validate_recovery_attestation(self, candidate_id: str, expected_attestation: dict) -> dict:
        """Read-only exact source/activation validation for the trusted parked recovery caller."""
        current = self.restart_attestation(candidate_id)
        if not isinstance(expected_attestation, dict) or canonical(current) != canonical(expected_attestation):
            raise UpdateError('UPDATE_RECOVERY_ATTESTATION_CHANGED')
        return current
