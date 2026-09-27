"""Run the declared supported harness regression scope with no model credentials."""
from pathlib import Path
import os
import subprocess
import sys
import tomllib

root = Path(__file__).resolve().parents[1]
paths = tomllib.loads((root / 'pyproject.toml').read_text())['tool']['oif']['verification']['paths']
env = dict(os.environ, PYTHONPATH=str(root / 'src'), PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
raise SystemExit(subprocess.call([sys.executable, '-B', '-m', 'pytest', '-q', *paths, *sys.argv[1:]], cwd=root, env=env))
