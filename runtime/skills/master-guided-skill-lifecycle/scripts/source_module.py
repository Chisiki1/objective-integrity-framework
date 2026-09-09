"""Load an exact sibling source without consulting or producing bytecode caches.

Entrypoints bootstrap this small module with runpy.run_path on this .py file;
ordinary import of this loader would itself write into the registered tree.
This is source loading, not authorization or a replacement for registry checks.
"""
from pathlib import Path
import hashlib
import sys
import types


def load_source(path):
    path = Path(path).resolve(strict=True)
    source = path.read_bytes()
    # Bind module identity to both location and version. Dataclass annotation
    # resolution needs a registered module; old in-flight versions stay distinct.
    identity = hashlib.sha256(str(path).encode('utf8') + b'\0' + source).hexdigest()
    name = '_mgskill_source_' + identity
    module = types.ModuleType(name)
    module.__file__ = str(path)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        exec(compile(source, str(path), 'exec'), module.__dict__)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module
