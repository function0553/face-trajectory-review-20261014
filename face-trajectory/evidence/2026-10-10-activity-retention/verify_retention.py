"""Repeatable retention checks using the real C++ worker and fresh SQLite only.

The signed Go/C++ HTTP path is exercised by verify_service.py. This file probes
the storage boundary directly, including historical snapshots and every SQLite
column; it is not camera accuracy, production deployment or capacity evidence.
"""
import argparse
import copy
import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path

from verify_core import GRAPH, POLICY, SPACE, T0, clock, gate, iso, observation, profile, scope, vec

WINDOW = 604800000
OLD = "EXPIRED_TRACE_SENTINEL_6ff1c140"
BOUNDARY = "BOUNDARY_TRACE_SENTINEL_2e66ebbf"
LIVE = "LIVE_TRACE_SENTINEL_cce09445"
UNKNOWN = "UNKNOWN_TRACE_SENTINEL_1d4c275f"
LEGACY_OLD = "LEGACY_EXPIRED_SENTINEL_d3c4bbaf"
LEGACY_LIVE = "LEGACY_LIVE_SENTINEL_4b58c428"
LEGACY_UNKNOWN = "LEGACY_UNKNOWN_SENTINEL_b5cdbb7e"


def compact(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def physical(activity, namespace):
    return json.dumps(["activity", activity, namespace], separators=(",", ":"))


class Runner:
    def __init__(self, worker, output, checks, calls, enabled=True):
        self.binary = worker.resolve()
        self.output = output
        self.dbpath = output / "isolated.sqlite"
        self.checks, self.calls = checks, calls
        self.process = None
        self.now = T0 + 1000
        self.enabled = enabled
        output.mkdir(parents=True)
        self.start()

    def start(self):
        self.process = subprocess.Popen(
            [str(self.binary), "--state-file", str(self.dbpath.resolve()), "--push-results", "true",
             "--retention-enabled", str(self.enabled).lower()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")

    def stop(self, force=False):
        if self.process is None or self.process.poll() is not None:
            return
        if force:
            self.process.kill()
        else:
            self.process.stdin.close()
        self.process.wait(30)
        if not force and self.process.returncode:
            raise RuntimeError(self.process.stderr.read())

    def restart(self):
        self.stop()
        self.start()

    def call(self, request, ok=True, now=None):
        if now is not None:
            self.now = now
        request = copy.deepcopy(request)
        request["_trusted_clock"] = clock(self.now)
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("worker exited: " + self.process.stderr.read())
        result = json.loads(line)
        self.calls.append({"scenario": self.output.name, "request": request, "response": result})
        if ok is not None and result.get("ok") != ok:
            raise AssertionError({"request": request, "response": result})
        return result

    def check(self, name, condition, evidence=None):
        item = {"name": name, "passed": bool(condition)}
        if evidence is not None:
            item["evidence"] = evidence
        self.checks.append(item)
        if not condition:
            raise AssertionError(name)

    def setup(self, activities=("A", "B")):
        good = profile()
        good["profile"]["calibrations"][0].update(
            valid_from_utc_ms=T0 - WINDOW * 4, valid_until_utc_ms=T0 + WINDOW * 100)
        self.call(good)
        bad = profile("over60", offset=60001)
        bad["profile"]["calibrations"][0].update(
            valid_from_utc_ms=T0 - WINDOW * 4, valid_until_utc_ms=T0 + WINDOW * 100)
        self.call(bad)
        for activity in activities:
            self.call({"op": "activity_sync", "activity_id": activity,
                       "activity_num": "NUM-" + activity, "venue_id": "VENUE",
                       "config_version": 1, "expected_config_version": 0})
            self.call({"op": "activity_bindings", "activity_id": activity, "config_version": 1,
                       "binding_version": 1, "expected_binding_version": 0,
                       "bindings": [{"source_id": source, "device_id": device, "kind": kind,
                                     "route_rule_id": "ROUTE-" + activity}
                                    for source, device, kind in [("S", "C", "CAMERA"),
                                                                ("G", "GATE", "GATE")]]})
            self.call({"op": "activity_monitoring", "activity_id": activity, "config_version": 1,
                       "binding_version": 1, "monitoring_version": 1,
                       "expected_monitoring_version": 0, "enabled": True})
            self.call(dict(scope(activity), op="monitoring_person_put", person_id="PERSON",
                           category="PRIORITY" if activity == "A" else "NORMAL", revision=1,
                           expected_revision=0, evidence_id="SYNTHETIC:registered-excluded",
                           templates=[{"template_id": "REGISTERED", "feature_space": SPACE,
                                       "embedding": vec(0)}]))

    def trajectory(self, activity="A", target="PERSON", version=0, ok=True):
        return self.call(dict(scope(activity), op="trajectory_get", target_id=target, version=version), ok)

    def rows(self):
        with sqlite3.connect(self.dbpath) as db:
            return db.execute("SELECT ns,key,value,ref,track,meta FROM state ORDER BY ns,key").fetchall()

    def selected(self, activity, namespace):
        return [row for row in self.rows() if row[0] == physical(activity, namespace)]

    def scan_marker(self, marker):
        return [{"ns": row[0], "key": row[1], "columns": [i for i, value in enumerate(row) if marker in value]}
                for row in self.rows() if any(marker in value for value in row)]

    def storage(self):
        with sqlite3.connect(self.dbpath) as db:
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            pages = db.execute("PRAGMA page_count").fetchone()[0]
            free = db.execute("PRAGMA freelist_count").fetchone()[0]
        rows = self.rows()
        tombstone = [r for r in rows if "retention_tombstone" in r[0]]
        total = sum(sum(len(v.encode()) for v in r) for r in rows)
        tombstone_bytes = sum(sum(len(v.encode()) for v in r) for r in tombstone)
        return {"rows": len(rows), "logical_bytes": total,
                "tombstone_rows": len(tombstone), "tombstone_bytes": tombstone_bytes,
                "non_tombstone_bytes": total - tombstone_bytes,
                "page_count": pages, "page_size": page_size,
                "freelist_count": free, "reusable_bytes": page_size * free,
                "files": {suffix or "main": (Path(str(self.dbpath) + suffix).stat().st_size
                                               if Path(str(self.dbpath) + suffix).exists() else 0)
                          for suffix in ("", "-wal", "-shm")}}


def seed_legacy(runner, template_snapshot):
    """Create the real previous refs-v1 representation, not a renamed test blob."""
    runner.stop()
    snapshot = copy.deepcopy(template_snapshot)
    snapshot.update(target_id="LEGACY", version=1, association_revision=0)
    snapshot["revision"].update(target_id="LEGACY", previous_version=0, version=1,
                                reason="SYNTHETIC_LEGACY_FIXTURE")
    old = copy.deepcopy(snapshot["evidence"][0])
    # Public API renders enum names; refs-v1 is the actual durable format whose
    # enum representation is numeric (UtcCapture=1, Confirmed=2).
    old.update(observation_id=LEGACY_OLD, target_id="LEGACY", captured_at_unix_ms=T0 - 1,
               time_source=1, link_status=2, attribution_evidence_id="association:" + LEGACY_OLD)
    live = copy.deepcopy(old)
    live.update(observation_id=LEGACY_LIVE, captured_at_unix_ms=T0 + 1,
                attribution_evidence_id="association:" + LEGACY_LIVE)
    snapshot["evidence"] = [old, live]
    graph = snapshot.pop("graph")
    snapshot.pop("evidence")
    snapshot.update(storage_format="refs-v1", graph_ref="SYNTHETIC:legacy-graph",
                    evidence_refs=["SYNTHETIC:legacy-old", "SYNTHETIC:legacy-live"])
    with sqlite3.connect(runner.dbpath) as db:
        def put(ns, key, value, ref="", track="", meta=""):
            db.execute("INSERT OR REPLACE INTO state VALUES(?,?,?,?,?,?)",
                       (ns, key, compact(value), ref, track, meta))
        put("object", "SYNTHETIC:legacy-graph", graph)
        put("object", "SYNTHETIC:legacy-old", old)
        put("object", "SYNTHETIC:legacy-live", live)
        put("trajectory", json.dumps(["LEGACY", 1], separators=(",", ":")), snapshot, "LEGACY")
        put("trajectory_head", "LEGACY", 1)
        put("template", "REGISTERED_LEGACY", {"template_id": "REGISTERED_LEGACY", "person_entity_id": "LEGACY_REGISTERED",
                                             "feature_space": SPACE, "embedding": vec(6)})
        # Unreachable previous-format object is a real replay/storage leak if
        # the sweep only follows current trajectory roots.
        put("object", "SYNTHETIC:legacy-unreferenced", {"observation_id": LEGACY_OLD})
        # Preserve clock-admitted managed receipts from genuine core-generated
        # rows. The fixture does not confer trust by adding a UTC string alone.
        # Previous deployments could have these roots without activity scope.
        for original, replacement in [(OLD, LEGACY_OLD), (LIVE, LEGACY_LIVE), (UNKNOWN, LEGACY_UNKNOWN)]:
            oldkey = json.dumps(["S", original], separators=(",", ":"))
            pointer = db.execute("SELECT value FROM state WHERE ns=? AND key=?",
                                 (physical("A", "monitoring_observation"), oldkey)).fetchone()
            if not pointer:
                raise AssertionError("managed fixture source missing: " + original)
            ptr = json.loads(pointer[0])
            record = json.loads(db.execute("SELECT value FROM state WHERE ns=? AND key=?",
                                          (physical("A", "object"), ptr["record_ref"])).fetchone()[0])
            payload = json.loads(db.execute("SELECT value FROM state WHERE ns=? AND key=?",
                                           (physical("A", "object"), record["payload_ref"])).fetchone()[0])

            def substitute(value):
                if isinstance(value, list):
                    return [substitute(x) for x in value]
                if isinstance(value, dict):
                    return {key: substitute(x) for key, x in value.items()}
                if isinstance(value, str):
                    return replacement if value == oldkey else value.replace(original, replacement)
                return value

            record, payload = substitute(record), substitute(payload)
            record["payload_ref"] = "SYNTHETIC:legacy-payload:" + replacement
            ptr["record_ref"] = "SYNTHETIC:legacy-record:" + replacement
            put("object", record["payload_ref"], payload)
            put("object", ptr["record_ref"], record)
            put("monitoring_observation", replacement, ptr)
            put("monitoring_observation_revision", json.dumps([replacement, 1], separators=(",", ":")),
                ptr, replacement)
            associated = db.execute("SELECT value,ref,track,meta FROM state WHERE ns=? AND key=?",
                                    (physical("A", "observation"), oldkey)).fetchone()
            if associated:
                put("observation", replacement, substitute(json.loads(associated[0])),
                    associated[1], substitute(associated[2]), substitute(associated[3]))
    runner.start()


def boundary_and_restart(worker, output, checks, calls):
    r = Runner(worker, output, checks, calls)
    try:
        r.setup()
        old_request = observation(OLD, t=T0 - 1)
        r.call(old_request)
        r.call(observation(BOUNDARY, t=T0))
        r.call(observation(LIVE, t=T0 + 1))
        b_request = observation("B_LIVE_SENTINEL_f8824992", a="B", t=T0 + 5000)
        b_receipt = r.call(b_request)
        r.check("known_capture_deadline_uses_capture_only",
                b_receipt["retention_deadline_unix_ms"] == T0 + 5000 + WINDOW)
        unknown_request = observation(UNKNOWN, t=T0)
        unknown_request["time_record"] = None
        r.check("unknown_time_enters_quarantine", r.call(unknown_request)["status"] == "HOLD")
        over_request = observation("OVER60_QUARANTINE_SENTINEL_d1b12f90", profile_name="over60")
        r.check("over60_enters_quarantine", r.call(over_request)["status"] == "HOLD")
        anonymous = r.call(observation("ANONYMOUS_EXPIRED_SENTINEL_cc0a1dce", t=T0 - 1, i=3))
        anon_target = anonymous["association"]["target_id"]
        a_full = r.trajectory()["trajectory"]
        versions = sorted(json.loads(row[1])[1] for row in r.selected("A", "trajectory")
                          if json.loads(row[1])[0] == "PERSON")
        b_full = r.trajectory("B")["trajectory"]
        registered_before = r.selected("A", "template")
        registered_before = [row for row in registered_before if row[1] == "REGISTERED"]
        seed_legacy(r, a_full)
        first_quarantine = T0 + 1000
        before = r.rows()
        dry = r.call({"op": "retention_run", "dry_run": True}, now=T0 + WINDOW)
        r.check("dry_run_preserves_every_persisted_column", r.rows() == before)
        activity_report = {a["activity_id"]: a for a in dry["activities"]}
        r.check("dry_run_exact_boundary_count", activity_report["A"]["observations_after"] == 2)
        r.check("dry_run_predicts_expired_rows", dry["rows_deleted"] > 0)
        report = r.call({"op": "retention_run", "dry_run": False})
        actual_report = {a["activity_id"]: a for a in report["activities"]}
        for activity, predicted in activity_report.items():
            actual = actual_report[activity]
            r.check("dry_run_exact_live_counts_match_real_pass_" + str(activity),
                    all(predicted[key] == actual[key] for key in
                        ("observations_before", "observations_after", "expired_event_count")))
            r.check("dry_run_exact_snapshot_counts_match_real_pass_" + str(activity),
                    all(predicted["snapshots"][key] == actual["snapshots"][key] for key in
                        ("versions_scanned", "versions_rewritten", "versions_deleted",
                         "evidence_before", "evidence_after", "evidence_removed")))
        r.check("explicit_cleanup_reports_physical_storage", "after_logical" in report and "after_physical" in report,
                {key: report.get(key) for key in ("before", "after_logical", "after_physical")})
        current = r.trajectory()["trajectory"]
        ids = [v["observation_id"] for v in current["evidence"]]
        r.check("capture_equal_cutoff_retained", BOUNDARY in ids)
        r.check("capture_before_cutoff_removed", OLD not in ids)
        r.check("valid_capture_retained", LIVE in ids)
        r.check("A_cleanup_leaves_B_valid_trajectory_identical", r.trajectory("B")["trajectory"] == b_full)
        for version in versions:
            answer = r.trajectory(version=version, ok=None)
            r.check("all_historical_versions_scrubbed_%d" % version,
                    not answer.get("ok") or OLD not in compact(answer))
        r.check("expired_marker_absent_all_state_columns", not r.scan_marker(OLD), r.scan_marker(OLD))
        r.check("legacy_refs_v1_expired_marker_removed", not r.scan_marker(LEGACY_OLD), r.scan_marker(LEGACY_OLD))
        r.check("legacy_refs_v1_live_shared_object_retained", bool(r.scan_marker(LEGACY_LIVE)))
        r.check("legacy_orphan_held_intake_does_not_gain_new_lease", not r.scan_marker(LEGACY_UNKNOWN),
                r.scan_marker(LEGACY_UNKNOWN))
        r.check("registered_gallery_excluded", bool(registered_before) and
                [row for row in r.selected("A", "template") if row[1] == "REGISTERED"] == registered_before)
        r.check("legacy_registered_gallery_excluded", any(row[0] == "template" and row[1] == "REGISTERED_LEGACY" for row in r.rows()))
        r.check("expired_automatic_template_removed", not any("ANONYMOUS_EXPIRED_SENTINEL" in value for row in r.rows() for value in row))
        r.check("anonymous_runtime_identity_retained_without_points", r.trajectory(target=anon_target)["trajectory"]["evidence"] == [])
        monitored = r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]
        r.check("minimal_runtime_remains_active", monitored["continue_monitoring"] is True)
        runtime = r.selected("A", "monitoring_runtime")
        r.check("runtime_seed_has_no_expired_time_camera_or_event", bool(runtime) and all(
            OLD not in row[2] and "captured_at" not in row[2] and "camera_id" not in row[2]
            and "event_ids" not in row[2] for row in runtime))
        repeated = r.call(old_request)
        r.check("expired_duplicate_cannot_resurrect", repeated["status"] == "EXPIRED" and repeated["duplicate"] is True)
        late_request = observation("NEW_KEY_OLD_CAPTURE_SENTINEL_d1ab5746", t=T0 - 1)
        r.check("expired_late_new_key_rejected", r.call(late_request)["status"] == "EXPIRED")
        changed = copy.deepcopy(old_request)
        changed["observation"]["embedding"] = vec(1)
        r.check("expired_tombstone_changed_content_conflicts", r.call(changed, False)["error_code"] == "SOURCE_CONFLICT")
        kept_request = observation(BOUNDARY, t=T0)
        retry_before = r.rows()
        r.check("live_retry_remains_idempotent", r.call(kept_request)["duplicate"] is True)
        r.check("live_retry_has_no_persisted_business_side_effect", r.rows() == retry_before)
        changed = copy.deepcopy(kept_request)
        changed["observation"]["embedding"] = vec(1)
        r.check("live_retry_changed_content_conflicts", r.call(changed, False)["error_code"] == "SOURCE_CONFLICT")
        r.check("unknown_retry_uses_same_first_quarantine", r.call(unknown_request)["duplicate"] is True)
        intake = r.selected("A", "activity_intake")
        unknown_record = next(json.loads(row[2]) for row in intake if UNKNOWN in row[1])
        r.check("unknown_first_quarantine_unchanged", unknown_record["first_quarantined_unix_ms"] == first_quarantine)
        r.restart()
        r.call({"op": "retention_run", "dry_run": False}, now=first_quarantine + WINDOW)
        r.check("quarantine_equal_deadline_retained_after_restart", r.call(unknown_request)["status"] == "HOLD")
        r.restart()
        r.call({"op": "retention_run", "dry_run": False}, now=first_quarantine + WINDOW + 1)
        r.check("quarantine_one_ms_after_deadline_expired", r.call(unknown_request)["status"] == "EXPIRED")
        r.check("quarantine_payload_removed_from_all_columns", not r.scan_marker(UNKNOWN), r.scan_marker(UNKNOWN))
        r.check("over60_payload_removed_at_first_deadline", not r.scan_marker("OVER60_QUARANTINE_SENTINEL"))
        r.check("unknown_A_first_deadline_does_not_prematurely_expire_known_B",
                r.trajectory("B")["trajectory"] == b_full)
        r.restart()
        r.check("expired_restart_retry_cannot_resurrect", r.call(old_request)["status"] == "EXPIRED")
        r.check("unknown_restart_retry_cannot_renew", r.call(unknown_request)["status"] == "EXPIRED")
        amended = copy.deepcopy(unknown_request)
        amended.update(reconsider=True, evidence_id="SYNTHETIC:attempted-requarantine")
        amended["time_record"] = {"profile_version": "good", "raw_time": iso(r.now)}
        r.check("changed_unknown_requarantine_cannot_renew_key", r.call(amended, False)["error_code"] == "SOURCE_CONFLICT")
        notif = r.call(dict(scope(), op="activity_results", after_sequence=0, limit=100))
        r.check("subscription_replay_has_no_expired_markers", all(
            marker not in compact(notif) for marker in (OLD, BOUNDARY, LIVE, UNKNOWN)))
        pending = r.call({"op": "push_next"})
        r.check("pending_delivery_has_no_expired_markers", all(
            marker not in compact(pending) for marker in (OLD, BOUNDARY, LIVE, UNKNOWN)))
        final = r.storage()
        # Physical verification is separate from logical scans. secure_delete
        # plus a successful checkpoint must remove the unique payload marker.
        for suffix in ("", "-wal"):
            path = Path(str(r.dbpath) + suffix)
            r.check("expired_marker_absent_file_" + (suffix or "main"),
                    not path.exists() or OLD.encode() not in path.read_bytes())
        return {"report": report, "final_storage": final}
    finally:
        r.stop()


def automatic_query_cleanup(worker, output, checks, calls):
    r = Runner(worker, output, checks, calls)
    try:
        r.setup(("A",))
        marker = "AUTOMATIC_EXPIRY_SENTINEL_aad0104e"
        request = observation(marker, t=T0)
        r.call(request)
        answer = r.trajectory()
        r.check("automatic_query_initially_sees_valid_point", marker in compact(answer))
        r.now = T0 + WINDOW
        r.check("automatic_query_equal_cutoff_retains_point", marker in compact(r.trajectory()))
        r.now += 1
        r.check("automatic_query_prunes_before_serving_expired_point", marker not in compact(r.trajectory()))
        r.check("automatic_cleanup_physically_erases_replay_marker", not r.scan_marker(marker))
        status = r.call({"op": "retention_status"})
        r.check("automatic_cleanup_observable_last_report", status["last_report"]["trusted_now_unix_ms"] == r.now)
        r.check("automatic_expired_retry_stays_expired", r.call(request)["status"] == "EXPIRED")
        return status
    finally:
        r.stop()


def interrupted_recovery(worker, output, checks, calls):
    r = Runner(worker, output, checks, calls)
    try:
        r.setup(("A",))
        marker = "INTERRUPTED_EXPIRED_SENTINEL_1caf5de2"
        request = observation(marker, t=T0)
        r.call(request)
        r.stop()
        eventkey = json.dumps(["S", marker], separators=(",", ":"))
        with sqlite3.connect(r.dbpath) as db:
            # Abort after the pass has already prepared runtime, snapshots,
            # tombstones and shared nodes. This checks transaction rollback,
            # then process termination/restart and safe rerun.
            db.execute("CREATE TRIGGER synthetic_retention_interrupt BEFORE DELETE ON state "
                       "WHEN OLD.ns=? AND OLD.key=? BEGIN SELECT RAISE(ABORT,'SYNTHETIC_INTERRUPT'); END;"
                       .replace("?", "'" + physical("A", "observation").replace("'", "''") + "'", 1)
                       .replace("?", "'" + eventkey.replace("'", "''") + "'", 1))
        r.start()
        before = r.rows()
        failed = r.call({"op": "retention_run", "dry_run": False}, False, now=T0 + WINDOW + 1)
        r.check("interrupted_cleanup_rolls_back_all_partial_state", r.rows() == before, failed)
        r.stop(force=True)
        with sqlite3.connect(r.dbpath) as db:
            db.execute("DROP TRIGGER synthetic_retention_interrupt")
            # Hold the actual DELETE transaction inside a deterministic test
            # trigger. Killing the worker while this loop is running proves
            # crash recovery after partial preparation, rather than merely
            # restarting an already-completed pass. This trigger exists only
            # in this newly created isolated test database.
            db.execute("CREATE TRIGGER synthetic_retention_delay BEFORE DELETE ON state "
                       "WHEN OLD.ns=? AND OLD.key=? BEGIN "
                       "SELECT sum(x) FROM (WITH RECURSIVE delay(x) AS "
                       "(SELECT 0 UNION ALL SELECT x+1 FROM delay WHERE x<100000000) SELECT x FROM delay); END;"
                       .replace("?", "'" + physical("A", "observation").replace("'", "''") + "'", 1)
                       .replace("?", "'" + eventkey.replace("'", "''") + "'", 1))
        r.start()
        r.call({"op": "health"})
        before_kill = r.rows()
        kill_request = {"op": "retention_run", "dry_run": False, "_trusted_clock": clock(r.now)}
        r.process.stdin.write(json.dumps(kill_request) + "\n")
        r.process.stdin.flush()
        time.sleep(.15)
        r.stop(force=True)
        completed_line = r.process.stdout.read()
        r.calls.append({"scenario": r.output.name, "request": kill_request,
                        "response": None, "process_killed_during_cleanup": True,
                        "stdout_after_kill": completed_line})
        r.check("actual_process_kill_interrupts_uncompleted_cleanup", not completed_line)
        r.check("actual_process_kill_recovers_partial_transaction", r.rows() == before_kill)
        with sqlite3.connect(r.dbpath) as db:
            db.execute("DROP TRIGGER synthetic_retention_delay")
        r.start()
        resumed = r.call({"op": "retention_run", "dry_run": False})
        r.check("terminated_process_cleanup_resumes", resumed["ok"] and not r.scan_marker(marker))
        r.check("resumed_cleanup_retry_stays_expired", r.call(request)["status"] == "EXPIRED")
        return {"failure": failed, "resumed": resumed}
    finally:
        r.stop()


def cycles(worker, output, checks, calls, count, events):
    r = Runner(worker, output, checks, calls)
    result = []
    try:
        r.setup(("A",))
        for cycle in range(count):
            base = T0 + cycle * (WINDOW + 2000)
            for index in range(events):
                r.call(observation("CYCLE_%02d_EVENT_%03d_SENTINEL" % (cycle, index),
                                   t=base + index), now=base + 1000)
            written = r.storage()
            report = r.call({"op": "retention_run", "dry_run": False}, now=base + WINDOW + events + 1)
            pruned = r.storage()
            r.check("cycle_%d_no_expired_observation_or_object_payload" % cycle,
                    not any("CYCLE_%02d_EVENT_" % cycle in value for row in r.rows() for value in row))
            r.check("cycle_%d_head_empty_and_runtime_active" % cycle,
                    r.trajectory()["trajectory"]["evidence"] == [] and
                    r.call(dict(scope(), op="monitoring_get", target_id="PERSON"))["monitoring"]["continue_monitoring"])
            r.check("cycle_%d_expired_retry_cannot_restore" % cycle,
                    r.call(observation("CYCLE_%02d_EVENT_%03d_SENTINEL" % (cycle, 0), t=base))["status"] == "EXPIRED")
            result.append({"cycle": cycle, "written": written, "pruned": pruned,
                           "report_before": report["before"], "report_after": report["after_logical"],
                           "report_physical": report.get("after_physical")})
        # Minimal irreversible idempotency hashes are reported separately.
        # No claim of a constant total DB size is made while they grow.
        baseline = max(row["pruned"]["non_tombstone_bytes"] for row in result[:2])
        last = result[-1]["pruned"]
        r.check("multi_cycle_trajectory_storage_growth_bounded",
                last["non_tombstone_bytes"] <= baseline + 65536,
                {"baseline_non_tombstone_bytes": baseline, "final": last,
                 "limit_extra_bytes": 65536})
        r.check("minimal_hash_tombstone_growth_separately_measured",
                last["tombstone_rows"] == count * events and last["tombstone_bytes"] > 0,
                {"rows": last["tombstone_rows"], "bytes": last["tombstone_bytes"],
                 "note": "This residual idempotency state grows; trajectory payload storage is bounded."})
        return result
    finally:
        r.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cycles", default=6, type=int)
    parser.add_argument("--events-per-cycle", default=12, type=int)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("fresh output required: " + str(args.output))
    if args.cycles < 3 or args.events_per_cycle < 2:
        raise RuntimeError("at least 3 cycles and 2 events per cycle required")
    args.output.mkdir(parents=True)
    checks, calls, evidence, failures = [], [], {}, []
    scenarios = [("boundary", lambda: boundary_and_restart(args.worker, args.output / "boundary", checks, calls)),
                 ("interruption", lambda: interrupted_recovery(args.worker, args.output / "interruption", checks, calls)),
                 ("automatic", lambda: automatic_query_cleanup(args.worker, args.output / "automatic", checks, calls)),
                 ("cycles", lambda: cycles(args.worker, args.output / "cycles", checks, calls,
                                           args.cycles, args.events_per_cycle))]
    for name, run in scenarios:
        try:
            evidence[name] = run()
        except BaseException as error:
            failures.append({"scenario": name, "error": repr(error)})
    (args.output / "calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"scope": "SYNTHETIC real C++ and isolated local SQLite; no production/capacity acceptance",
               "checks": checks, "passed": sum(check["passed"] for check in checks),
               "failures": failures, "evidence": evidence,
               "worker_sha256": hashlib.sha256(args.worker.read_bytes()).hexdigest(),
               "storage_report": "Query/replay checks, live rows, reusable pages, main/WAL bytes and multi-cycle payload growth are reported separately."}
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": summary["passed"], "failures": failures, "summary": str((args.output / "summary.json").resolve())}))
    if failures or any(not check["passed"] for check in checks):
        raise RuntimeError("retention verification failed")


if __name__ == "__main__":
    main()
