"""Independent adversarial edge checks against real C++ and fresh SQLite.

This suite intentionally tests requirements beyond verify_retention.py. It
records failures without hiding them and touches only its fresh output DBs.
"""
import argparse
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

from verify_core import SPACE, T0, gate, observation, profile, scope, vec
from verify_retention import Runner, WINDOW, compact


def dry_statistics(worker, output, checks, calls):
    r = Runner(worker, output, checks, calls)
    try:
        r.setup(("A",))
        r.call(observation("DRY_OLD_SENTINEL_8b3c", t=T0 - 1))
        r.call(observation("DRY_LIVE_SENTINEL_ba46", t=T0 + 10000))
        before = r.rows()
        dry = r.call({"op": "retention_run", "dry_run": True}, now=T0 + WINDOW)
        after_dry = r.rows()
        real = r.call({"op": "retention_run", "dry_run": False})
        checks.append({"name": "dry_statistics_does_not_mutate",
                       "passed": before == after_dry})
        checks.append({"name": "dry_total_delete_prediction_exact",
                       "passed": dry["rows_deleted"] == real["rows_deleted"],
                       "evidence": {"dry": dry["rows_deleted"], "real": real["rows_deleted"]}})
        da = {a["activity_id"]: a for a in dry["activities"]}
        ra = {a["activity_id"]: a for a in real["activities"]}
        checks.append({"name": "dry_shared_object_prediction_exact",
                       "passed": all(da[a]["shared_objects_collected"] == ra[a]["shared_objects_collected"]
                                     for a in da),
                       "evidence": {"dry": da, "real": ra}})
        return {"dry": dry, "real": real}
    finally:
        r.stop()


def crossing_ambiguity(worker, output, checks, calls):
    r = Runner(worker, output, checks, calls)
    try:
        r.setup(("A",))
        overlap = profile("overlap", error=30000)
        overlap["profile"]["calibrations"][0].update(
            valid_from_utc_ms=T0 - WINDOW * 4, valid_until_utc_ms=T0 + WINDOW * 100)
        r.call(overlap)
        expired = "CROSS_CUTOFF_OLD_POSITIVE_b2d9"
        live = "CROSS_CUTOFF_LIVE_EXIT_FACE_b391"
        r.call(observation(expired, t=T0 - 1, profile_name="overlap"))
        r.call(observation(live, t=T0 + 1, profile_name="overlap"))
        r.call(gate("CROSS_CUTOFF_EXIT_d240", live, T0 + 1, profile_name="overlap"))
        before = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "crossing_fixture_is_admitted_ambiguous",
                       "passed": before["status"] == "AMBIGUOUS", "evidence": before})
        r.call({"op": "retention_run", "dry_run": False}, now=T0 + WINDOW)
        after = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "retention_preserves_crossing_interval_order_ambiguity",
                       "passed": after["status"] == "AMBIGUOUS" and after["continue_monitoring"],
                       "evidence": after})
        checks.append({"name": "crossing_runtime_contains_no_expired_trace",
                       "passed": not r.scan_marker(expired), "evidence": r.scan_marker(expired)})
        r.restart()
        resumed = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "crossing_ambiguity_restores_after_restart",
                       "passed": resumed["status"] == "AMBIGUOUS" and resumed["continue_monitoring"],
                       "evidence": resumed})
        runtime = [json.loads(row[2]) for row in r.selected("A", "monitoring_runtime")]
        dependencies = [dependency for value in runtime for dependency in value.get("boundary_dependencies", [])]
        checks.append({"name": "crossing_dependencies_are_minimal_hash_kind_tokens",
                       "passed": bool(dependencies) and all(
                           set(dependency) == {"event_hash", "pruned_positive", "pruned_exit"} and
                           len(dependency["event_hash"]) == 64 for dependency in dependencies),
                       "evidence": runtime})
        r.call({"op": "retention_run", "dry_run": False}, now=T0 + WINDOW + 2)
        fully_pruned = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        runtime = [json.loads(row[2]) for row in r.selected("A", "monitoring_runtime")]
        checks.append({"name": "fully_expired_boundary_preserves_minimal_ambiguous_runtime",
                       "passed": fully_pruned["status"] == "AMBIGUOUS" and
                       fully_pruned["continue_monitoring"] and fully_pruned["event_groups"] == [],
                       "evidence": fully_pruned})
        checks.append({"name": "fully_expired_boundary_drops_dependency_hashes",
                       "passed": all(value.get("boundary_dependencies", []) == [] for value in runtime) and
                       not r.scan_marker(live) and not r.scan_marker("CROSS_CUTOFF_EXIT_d240"),
                       "evidence": runtime})
        r.restart()
        no_history = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "fully_pruned_ambiguity_survives_restart",
                       "passed": no_history == fully_pruned})
        r.call(observation("LATER_CLEAR_SIGHTING_dcb9", t=T0 + 100000))
        new_active = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "later_unambiguous_sighting_resolves_pruned_boundary_state",
                       "passed": new_active["status"] == "ACTIVE" and new_active["continue_monitoring"],
                       "evidence": new_active})
        r.call(gate("LATER_CLEAR_EXIT_76e8", "LATER_CLEAR_SIGHTING_dcb9", T0 + 100000))
        new_closed = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "later_clear_exit_closes_without_permanent_ambiguity",
                       "passed": new_closed["status"] == "CLOSED" and not new_closed["continue_monitoring"],
                       "evidence": new_closed})
        return {"before": before, "after": after, "resumed": resumed,
                "fully_pruned": fully_pruned, "later_active": new_active, "later_closed": new_closed}
    finally:
        r.stop()


