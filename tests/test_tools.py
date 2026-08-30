from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def simple_schema_validate(schema: dict, data: dict) -> list[str]:
    failures: list[str] = []

    def check(node: dict, value, path: str) -> None:
        if "const" in node and value != node["const"]:
            failures.append(f"{path}: const")
        if "enum" in node and value not in node["enum"]:
            failures.append(f"{path}: enum")
        if "minItems" in node and isinstance(value, list) and len(value) < node["minItems"]:
            failures.append(f"{path}: minItems")
        if "minimum" in node and isinstance(value, int) and value < node["minimum"]:
            failures.append(f"{path}: minimum")
        expected_type = node.get("type")
        if expected_type is None and "properties" in node and isinstance(value, dict):
            for key, child in node.get("properties", {}).items():
                if key in value:
                    if "$ref" in child:
                        child = schema["$defs"][child["$ref"].split("/")[-1]]
                    check(child, value[key], f"{path}.{key}")
            return
        if expected_type == "object":
            if not isinstance(value, dict):
                failures.append(f"{path}: object")
                return
            for key in node.get("required", []):
                if key not in value:
                    failures.append(f"{path}.{key}: missing")
            if "minProperties" in node and len(value) < node["minProperties"]:
                failures.append(f"{path}: minProperties")
            if "anyOf" in node:
                branch_results = []
                for branch in node["anyOf"]:
                    branch_failures: list[str] = []
                    old_failures = failures[:]
                    failures.clear()
                    check(branch, value, path)
                    branch_failures.extend(failures)
                    failures[:] = old_failures
                    branch_results.append(branch_failures)
                if not any(not result for result in branch_results):
                    failures.append(f"{path}: anyOf")
            if "oneOf" in node:
                passing = 0
                for branch in node["oneOf"]:
                    old_failures = failures[:]
                    failures.clear()
                    check(branch, value, path)
                    branch_passes = not failures
                    failures[:] = old_failures
                    if branch_passes:
                        passing += 1
                if passing != 1:
                    failures.append(f"{path}: oneOf")
            for key, child in node.get("properties", {}).items():
                if key in value:
                    if "$ref" in child:
                        child = schema["$defs"][child["$ref"].split("/")[-1]]
                    check(child, value[key], f"{path}.{key}")
        elif expected_type == "array":
            if not isinstance(value, list):
                failures.append(f"{path}: array")
                return
            if "minItems" in node and len(value) < node["minItems"]:
                failures.append(f"{path}: minItems")
            child = node.get("items")
            if child:
                if "$ref" in child:
                    child = schema["$defs"][child["$ref"].split("/")[-1]]
                for index, item in enumerate(value):
                    check(child, item, f"{path}[{index}]")
        elif expected_type == "string":
            if not isinstance(value, str):
                failures.append(f"{path}: string")
            elif "minLength" in node and len(value) < node["minLength"]:
                failures.append(f"{path}: minLength")
        elif expected_type == "integer":
            if not isinstance(value, int):
                failures.append(f"{path}: integer")
            elif "minimum" in node and value < node["minimum"]:
                failures.append(f"{path}: minimum")
        elif expected_type == "boolean" and not isinstance(value, bool):
            failures.append(f"{path}: boolean")

    check(schema, data, "$")
    return failures


