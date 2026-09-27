"""Final arguments shared by proposal admission and the actual file executor.

These reads establish shape and confinement, not authorization or future CAS
success. The executor still checks live bytes inside its execution boundary.
"""
import base64
import binascii
import hashlib
import os
from pathlib import Path

from .capabilities import confined
from .access_paths import operation_path
from .models import PolicyError


def file_write_input(workspace: Path, args: dict, *, full_access=False):
    if not isinstance(args, dict):
        raise PolicyError('WRITE_ARGUMENTS: args must be an object')
    extra = set(args) - {'path', 'text', 'base64', 'expected_sha256'}
    if extra:
        raise PolicyError('ARGUMENT_UNSUPPORTED: ' + ','.join(sorted(extra)))
    if ('text' in args) == ('base64' in args):
        raise PolicyError('WRITE_CONTENT: supply exactly one of text or base64')
    if 'text' in args:
        if not isinstance(args['text'], str):
            raise PolicyError('WRITE_CONTENT: text must be a string')
        try:
            data = args['text'].encode('utf-8')
        except UnicodeError as error:
            raise PolicyError('WRITE_CONTENT: text must encode as UTF-8') from error
    else:
        if not isinstance(args['base64'], str):
            raise PolicyError('WRITE_CONTENT: base64 must be a string')
        try:
            data = base64.b64decode(args['base64'], validate=True)
        except (ValueError, binascii.Error) as error:
            raise PolicyError('WRITE_CONTENT: base64 must be valid encoded bytes') from error
    expected = args.get('expected_sha256')
    if expected is not None and (not isinstance(expected, str) or len(expected) != 64
            or any(c not in '0123456789abcdef' for c in expected)):
        raise PolicyError('WRITE_CONFLICT: expected_sha256 must be null or an exact lowercase SHA256')
    path = operation_path(workspace, args.get('path', ''), full_access=full_access, allow_missing=True)
    if path.exists() and not path.is_file():
        raise PolicyError('WRITE_TARGET: target must be a file or absent')
    return path, data


def operation_effect(workspace: Path, operation: dict):
    """Compare an actual planned effect, never a shared path or a new UUID alone.

    Read-only diagnosis is not a write. A different replacement can be corrective
    work and still needs the independent admission/review of its real purpose.
    """
    if operation['kind'] == 'file_write':
        path, data = file_write_input(workspace, operation['args'])
        return {'kind': 'file_write', 'target': os.path.normcase(str(path.resolve())),
                'bytes_sha256': hashlib.sha256(data).hexdigest()}
    if operation['kind'] in {'exec', 'child_integrate'}:
        return {'kind': operation['kind'], 'args': operation['args']}
    return None
