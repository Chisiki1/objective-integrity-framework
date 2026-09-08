#!/usr/bin/env python3
"""Read explicit hook/capture files, never invoke, repair, trust or write anything.

Metadata is an as-read projection, not host discovery, journal validation or
permission. Input contents, commands and prompt text are never returned.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat

EVENTS = frozenset(('UserPromptSubmit', 'SessionStart', 'PreCompact', 'PostCompact',
    'Stop', 'PreToolUse', 'PermissionRequest', 'PostToolUse', 'SubagentStart',
    'SubagentStop', 'Interrupt', 'SessionEnd'))
LIMIT = 16 * 1024 * 1024


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def read_snapshot(path, *, limit=LIMIT):
    """Bounded regular-file read. Errors are metadata, never copied input text."""
    result = {'path': str(path), 'status': 'UNAVAILABLE', 'sha256': None}
    try:
        p = Path(path)
        if not p.is_absolute(): raise ValueError('absolute path required')
        for component in [*reversed(p.parents), p]:
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('link/reparse path is outside pure-file input contract')
        before = p.stat()
        if not stat.S_ISREG(before.st_mode): raise ValueError('regular file required')
        if before.st_size > limit:
            return {**result, 'status': 'LIMITED', 'reason': 'input exceeds bounded read'}, None
        with p.open('rb') as stream: raw = stream.read(limit + 1)
        after = p.stat()
        if len(raw) > limit:
            return {**result, 'status': 'LIMITED', 'reason': 'input grew beyond bounded read'}, None
        if _identity(before) != _identity(after) or len(raw) != before.st_size:
            return {**result, 'status': 'CHANGED_DURING_READ', 'reason': 'reconcile this partition; no retry performed'}, None
        return {**result, 'status': 'OBSERVED', 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest().upper()}, raw
    except (OSError, ValueError) as error:
        # Exception type only: an OS/JSON error can otherwise echo private input.
        return {**result, 'reason': type(error).__name__}, None


def hooks_projection(path):
    result, raw = read_snapshot(path)
    result.update(scope='only this supplied configuration file', configured_events=None)
    if raw is None: return result
    try:
        data = _json(raw.decode('utf-8-sig'))
        if not isinstance(data, dict) or not isinstance(data.get('hooks'), dict):
            raise ValueError('unsupported hook container')
        rows, issues = [], 0
        for event, groups in data['hooks'].items():
            if not isinstance(groups, list):
                issues += 1
                continue
            counts = dict(command=0, mcp_tool=0, other=0)
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                    issues += 1
                    continue
                for handler in group['hooks']:
                    if not isinstance(handler, dict):
                        issues += 1
                        continue
                    kind = handler.get('type')
                    counts[kind if kind in ('command', 'mcp_tool') else 'other'] += 1
            # Unknown labels can contain private strings; retain identity only.
            rows.append({'event': event if event in EVENTS else 'UNKNOWN_EVENT',
                'event_sha256': hashlib.sha256(event.encode()).hexdigest().upper(),
                'declared_handlers': counts})
        result.update(configured_events=rows, malformed_members=issues,
            status='PARTIAL' if issues else 'OBSERVED',
            execution_eligibility='NOT_EVALUATED: declarations may be disabled, unsupported or untrusted')
    except (UnicodeError, ValueError, TypeError, RecursionError):
        result.update(status='INVALID', reason='unreadable or unsupported JSON shape')
    return result


def capture_projection(path, source_sha256=None):
    result, raw = read_snapshot(path)
    result.update(scope='user_prompt_source metadata only; no journal-chain validation',
        observed_capture_records=None, matching_source_records=None)
    if raw is None: return result
    observed, matches, issues, last_seq = 0, 0, 0, None
    for line in raw.splitlines():
        try:
            row = _json(line)
            if not isinstance(row, dict): raise ValueError('record must be an object')
            if not isinstance(row.get('event_type'), str) or not row['event_type'].strip():
                raise ValueError('unknown journal record envelope')
            if row.get('event_type') != 'user_prompt_source': continue
            payload = row.get('payload')
            if not isinstance(payload, dict): raise ValueError('payload must be an object')
            digest = payload.get('source_sha256')
            if not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{64}', digest):
                raise ValueError('source digest malformed')
            observed += 1
            if source_sha256 and digest.upper() == source_sha256:
                matches += 1
                if type(row.get('seq')) is int: last_seq = row['seq']
        except (UnicodeError, ValueError, TypeError, RecursionError):
            issues += 1
    result.update(status='PARTIAL' if issues else 'OBSERVED', malformed_records=issues,
        observed_capture_records=observed,
        matching_source_records=matches if source_sha256 else None,
        last_matching_seq=last_seq,
        provenance='persisted metadata only; host event, actor, source bytes and semantic use not verified')
    return result


def snapshot(*, hooks, journal=None, source_sha256=None, runtime=None):
    if source_sha256 is not None:
        if journal is None or not re.fullmatch('[0-9a-fA-F]{64}', source_sha256):
            raise ValueError('exact source SHA256 requires an explicit journal')
        source_sha256 = source_sha256.upper()
    return {'schema': 'workflow-capability-snapshot-v1', 'hooks': hooks_projection(hooks),
        'capture': capture_projection(journal, source_sha256) if journal else {'status': 'NOT_REQUESTED'},
        'runtime_identity': read_snapshot(runtime)[0] if runtime else {'status': 'NOT_REQUESTED'},
        'trust': 'NOT_EVALUATED', 'host_delivery': 'NOT_EVALUATED',
        'actor_identity': 'NOT_EVALUATED', 'semantic_effect': 'NOT_EVALUATED',
        'permission_granted': False, 'writes_performed': False,
        'proof_ceiling': 'metadata for supplied as-read files, not an atomic multi-file cut or effective-host coverage'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hooks', required=True)
    parser.add_argument('--journal')
    parser.add_argument('--source-sha256')
    parser.add_argument('--runtime')
    args = parser.parse_args()
    try:
        result = snapshot(hooks=args.hooks, journal=args.journal,
            source_sha256=args.source_sha256, runtime=args.runtime)
    except ValueError:
        parser.error('invalid source digest/journal combination')
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return 0  # Inspect each partition; unknown evidence does not block ordinary work.


if __name__ == '__main__': raise SystemExit(main())
