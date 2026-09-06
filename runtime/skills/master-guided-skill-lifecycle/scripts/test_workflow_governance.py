import hashlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "workflow_governance", Path(__file__).with_name("workflow_governance.py")
)
g = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(g)


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.refs = {}
        for name, text in {
            "user": "user condition v1\n",
            "method": "delegated method v1\n",
            "higher": "higher priority v1\n",
            "unknown": "unclassified v1\n",
            "source": "source event\n",
            "evaluation": "evaluation plan\n",
            "independent": "independent review\n",
            "rollback": "rollback plan\n",
            "approval": "approval source bytes\n",
            "semantic": "semantic review bytes\n",
        }.items():
            path = self.root / f"{name}.txt"
            path.write_text(text, encoding="utf-8", newline="")
            self.refs[name] = self.ref(path)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def ref(path):
        raw = Path(path).read_bytes()
        return {"path": str(Path(path).absolute()), "sha256": hashlib.sha256(raw).hexdigest().upper()}

    def condition(self, condition_id, tier, ref_name):
        return {"id": condition_id, "tier": tier, "text_ref": self.refs[ref_name]}

    def proposal(self, changes, dependency_ids=None, scope="global-workflow"):
        return {
            "id": "gov-proposal-1",
            "version": "1.0.0",
            "owner": "COORDINATED-WORK",
            "scope": scope,
            "changes": [
                {
                    "condition_id": condition_id,
                    "before_sha256": before_sha256,
                    "after_text": after_text,
                }
                for condition_id, before_sha256, after_text in changes
            ],
            "dependency_ids": dependency_ids or [item[0] for item in changes],
            "source_refs": [self.refs["source"]],
            "rationale": "bounded governance improvement",
            "evaluation_ref": self.refs["evaluation"],
            "independent_ref": self.refs["independent"],
            "rollback_ref": self.refs["rollback"],
            "next_consumer": "independent early reviewer",
            "return_step": "return to the source-bound objective",
        }

    def request(self, proposal=None, conditions=None, approval=None, effect_state="NONE"):
        return {
            "schema_version": "workflow-governance-v1",
            "proposal": proposal,
            "current_conditions": conditions if conditions is not None else [],
            "approval": approval,
            "as_of": "2026-09-07T12:00:00+09:00",
            "effect_state": effect_state,
        }

    def approval(self, proposal, condition_ids, **overrides):
        value = {
            "proposal_sha256": g._canonical_sha256(proposal),
            "scope": proposal["scope"],
            "condition_ids": sorted(condition_ids),
            "source_ref": self.refs["approval"],
            "decision": "APPROVE",
            "expires_at": "2026-09-08T12:00:00+09:00",
            "withdrawn": False,
            "semantic_review_ref": self.refs["semantic"],
        }
        value.update(overrides)
        return value

    def evaluate(self, request):
        path = self.root / "request.json"
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8", newline="")
        return g.evaluate(str(path.absolute()))

    def snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def assert_non_authorizing(self, result):
        self.assertIs(result["authority_granted"], False)
        self.assertEqual(result["semantic_authority"], "UNPROVEN")
        self.assertIs(result["dependent_scope"]["unrelated_work_eligible"], True)

    def test_quiet_no_candidate_is_lightweight_no_change(self):
        result = self.evaluate(self.request())
        self.assertEqual(result["route"], "NO_CHANGE")
        self.assertEqual(result["condition_dispositions"], [])
        self.assert_non_authorizing(result)

    def test_delegated_method_candidate_stays_candidate_only(self):
        condition = self.condition("METHOD-1", "DELEGATED_METHOD", "method")
        proposal = self.proposal([("METHOD-1", self.refs["method"]["sha256"], "delegated method v2")])
        result = self.evaluate(self.request(proposal, [condition]))
        self.assertEqual(result["route"], "WITHIN_AUTHORITY_CANDIDATE")
        self.assertEqual(result["condition_dispositions"][0]["disposition"], "CHANGED_CANDIDATE")
        self.assert_non_authorizing(result)

    def test_user_condition_without_approval_needs_user_decision(self):
        condition = self.condition("USER-1", "USER_CONDITION", "user")
        proposal = self.proposal([("USER-1", self.refs["user"]["sha256"], "user condition v2")])
        result = self.evaluate(self.request(proposal, [condition]))
        self.assertEqual(result["route"], "USER_DECISION")
        self.assertEqual([x["code"] for x in result["reasons"]], ["APPROVAL_ABSENT"])
        self.assert_non_authorizing(result)

    def test_matching_approval_requires_source_review(self):
        condition = self.condition("USER-1", "USER_CONDITION", "user")
        proposal = self.proposal([("USER-1", self.refs["user"]["sha256"], "radically revised")])
        approval = self.approval(proposal, ["USER-1"])
        result = self.evaluate(self.request(proposal, [condition], approval))
        self.assertEqual(result["route"], "MATCHED_APPROVAL_REQUIRES_SOURCE_REVIEW")
        self.assertEqual(result["reasons"][0]["code"], "STRUCTURAL_MATCH_ONLY")
        self.assert_non_authorizing(result)

    def test_copied_or_self_authored_approval_bytes_never_become_authority(self):
        condition = self.condition("USER-1", "USER_CONDITION", "user")
        proposal = self.proposal([("USER-1", self.refs["user"]["sha256"], "copied proposal")])
        approval = self.approval(proposal, ["USER-1"])
        first = self.evaluate(self.request(proposal, [condition], approval))
        second = self.evaluate(self.request(proposal, [condition], approval))
        self.assertEqual(first["route"], "MATCHED_APPROVAL_REQUIRES_SOURCE_REVIEW")
        self.assertEqual(first["proposal_sha256"], second["proposal_sha256"])
        self.assert_non_authorizing(first)

    def test_all_approval_failure_families_are_preserved(self):
        condition = self.condition("USER-1", "USER_CONDITION", "user")
        proposal = self.proposal([("USER-1", self.refs["user"]["sha256"], "v2")])
        approval = self.approval(
            proposal,
            ["OTHER"],
            proposal_sha256="0" * 64,
            scope="other-chat",
            decision="REJECT",
            expires_at="2026-09-06T12:00:00+09:00",
            withdrawn=True,
        )
        result = self.evaluate(self.request(proposal, [condition], approval))
        self.assertEqual(result["route"], "USER_DECISION")
        self.assertEqual(
            {item["code"] for item in result["reasons"]},
            {
                "APPROVAL_REJECTED", "APPROVAL_WITHDRAWN", "APPROVAL_EXPIRED",
                "APPROVAL_PROPOSAL_MISMATCH", "APPROVAL_SCOPE_MISMATCH",
                "APPROVAL_CONDITION_IDS_MISMATCH",
            },
        )
        self.assert_non_authorizing(result)

    def test_changed_baseline_precedes_matching_approval(self):
        condition = self.condition("USER-1", "USER_CONDITION", "user")
        proposal = self.proposal([("USER-1", "1" * 64, "v2")])
        approval = self.approval(proposal, ["USER-1"])
        result = self.evaluate(self.request(proposal, [condition], approval))
        self.assertEqual(result["route"], "REFRESH_DEPENDENT_BASELINE")
        self.assertIn("CURRENT_CONDITION_CHANGED", [item["code"] for item in result["reasons"]])
        self.assert_non_authorizing(result)

    def test_higher_priority_precedes_approval(self):
        condition = self.condition("SYS-1", "HIGHER_PRIORITY", "higher")
        proposal = self.proposal([("SYS-1", self.refs["higher"]["sha256"], "attempted override")])
        approval = self.approval(proposal, ["SYS-1"])
        result = self.evaluate(self.request(proposal, [condition], approval))
        self.assertEqual(result["route"], "OUTSIDE_USER_AMENDMENT")
        self.assert_non_authorizing(result)

    def test_unknown_tier_requires_authority_resolution(self):
        condition = self.condition("UNK-1", "UNKNOWN", "unknown")
        proposal = self.proposal([("UNK-1", self.refs["unknown"]["sha256"], "classified later")])
        result = self.evaluate(self.request(proposal, [condition]))
        self.assertEqual(result["route"], "RESOLVE_AUTHORITY")
        self.assert_non_authorizing(result)

    def test_unknown_effect_precedes_baseline_and_authority_routes(self):
        condition = self.condition("SYS-1", "HIGHER_PRIORITY", "higher")
        proposal = self.proposal([("SYS-1", "2" * 64, "attempted retry")])
        result = self.evaluate(self.request(proposal, [condition], effect_state="UNKNOWN"))
        self.assertEqual(result["route"], "RECONCILE_UNKNOWN_EFFECT")
        self.assertEqual(result["first_fault"]["code"], "UNKNOWN_EFFECT")
        self.assert_non_authorizing(result)

    def test_unknown_effect_precedes_null_proposal(self):
        result = self.evaluate(self.request(effect_state="UNKNOWN"))
        self.assertEqual(result["route"], "RECONCILE_UNKNOWN_EFFECT")
        self.assert_non_authorizing(result)

    def test_dependency_closed_multi_condition_bundle(self):
        conditions = [
            self.condition("METHOD-1", "DELEGATED_METHOD", "method"),
            self.condition("USER-1", "USER_CONDITION", "user"),
        ]
        changes = [
            ("METHOD-1", self.refs["method"]["sha256"], "method v2"),
            ("USER-1", self.refs["user"]["sha256"], "user v2"),
        ]
        proposal = self.proposal(changes, dependency_ids=["METHOD-1", "USER-1"])
        approval = self.approval(proposal, ["METHOD-1", "USER-1"])
        result = self.evaluate(self.request(proposal, conditions, approval))
        self.assertEqual(result["route"], "MATCHED_APPROVAL_REQUIRES_SOURCE_REVIEW")
        self.assertEqual({x["id"] for x in result["condition_dispositions"]}, {"METHOD-1", "USER-1"})
        self.assert_non_authorizing(result)

    def test_unchanged_dependency_is_preserved_without_becoming_an_approval_target(self):
        conditions = [
            self.condition("USER-1", "USER_CONDITION", "user"),
            self.condition("METHOD-1", "DELEGATED_METHOD", "method"),
        ]
        proposal = self.proposal(
            [("USER-1", self.refs["user"]["sha256"], "user v2")],
            dependency_ids=["METHOD-1", "USER-1"],
        )
        approval = self.approval(proposal, ["USER-1"])
        result = self.evaluate(self.request(proposal, conditions, approval))
        by_id = {item["id"]: item for item in result["condition_dispositions"]}
        self.assertEqual(result["route"], "MATCHED_APPROVAL_REQUIRES_SOURCE_REVIEW")
        self.assertEqual(by_id["METHOD-1"]["disposition"], "PRESERVED_UNCHANGED")
        self.assertIs(by_id["METHOD-1"]["dependency"], True)
        self.assert_non_authorizing(result)

    def test_dependency_omission_is_invalid_request(self):
        condition = self.condition("METHOD-1", "DELEGATED_METHOD", "method")
        proposal = self.proposal(
            [("METHOD-1", self.refs["method"]["sha256"], "method v2")], dependency_ids=["OTHER"]
        )
        result = self.evaluate(self.request(proposal, [condition]))
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertIn("DEPENDENCY_OMISSION", [item["code"] for item in result["reasons"]])
        self.assert_non_authorizing(result)

    def test_missing_current_condition_refreshes_dependent_baseline(self):
        proposal = self.proposal([("MISSING-1", "3" * 64, "candidate")])
        result = self.evaluate(self.request(proposal, []))
        self.assertEqual(result["route"], "REFRESH_DEPENDENT_BASELINE")
        self.assertEqual(result["first_fault"]["code"], "MISSING_CURRENT_CONDITION")
        self.assert_non_authorizing(result)

    def test_relative_bound_reference_is_rejected(self):
        condition = self.condition("METHOD-1", "DELEGATED_METHOD", "method")
        condition["text_ref"]["path"] = "method.txt"
        proposal = self.proposal([("METHOD-1", self.refs["method"]["sha256"], "v2")])
        result = self.evaluate(self.request(proposal, [condition]))
        self.assertEqual(result["route"], "REFRESH_DEPENDENT_BASELINE")
        self.assertEqual(result["source_hashes"][0]["error_code"], "PATH_NOT_ABSOLUTE")
        self.assert_non_authorizing(result)

    def test_proposal_reference_hash_mismatch_is_invalid_request(self):
        condition = self.condition("METHOD-1", "DELEGATED_METHOD", "method")
        proposal = self.proposal([("METHOD-1", self.refs["method"]["sha256"], "v2")])
        proposal["source_refs"][0]["sha256"] = "4" * 64
        result = self.evaluate(self.request(proposal, [condition]))
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertEqual(result["first_fault"]["code"], "HASH_MISMATCH")
        self.assert_non_authorizing(result)

    def test_duplicate_json_keys_fail_closed(self):
        path = self.root / "duplicate.json"
        path.write_text(
            '{"schema_version":"workflow-governance-v1","schema_version":"other"}',
            encoding="utf-8",
        )
        result = g.evaluate(str(path.absolute()))
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertEqual(result["first_fault"]["code"], "DUPLICATE_JSON_KEY")
        self.assert_non_authorizing(result)

    def test_nonfinite_number_fails_closed(self):
        path = self.root / "nonfinite.json"
        path.write_text('{"schema_version":NaN}', encoding="utf-8")
        result = g.evaluate(str(path.absolute()))
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertIn("nonfinite", result["first_fault"]["detail"])
        self.assert_non_authorizing(result)

    def test_overflowing_json_float_fails_closed(self):
        path = self.root / "overflow-float.json"
        path.write_text('{"schema_version":1e999}', encoding="utf-8")
        result = g.evaluate(str(path.absolute()))
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertIn("nonfinite", result["first_fault"]["detail"])
        self.assert_non_authorizing(result)

    def test_unknown_fields_fail_closed(self):
        request = self.request()
        request["permission"] = True
        result = self.evaluate(request)
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertEqual(result["first_fault"]["code"], "UNKNOWN_FIELD")
        self.assert_non_authorizing(result)

    def test_all_enum_fields_reject_unhashable_json_values(self):
        condition = self.condition("USER-1", "USER_CONDITION", "user")
        proposal = self.proposal([("USER-1", self.refs["user"]["sha256"], "v2")])
        approval = self.approval(proposal, ["USER-1"])
        cases = []
        for invalid in (["UNKNOWN"], {"value": "UNKNOWN"}):
            request = copy.deepcopy(self.request(proposal, [condition], approval))
            request["effect_state"] = invalid
            cases.append(("effect_state", request))
        for invalid in (["USER_CONDITION"], {"value": "USER_CONDITION"}):
            request = copy.deepcopy(self.request(proposal, [condition], approval))
            request["current_conditions"][0]["tier"] = invalid
            cases.append(("current_conditions[0].tier", request))
        for invalid in (["APPROVE"], {"value": "APPROVE"}):
            request = copy.deepcopy(self.request(proposal, [condition], approval))
            request["approval"]["decision"] = invalid
            cases.append(("approval.decision", request))
        for label, request in cases:
            with self.subTest(label=label, invalid=request):
                result = self.evaluate(request)
                self.assertEqual(result["route"], "INVALID_REQUEST")
                details = {"effect_state": "must be NONE, KNOWN, or UNKNOWN",
                           "current_conditions[0].tier": "invalid authority tier",
                           "approval.decision": "must be APPROVE or REJECT"}
                expected = {"code": "MALFORMED_ENUM", "field": label, "detail": details[label]}
                self.assertEqual([item for item in result["reasons"] if item["code"] == "MALFORMED_ENUM"], [expected])
                self.assertEqual(result["first_fault"], expected)
                self.assert_non_authorizing(result)

    def test_lone_surrogate_in_value_returns_structured_invalid_result(self):
        request = self.request()
        request["as_of"] = "2026-09-07T12:00:00+09:00\ud800"
        path = self.root / "surrogate-value.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        result = g.evaluate(str(path.absolute()))
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertEqual(result["first_fault"]["code"], "INVALID_UNICODE_SCALAR")
        self.assert_non_authorizing(result)

    def test_lone_surrogate_in_key_returns_structured_cli_result(self):
        request = self.request()
        request["bad\ud800key"] = "value"
        path = self.root / "surrogate-key.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = g.main(["--input", str(path.absolute())])
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertEqual(result["first_fault"]["code"], "INVALID_UNICODE_SCALAR")
        self.assert_non_authorizing(result)

    def test_nul_bound_path_returns_structured_invalid_result(self):
        condition = self.condition("METHOD-1", "DELEGATED_METHOD", "method")
        condition["text_ref"]["path"] += "\x00escape"
        proposal = self.proposal([("METHOD-1", self.refs["method"]["sha256"], "v2")])
        result = self.evaluate(self.request(proposal, [condition]))
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertEqual(result["first_fault"]["code"], "INVALID_PATH")
        self.assert_non_authorizing(result)

    def test_invalid_cli_input_path_scalars_return_structured_result(self):
        for invalid in (str(self.root / "bad") + "\x00path", str(self.root / "bad") + "\ud800path"):
            with self.subTest(invalid=repr(invalid)):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = g.main(["--input", invalid])
                result = json.loads(output.getvalue())
                self.assertEqual(exit_code, 2)
                self.assertEqual(result["route"], "INVALID_REQUEST")
                self.assertEqual(result["first_fault"]["code"], "INVALID_PATH")
                self.assert_non_authorizing(result)

    def test_naive_as_of_time_fails_closed(self):
        request = self.request()
        request["as_of"] = "2026-09-07T12:00:00"
        result = self.evaluate(request)
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertIn("MALFORMED_TIME", [item["code"] for item in result["reasons"]])
        self.assert_non_authorizing(result)

    def test_empty_after_text_is_rejected(self):
        condition = self.condition("METHOD-1", "DELEGATED_METHOD", "method")
        proposal = self.proposal([("METHOD-1", self.refs["method"]["sha256"], "")])
        result = self.evaluate(self.request(proposal, [condition]))
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assert_non_authorizing(result)

    def test_unicode_is_hashed_from_canonical_utf8(self):
        condition = self.condition("METHOD-SAMPLE", "DELEGATED_METHOD", "method")
        proposal = self.proposal([("METHOD-SAMPLE", self.refs["method"]["sha256"], "Improvement proposal 🌱")])
        result = self.evaluate(self.request(proposal, [condition]))
        expected = hashlib.sha256(
            json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest().upper()
        self.assertEqual(result["proposal_sha256"], expected)
        self.assertEqual(result["route"], "WITHIN_AUTHORITY_CANDIDATE")
        self.assert_non_authorizing(result)

    def test_bound_files_are_unchanged_and_duplicate_paths_read_once(self):
        condition = self.condition("METHOD-1", "DELEGATED_METHOD", "method")
        proposal = self.proposal([("METHOD-1", self.refs["method"]["sha256"], "v2")])
        proposal["source_refs"].append(dict(proposal["source_refs"][0]))
        request = self.request(proposal, [condition])
        original = g._read_bound_file
        seen = []

        def recording_read(path, validated=None):
            seen.append(os.path.normcase(os.path.normpath(path)))
            return original(path, validated)

        request_path = self.root / "request.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        before = self.snapshot()
        source_key = os.path.normcase(os.path.normpath(self.refs["source"]["path"]))
        with mock.patch.object(g, "_read_bound_file", side_effect=recording_read):
            result = g.evaluate(str(request_path.absolute()))
        self.assertEqual(seen.count(source_key), 1)
        self.assertIn("request.json", before)
        self.assertEqual(before, self.snapshot())
        self.assert_non_authorizing(result)

    def test_safe_first_normalized_alias_still_screens_alias_components(self):
        alias_component = self.root / "alias-component"
        alias_component.mkdir()
        condition = self.condition("METHOD-1", "DELEGATED_METHOD", "method")
        proposal = self.proposal([("METHOD-1", self.refs["method"]["sha256"], "v2")])
        safe_ref = dict(self.refs["source"])
        alias_ref = {
            "path": str(alias_component / ".." / Path(self.refs["source"]["path"]).name),
            "sha256": self.refs["source"]["sha256"],
        }
        proposal["source_refs"] = [safe_ref, alias_ref]
        original = g._validate_bound_path
        validated_paths = []

        def validate_each_spelling(path):
            validated_paths.append(path)
            if path == alias_ref["path"]:
                raise g.BoundFileError("REPARSE_PATH", path, "simulated reparse component")
            return original(path)

        with mock.patch.object(g, "_validate_bound_path", side_effect=validate_each_spelling):
            result = self.evaluate(self.request(proposal, [condition]))
        alias_result = next(item for item in result["source_hashes"] if item["role"] == "proposal.source_refs[1]")
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertEqual(alias_result["error_code"], "REPARSE_PATH")
        self.assertIn(safe_ref["path"], validated_paths)
        self.assertIn(alias_ref["path"], validated_paths)
        self.assert_non_authorizing(result)

    def test_invalid_approval_ref_preserves_all_lifecycle_mismatch_reasons(self):
        condition = self.condition("USER-1", "USER_CONDITION", "user")
        proposal = self.proposal([("USER-1", self.refs["user"]["sha256"], "v2")])
        approval = self.approval(
            proposal,
            ["OTHER"],
            proposal_sha256="0" * 64,
            scope="other-chat",
            decision="REJECT",
            expires_at="2026-09-06T12:00:00+09:00",
            withdrawn=True,
        )
        approval["source_ref"] = dict(approval["source_ref"])
        approval["source_ref"]["sha256"] = "5" * 64
        result = self.evaluate(self.request(proposal, [condition], approval, effect_state="UNKNOWN"))
        codes = [item["code"] for item in result["reasons"]]
        self.assertEqual(result["route"], "INVALID_REQUEST")
        self.assertEqual(result["first_fault"]["code"], "HASH_MISMATCH")
        for code in (
            "APPROVAL_REFERENCE_MISMATCH", "APPROVAL_REJECTED", "APPROVAL_WITHDRAWN",
            "APPROVAL_EXPIRED", "APPROVAL_PROPOSAL_MISMATCH", "APPROVAL_SCOPE_MISMATCH",
            "APPROVAL_CONDITION_IDS_MISMATCH", "UNKNOWN_EFFECT",
        ):
            self.assertIn(code, codes)
        self.assert_non_authorizing(result)

    def test_reparse_detection_blocks_only_proposal(self):
        condition = self.condition("METHOD-1", "DELEGATED_METHOD", "method")
        proposal = self.proposal([("METHOD-1", self.refs["method"]["sha256"], "v2")])
        request_path = self.root / "request.json"
        request_path.write_text(json.dumps(self.request(proposal, [condition])), encoding="utf-8")
        original = g._is_reparse

        def mark_method_reparse(st):
            return original(st) or st.st_ino == os.lstat(Path(self.refs["method"]["path"])).st_ino

        with mock.patch.object(g, "_is_reparse", side_effect=mark_method_reparse):
            result = g.evaluate(str(request_path.absolute()))
        self.assertEqual(result["route"], "REFRESH_DEPENDENT_BASELINE")
        self.assertEqual(result["source_hashes"][0]["error_code"], "REPARSE_PATH")
        self.assert_non_authorizing(result)

    def test_actual_symlink_component_is_rejected_when_supported(self):
        target_dir = self.root / "real"
        link_dir = self.root / "link"
        target_dir.mkdir()
        target = target_dir / "bound.txt"
        target.write_text("bound", encoding="utf-8")
        try:
            os.symlink(target_dir, link_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(g.BoundFileError) as caught:
            g._read_bound_file(str((link_dir / "bound.txt").absolute()))
        self.assertEqual(caught.exception.code, "REPARSE_PATH")


if __name__ == "__main__":
    unittest.main()