def post_prune_late_exit(worker, output, checks, calls):
    r = Runner(worker, output, checks, calls)
    try:
        r.setup(("A", "B"))
        r.call(observation("PRUNED_HISTORY_OLD_f24b", t=T0 - 1))
        r.call(observation("PRUNED_EXIT_FACE_6174", t=T0 + 10000))
        r.call(gate("PRUNED_EXIT_22d8", "PRUNED_EXIT_FACE_6174", T0 + 10000))
        r.call(observation("PRUNED_NEW_SESSION_d8c4", t=T0 + 20000))
        r.call(observation("B_ACTIVE_d9a5", a="B", t=T0 + 30000))
        r.call({"op": "retention_run", "dry_run": False}, now=T0 + WINDOW)
        before = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        b_before = r.call(dict(scope("B"), op="monitoring_get", target_id="PERSON"))["monitoring"]
        r.call(gate("PRUNED_LATE_EXIT_b4df", "PRUNED_EXIT_FACE_6174", T0 + 10001))
        after = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "late_exit_after_prune_does_not_close_new_session",
                       "passed": before["status"] == after["status"] == "ACTIVE" and
                       before["sessions"][-1]["session_id"] == after["sessions"][-1]["session_id"],
                       "evidence": {"before": before, "after": after}})
        b_after = r.call(dict(scope("B"), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "late_exit_after_prune_leaves_B_unchanged",
                       "passed": b_before == b_after})
        r.restart()
        restored = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "pruned_late_exit_runtime_restores",
                       "passed": restored == after})
        return {"before": before, "after": after, "restored": restored}
    finally:
        r.stop()


