#!/usr/bin/env python3
"""Narrow host hook adapter; observational context is never task authority.

No shell execution, user-source classification, Skill creation, tool rewriting,
master mutation, background work or Stop continuation occurs here. The sole
denial is the existing exact PowerShell checker finding its known parser family.
Original PostToolUse feedback is never blocked or replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest().upper()


def plain(path, *, absent=False):
    p = Path(path)
    if not p.is_absolute() or '..' in p.parts or (os.name == 'nt' and ':' in str(p)[len(p.drive):]):
        raise ValueError('plain absolute path required')
    for part in [*reversed(p.parents), p]:
        try: s = part.lstat()
        except FileNotFoundError:
            if absent: continue
            raise
        if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked path is unsupported')
        if stat.S_ISREG(s.st_mode) and s.st_nlink != 1:
            raise ValueError('hardlinked file is unsupported')
    return p


def context(event, text):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}


def _observe(config, event, payload, reason, *, detail=None):
    """Keep current observation separate from immutable, payload-free history.

    Shared session IDs scope storage, not the identity/permission of a child.
    Payload/output bodies stay in the original host result; only hashes persist.
    """
    root = plain(config['observation_root'])
    if not root.is_dir(): raise ValueError('pre-created observation root required')
    session = payload.get('session_id')
    if not isinstance(session, str) or not session: raise ValueError('session unavailable')
    folder = root / sha(session.encode())[:40]
    plain(folder, absent=True); folder.mkdir(exist_ok=True)
    archive = folder / 'history'; plain(archive, absent=True); archive.mkdir(exist_ok=True)
    observed = {'schema': 'in-work-hook-observation-v1', 'event': event,
        'tool_use_id': str(payload.get('tool_use_id', '')), 'turn_id': str(payload.get('turn_id', '')),
        'input_sha256': sha(canonical(payload.get('tool_input'))),
        'output_sha256': sha(canonical(payload.get('tool_response'))),
        'reason': reason, 'detail': detail, 'actor': 'unverified-shared-session',
        'semantic_effect': 'not-observed', 'permission_granted': False}
    key = sha(canonical(observed)); target = archive / (key + '.json')
    plain(target, absent=True)
    if not target.exists():
        with target.open('xb') as f: f.write(canonical(observed) + b'\n')
    elif target.read_bytes() != canonical(observed) + b'\n':
        raise ValueError('observation identity collision')
    # This is a disposable current pointer, never an objective/learning master.
    current = folder / 'current.json'; plain(current, absent=True)
    temporary = folder / ('current-' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as f: f.write(canonical({'latest_ref': str(target), 'sha256': sha(target.read_bytes())}) + b'\n')
    temporary.replace(current)
    return str(target)


def _powershell_input(payload, config):
    if payload.get('tool_name') != 'Bash' or not isinstance(payload.get('tool_input'), dict):
        return None
    value = payload['tool_input']
    command = value.get('command')
    if not isinstance(command, str): return None
    shell = value.get('shell', config.get('default_shell'))
    if not isinstance(shell, str) or Path(shell).stem.lower() not in {'powershell', 'pwsh'}:
        return None
    # Sound exclusion only for this one known recurrence discriminator. Literal
    # dollar intent is not available in generic tool input and is never guessed.
    if 'foreach' not in command.lower(): return None
    return command


def _pre(payload, config):
    command = _powershell_input(payload, config)
    if command is None: return {}
    checker = plain(config['powershell_checker']['path'])
    if sha(checker.read_bytes()) != config['powershell_checker']['sha256']:
        raise ValueError('reviewed checker bytes changed; no denial inferred')
    # The known reviewed parser runs locally, never the submitted command.
    result = subprocess.run([config['powershell_executable'], '-NoProfile', '-NonInteractive',
        '-File', str(checker), '-CommandText', command], capture_output=True, timeout=6, check=False)
    try: body = json.loads(result.stdout.decode('utf-8-sig'))
    except (UnicodeError, ValueError): raise ValueError('checker feedback unreadable; submitted command not executed by hook')
    findings = [f for member in body.get('members', []) for f in member.get('findings', [])]
    known = [f for f in findings if f.get('constraint_id') == 'PS-FOREACH-PIPE-001']
    if result.returncode == 2 and body.get('decision') == 'BLOCK' and known:
        ref = _observe(config, 'PreToolUse', payload, 'known-powerShell-direct-foreach-pipe',
            detail={'command_sha256': sha(command.encode()), 'constraint_ids': ['PS-FOREACH-PIPE-001']})
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
            'permissionDecisionReason': 'The existing PowerShell parser found a foreach statement piped directly. Fix that exact input using powershell-exact-action before resubmission. No command was run by this hook. Evidence: ' + ref}}
    if result.returncode != 0:
        return context('PreToolUse', 'The mechanical checker could not establish its known discriminator. No permission or safety decision was issued; inspect the original action under the normal workflow.')
    return {}


def _response(value):
    # The host may supply display text instead of its own structured exit status.
    # Parse only a complete bounded JSON value, never command text or a substring.
    if isinstance(value, str) and len(value) <= 131072:
        try: value = json.loads(value)
        except (ValueError, TypeError): return None
    return value if isinstance(value, dict) else None


def _method_destinations(payload, config):
    value = payload.get('tool_input')
    command = value.get('command', '') if isinstance(value, dict) else ''
    if payload.get('tool_name') != 'apply_patch' or not isinstance(command, str): return []
    roots = [config['skills_root'], *config.get('additional_skill_roots', [])]
    roots = [os.path.normcase(os.path.abspath(p)) for p in roots]
    found = []
    for line in command.splitlines():
        match = re.fullmatch(r'\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)', line)
        if not match: continue  # A plus-prefixed patch body is not a destination.
        path = Path(match[1])
        if not path.is_absolute():
            cwd = payload.get('cwd')
            if not isinstance(cwd, str) or not Path(cwd).is_absolute(): continue
            path = Path(cwd) / path
        path = os.path.normcase(os.path.abspath(path))
        if any(path == root or path.startswith(root + os.sep) for root in roots): found.append(path)
    return sorted(set(found))


def _post(payload, config):
    response = _response(payload.get('tool_response'))
    operation = bool(response and response.get('schema') == 'work-operation-summary-v1'
        and isinstance(response.get('result_ref'), dict)
        and isinstance(response['result_ref'].get('path'), str)
        and re.fullmatch('[A-Fa-f0-9]{64}', str(response['result_ref'].get('sha256', '')))
        and response.get('status') in {'succeeded', 'failed', 'unknown'})
    # Accepted nonzero codes are not failures. An empty host string is unknown,
    # not success, failure, or evidence that a named procedure was used.
    failed = bool(response and (response.get('isError') is True or
        (operation and response['status'] in {'failed', 'unknown'}) or
        (not operation and type(response.get('exit_code')) is int and response['exit_code'] != 0)))
    method_change = _method_destinations(payload, config)
    known_operation = operation
    if not (failed or known_operation or method_change): return {}
    reason = 'first-fault-result' if failed else 'method-edit-request' if method_change else 'ordinary-procedure-result'
    ref = _observe(config, 'PostToolUse', payload, reason,
        detail={'result_shape': 'operation-summary' if operation else 'host-object' if response else 'unavailable',
                'method_destinations': method_change, 'application_and_benefit': 'not-established'})
    message = ('Keep the original result and pending effects. Use the lifecycle in-work result/next-operation route now if this result changes the next necessary action; '
        'compare reuse, repair, new operation, no-change or concrete defer. Do not postpone an eligible improvement until task end, infer a cause from failure alone, or add work just to record learning. '
        'For a relevant method change, each continuing actor must reconcile its own required resources at the next safe boundary; do not replace in-flight bytes or impersonate a child. '
        'Observation only: ' + ref + '. Route: ' + config['instruction_path'])
    return context('PostToolUse', message)


def handle(payload, config):
    event = payload.get('hook_event_name')
    if event == 'PreToolUse': return _pre(payload, config)
    if event == 'PostToolUse': return _post(payload, config)
    if event in {'SessionStart', 'SubagentStart'}:
        # SessionStart(source=compact), not PostCompact, supplies the supported
        # root continuation context. This observation proves invocation only.
        _observe(config, event, payload, 'continuing-method-context',
            detail={'source': payload.get('source') if payload.get('source') in {'startup', 'resume', 'clear', 'compact'} else 'unreported'})
        return context(event, 'Resume the same source-bound objective and unfinished effects. For this material work, read or reuse your own unchanged required Skill coverage; '
            'use work_io boundary for relevant changes before the next safe dependent action. Parent reading does not certify child reading. '
            'Carry useful results into the current next necessary operation with the lifecycle route; no forced Skill creation or background continuation. Read: ' + config['instruction_path'])
    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--config', required=True)
    args = parser.parse_args()
    started = time.monotonic()
    try:
        config = json.loads(plain(args.config).read_bytes())
        raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024: raise ValueError('hook input exceeds bounded reader limit')
        payload = json.loads(raw)
        result = handle(payload, config)
    except Exception as error:
        # A failed observation cannot conceal original feedback or authorize a
        # tool. Do not echo raw payloads, command content or secrets in warnings.
        result = {'systemMessage': 'In-work hook unavailable (' + type(error).__name__ + '); normal workflow and original feedback remain authoritative.'}
    if result: print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