class ToolTests(unittest.TestCase):
    def run_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, *args], cwd=ROOT, text=True, capture_output=True)

    def test_privacy_scan_passes_repository(self) -> None:
        result = self.run_tool("tools/privacy_scan.py", ".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_drop_check_passes(self) -> None:
        result = self.run_tool("tools/no_drop_check.py", ".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_history_scan_passes_repository(self) -> None:
        result = self.run_tool("tools/history_scan.py", ".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_history_scan_fails_without_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            subprocess.run(["git", "init", "-b", "main"], cwd=target, check=True, capture_output=True)
            result = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "history_scan.py"), str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing HEAD", result.stdout)

    def test_link_check_passes_repository(self) -> None:
        result = self.run_tool("tools/link_check.py", ".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bootstrap_dry_run_and_apply_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            dry = self.run_tool("tools/bootstrap.py", "--destination", str(target), "--adapter", "generic")
            self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
            self.assertFalse((target / "objective-integrity-adapter.md").exists())
            applied = self.run_tool("tools/bootstrap.py", "--destination", str(target), "--adapter", "generic", "--apply")
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            self.assertTrue((target / "objective-integrity-adapter.md").exists())
            backups = sorted((target / ".objective-integrity-backups").iterdir())
            rolled = self.run_tool("tools/bootstrap.py", "--rollback", str(backups[-1]))
            self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
            self.assertFalse((target / "objective-integrity-adapter.md").exists())

    def test_bootstrap_skill_book_adapter_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            applied = self.run_tool("tools/bootstrap.py", "--destination", str(target), "--adapter", "skill-book", "--apply")
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            self.assertTrue((target / "objective-integrity" / "skill-selection-receipt.md").exists())
            backups = sorted((target / ".objective-integrity-backups").iterdir())
            rolled = self.run_tool("tools/bootstrap.py", "--rollback", str(backups[-1]))
            self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
            self.assertFalse((target / "objective-integrity" / "skill-selection-receipt.md").exists())

    def test_bootstrap_refuses_agents_destination_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".agents"
            target.mkdir()
            result = self.run_tool("tools/bootstrap.py", "--destination", str(target), "--adapter", "codex", "--apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing global-looking destination", result.stderr + result.stdout)

    def test_exact_action_check_blocks_foreach_pipe(self) -> None:
        result = self.run_tool(
            "tools/exact_action_check.py",
            "--command",
            "foreach ($item in $items) { $item } | Select-Object -First 1",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("mechanical::powershell-foreach-pipe", result.stdout)

    def test_exact_action_check_allows_safe_command(self) -> None:
        result = self.run_tool("tools/exact_action_check.py", "--command", "Get-ChildItem | Select-Object -First 1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_skill_lifecycle_prevented_requires_preserved_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "effect.json"
            path.write_text(json.dumps({
                "event_id": "EVT-1",
                "objective_id": "OBJ-1",
                "source_claims": ["SRC-1"],
                "skill_id": "SKILL-1",
                "skill_version": "1.0.0",
                "registry_sha256": "A" * 64,
                "selection_snapshot_sha256": "B" * 64,
                "planned_objective_delta": "block a known parser family before execution",
                "actual_objective_delta": "blocked before execution",
                "planned_evidence_delta": "preflight receipt",
                "actual_evidence_delta": "preflight receipt",
                "outcome": "prevented",
                "consumer_result": "not observed",
                "side_effects": [],
                "rollback_result": "not needed",
                "elapsed_ms": 1,
                "tool_calls": 1,
                "rework": "none",
                "proof_ceiling": "mechanical prevention only"
            }), encoding="utf-8")
            result = self.run_tool("tools/skill_lifecycle.py", "effect", "--input", str(path))
            self.assertEqual(result.returncode, 2)
            self.assertIn("candidate_preserved", result.stdout)

    def test_skill_resolver_returns_selected_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_root = root / "skills"
            skill_dir = skill_root / "example-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: example-skill\ndescription: Example.\n---\n\n# Example\n", encoding="utf-8")
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": "skill-registry-v1",
                "registry_id": "REG-1",
                "entries": [
                    {
                        "skill_id": "SKILL-1",
                        "name": "example-skill",
                        "version": "1.0.0",
                        "origin": "project",
                        "relative_path": "example-skill",
                        "status": "active-bounded",
                        "disclosure_class": "blind_safe_mechanical",
                        "match_clauses": [{"job": ["repair"]}],
                        "proof_ceiling": "selection identity"
                    },
                    {
                        "skill_id": "SKILL-2",
                        "name": "other-skill",
                        "version": "1.0.0",
                        "origin": "project",
                        "relative_path": "other-skill",
                        "status": "active-bounded",
                        "disclosure_class": "blind_safe_mechanical",
                        "match_clauses": [{"job": ["deployment"]}],
                        "proof_ceiling": "selection identity"
                    }
                ]
            }), encoding="utf-8")
            request = root / "input.json"
            request.write_text(json.dumps({
                "objective_id": "OBJ-1",
                "source_claims": ["SRC-1"],
                "job": ["repair"],
                "action": [],
                "tool": [],
                "environment": [],
                "cause_families": [],
                "permissions": [],
                "resources": [],
                "consumers": [],
                "risks": [],
                "blind_phase": "none"
            }), encoding="utf-8")
            result = self.run_tool(
                "tools/skill_resolver.py",
                "--registry",
                str(registry),
                "--input",
                str(request),
                "--root",
                f"project={skill_root}",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(len(receipt["selected"]), 1)
            self.assertEqual(len(receipt["rejected"]), 1)
            self.assertEqual(receipt["selected"][0]["path_status"], "ok")

    def test_skill_resolver_blind_initial_withholds_non_safe_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_root = root / "skills"
            skill_dir = skill_root / "semantic-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: semantic-skill\ndescription: Example.\n---\n\n# Example\n", encoding="utf-8")
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": "skill-registry-v1",
                "registry_id": "REG-1",
                "entries": [{
                    "skill_id": "SECRET-SKILL-ID",
                    "name": "semantic-skill",
                    "version": "1.0.0",
                    "origin": "project",
                    "relative_path": "semantic-skill",
                    "status": "active-bounded",
                    "disclosure_class": "post-blind-semantic",
                    "match_clauses": [{"job": ["audit"]}],
                    "proof_ceiling": "selection identity",
                    "rollback": "discard"
                }]
            }), encoding="utf-8")
            request = root / "input.json"
            request.write_text(json.dumps({
                "objective_id": "OBJ-1",
                "source_claims": ["SRC-1"],
                "job": ["audit"],
                "blind_phase": "initial"
            }), encoding="utf-8")
            result = self.run_tool("tools/skill_resolver.py", "--registry", str(registry), "--input", str(request), "--root", f"project={skill_root}")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("SECRET-SKILL-ID", result.stdout)
            self.assertEqual(json.loads(result.stdout)["withheld_non_blind_safe_count"], 1)

    def test_skill_resolver_rejects_duplicate_active_names_across_origins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_root = root / "user"
            project_root = root / "project"
            for base in [user_root, project_root]:
                skill_dir = base / "same-name"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text("---\nname: same-name\ndescription: Example.\n---\n\n# Example\n", encoding="utf-8")
            registry = root / "registry.json"
            entries = []
            for origin in ["user", "project"]:
                entries.append({
                    "skill_id": f"SKILL-{origin}",
                    "name": "same-name",
                    "version": "1.0.0",
                    "origin": origin,
                    "relative_path": "same-name",
                    "status": "active-bounded",
                    "disclosure_class": "blind_safe_mechanical",
                    "match_clauses": [{"job": ["repair"]}],
                    "proof_ceiling": "selection identity",
                    "rollback": "discard"
                })
            registry.write_text(json.dumps({"schema_version": "skill-registry-v1", "registry_id": "REG-1", "entries": entries}), encoding="utf-8")
            request = root / "input.json"
            request.write_text(json.dumps({"objective_id": "OBJ-1", "source_claims": ["SRC-1"], "job": ["repair"], "blind_phase": "none"}), encoding="utf-8")
            result = self.run_tool(
                "tools/skill_resolver.py",
                "--registry",
                str(registry),
                "--input",
                str(request),
                "--root",
                f"user={user_root}",
                "--root",
                f"project={project_root}",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["selected"], [])
            self.assertIn("duplicate_active_origin_name_conflict", result.stdout)

    def test_strengthened_schemas_reject_hollow_records(self) -> None:
        skill_receipt_schema = json.loads((ROOT / "schemas" / "skill-selection-receipt.schema.json").read_text(encoding="utf-8"))
        lifecycle_schema = json.loads((ROOT / "schemas" / "skill-lifecycle-record.schema.json").read_text(encoding="utf-8"))
        scenario_schema = json.loads((ROOT / "schemas" / "scenario-interaction-ledger.schema.json").read_text(encoding="utf-8"))

        empty_receipt = {
            "schema_version": "skill-selection-receipt-v1",
            "objective_id": "OBJ",
            "source_claims": ["SRC"],
            "registry_sha256": "A" * 64,
            "selection_snapshot_sha256": "B" * 64,
            "selected": [],
            "rejected": [],
            "fallback_normal_workflow": True,
            "proof_ceiling": "identity only",
        }
        hollow_lifecycle = {
            "schema_version": "skill-transition-v1",
            "event_id": "EVT",
            "objective_id": "OBJ",
            "source_claims": ["SRC"],
            "skill_id": "SKILL",
            "state": {},
            "evidence": {},
            "proof_ceiling": "structural only",
        }
        hollow_scenario = {
            "record_id": "SCN",
            "status": "draft",
            "scenario_families": [{}],
            "interaction_closure": {},
            "boundary_state_continuity": [{}],
            "workload_progress": {},
            "semantic_recomposition": {},
            "evidence_limit": "draft only",
        }
        self.assertTrue(simple_schema_validate(skill_receipt_schema, empty_receipt))
        self.assertTrue(simple_schema_validate(lifecycle_schema, hollow_lifecycle))
        self.assertTrue(simple_schema_validate(scenario_schema, hollow_scenario))


if __name__ == "__main__":
    unittest.main()