def future_time_lease(worker, output, checks, calls):
    r = Runner(worker, output, checks, calls)
    try:
        r.setup(("A", "B"))
        start = r.now
        future = observation("FUTURE_LONG_LEASE_49db", t=start + 90 * 86400000)
        held = r.call(future)
        checks.append({"name": "impossible_future_capture_enters_quarantine",
                       "passed": held["status"] == "HOLD" and not held.get("time_admitted", True),
                       "evidence": held})
        checks.append({"name": "impossible_future_capture_uses_first_quarantine_deadline",
                       "passed": held.get("retention_deadline_unix_ms") == start + WINDOW})
        allowed = r.call(observation("FUTURE_60S_BOUNDARY_163a", a="B", t=start + 60000))
        checks.append({"name": "future_at_60_second_boundary_is_admitted",
                       "passed": allowed["status"] == "ACCEPTED", "evidence": allowed})
        over = r.call(observation("FUTURE_60S_PLUS_ONE_2c2d", t=start + 60001))
        checks.append({"name": "future_one_ms_over_60s_is_quarantined",
                       "passed": over["status"] == "HOLD" and not over.get("time_admitted", True),
                       "evidence": over})
        r.restart()
        r.call({"op": "retention_run", "dry_run": False}, now=start + WINDOW + 1)
        expired = r.call(future)
        checks.append({"name": "future_timestamp_cannot_extend_quarantine_past_168_hours",
                       "passed": expired["status"] == "EXPIRED" and
                       not r.scan_marker("FUTURE_LONG_LEASE_49db"), "evidence": expired})
        valid_b = r.trajectory("B")["trajectory"]
        checks.append({"name": "future_quarantine_cleanup_does_not_expire_B_capture_clock_window",
                       "passed": any(value["observation_id"] == "FUTURE_60S_BOUNDARY_163a"
                                     for value in valid_b["evidence"]), "evidence": valid_b})
        return {"held": held, "allowed": allowed, "over": over, "expired": expired}
    finally:
        r.stop()


def live_gate_expired_face_dependency(worker, output, checks, calls):
    r = Runner(worker, output, checks, calls)
    try:
        r.setup(("A", "B"))
        marker = "EXPIRED_GATE_BOUND_FACE_2bf9"
        r.call(observation(marker, t=T0 - 1))
        request = gate("LIVE_GATE_OLD_FACE_a7dc", marker, T0 + 1)
        bound = r.call(request)
        checks.append({"name": "gate_dependency_fixture_is_bound",
                       "passed": bound["status"] == "BOUND", "evidence": bound})
        r.call(observation("B_VALID_OLD_DEPENDENCY_abc1", a="B", t=T0 + 2000))
        b_before = r.trajectory("B")["trajectory"]
        r.call({"op": "retention_run", "dry_run": False}, now=T0 + WINDOW)
        checks.append({"name": "live_gate_intake_removes_expired_face_reference_every_column",
                       "passed": not r.scan_marker(marker), "evidence": r.scan_marker(marker)})
        state_after = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "live_gate_retains_authoritatively_bound_closed_state_after_face_expiry",
                       "passed": state_after["status"] == "CLOSED" and not state_after["continue_monitoring"],
                       "evidence": state_after})
        checks.append({"name": "live_gate_own_capture_history_retained_with_scrubbed_expired_dependency",
                       "passed": "LIVE_GATE_OLD_FACE_a7dc" in compact(state_after) and
                       marker not in compact(state_after), "evidence": state_after})
        before_retry = r.rows()
        retry = r.call(request)
        checks.append({"name": "live_gate_retry_with_pruned_dependency_is_idempotent",
                       "passed": retry.get("duplicate", False) and before_retry == r.rows() and
                       marker not in compact(retry), "evidence": retry})
        pending = r.call({"op": "push_next"})
        notification = r.call(dict(scope(), op="activity_results", after_sequence=0))
        checks.append({"name": "pruned_face_binding_absent_from_notification_and_push_replay",
                       "passed": marker not in compact(pending) and marker not in compact(notification)})
        checks.append({"name": "pruned_face_binding_does_not_damage_B_valid_trajectory",
                       "passed": b_before == r.trajectory("B")["trajectory"]})
        r.restart()
        restored = r.call(request)
        restored_state = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "pruned_binding_retry_after_restart_cannot_restore_face_reference",
                       "passed": restored.get("duplicate", False) and not r.scan_marker(marker) and
                       marker not in compact(restored), "evidence": restored})
        checks.append({"name": "live_gate_bound_closed_state_after_face_expiry_restores",
                       "passed": restored_state == state_after and restored_state["status"] == "CLOSED",
                       "evidence": restored_state})
        return {"bound": bound, "retry": retry, "restored": restored}
    finally:
        r.stop()


