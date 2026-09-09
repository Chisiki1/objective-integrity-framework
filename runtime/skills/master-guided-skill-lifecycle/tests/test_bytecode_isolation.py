"""Default-interpreter entrypoints must not mutate registered Skill resources."""
from pathlib import Path
import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest

S = Path(__file__).resolve().parents[1] / 'scripts'


class BytecodeIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.s = self.root / 'skill' / 'scripts'
        shutil.copytree(S, self.s, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.env = dict(os.environ)
        for key in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX', 'PYTHONPATH', 'PYTHONHOME'):
            self.env.pop(key, None)

    def tearDown(self):
        self.temp.cleanup()

    def members(self):
        return {p.relative_to(self.s).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.s.rglob('*') if p.is_file()}

    def call(self, script, *args, good=True):
        # Deliberately no -B: the former test harness hid normal Python writes.
        cp = subprocess.run([sys.executable, str(self.s / script), *map(str, args)],
                            cwd=self.root, env=self.env, capture_output=True, timeout=15)
        if good:
            self.assertEqual(cp.returncode, 0, cp.stderr.decode('utf8', 'replace'))
        return cp

    def test_cold_and_repeated_cli_do_not_change_registered_tree(self):
        before = self.members()
        for _ in range(2):
            for name in ('operation_io.py', 'materialize_skill_candidate.py', 'isolated_registry_adoption.py', 'work_io.py'):
                self.call(name, '--help')
                self.assertEqual(self.members(), before, name)

    def test_actual_run_consume_and_candidate_input_preserve_tree_and_result(self):
        before = self.members()
        source = self.root / 'source.txt'; source.write_text('authorized isolated operation')
        source_ref = dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest().upper())
        spec = dict(schema='work-operation-v1', operation_id='bytecode-case', owner_chat_id='test', source=source_ref,
                    purpose='Preserve actual status and streams', argv=[sys.executable, '-c', 'print("actual"); raise SystemExit(7)'],
                    cwd=str(self.root), timeout_seconds=5, accepted_exit_codes=[7], methods=[],
                    next_consumer='test-review', effect_scope='read-only')
        request = self.root / 'request.json'; request.write_text(json.dumps(spec))
        summary = json.loads(self.call('operation_io.py', 'run', '--input', request, '--output-root', self.root / 'operation').stdout)
        self.assertEqual((summary['status'], summary['exit_code']), ('succeeded', 7))
        result = self.root / 'operation' / 'result.json'
        original = result.read_bytes()
        for _ in range(2):
            handoff = json.loads(self.call('operation_io.py', 'consume', '--result', result).stdout)
            self.assertEqual(handoff['operation']['exit_code'], 7)
            self.assertFalse(handoff['permission_granted'])
        package = self.root / 'package'; package.mkdir()
        (package / 'SKILL.md').write_text('---\nname: example\ndescription: Bounded fixture\n---\nExample')
        (package / 'script.py').write_text('print("resource")')
        value = json.loads(self.call('operation_io.py', 'candidate-input', '--result', result, '--package-root', package,
                                    '--candidate-id', 'example', '--operation', 'CREATE', '--output', self.root / 'candidate.json').stdout)
        self.assertEqual(len(value['candidate']['package']['members']), 2)
        self.assertEqual(result.read_bytes(), original)
        self.assertEqual(self.members(), before)

    def test_preexisting_unchecked_bytecode_cannot_replace_source(self):
        for name in ('in_work_learning.py', 'skill_package.py', 'source_module.py'):
            path = self.s / name; original = path.read_bytes()
            path.write_text('raise RuntimeError("BYTECODE_WAS_EXECUTED")\n')
            py_compile.compile(str(path), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
            path.write_bytes(original)
        before = self.members()
        for name in ('operation_io.py', 'materialize_skill_candidate.py', 'isolated_registry_adoption.py'):
            self.call(name, '--help')
        self.assertEqual(self.members(), before)  # Loader neither executes nor deletes cache.

    def test_missing_source_fails_without_bytecode_or_search_path_fallback(self):
        path = self.s / 'skill_package.py'
        py_compile.compile(str(path), cfile=str(self.s / 'skill_package.pyc'), doraise=True)
        path.unlink()
        cp = self.call('materialize_skill_candidate.py', '--help', good=False)
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn(b'usage:', cp.stdout)

    def test_default_environment_still_allows_unrelated_python_bytecode(self):
        # The repair must not depend on disabling bytecode globally for the process.
        (self.root / 'unrelated.py').write_text('VALUE = 1\n')
        code = ('import sys,runpy; before=sys.dont_write_bytecode; '
                'runpy.run_path(sys.argv[1],run_name="operation_probe"); '
                'import unrelated; print(before,sys.dont_write_bytecode)')
        cp = subprocess.run([sys.executable, '-c', code, str(self.s / 'operation_io.py')],
                            cwd=self.root, env=self.env, capture_output=True, timeout=15)
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout.strip(), b'False False')
        self.assertTrue(list((self.root / '__pycache__').glob('unrelated.*.pyc')))


if __name__ == '__main__':
    unittest.main()
