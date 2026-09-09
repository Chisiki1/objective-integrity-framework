#!/usr/bin/env python3
"""Normal/adverse package consumers, confined to synthetic temporary directories.

Run after integration: python -B -m unittest discover -s <skill>/tests
No active registry is read or changed. A staged script is actually executed;
that fixture execution is not a measured ordinary-task learning effect.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import skill_package as package
import materialize_skill_candidate as materializer
import isolated_registry_adoption as adopter


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def member_digest(members: list[dict[str, str]]) -> str:
    # Independent fixture serialization, not the implementation under test.
    normalized = sorted(({"path": row["path"], "sha256": row["sha256"].upper()} for row in members), key=lambda row: row["path"])
    return digest(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def receipt_digest(value: dict[str, object]) -> str:
    return digest(json.dumps({key: item for key, item in value.items() if key != "materialization_receipt_sha256"}, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def namespace(root: Path) -> dict[str, str]:
    result = {}
    for item in root.rglob("*"):
        name = item.relative_to(root).as_posix()
        if item.is_symlink():
            result[name] = "SYMLINK:" + os.readlink(item)
        elif item.is_file():
            result[name] = digest(item.read_bytes())
        else:
            result[name] = "DIRECTORY"
    return result


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.source = root / "source"
        self.inactive = root / "inactive"
        self.discovery = root / "discovery"
        for directory in (self.source, self.inactive, self.discovery):
            directory.mkdir()
        content = {
            "SKILL.md": b"---\nname: synthetic-package\ndescription: Produce a synthetic report from exact resources.\n---\n\nUse scripts/report.py with the supplied JSON values.\n",
            "scripts/report.py": (
                "import json, sys\nfrom pathlib import Path\n"
                "root = Path(__file__).resolve().parents[1]\n"
                "config = json.loads((root / 'references/settings.json').read_text(encoding='utf-8'))\n"
                "values = json.loads(sys.argv[1])['values']\n"
                "template = (root / 'templates/report.txt').read_text(encoding='utf-8')\n"
                "stamp = (root / 'assets/stamp.txt').read_text(encoding='utf-8')\n"
                "print(template.format(total=sum(values) * config['scale'], stamp=stamp))\n"
            ).encode("utf-8"),
            "references/settings.json": b'{"scale": 2}\n',
            "templates/report.txt": b"synthetic total: {total} | {stamp}",
            "assets/stamp.txt": b"isolated-fixture",
        }
        for name, value in content.items():
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        self.members = sorted(({"path": name, "sha256": digest(value)} for name, value in content.items()), key=lambda row: row["path"])
        self.registry = write_json(root / "registry.json", {"schema_version": "mgskill-registry-v1", "registry_id": "SYNTHETIC", "entries": []})
        self.input_path = root / "candidate.json"
        self.receipt_path = root / "material.json"
        self.data = {
            "schema_version": "mgskill-inactive-candidate-v2", "operation": "CREATE", "event_id": "EVT-SYNTHETIC", "objective_id": "OBJ-SYNTHETIC", "source_claims": ["SRC-SYNTHETIC"],
            "owner": {"lane": "COORDINATED-WORK", "writer": "fixture", "task_id": "task-synthetic", "chat_id": "chat-synthetic", "lease_id": "lease-synthetic", "source_sha256": "A" * 64},
            "candidate": {"candidate_id": "synthetic-package", "artifact_path": str(self.source / "SKILL.md"), "artifact_sha256": digest(content["SKILL.md"]), "prior_candidate_id": None, "equivalent_fingerprints": [],
                          "package": {"root": str(self.source), "members": self.members, "manifest_sha256": member_digest(self.members)}},
            "evidence": {"source": "synthetic unit fixture"}, "rollback": {"owner": "fixture", "method": "remove only owned temporary output"},
            "independent_challenge": {"status": "completed"}, "proof_ceiling": "isolated fixture only",
        }
        self.save()

    def save(self) -> None:
        write_json(self.input_path, self.data)

    def legacy(self) -> None:
        self.data["schema_version"] = "mgskill-inactive-candidate-v1"
        del self.data["candidate"]["package"]
        self.save()

    def material_args(self, *, apply: bool = True, root: Path | None = None) -> list[str]:
        args = ["--input", str(self.input_path), "--candidate-root", str(root or self.inactive),
                "--expected-root-manifest-sha256", digest(b""), "--discovery-root", str(self.discovery)]
        return args + (["--apply"] if apply else [])

    def entry(self, material: dict[str, object]) -> dict[str, object]:
        return {"skill_id": "SKILL-SYNTHETIC", "name": "synthetic-package", "version": "1.0.0", "origin": "user", "relative_path": "synthetic-package", "status": "candidate",
                "match_clauses": [{"job": ["synthetic-report"]}], "files": material["candidate_member_set"]}

    def adoption_args(self, material: dict[str, object], *, destination: Path | None = None) -> list[str]:
        write_json(self.receipt_path, material)
        return ["--proposal", str(self.receipt_path), "--candidate-root", str(self.inactive), "--isolated-adoption-root", str(destination or self.root / "adopted"),
                "--expected-destination-manifest-sha256", "ABSENT", "--registry", str(self.registry), "--expected-registry-sha256", digest(self.registry.read_bytes()),
                "--registry-entry", json.dumps(self.entry(material)), "--discovery-root", str(self.discovery), "--apply"]


class SkillPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="skill-package-")
        self.root = Path(self.temporary.name).resolve()
        self.fixture = Fixture(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cli(self, name: str, args: list[str], expected: int = 0, fault: tuple[str, str] | None = None) -> dict[str, object]:
        env = {key: value for key, value in os.environ.items() if key not in {"MGSKILL_TEST_ADOPTION_FAULT", "MGSKILL_TEST_MATERIALIZATION_FAULT"}}
        if fault:
            env[fault[0]] = fault[1]
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / name), *args], capture_output=True, text=True, encoding="utf-8", env=env, timeout=45, check=False)
        self.assertEqual(result.returncode, expected, f"stdout={result.stdout}\nstderr={result.stderr}")
        try:
            return json.loads(result.stdout)
        except ValueError:
            self.fail(f"invalid JSON stdout={result.stdout!r}; stderr={result.stderr!r}")

    def in_process(self, module: object, args: list[str], expected: int = 2) -> dict[str, object]:
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["fixture", *args]), contextlib.redirect_stdout(output):
            code = module.main()
        self.assertEqual(code, expected, output.getvalue())
        return json.loads(output.getvalue())

    def materialize(self) -> dict[str, object]:
        return self.cli("materialize_skill_candidate.py", self.fixture.material_args())

    def reject_material(self) -> dict[str, object]:
        before = namespace(self.root)
        result = self.cli("materialize_skill_candidate.py", self.fixture.material_args(), 2)
        self.assertEqual(result["writes_performed"], [])
        self.assertEqual(result["effect_state"], "none")
        self.assertEqual(namespace(self.root), before)
        return result

    def reject_adoption(self, args: list[str]) -> dict[str, object]:
        before = namespace(self.root)
        result = self.cli("isolated_registry_adoption.py", args, 2)
        self.assertEqual(result["writes_performed"], [])
        self.assertEqual(result["effect_state"], "none")
        self.assertEqual(namespace(self.root), before)
        return result

    def test_resource_package_stages_and_executes_real_resource_consumer(self) -> None:
        source_before, registry_before = namespace(self.fixture.source), self.fixture.registry.read_bytes()
        material = self.materialize()
        self.assertEqual(material["candidate_member_set"], self.fixture.members)
        self.assertEqual(material["candidate_package_manifest_sha256"], member_digest(self.fixture.members))
        self.assertEqual(material["support_mode"], "resource-package")
        result = self.cli("isolated_registry_adoption.py", self.fixture.adoption_args(material))
        self.assertEqual(result["transaction_state"], "VERIFIED")
        self.assertEqual(result["package_member_set"], self.fixture.members)
        staged = Path(result["package_root"])
        self.assertEqual(namespace(staged), source_before)
        self.assertEqual(self.fixture.registry.read_bytes(), registry_before)
        self.assertEqual((self.root / "adopted" / "registry.json.backup").read_bytes(), registry_before)
        registered = json.loads((self.root / "adopted" / "registry.json").read_text(encoding="utf-8"))
        self.assertEqual(registered["entries"][0]["status"], "candidate")
        self.assertEqual(registered["entries"][0]["files"], self.fixture.members)
        action = subprocess.run([sys.executable, "-B", str(staged / "scripts/report.py"), '{"values":[1,2,3]}'], capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
        self.assertEqual(action.returncode, 0, f"stdout={action.stdout}; stderr={action.stderr}")
        self.assertEqual(action.stdout.strip(), "synthetic total: 12 | isolated-fixture")
        self.assertEqual(namespace(staged), source_before, "execution must not add pycache/undeclared resources")
        self.assertEqual(namespace(self.fixture.source), source_before)
        self.assertEqual(list(self.fixture.discovery.iterdir()), [])

    def test_instruction_only_v1_and_historical_receipt_remain_usable(self) -> None:
        self.fixture.legacy()
        material = self.materialize()
        self.assertEqual(material["schema_version"], "mgskill-inactive-candidate-proposal-v1")
        self.assertEqual(material["support_mode"], "instruction-only")
        self.assertEqual([row["path"] for row in material["candidate_member_set"]], ["SKILL.md"])
        # Emulate a pre-package receipt: new optional fields cannot be required.
        for key in ("candidate_package_root", "candidate_package_manifest_sha256", "transaction_state", "effect_state", "publish_method"):
            material.pop(key)
        material["readback"] = {"status": "observed", "artifact_sha256": material["candidate_artifact_sha256"], "destination_exists": True}
        material["materialization_receipt_sha256"] = receipt_digest(material)
        result = self.cli("isolated_registry_adoption.py", self.fixture.adoption_args(material))
        self.assertEqual(result["decision"], "ISOLATED_ADOPTION_STAGED")
        self.assertEqual(set(namespace(Path(result["package_root"]))), {"SKILL.md"})

    def test_plan_and_pending_challenge_do_not_write(self) -> None:
        before = namespace(self.root)
        ready = self.cli("materialize_skill_candidate.py", self.fixture.material_args(apply=False))
        self.assertEqual(ready["decision"], "INACTIVE_CANDIDATE_READY")
        self.assertEqual(namespace(self.root), before)
        self.fixture.data["independent_challenge"]["status"] = "pending"
        self.fixture.save()
        before = namespace(self.root)
        held = self.cli("materialize_skill_candidate.py", self.fixture.material_args(apply=False), 3)
        self.assertEqual(held["decision"], "INACTIVE_CANDIDATE_HOLD_INDEPENDENT_CHALLENGE")
        self.assertEqual(namespace(self.root), before)
        self.reject_material()

    def test_v2_instruction_only_is_a_valid_exact_package(self) -> None:
        for folder in ("scripts", "references", "templates", "assets"):
            import shutil
            shutil.rmtree(self.fixture.source / folder)
        members = [row for row in self.fixture.members if row["path"] == "SKILL.md"]
        self.fixture.data["candidate"]["package"].update(members=members, manifest_sha256=member_digest(members))
        self.fixture.save()
        result = self.materialize()
        self.assertEqual(result["candidate_member_set"], members)

    def test_unsafe_duplicate_and_aliased_members_reject_without_writes(self) -> None:
        original = copy.deepcopy(self.fixture.data)
        for name in ("../escape.py", "/absolute.py", "scripts/../escape.py", "scripts\\escape.py", "C:/escape.py", "scripts/CON.txt", "scripts/x.", "scripts/x ", "scripts/x:stream", "scripts//x"):
            with self.subTest(name=name):
                self.fixture.data = copy.deepcopy(original)
                rows = self.fixture.data["candidate"]["package"]["members"]
                rows.append({"path": name, "sha256": "A" * 64})
                self.fixture.data["candidate"]["package"]["manifest_sha256"] = member_digest(rows)
                self.fixture.save()
                self.reject_material()
        for extra in ({"path": "SKILL.md", "sha256": "A" * 64}, {"path": "skill.md", "sha256": "A" * 64}, {"path": "Scripts/other.py", "sha256": "A" * 64}, {"path": "scripts", "sha256": "A" * 64}):
            with self.subTest(extra=extra):
                self.fixture.data = copy.deepcopy(original)
                rows = self.fixture.data["candidate"]["package"]["members"]
                rows.append(extra)
                self.fixture.data["candidate"]["package"]["manifest_sha256"] = member_digest(rows)
                self.fixture.save()
                self.reject_material()

    def test_extra_source_file_rejected(self) -> None:
        (self.fixture.source / "scripts/unreviewed.py").write_text("print('not allowed')\n", encoding="utf-8")
        self.reject_material()

    def test_missing_source_file_rejected(self) -> None:
        (self.fixture.source / "assets/stamp.txt").unlink()
        self.reject_material()

    def test_changed_source_bytes_rejected(self) -> None:
        (self.fixture.source / "scripts/report.py").write_text("print('changed')\n", encoding="utf-8")
        self.reject_material()

    def test_wrong_manifest_rejected(self) -> None:
        self.fixture.data["candidate"]["package"]["manifest_sha256"] = "0" * 64
        self.fixture.save()
        self.reject_material()

    def test_different_artifact_outside_package_rejected(self) -> None:
        other = self.root / "other"
        other.mkdir()
        (other / "SKILL.md").write_bytes((self.fixture.source / "SKILL.md").read_bytes())
        self.fixture.data["candidate"]["artifact_path"] = str(other / "SKILL.md")
        self.fixture.save()
        self.reject_material()

    def test_duplicate_json_and_nonfinite_values_rejected(self) -> None:
        original = self.fixture.input_path.read_text(encoding="utf-8")
        for payload in (original.rstrip()[:-1] + ', "schema_version":"mgskill-inactive-candidate-v2"}', original.replace('"CREATE"', "NaN", 1)):
            with self.subTest(payload=payload[-100:]):
                self.fixture.input_path.write_text(payload, encoding="utf-8")
                self.reject_material()

    def test_active_root_source_overlap_and_existing_destination_reject(self) -> None:
        for root in (self.fixture.discovery, self.fixture.source):
            with self.subTest(root=root):
                before = namespace(self.root)
                result = self.cli("materialize_skill_candidate.py", self.fixture.material_args(root=root), 2)
                self.assertEqual(result["writes_performed"], [])
                self.assertEqual(namespace(self.root), before)
        (self.fixture.inactive / "synthetic-package").mkdir()
        self.reject_material()

    def test_hardlinked_resource_rejected(self) -> None:
        target = self.fixture.source / "assets/stamp.txt"
        try:
            os.link(target, self.root / "shared-stamp.txt")
        except OSError as exc:
            self.skipTest(f"hardlink unavailable on this fixture host: {exc}")
        self.reject_material()

    def test_hardlinked_control_file_rejected(self) -> None:
        try:
            os.link(self.fixture.input_path, self.root / "shared-input.json")
        except OSError as exc:
            self.skipTest(f"hardlink unavailable on this fixture host: {exc}")
        self.reject_material()

    def test_symlink_resource_rejected_without_following_target(self) -> None:
        target = self.root / "outside.txt"
        target.write_bytes((self.fixture.source / "assets/stamp.txt").read_bytes())
        resource = self.fixture.source / "assets/stamp.txt"
        resource.unlink()
        try:
            resource.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink unavailable on this fixture host: {exc}")
        self.reject_material()
        self.assertEqual(target.read_bytes(), b"isolated-fixture")

    def test_symlink_ancestor_rejected(self) -> None:
        alias = self.root / "source-alias"
        try:
            alias.symlink_to(self.fixture.source, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable on this fixture host: {exc}")
        self.fixture.data["candidate"]["package"]["root"] = str(alias)
        self.fixture.data["candidate"]["artifact_path"] = str(alias / "SKILL.md")
        self.fixture.save()
        self.reject_material()

    def test_reparse_attribute_check_is_not_just_symlink_check(self) -> None:
        information = types.SimpleNamespace(st_mode=stat.S_IFDIR, st_nlink=1, st_file_attributes=0x400)
        with self.assertRaisesRegex(package.PackageError, "reparse"):
            package._check_stat(self.root / "synthetic-junction", information)

    def test_changed_source_after_staging_is_caught_and_stage_removed(self) -> None:
        real_copy = materializer.package.copy_package
        def change_after_copy(source, destination, writes):
            real_copy(source, destination, writes)
            (self.fixture.source / "scripts/report.py").write_text("print('changed during copy')\n", encoding="utf-8")
        with mock.patch.object(materializer.package, "copy_package", side_effect=change_after_copy):
            result = self.in_process(materializer, self.fixture.material_args())
        self.assertEqual(result["effect_state"], "partial")
        self.assertEqual(result["rollback_result"], "precommit-staging-removed")
        self.assertEqual(list(self.fixture.inactive.iterdir()), [])
        self.assertIn("CAS", result["primary_fault"])

    def test_unowned_candidate_root_change_is_preserved(self) -> None:
        real_copy = materializer.package.copy_package
        def add_foreign_file(source, destination, writes):
            real_copy(source, destination, writes)
            (self.fixture.inactive / "unowned.txt").write_text("retain", encoding="utf-8")
        with mock.patch.object(materializer.package, "copy_package", side_effect=add_foreign_file):
            result = self.in_process(materializer, self.fixture.material_args())
        self.assertIn("root CAS", result["primary_fault"])
        self.assertEqual(namespace(self.fixture.inactive), {"unowned.txt": digest(b"retain")})

    def test_unavailable_publication_is_a_preflight_rejection(self) -> None:
        before = namespace(self.root)
        with mock.patch.object(materializer.package, "publish_capability", side_effect=materializer.package.PackageError("unsupported fixture host")):
            result = self.in_process(materializer, self.fixture.material_args())
        self.assertEqual(result["writes_performed"], [])
        self.assertEqual(namespace(self.root), before)

    def test_adoption_current_resource_and_extra_member_rejected(self) -> None:
        material = self.materialize()
        args = self.fixture.adoption_args(material)
        directory = Path(material["candidate_package_root"])
        script = directory / "scripts/report.py"
        original = script.read_bytes()
        script.write_bytes(b"changed")
        self.reject_adoption(args)
        script.write_bytes(original)
        (directory / "unexpected.txt").write_bytes(b"extra")
        self.reject_adoption(args)

    def test_adoption_receipt_member_entry_and_registry_cas_rejected(self) -> None:
        material = self.materialize()
        original_args = self.fixture.adoption_args(material)
        wrong = copy.deepcopy(material)
        wrong["candidate_member_set"] = [row for row in wrong["candidate_member_set"] if row["path"] == "SKILL.md"]
        wrong["materialization_receipt_sha256"] = receipt_digest(wrong)
        self.reject_adoption(self.fixture.adoption_args(wrong))
        self.fixture.adoption_args(material)
        for key, value in (("--expected-registry-sha256", "0" * 64), ("--registry-entry", json.dumps({**self.fixture.entry(material), "files": []})),
                           ("--registry-entry", json.dumps({**self.fixture.entry(material), "status": "active-bounded"})),
                           ("--isolated-adoption-root", str(self.fixture.discovery)), ("--candidate-root", str(self.fixture.source))):
            with self.subTest(key=key, value=value):
                args = list(original_args)
                args[args.index(key) + 1] = value
                self.reject_adoption(args)

    def test_adoption_duplicate_registry_identity_rejected(self) -> None:
        material = self.materialize()
        write_json(self.fixture.registry, {"schema_version": "mgskill-registry-v1", "registry_id": "SYNTHETIC", "entries": [self.fixture.entry(material)]})
        self.reject_adoption(self.fixture.adoption_args(material))

    def test_adoption_hardlinked_registry_rejected(self) -> None:
        material = self.materialize()
        args = self.fixture.adoption_args(material)
        try:
            os.link(self.fixture.registry, self.root / "registry-alias.json")
        except OSError as exc:
            self.skipTest(f"hardlink unavailable on this fixture host: {exc}")
        self.reject_adoption(args)

    def test_adoption_source_registry_changed_during_staging_preserved(self) -> None:
        material = self.materialize()
        args = self.fixture.adoption_args(material)
        real_copy = adopter.package.copy_package
        def change_registry(source, destination, writes):
            real_copy(source, destination, writes)
            self.fixture.registry.write_text('{"external":"changed"}\n', encoding="utf-8")
        with mock.patch.object(adopter.package, "copy_package", side_effect=change_registry):
            result = self.in_process(adopter, args)
        self.assertIn("CAS", result["primary_fault"])
        self.assertEqual(result["rollback_result"], "precommit-staging-removed")
        self.assertFalse((self.root / "adopted").exists())
        self.assertEqual(self.fixture.registry.read_text(encoding="utf-8"), '{"external":"changed"}\n')

    def test_materialization_faults_keep_commit_state_and_first_fault(self) -> None:
        for index, (point, committed) in enumerate((("after-skill-copy", False), ("before-rename", False), ("after-rename", True), ("after-readback", True), ("after-verified", True))):
            with self.subTest(point=point):
                root = self.root / f"inactive-fault-{index}"
                root.mkdir()
                result = self.cli("materialize_skill_candidate.py", self.fixture.material_args(root=root), 2, ("MGSKILL_TEST_MATERIALIZATION_FAULT", point))
                self.assertEqual(result["primary_fault"], f"test fault:{point}")
                self.assertEqual((root / "synthetic-package").exists(), committed)
                self.assertEqual(result["effect_state"], "unknown" if committed else "partial")
                self.assertFalse(result["stage_exists"])

    def test_adoption_faults_preserve_package_and_committed_destination(self) -> None:
        material = self.materialize()
        source_before = namespace(self.fixture.inactive)
        for index, (point, committed) in enumerate((("after-skill-copy", False), ("after-backup", False), ("after-registry-stage", False), ("before-rename", False), ("after-rename", True), ("after-readback", True), ("after-verified", True))):
            with self.subTest(point=point):
                destination = self.root / f"adopt-fault-{index}"
                args = self.fixture.adoption_args(material, destination=destination)
                result = self.cli("isolated_registry_adoption.py", args, 2, ("MGSKILL_TEST_ADOPTION_FAULT", point))
                self.assertEqual(result["primary_fault"], f"test fault:{point}")
                self.assertEqual(result["decision"], "OUTCOME_UNKNOWN" if committed else "ADOPTION_FAILED")
                self.assertEqual(destination.exists(), committed)
                self.assertFalse(result["stage_exists"])
                if committed:
                    self.assertEqual(namespace(destination / "synthetic-package"), namespace(self.fixture.source))
                self.assertEqual(namespace(self.fixture.inactive), source_before)

    def test_cleanup_failure_is_retained_not_no_effect(self) -> None:
        real_copy = materializer.package.copy_package
        def partial_then_fail(source, destination, writes):
            real_copy(source, destination, writes)
            raise RuntimeError("first synthetic copy fault")
        with mock.patch.object(materializer.package, "copy_package", side_effect=partial_then_fail), mock.patch.object(materializer.package.shutil, "rmtree", side_effect=OSError("synthetic cleanup fault")):
            result = self.in_process(materializer, self.fixture.material_args())
        self.assertEqual(result["primary_fault"], "first synthetic copy fault")
        self.assertEqual(result["effect_state"], "unknown")
        self.assertTrue(result["stage_exists"])
        self.assertIn("synthetic cleanup fault", result["cleanup_failures"][0])

    def test_exception_after_publish_preserves_owned_destination(self) -> None:
        real_publish = materializer.package.publish_directory
        def publish_then_raise(stage, destination):
            real_publish(stage, destination)
            raise OSError("synthetic post-rename interruption")
        with mock.patch.object(materializer.package, "publish_directory", side_effect=publish_then_raise):
            result = self.in_process(materializer, self.fixture.material_args())
        self.assertEqual(result["transaction_state"], "COMMITTED")
        self.assertEqual(result["effect_state"], "unknown")
        self.assertTrue((self.fixture.inactive / "synthetic-package" / "scripts/report.py").is_file())

    def test_no_replace_publication_preserves_a_racing_destination(self) -> None:
        stage, destination = self.root / "owned-stage", self.root / "racing-destination"
        stage.mkdir()
        (stage / "owned.txt").write_bytes(b"owned")
        real_checked = package.checked_path
        def create_destination_after_check(value, **kwargs):
            result = real_checked(value, **kwargs)
            if Path(value) == destination and kwargs.get("absent"):
                destination.mkdir()
                (destination / "other-owner.txt").write_bytes(b"preserve")
            return result
        with mock.patch.object(package, "checked_path", side_effect=create_destination_after_check):
            with self.assertRaises(OSError):
                package.publish_directory(stage, destination)
        self.assertEqual((destination / "other-owner.txt").read_bytes(), b"preserve")
        self.assertEqual((stage / "owned.txt").read_bytes(), b"owned")


if __name__ == "__main__":
    unittest.main()