def corrected_gate_identity_freeze(worker, output, checks, calls):
    r = Runner(worker, output, checks, calls)
    try:
        r.setup(("A",))
        corrected_target = "CORRECTED_PERSON"
        r.call(dict(scope(), op="monitoring_person_put", person_id=corrected_target, category="NORMAL",
                    revision=1, expected_revision=0, evidence_id="SYNTHETIC:registered-correction-target",
                    templates=[{"template_id": "REGISTERED_CORRECTED", "feature_space": SPACE,
                                "embedding": vec(1)}]))
        marker = "CORRECTED_EXPIRED_FACE_4c89"
        r.call(observation(marker, t=T0 - 1))
        request = gate("CORRECTED_LIVE_GATE_a4b2", marker, T0 + 1)
        r.call(request)
        r.call(dict(scope(), op="association_correct", source_id="S", observation_id=marker,
                    target_id=corrected_target, evidence_id="SYNTHETIC:authoritative-face-attribution-correction"))
        before = r.call(dict(scope(), op="monitoring_get", target_id=corrected_target))["monitoring"]
        original = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        checks.append({"name": "live_face_attribution_correction_moves_gate_to_correct_target",
                       "passed": before["status"] == "CLOSED" and original["status"] == "NOT_STARTED",
                       "evidence": {"corrected": before, "original": original}})
        r.call({"op": "retention_run", "dry_run": False}, now=T0 + WINDOW)
        frozen = r.call(dict(scope(), op="monitoring_get", target_id=corrected_target))["monitoring"]
        wrong = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        records = [json.loads(row[2]) for row in r.selected("A", "monitoring_gate")]
        checks.append({"name": "pruned_face_freezes_current_corrected_identity_not_old_binding_cache",
                       "passed": frozen["status"] == "CLOSED" and wrong["status"] == "NOT_STARTED" and
                       len(records) == 1 and records[0].get("bound_target_id") == corrected_target,
                       "evidence": {"corrected": frozen, "original": wrong, "gate_records": records}})
        checks.append({"name": "corrected_gate_keeps_no_expired_face_reference",
                       "passed": not r.scan_marker(marker), "evidence": r.scan_marker(marker)})
        r.restart()
        restored = r.call(dict(scope(), op="monitoring_get", target_id=corrected_target))["monitoring"]
        checks.append({"name": "current_corrected_gate_identity_survives_face_prune_restart",
                       "passed": restored == frozen})
        retry = r.call(request)
        checks.append({"name": "original_gate_retry_cannot_restore_stale_original_identity",
                       "passed": retry.get("duplicate", False) and not r.scan_marker(marker) and
                       r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]["status"] == "NOT_STARTED",
                       "evidence": retry})
        r.call({"op": "retention_run", "dry_run": False}, now=T0 + WINDOW + 2)
        closed = r.call(dict(scope(), op="monitoring_get", target_id=corrected_target))["monitoring"]
        checks.append({"name": "corrected_gate_expiry_keeps_closed_minimum_state_without_gate_history",
                       "passed": closed["status"] == "CLOSED" and not closed["continue_monitoring"] and
                       closed["event_groups"] == [] and not r.scan_marker("CORRECTED_LIVE_GATE_a4b2"),
                       "evidence": closed})
        return {"before": before, "frozen": frozen, "closed": closed}
    finally:
        r.stop()


