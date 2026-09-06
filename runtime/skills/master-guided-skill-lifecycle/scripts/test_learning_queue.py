from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import inspect
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import learning_queue as queue


def metric_set(*, consumer_value: object = "no-effect") -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name in sorted(queue.METRIC_FIELDS):
        if name == "consumer_outcome":
            result[name] = {"status": "observed", "value": consumer_value}
        elif name in {"elapsed_ms", "tool_calls"}:
            result[name] = {"status": "observed", "value": 0}
        elif name == "token_usage":
            result[name] = {"status": "unavailable", "reason": "evidence://token-counter-unavailable"}
        else:
            result[name] = {"status": "not_applicable", "reason": f"evidence://metric-na/{name}"}
    return result


def base_event(
    candidate_id: str = "candidate-1",
    event_id: str = "event-1",
    revision: int = 0,
    state: str = "discovered",
) -> dict[str, object]:
    return {
        "schema_version": "learning-queue-event-v1",
        "event_id": event_id,
        "candidate_id": candidate_id,
        "expected_revision": revision,
        "state": state,
        "owner_ref": "owner://ASTRA-OPT-20260906-01",
        "objective_ref": "objective://O-LEARNING-1",
        "source_refs": ["source://7DC58DFC", "source://7705A234"],
        "project_event_ref": f"project-event://{event_id}",
        "global_disposition_ref": f"global-disposition://{event_id}",
        "family_keys": ["family://queue", "family://learning"],
        "next_consumer_ref": "consumer://authorized-parent",
        "next_use_trigger": "trigger://ordinary-work/queue",
        "evidence_refs": [f"evidence://{event_id}"],
        "rollback_refs": [],
        "predecessor_candidate_ids": [],
    }


def as_prepared(
    event: dict[str, object],
    event_id: str,
    revision: int,
    artifact_path: Path,
    artifact_sha256: str,
) -> dict[str, object]:
    result = copy.deepcopy(event)
    result.update(
        {
            "event_id": event_id,
            "expected_revision": revision,
            "state": "prepared",
            "project_event_ref": f"project-event://{event_id}",
            "global_disposition_ref": f"global-disposition://{event_id}",
            "artifact_ref": {"path": str(artifact_path), "sha256": artifact_sha256},
            "rollback_refs": ["rollback://prepared-artifact"],
            "evidence_refs": [f"evidence://{event_id}"],
        }
    )
    return result


def as_evaluated(
    event: dict[str, object],
    event_id: str,
    revision: int,
    artifact_path: Path,
    artifact_sha256: str,
) -> dict[str, object]:
    result = as_prepared(event, event_id, revision, artifact_path, artifact_sha256)
    result["state"] = "evaluated"
    result.update(
        {
            "evaluation_ref": "evaluation://bounded-replay",
            "independent_challenge_ref": "challenge://independent-1",
            "normal_counterexample_ref": "counterexample://ordinary-no-match",
            "rollback_plan_ref": "rollback-plan://candidate-1",
        }
    )
    return result


def as_active(
    event: dict[str, object],
    event_id: str,
    revision: int,
    artifact_path: Path,
    artifact_sha256: str,
) -> dict[str, object]:
    result = as_evaluated(event, event_id, revision, artifact_path, artifact_sha256)
    result["state"] = "active-bounded"
    result.update(
        {
            "activation_receipt_ref": "activation://actual-bounded-receipt",
            "activation_source_authority_ref": "authority://source-clause-ACT-1",
        }
    )
    return result


def as_measured(
    event: dict[str, object],
    event_id: str,
    revision: int,
    artifact_path: Path,
    artifact_sha256: str,
) -> dict[str, object]:
    result = as_active(event, event_id, revision, artifact_path, artifact_sha256)
    result["state"] = "measured"
    result["measurement"] = {
        "action_ref": "action://later-real-use",
        "outcome_ref": "outcome://later-no-effect",
        "independent_observation_ref": "observation://independent-consumer",
        "metrics": metric_set(),
    }
    return result


class LearningQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "learning.sqlite3"
        self.artifact = self.root / "candidate.md"
        self.artifact.write_bytes(b"bounded candidate artifact\n")
        self.artifact_sha256 = hashlib.sha256(self.artifact.read_bytes()).hexdigest().upper()
        queue.init_db(self.db)

    def assert_error(self, code: str, callable_value, *args, **kwargs) -> queue.QueueError:
        with self.assertRaises(queue.QueueError) as caught:
            callable_value(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.commit_status, "not_committed")
        return caught.exception

    def advance_to_measured(self, candidate_id: str) -> tuple[dict[str, object], dict[str, object]]:
        discovered = base_event(candidate_id, f"{candidate_id}-1")
        queue.upsert_candidate(self.db, discovered)
        queue.upsert_candidate(
            self.db,
            as_prepared(discovered, f"{candidate_id}-2", 1, self.artifact, self.artifact_sha256),
        )
        queue.upsert_candidate(
            self.db,
            as_evaluated(discovered, f"{candidate_id}-3", 2, self.artifact, self.artifact_sha256),
        )
        queue.upsert_candidate(
            self.db,
            as_active(discovered, f"{candidate_id}-4", 3, self.artifact, self.artifact_sha256),
        )
        measured = as_measured(
            discovered, f"{candidate_id}-5", 4, self.artifact, self.artifact_sha256
        )
        queue.upsert_candidate(self.db, measured)
        return discovered, measured

    def test_explicit_absolute_database_and_exact_two_data_tables(self) -> None:
        self.assert_error("DB_PATH_NOT_ABSOLUTE", queue.init_db, "relative.sqlite3")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            triggers = {
                row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
            }
        self.assertEqual(tables, {"queue_events", "candidate_current"})
        self.assertEqual(triggers, {"queue_events_no_update", "queue_events_no_delete"})

    def test_database_reparse_ancestor_is_rejected_when_platform_can_create_one(self) -> None:
        target = self.root / "real-parent"
        target.mkdir()
        link = self.root / "linked-parent"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        self.assert_error("DB_REPARSE_PATH_REJECTED", queue.init_db, link / "queue.sqlite3")

    def test_reads_and_mutation_refuse_absent_database_without_creating_it(self) -> None:
        absent = self.root / "absent.sqlite3"
        self.assert_error("DB_NOT_FOUND", queue.get_status, absent, "candidate-1")
        self.assertFalse(absent.exists())
        self.assert_error("DB_NOT_FOUND", queue.get_history, absent, "candidate-1")
        self.assertFalse(absent.exists())
        self.assert_error(
            "DB_NOT_FOUND",
            queue.find_due,
            absent,
            objective_ref="objective://none",
            family_key="family://none",
            trigger="trigger://none",
        )
        self.assertFalse(absent.exists())
        self.assert_error("DB_NOT_FOUND", queue.upsert_candidate, absent, base_event())
        self.assertFalse(absent.exists())

    def test_readonly_uri_handles_reserved_path_characters(self) -> None:
        special_root = self.root / "space # percent %"
        special_root.mkdir()
        special_db = special_root / "queue #1%.sqlite3"
        queue.init_db(special_db)
        queue.upsert_candidate(special_db, base_event())
        before = special_db.read_bytes()
        self.assertEqual(queue.get_status(special_db, "candidate-1")["revision"], 1)
        self.assertEqual(len(queue.get_history(special_db, "candidate-1")["events"]), 1)
        self.assertEqual(
            queue.find_due(
                special_db,
                objective_ref="objective://O-LEARNING-1",
                family_key="family://queue",
                trigger="trigger://ordinary-work/queue",
            )["match_count"],
            1,
        )
        self.assertEqual(special_db.read_bytes(), before)

    def test_discovered_idempotency_history_and_event_conflict(self) -> None:
        event = base_event()
        first = queue.upsert_candidate(self.db, event)
        second = queue.upsert_candidate(self.db, copy.deepcopy(event))
        self.assertEqual(first, second)
        self.assertEqual(first["commit_status"], "committed")
        self.assertFalse(first["authority_granted"])
        history = queue.get_history(self.db, "candidate-1")
        self.assertEqual(len(history["events"]), 1)
        self.assertEqual(history["events"][0]["payload"]["project_event_ref"], "project-event://event-1")

        changed = copy.deepcopy(event)
        changed["next_use_trigger"] = "trigger://changed"
        self.assert_error("EVENT_ID_CONFLICT", queue.upsert_candidate, self.db, changed)
        self.assertEqual(len(queue.get_history(self.db, "candidate-1")["events"]), 1)

    def test_every_common_reference_binding_is_required(self) -> None:
        for field in (
            "owner_ref",
            "objective_ref",
            "source_refs",
            "project_event_ref",
            "global_disposition_ref",
            "family_keys",
            "next_consumer_ref",
            "next_use_trigger",
            "evidence_refs",
            "rollback_refs",
        ):
            with self.subTest(field=field):
                event = base_event(event_id=f"event-missing-{field.replace('_', '-')}")
                event.pop(field)
                with self.assertRaises(queue.QueueError):
                    queue.upsert_candidate(self.db, event)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM queue_events").fetchone()[0], 0)

    def test_stale_and_illegal_transitions_do_not_mutate(self) -> None:
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        stale = as_prepared(discovered, "event-stale", 0, self.artifact, self.artifact_sha256)
        self.assert_error("STALE_REVISION", queue.upsert_candidate, self.db, stale)
        illegal = as_evaluated(discovered, "event-illegal", 1, self.artifact, self.artifact_sha256)
        self.assert_error("ILLEGAL_TRANSITION", queue.upsert_candidate, self.db, illegal)
        status = queue.get_status(self.db, "candidate-1")
        self.assertEqual((status["revision"], status["state"]), (1, "discovered"))
        self.assertEqual(len(queue.get_history(self.db, "candidate-1")["events"]), 1)

    def test_prepared_requires_absolute_hash_bound_artifact_and_rollback(self) -> None:
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        prepared = as_prepared(discovered, "event-2", 1, self.artifact, self.artifact_sha256)
        no_artifact = copy.deepcopy(prepared)
        no_artifact.pop("artifact_ref")
        self.assert_error("ARTIFACT_REQUIRED", queue.upsert_candidate, self.db, no_artifact)
        bad_hash = copy.deepcopy(prepared)
        bad_hash["artifact_ref"]["sha256"] = "not-a-hash"  # type: ignore[index]
        self.assert_error("INVALID_SHA256", queue.upsert_candidate, self.db, bad_hash)
        wrong_hash = copy.deepcopy(prepared)
        wrong_hash["artifact_ref"]["sha256"] = "B" * 64  # type: ignore[index]
        self.assert_error("ARTIFACT_HASH_MISMATCH", queue.upsert_candidate, self.db, wrong_hash)
        no_rollback = copy.deepcopy(prepared)
        no_rollback["rollback_refs"] = []
        self.assert_error("ROLLBACK_REFERENCE_REQUIRED", queue.upsert_candidate, self.db, no_rollback)
        result = queue.upsert_candidate(self.db, prepared)
        self.assertEqual(result["state"], "prepared")

    def test_evaluated_and_active_require_external_evidence_references(self) -> None:
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        prepared = as_prepared(discovered, "event-2", 1, self.artifact, self.artifact_sha256)
        queue.upsert_candidate(self.db, prepared)
        evaluated = as_evaluated(discovered, "event-3", 2, self.artifact, self.artifact_sha256)
        missing_challenge = copy.deepcopy(evaluated)
        missing_challenge.pop("independent_challenge_ref")
        self.assert_error("INVALID_REFERENCE", queue.upsert_candidate, self.db, missing_challenge)
        queue.upsert_candidate(self.db, evaluated)
        evaluated_result = queue.get_status(self.db, "candidate-1")
        self.assertIn("separately authorized", evaluated_result["next_action_for_owner"])
        active = as_active(discovered, "event-4", 3, self.artifact, self.artifact_sha256)
        active.pop("activation_source_authority_ref")
        self.assert_error("INVALID_REFERENCE", queue.upsert_candidate, self.db, active)
        active = as_active(discovered, "event-4-valid", 3, self.artifact, self.artifact_sha256)
        result = queue.upsert_candidate(self.db, active)
        self.assertFalse(result["authority_granted"])
        self.assertIn("later action outcome", result["next_action_for_owner"])

    def test_measured_requires_later_observation_and_typed_nonplaceholder_metrics(self) -> None:
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        queue.upsert_candidate(
            self.db, as_prepared(discovered, "event-2", 1, self.artifact, self.artifact_sha256)
        )
        queue.upsert_candidate(
            self.db, as_evaluated(discovered, "event-3", 2, self.artifact, self.artifact_sha256)
        )
        queue.upsert_candidate(
            self.db, as_active(discovered, "event-4", 3, self.artifact, self.artifact_sha256)
        )
        measured = as_measured(discovered, "event-5", 4, self.artifact, self.artifact_sha256)

        unknown = copy.deepcopy(measured)
        unknown["measurement"]["metrics"]["invented"] = {  # type: ignore[index]
            "status": "observed",
            "value": 1,
        }
        self.assert_error("INVALID_METRIC_SET", queue.upsert_candidate, self.db, unknown)

        placeholder = copy.deepcopy(measured)
        placeholder["measurement"]["metrics"]["token_usage"]["value"] = 0  # type: ignore[index]
        self.assert_error("METRIC_PLACEHOLDER_REJECTED", queue.upsert_candidate, self.db, placeholder)

        unknown_status = copy.deepcopy(measured)
        unknown_status["measurement"]["metrics"]["elapsed_ms"] = {  # type: ignore[index]
            "status": "guessed",
            "reason": "none",
        }
        self.assert_error("INVALID_METRIC_STATUS", queue.upsert_candidate, self.db, unknown_status)

        result = queue.upsert_candidate(self.db, measured)
        self.assertEqual(result["state"], "measured")
        payload = queue.get_status(self.db, "candidate-1")["payload"]
        self.assertEqual(payload["measurement"]["metrics"]["elapsed_ms"]["value"], 0)
        self.assertEqual(payload["measurement"]["metrics"]["consumer_outcome"]["value"], "no-effect")
        self.assertEqual(payload["measurement"]["metrics"]["token_usage"]["status"], "unavailable")

    def test_no_measurement_is_required_before_bounded_activation(self) -> None:
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        queue.upsert_candidate(
            self.db, as_prepared(discovered, "event-2", 1, self.artifact, self.artifact_sha256)
        )
        queue.upsert_candidate(
            self.db, as_evaluated(discovered, "event-3", 2, self.artifact, self.artifact_sha256)
        )
        active = as_active(discovered, "event-4", 3, self.artifact, self.artifact_sha256)
        self.assertNotIn("measurement", active)
        self.assertEqual(queue.upsert_candidate(self.db, active)["state"], "active-bounded")

    def test_measured_exit_preserves_measurement_and_permits_prepared_resumption(self) -> None:
        discovered, measured = self.advance_to_measured("candidate-defer")
        deferred = copy.deepcopy(measured)
        deferred.update(
            {
                "event_id": "candidate-defer-6",
                "expected_revision": 5,
                "state": "deferred",
                "project_event_ref": "project-event://candidate-defer-6",
                "global_disposition_ref": "global-disposition://candidate-defer-6",
                "reason": "reason://await-next-use",
                "return_trigger": "trigger://next-use-ready",
            }
        )
        queue.upsert_candidate(self.db, deferred)
        self.assertEqual(
            queue.get_status(self.db, "candidate-defer")["payload"]["measurement"],
            measured["measurement"],
        )

        _, omit_base = self.advance_to_measured("candidate-omit")
        omitted = copy.deepcopy(omit_base)
        omitted.update(
            {
                "event_id": "candidate-omit-6",
                "expected_revision": 5,
                "state": "retired",
                "project_event_ref": "project-event://candidate-omit-6",
                "global_disposition_ref": "global-disposition://candidate-omit-6",
                "reason": "reason://harm-observed",
                "return_trigger": "trigger://reviewed-recovery",
            }
        )
        omitted.pop("measurement")
        self.assert_error("STAGE_REFERENCE_CHANGED", queue.upsert_candidate, self.db, omitted)

        changed = copy.deepcopy(omit_base)
        changed.update(
            {
                "event_id": "candidate-omit-6-changed",
                "expected_revision": 5,
                "state": "retired",
                "project_event_ref": "project-event://candidate-omit-6-changed",
                "global_disposition_ref": "global-disposition://candidate-omit-6-changed",
                "reason": "reason://harm-observed",
                "return_trigger": "trigger://reviewed-recovery",
            }
        )
        changed["measurement"]["outcome_ref"] = "outcome://unauthorized-rewrite"  # type: ignore[index]
        self.assert_error("STAGE_REFERENCE_CHANGED", queue.upsert_candidate, self.db, changed)

        _, retire_base = self.advance_to_measured("candidate-retire-success")
        retired = copy.deepcopy(retire_base)
        retired.update(
            {
                "event_id": "candidate-retire-success-6",
                "expected_revision": 5,
                "state": "retired",
                "project_event_ref": "project-event://candidate-retire-success-6",
                "global_disposition_ref": "global-disposition://candidate-retire-success-6",
                "reason": "reason://measured-harm",
                "return_trigger": "trigger://reviewed-recovery",
            }
        )
        queue.upsert_candidate(self.db, retired)
        self.assertEqual(queue.get_status(self.db, "candidate-retire-success")["state"], "retired")

        resume_discovered, _ = self.advance_to_measured("candidate-resume")
        resumed = as_prepared(
            resume_discovered,
            "candidate-resume-6",
            5,
            self.artifact,
            self.artifact_sha256,
        )
        self.assertNotIn("measurement", resumed)
        self.assertEqual(queue.upsert_candidate(self.db, resumed)["state"], "prepared")

        _, supersede_base = self.advance_to_measured("candidate-supersede")
        successor = base_event("candidate-successor", "candidate-successor-1")
        successor["predecessor_candidate_ids"] = ["candidate-supersede"]
        queue.upsert_candidate(self.db, successor)
        superseded = copy.deepcopy(supersede_base)
        superseded.update(
            {
                "event_id": "candidate-supersede-6",
                "expected_revision": 5,
                "state": "superseded",
                "project_event_ref": "project-event://candidate-supersede-6",
                "global_disposition_ref": "global-disposition://candidate-supersede-6",
                "reason": "reason://reviewed-successor",
                "successor_candidate_id": "candidate-successor",
            }
        )
        queue.upsert_candidate(self.db, superseded)
        self.assertEqual(queue.get_status(self.db, "candidate-supersede")["state"], "superseded")

    def test_terminal_state_cannot_introduce_measurement(self) -> None:
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        retired = copy.deepcopy(discovered)
        retired.update(
            {
                "event_id": "event-2",
                "expected_revision": 1,
                "state": "retired",
                "project_event_ref": "project-event://event-2",
                "global_disposition_ref": "global-disposition://event-2",
                "reason": "reason://one-off",
                "return_trigger": "trigger://material-change",
                "measurement": as_measured(
                    discovered, "unused", 1, self.artifact, self.artifact_sha256
                )["measurement"],
            }
        )
        self.assert_error("PREMATURE_MEASUREMENT", queue.upsert_candidate, self.db, retired)

    def test_deferred_due_uses_exact_context_and_never_authorizes(self) -> None:
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        deferred = copy.deepcopy(discovered)
        deferred.update(
            {
                "event_id": "event-2",
                "expected_revision": 1,
                "state": "deferred",
                "project_event_ref": "project-event://event-2",
                "global_disposition_ref": "global-disposition://event-2",
                "evidence_refs": ["evidence://event-2"],
                "reason": "reason://incomplete-evidence",
                "return_trigger": "trigger://evidence-complete",
            }
        )
        queue.upsert_candidate(self.db, deferred)

        wrong_trigger = queue.find_due(
            self.db,
            objective_ref="objective://O-LEARNING-1",
            family_key="family://queue",
            trigger="trigger://ordinary-work/queue",
        )
        self.assertEqual(wrong_trigger["matches"], [])
        due = queue.find_due(
            self.db,
            objective_ref="objective://O-LEARNING-1",
            family_key="family://queue",
            trigger="trigger://evidence-complete",
        )
        self.assertEqual([item["candidate_id"] for item in due["matches"]], ["candidate-1"])
        self.assertFalse(due["authority_granted"])
        self.assertFalse(due["matches"][0]["authority_granted"])
        self.assertIn("does not authorize", due["matches"][0]["next_action_for_owner"])
        repeated = queue.find_due(
            self.db,
            objective_ref="objective://O-LEARNING-1",
            family_key="family://queue",
            trigger="trigger://evidence-complete",
        )
        self.assertEqual(repeated, due)
        self.assertEqual(len(queue.get_history(self.db, "candidate-1")["events"]), 2)

    def test_defer_after_preparation_cannot_drop_underlying_stage_references(self) -> None:
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        prepared = as_prepared(discovered, "event-2", 1, self.artifact, self.artifact_sha256)
        queue.upsert_candidate(self.db, prepared)
        dropped = copy.deepcopy(discovered)
        dropped.update(
            {
                "event_id": "event-3-dropped",
                "expected_revision": 2,
                "state": "deferred",
                "project_event_ref": "project-event://event-3-dropped",
                "global_disposition_ref": "global-disposition://event-3-dropped",
                "evidence_refs": ["evidence://event-3-dropped"],
                "reason": "reason://awaiting-review",
                "return_trigger": "trigger://review-arrives",
            }
        )
        self.assert_error("STAGE_REFERENCE_CHANGED", queue.upsert_candidate, self.db, dropped)
        retained = copy.deepcopy(prepared)
        retained.update(
            {
                "event_id": "event-3",
                "expected_revision": 2,
                "state": "deferred",
                "project_event_ref": "project-event://event-3",
                "global_disposition_ref": "global-disposition://event-3",
                "evidence_refs": ["evidence://event-3"],
                "reason": "reason://awaiting-review",
                "return_trigger": "trigger://review-arrives",
            }
        )
        queue.upsert_candidate(self.db, retained)
        status = queue.get_status(self.db, "candidate-1")
        self.assertEqual(status["payload"]["artifact_ref"]["sha256"], self.artifact_sha256)

    def test_normal_no_matching_knowledge_is_a_read_only_no_effect(self) -> None:
        sentinel = self.root / "unrelated-product.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        database_before = self.db.read_bytes()
        result = queue.find_due(
            self.db,
            objective_ref="objective://none",
            family_key="family://none",
            trigger="trigger://none",
        )
        self.assertEqual(result["match_count"], 0)
        self.assertEqual(self.db.read_bytes(), database_before)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_due_cursor_retrieves_all_pages_and_binds_query_and_snapshot(self) -> None:
        for index in range(3):
            queue.upsert_candidate(
                self.db,
                base_event(f"candidate-page-{index}", f"event-page-{index}"),
            )
        first = queue.find_due(
            self.db,
            objective_ref="objective://O-LEARNING-1",
            family_key="family://queue",
            trigger="trigger://ordinary-work/queue",
            limit=1,
        )
        self.assertEqual(first["returned_count"], 1)
        self.assertEqual(first["remaining_count"], 2)
        self.assertTrue(first["has_more"])
        self.assertIsNotNone(first["next_cursor"])
        self.assertFalse(first["authority_granted"])

        second = queue.find_due(
            self.db,
            objective_ref="objective://O-LEARNING-1",
            family_key="family://queue",
            trigger="trigger://ordinary-work/queue",
            limit=1,
            cursor=first["next_cursor"],
        )
        third = queue.find_due(
            self.db,
            objective_ref="objective://O-LEARNING-1",
            family_key="family://queue",
            trigger="trigger://ordinary-work/queue",
            limit=1,
            cursor=second["next_cursor"],
        )
        all_ids = [
            page["matches"][0]["candidate_id"] for page in (first, second, third)
        ]
        self.assertEqual(all_ids, sorted(all_ids))
        self.assertEqual(len(set(all_ids)), 3)
        self.assertFalse(third["has_more"])
        self.assertIsNone(third["next_cursor"])
        for candidate_id in all_ids:
            self.assertEqual(len(queue.get_history(self.db, candidate_id)["events"]), 1)

        self.assert_error(
            "CURSOR_QUERY_MISMATCH",
            queue.find_due,
            self.db,
            objective_ref="objective://different",
            family_key="family://queue",
            trigger="trigger://ordinary-work/queue",
            limit=1,
            cursor=first["next_cursor"],
        )

        unrelated = base_event("candidate-unrelated", "event-unrelated")
        unrelated["objective_ref"] = "objective://unrelated"
        unrelated["family_keys"] = ["family://unrelated"]
        queue.upsert_candidate(self.db, unrelated)
        self.assert_error(
            "CURSOR_SNAPSHOT_CHANGED",
            queue.find_due,
            self.db,
            objective_ref="objective://O-LEARNING-1",
            family_key="family://queue",
            trigger="trigger://ordinary-work/queue",
            limit=1,
            cursor=first["next_cursor"],
        )

    def test_status_and_history_declare_read_snapshot_boundary(self) -> None:
        status_source = inspect.getsource(queue.get_status)
        history_source = inspect.getsource(queue.get_history)
        self.assertIn("_begin_read(connection)", status_source)
        self.assertIn("_begin_read(connection)", history_source)
        self.assertIn("connection.commit()", status_source)
        self.assertIn("connection.commit()", history_source)

    def test_status_graph_is_one_snapshot_during_concurrent_writer(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
        old = base_event("candidate-old-snapshot", "old-snapshot-1")
        successor = base_event("candidate-new-snapshot", "new-snapshot-1")
        successor["predecessor_candidate_ids"] = ["candidate-old-snapshot"]
        queue.upsert_candidate(self.db, old)
        queue.upsert_candidate(self.db, successor)
        superseded = copy.deepcopy(old)
        superseded.update(
            {
                "event_id": "old-snapshot-2",
                "expected_revision": 1,
                "state": "superseded",
                "project_event_ref": "project-event://old-snapshot-2",
                "global_disposition_ref": "global-disposition://old-snapshot-2",
                "evidence_refs": ["evidence://old-snapshot-2"],
                "reason": "reason://snapshot-successor",
                "successor_candidate_id": "candidate-new-snapshot",
            }
        )
        reader_row_observed = threading.Event()
        writer_done = threading.Event()
        writer_errors: list[str] = []
        original = queue._status_from_row

        def paused_status(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
            reader_row_observed.set()
            if not writer_done.wait(10):
                raise AssertionError("concurrent writer did not finish while read snapshot was open")
            return original(connection, row)

        def writer() -> None:
            if not reader_row_observed.wait(10):
                writer_errors.append("reader did not reach composite boundary")
                writer_done.set()
                return
            try:
                queue.upsert_candidate(self.db, superseded)
            except queue.QueueError as exc:
                writer_errors.append(exc.code)
            finally:
                writer_done.set()

        thread = threading.Thread(target=writer)
        thread.start()
        with mock.patch.object(queue, "_status_from_row", paused_status):
            during = queue.get_status(self.db, "candidate-new-snapshot")
        thread.join(timeout=15)
        self.assertEqual(writer_errors, [])
        self.assertEqual(during["reverse_predecessor_ids"], [])
        after = queue.get_status(self.db, "candidate-new-snapshot")
        self.assertEqual(after["reverse_predecessor_ids"], ["candidate-old-snapshot"])
        self.assertNotEqual(during["snapshot_sha256"], after["snapshot_sha256"])

    def test_history_revision_and_events_are_one_snapshot_during_concurrent_writer(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        prepared = as_prepared(
            discovered, "event-2", 1, self.artifact, self.artifact_sha256
        )
        current_row_observed = threading.Event()
        writer_done = threading.Event()
        writer_errors: list[str] = []
        original = queue._read_candidate_events

        def paused_events(
            connection: sqlite3.Connection, candidate_id: str
        ) -> list[sqlite3.Row]:
            current_row_observed.set()
            if not writer_done.wait(10):
                raise AssertionError("concurrent writer did not finish while history snapshot was open")
            return original(connection, candidate_id)

        def writer() -> None:
            if not current_row_observed.wait(10):
                writer_errors.append("history reader did not reach composite boundary")
                writer_done.set()
                return
            try:
                queue.upsert_candidate(self.db, prepared)
            except queue.QueueError as exc:
                writer_errors.append(exc.code)
            finally:
                writer_done.set()

        thread = threading.Thread(target=writer)
        thread.start()
        with mock.patch.object(queue, "_read_candidate_events", paused_events):
            during = queue.get_history(self.db, "candidate-1")
        thread.join(timeout=15)
        self.assertEqual(writer_errors, [])
        self.assertEqual(during["revision"], 1)
        self.assertEqual(len(during["events"]), 1)
        after = queue.get_history(self.db, "candidate-1")
        self.assertEqual(after["revision"], 2)
        self.assertEqual(len(after["events"]), 2)
        self.assertNotEqual(during["snapshot_sha256"], after["snapshot_sha256"])

    def test_due_snapshot_and_matches_are_consistent_during_concurrent_writer(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
        queue.upsert_candidate(self.db, base_event("candidate-due-old", "due-old-1"))
        new_event = base_event("candidate-due-new", "due-new-1")
        frontier_observed = threading.Event()
        writer_done = threading.Event()
        writer_errors: list[str] = []
        original = queue._projection_snapshot_sha256

        def paused_frontier(connection: sqlite3.Connection) -> str:
            snapshot = original(connection)
            frontier_observed.set()
            if not writer_done.wait(10):
                raise AssertionError("concurrent writer did not finish while due snapshot was open")
            return snapshot

        def writer() -> None:
            if not frontier_observed.wait(10):
                writer_errors.append("due reader did not reach snapshot boundary")
                writer_done.set()
                return
            try:
                queue.upsert_candidate(self.db, new_event)
            except queue.QueueError as exc:
                writer_errors.append(exc.code)
            finally:
                writer_done.set()

        thread = threading.Thread(target=writer)
        thread.start()
        with mock.patch.object(queue, "_projection_snapshot_sha256", paused_frontier):
            during = queue.find_due(
                self.db,
                objective_ref="objective://O-LEARNING-1",
                family_key="family://queue",
                trigger="trigger://ordinary-work/queue",
            )
        thread.join(timeout=15)
        self.assertEqual(writer_errors, [])
        self.assertEqual(during["match_count"], 1)
        after = queue.find_due(
            self.db,
            objective_ref="objective://O-LEARNING-1",
            family_key="family://queue",
            trigger="trigger://ordinary-work/queue",
        )
        self.assertEqual(after["match_count"], 2)
        self.assertNotEqual(during["snapshot_sha256"], after["snapshot_sha256"])

    def test_retire_and_supersede_preserve_history_and_reverse_references(self) -> None:
        old = base_event("candidate-old", "old-1")
        queue.upsert_candidate(self.db, old)
        successor = base_event("candidate-new", "new-1")
        successor["predecessor_candidate_ids"] = ["candidate-old"]
        queue.upsert_candidate(self.db, successor)

        missing_successor = copy.deepcopy(old)
        missing_successor.update(
            {
                "event_id": "old-bad",
                "expected_revision": 1,
                "state": "superseded",
                "project_event_ref": "project-event://old-bad",
                "global_disposition_ref": "global-disposition://old-bad",
                "evidence_refs": ["evidence://old-bad"],
                "reason": "reason://reviewed-merge",
                "successor_candidate_id": "candidate-missing",
            }
        )
        self.assert_error("MISSING_SUCCESSOR", queue.upsert_candidate, self.db, missing_successor)

        superseded = copy.deepcopy(missing_successor)
        superseded.update(
            {
                "event_id": "old-2",
                "successor_candidate_id": "candidate-new",
                "project_event_ref": "project-event://old-2",
                "global_disposition_ref": "global-disposition://old-2",
                "evidence_refs": ["evidence://old-2"],
            }
        )
        queue.upsert_candidate(self.db, superseded)
        old_history = queue.get_history(self.db, "candidate-old")
        self.assertEqual([item["state"] for item in old_history["events"]], ["discovered", "superseded"])
        new_status = queue.get_status(self.db, "candidate-new")
        self.assertEqual(new_status["reverse_predecessor_ids"], ["candidate-old"])

        retired = base_event("candidate-retired", "retired-1")
        queue.upsert_candidate(self.db, retired)
        retired.update(
            {
                "event_id": "retired-2",
                "expected_revision": 1,
                "state": "retired",
                "project_event_ref": "project-event://retired-2",
                "global_disposition_ref": "global-disposition://retired-2",
                "evidence_refs": ["evidence://retired-2"],
                "reason": "reason://one-off-not-reusable",
                "return_trigger": "trigger://material-new-evidence",
            }
        )
        queue.upsert_candidate(self.db, retired)
        self.assertEqual(len(queue.get_history(self.db, "candidate-retired")["events"]), 2)
        due = queue.find_due(
            self.db,
            objective_ref="objective://O-LEARNING-1",
            family_key="family://queue",
            trigger="trigger://material-new-evidence",
        )
        self.assertNotIn("candidate-retired", [item["candidate_id"] for item in due["matches"]])

    def test_atomic_rollback_when_projection_publication_fails(self) -> None:
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER test_projection_failure BEFORE UPDATE ON candidate_current "
                "BEGIN SELECT RAISE(ABORT, 'injected projection failure'); END"
            )
        prepared = as_prepared(discovered, "event-2", 1, self.artifact, self.artifact_sha256)
        self.assert_error("DB_MUTATION_FAILED", queue.upsert_candidate, self.db, prepared)
        self.assertEqual(queue.get_status(self.db, "candidate-1")["revision"], 1)
        self.assertEqual(len(queue.get_history(self.db, "candidate-1")["events"]), 1)

    def test_history_table_rejects_update_and_delete(self) -> None:
        queue.upsert_candidate(self.db, base_event())
        with closing(sqlite3.connect(self.db)) as connection, connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE queue_events SET to_state='retired' WHERE event_id='event-1'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM queue_events WHERE event_id='event-1'")
        self.assertEqual(queue.get_status(self.db, "candidate-1")["state"], "discovered")

    def test_concurrent_writers_use_begin_immediate_and_cas(self) -> None:
        discovered = base_event()
        queue.upsert_candidate(self.db, discovered)
        first = as_prepared(
            discovered, "event-concurrent-a", 1, self.artifact, self.artifact_sha256
        )
        second = as_prepared(
            discovered, "event-concurrent-b", 1, self.artifact, self.artifact_sha256
        )
        barrier = threading.Barrier(3)
        results: list[str] = []
        lock = threading.Lock()

        def writer(event: dict[str, object]) -> None:
            barrier.wait()
            try:
                queue.upsert_candidate(self.db, event)
                outcome = "committed"
            except queue.QueueError as exc:
                outcome = exc.code
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=writer, args=(first,)), threading.Thread(target=writer, args=(second,))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(sorted(results), ["STALE_REVISION", "committed"])
        self.assertEqual(queue.get_status(self.db, "candidate-1")["revision"], 2)
        self.assertEqual(len(queue.get_history(self.db, "candidate-1")["events"]), 2)

    def test_malformed_reference_and_unknown_fields_leave_queue_unchanged(self) -> None:
        bad = base_event()
        bad["source_refs"] = ["embedded\nmaster body"]
        self.assert_error("INVALID_REFERENCE", queue.upsert_candidate, self.db, bad)
        unknown = base_event()
        unknown["semantic_pass"] = True
        self.assert_error("UNKNOWN_EVENT_FIELDS", queue.upsert_candidate, self.db, unknown)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            count = connection.execute("SELECT COUNT(*) FROM queue_events").fetchone()[0]
        self.assertEqual(count, 0)

    def test_postcommit_publication_failure_reports_known_status(self) -> None:
        class BrokenStdout:
            def write(self, _value: str) -> int:
                raise OSError("stdout unavailable")

            def flush(self) -> None:
                pass

        stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", BrokenStdout()), mock.patch.object(sys, "stderr", stderr):
            with self.assertRaises(queue.QueueError) as caught:
                queue._emit_json({"ok": True, "commit_status": "committed"})
        self.assertEqual(caught.exception.commit_status, "committed")
        fallback = json.loads(stderr.getvalue())
        self.assertEqual(fallback["error_code"], "POST_COMMIT_PUBLICATION_FAILED")
        self.assertEqual(fallback["commit_status"], "committed")


if __name__ == "__main__":
    unittest.main()
