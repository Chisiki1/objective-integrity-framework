"""V2 boundary regressions; structural assertions, not semantic proof."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


def receipt():
    # Reuse the frozen legacy fixture function, not its top-level test runner.
    tree = ast.parse((ROOT / "test_v14_transition_actions.py").read_text())
    function = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == "receipt")
    ns = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "legacy-fixture", "exec"), ns)
    value = ns["receipt"]("finalized_dependent_action")
    value.update(schema_version="workflow-transition-admission-v2", owner_role="COORDINATED-WORK", job_id="job", job_shape_sha256="A"*64)
    value["topology"] = dict(read_only_auditor=False, implementing_owner_role="COORDINATED-WORK", owner_task_id="chat", owner_lease_id="lease", chat_id="chat", parent_lease_ref="", resource_claims_ref="scope", recovery_mode="NORMAL")
    value["skill_effect"].update(phase="PRE_ACTION", outcome_capture_ref="")
    value["stage_allocation"].update(view_sha256="", result_ref="", result_file_sha256="", selected_configuration_id="", allocator_script_ref="", allocator_script_sha256="")
    return value


class TransitionTests(unittest.TestCase):
    def run_value(self, value):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "input.json"
            p.write_text(json.dumps(value), encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", str(ROOT / "transition_admission.py"), "--input", str(p)], capture_output=True, text=True, encoding="utf-8")
            self.assertIn(result.returncode, (0, 3), result.stderr)
            return json.loads(result.stdout)

    def test_same_chat_and_child(self):
        value = receipt()
        self.assertEqual(self.run_value(value)["decision"], "ADMIT")
        value["owner_role"] = value["topology"]["implementing_owner_role"] = "BOUNDED-SUBAGENT"
        value["topology"]["owner_task_id"] = "child"
        self.assertNotEqual(self.run_value(value)["decision"], "ADMIT")
        value["topology"]["parent_lease_ref"] = "parent-lease"
        self.assertEqual(self.run_value(value)["decision"], "ADMIT")
        value["topology"]["read_only_auditor"] = True
        self.assertIn("AUDITOR_CANNOT_IMPLEMENT_REVIEWED_CANDIDATE", self.run_value(value)["holds"])

    def test_no_source_or_scenario_bypass(self):
        for mutation in (lambda v: v["topology"].update(chat_id="other"), lambda v: v["correction_admission"].update(authority_ref=""), lambda v: v["scenario_recomposition"].update(unresolved_relation_ids=["orphan"])):
            value = receipt()
            mutation(value)
            self.assertNotEqual(self.run_value(value)["decision"], "ADMIT")

    def test_pre_action_is_not_future_effect(self):
        value = receipt()
        value["skill_effect"].update(applicable=True, action_finalized=True, selected_ref="selection", bytes_ref="bytes", application_ref="plan", outcome_capture_ref="capture")
        self.assertEqual(self.run_value(value)["decision"], "ADMIT")
        value["skill_effect"]["effect_ref"] = "pretend-future-success"
        self.assertIn("FUTURE_EFFECT_CANNOT_AUTHORIZE_ACTION", self.run_value(value)["holds"])
        value["skill_effect"].update(phase="POST_ACTION", effect_ref="")
        self.assertNotEqual(self.run_value(value)["decision"], "ADMIT")

    def test_result_hash_decision_and_input_consumption(self):
        with tempfile.TemporaryDirectory() as d:
            inp, out = Path(d)/"allocation.json", Path(d)/"result.json"
            digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest().upper()
            value = receipt()
            allocator = ROOT.parents[1]/"master-guided-skill-lifecycle/scripts/stage_allocation.py"
            view = dict(schema_version="stage-allocation-v2", objective_id=value["objective_id"], source_sha256=value["source_sha256"], stage_id="candidate-edit", job_id=value["job_id"], job_shape_sha256=value["job_shape_sha256"], trigger="JOB_SHAPE_CHANGED", current_lease_id="lease", owner=dict(lane="COORDINATED-WORK",task_id="chat",lease_id="lease"), context_reuse=dict(benefit_positive=True,evidence="same bounded job",replacement_cost="positive",current_configuration_id="adequate-current"), selection_authority=dict(mode="measured-comparison",source_clause="current-source",configuration_id="",evidence="bounded fixture"), candidates=[dict(configuration_id="adequate-current",capability_needs=[dict(need="read typed structure",met=True,evidence="fixture observation")],representative_eval_version="fixture-v1",risk_adjusted_total_cost=dict(status="unavailable",reason="not measured"),lower_configuration_failure_prediction="none claimed",orchestrator_state=dict(selected_model="fixture",selected_reasoning="high",injectable=True,requested_model="fixture",requested_reasoning="high",accepted="accepted",effective_model=None,effective_reasoning=None,fallback="none"))],proof_ceiling="isolated typed allocation evidence only")
            inp.write_text(json.dumps(view), encoding="utf-8")
            actual = subprocess.run([sys.executable,"-B",str(allocator),"--input",str(inp)],capture_output=True)
            self.assertEqual(actual.returncode,0,actual.stderr)
            body=json.loads(actual.stdout)
            self.assertEqual(body["decision"],"RETAIN_CURRENT")
            out.write_bytes(actual.stdout)
            value["stage_allocation"].update(job_shape_changed=True, view_ref=str(inp), view_sha256=digest(inp), result_ref=str(out), result_file_sha256=digest(out), disposition=body["decision"], selected_configuration_id=body["selected_configuration_id"], allocator_script_ref=str(allocator), allocator_script_sha256=digest(allocator))
            self.assertEqual(self.run_value(value)["decision"], "ADMIT")
            value["stage_allocation"]["disposition"] = "SELECTED"
            self.assertNotEqual(self.run_value(value)["decision"], "ADMIT")
            value["stage_allocation"]["disposition"] = "RETAIN_CURRENT"
            for field in ("objective_id", "source_sha256", "job_id", "job_shape_sha256"):
                other=copy.deepcopy(value)
                other[field]="B"*64 if "sha256" in field else "other"
                self.assertNotEqual(self.run_value(other)["decision"],"ADMIT")
            other=copy.deepcopy(value)
            other["topology"]["owner_lease_id"]="other-lease"
            self.assertNotEqual(self.run_value(other)["decision"],"ADMIT")
            inp.write_text('{"fixture":false}', encoding="utf-8")
            self.assertNotEqual(self.run_value(value)["decision"], "ADMIT")


if __name__ == "__main__":
    unittest.main()