def physical_cleanup_reader_recovery(worker, output, checks, calls):
    r = Runner(worker, output, checks, calls)
    reader = None
    try:
        r.setup(("A", "B"))
        marker = "BUSY_READER_EXPIRED_TRACE_efb0"
        expired_request = observation(marker, t=T0 - 1)
        r.call(expired_request)
        r.call(observation("BUSY_READER_VALID_B_e13a", a="B", t=T0 + 1000))
        b_before = r.trajectory("B")["trajectory"]
        reader = sqlite3.connect(r.dbpath)
        reader.execute("BEGIN")
        held_rows = reader.execute("SELECT value FROM state").fetchall()
        checks.append({"name": "physical_recovery_fixture_holds_real_SQLite_read_snapshot",
                       "passed": any(marker in value[0] for value in held_rows)})
        pending = r.call({"op": "retention_run", "dry_run": False}, ok=None, now=T0 + WINDOW)
        checks.append({"name": "reader_blocked_physical_cleanup_reports_failure_and_incomplete",
                       "passed": pending.get("ok") is False and pending.get("complete") is False and
                       pending.get("maintenance_status") == "PENDING" and
                       "checkpoint" in pending.get("maintenance_error", "").lower(),
                       "evidence": pending})
        checks.append({"name": "blocked_checkpoint_does_not_rollback_committed_logical_expiry",
                       "passed": not r.scan_marker(marker) and pending.get("rows_deleted", 0) > 0,
                       "evidence": {"current_rows_with_expired_marker": r.scan_marker(marker)}})
        checks.append({"name": "pending_reader_snapshot_retains_old_pages_until_released",
                       "passed": any(marker in value[0] for value in
                           reader.execute("SELECT value FROM state").fetchall())})
        reader.rollback()
        reader.close()
        reader = None
        recovered = r.call({"op": "retention_run", "dry_run": False})
        checks.append({"name": "released_reader_next_retention_finishes_physical_cleanup",
                       "passed": recovered.get("complete") is True and
                       recovered.get("maintenance_status") == "COMPLETE" and
                       recovered.get("after_physical", {}).get("wal_bytes") == 0,
                       "evidence": recovered})
        files_with_marker = [str(path) for path in
            (r.dbpath, Path(str(r.dbpath) + "-wal"), Path(str(r.dbpath) + "-shm"))
            if path.exists() and marker.encode() in path.read_bytes()]
        checks.append({"name": "recovered_main_WAL_SHM_contain_no_expired_trace_bytes",
                       "passed": not files_with_marker, "evidence": files_with_marker})
        replay = r.call(expired_request)
        checks.append({"name": "expired_replay_after_checkpoint_recovery_stays_expired",
                       "passed": replay.get("status") == "EXPIRED" and not r.scan_marker(marker),
                       "evidence": replay})
        checks.append({"name": "reader_recovery_preserves_B_valid_trajectory",
                       "passed": b_before == r.trajectory("B")["trajectory"]})
        return {"pending": pending, "recovered": recovered, "replay": replay}
    finally:
        if reader is not None:
            reader.rollback()
            reader.close()
        r.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("fresh output required")
    args.output.mkdir(parents=True)
    checks, calls, failures, evidence = [], [], [], {}
    for name, fn in [("dry-statistics", dry_statistics), ("crossing-ambiguity", crossing_ambiguity),
                     ("post-prune-exit", post_prune_late_exit), ("future-time", future_time_lease),
                     ("live-gate-pruned-face", live_gate_expired_face_dependency),
                     ("corrected-gate-freeze", corrected_gate_identity_freeze),
                     ("physical-reader-recovery", physical_cleanup_reader_recovery)]:
        try:
            evidence[name] = fn(args.worker, args.output / name, checks, calls)
        except BaseException as error:
            failures.append({"scenario": name, "error": repr(error)})
    summary = {"scope": "SYNTHETIC real C++ and fresh local SQLite; independent requirement edge audit",
               "checks": checks, "passed": sum(check["passed"] for check in checks),
               "failed": sum(not check["passed"] for check in checks), "failures": failures,
               "evidence": evidence, "worker_sha256": hashlib.sha256(args.worker.read_bytes()).hexdigest()}
    (args.output / "calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": summary["passed"], "failed": summary["failed"], "failures": failures,
                      "summary": str((args.output / "summary.json").resolve())}))
    if summary["failed"] or failures:
        raise RuntimeError("retention edge verification failed")


if __name__ == "__main__":
    main()
