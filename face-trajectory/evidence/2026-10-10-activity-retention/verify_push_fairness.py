"""Real Go/C++/SQLite/backend-receiver fairness and scoped-order integration.

All identities/UTC references are synthetic, keys remain in memory, and every
file/server/process belongs to a fresh isolated local output directory.
"""
import argparse
import base64
import hashlib
import json
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from verify_core import SPACE, T0, observation, profile, scope, vec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True, type=Path)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("fresh output directory required")
    args.output.mkdir(parents=True)
    service, worker = args.output / "service-under-audit.exe", args.output / "worker-under-audit.exe"
    shutil.copyfile(args.service, service)
    shutil.copyfile(args.worker, worker)
    checks, calls, attempts, failures = [], [], [], []
    process = None
    receiver_lock = threading.Lock()
    recover_a = threading.Event()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status = 503 if event["activity_id"] == "A" and not recover_a.is_set() else 204
            with receiver_lock:
                attempts.append({"event": event, "status": status, "monotonic": time.monotonic(),
                                 "idempotency_key": self.headers.get("Idempotency-Key")})
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    receiver_thread.start()
    receiver_url = "http://127.0.0.1:%d/results" % receiver.server_port
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    keyfile, proof, database = args.output / "public-keys.json", args.output / "clock-proof.txt", args.output / "isolated.sqlite"
    keyfile.write_text(json.dumps({"keys": {"synthetic": public}}), encoding="utf-8")
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        port = connection.getsockname()[1]
    url = "http://127.0.0.1:%d" % port

    def signed(body):
        data = json.dumps(body, separators=(",", ":")).encode()
        encoded = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        return encoded(data) + "." + encoded(key.sign(data))

    def token(activity):
        return signed(dict(issuer="SYNTHETIC", key_id="synthetic", subject="viewer-" + activity,
                           session="session-" + activity, context_id="context-" + activity,
                           context_generation=1, authz_revision=1, not_before_unix_ms=T0 - 100000,
                           expires_unix_ms=T0 + 3600000,
                           grants=[{"activity_id": activity, "actions": ["query", "subscribe", "config", "monitor", "ingest", "identity"]}],
                           global_actions=["global_config", "push_status", "retention"]))

    def call(path, body=None, bearer=None):
        request = urllib.request.Request(url + path,
            data=None if body is None else json.dumps({k: v for k, v in body.items() if k != "op"}).encode(),
            headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + bearer} if bearer else {})})
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        calls.append({"path": path, "request": body, "response": result})
        return result

    def check(name, condition, evidence=None):
        checks.append({"name": name, "passed": bool(condition), "evidence": evidence})
        if not condition:
            raise AssertionError(name)

    def snapshot():
        with receiver_lock:
            return list(attempts)

    def until(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = snapshot()
            if predicate(value):
                return value
            time.sleep(.01)
        raise RuntimeError("expected isolated backend delivery state did not arrive")

    def accepted(items, activity):
        return [value for value in items if value["event"]["activity_id"] == activity and value["status"] == 204]

    starts = 0

    def start_service(now):
        nonlocal process, starts
        starts += 1
        with (args.output / ("service-%d.log" % starts)).open("w", encoding="utf-8") as log:
            process = subprocess.Popen([str(service.resolve()), "-worker", str(worker.resolve()),
                "-state-file", str(database.resolve()), "-addr", "127.0.0.1:%d" % port,
                "-auth-public-key-file", str(keyfile.resolve()), "-auth-issuer", "SYNTHETIC",
                "-clock-public-key-file", str(keyfile.resolve()), "-clock-issuer", "SYNTHETIC",
                "-clock-proof-file", str(proof.resolve()), "-push-results", "-push-url", receiver_url,
                "-retention-enabled", "-retention-interval", "60s"], stdout=log, stderr=log)
        for _ in range(100):
            try:
                health = call("/health")
                break
            except (OSError, urllib.error.URLError):
                time.sleep(.03)
        else:
            raise RuntimeError("isolated Go/C++ service did not start")
        # The nonce is created before C++ cold start. A health reply near 1s
        # already has insufficient remaining uncertainty budget. Wait for a
        # fresh nonce and sign it; never enlarge the production time policy.
        proof_deadline = time.monotonic() + 35
        while time.monotonic() < proof_deadline:
            if health["clock"].get("challenge_valid_for_ms", 0) >= 29500:
                temporary_proof = proof.with_suffix('.tmp')
                temporary_proof.write_text(signed(dict(issuer="SYNTHETIC", key_id="synthetic", challenge=health["clock"]["challenge"],
                    now_unix_ms=now, uncertainty_ms=0, valid_for_ms=120000, source_verified=True,
                    source="SYNTHETIC_TEST", evidence_id="SYNTHETIC:fair-push-local-only")), encoding="utf-8")
                temporary_proof.replace(proof)
                receipt = call("/health")
                if receipt["clock"].get("trusted", False):
                    break
            time.sleep(.05)
            health = call("/health")
        else:
            raise RuntimeError("no fresh challenge accepted the synthetic verified UTC proof: " + json.dumps(health["clock"]))

    try:
        start_service(T0 + 1000)
        A, B = token("A"), token("B")
        call("/v1/clocks/profiles", profile(), A)
        for activity, bearer in [("A", A), ("B", B)]:
            call("/v1/activities/sync", dict(activity_id=activity, activity_num="NUM-" + activity,
                venue_id="VENUE", config_version=1, expected_config_version=0), bearer)
            call("/v1/activities/bindings", dict(activity_id=activity, config_version=1,
                binding_version=1, expected_binding_version=0, bindings=[{"source_id": "S", "device_id": "C",
                "kind": "CAMERA", "route_rule_id": "ROUTE-" + activity}]), bearer)
            call("/v1/activities/monitoring", dict(activity_id=activity, config_version=1, binding_version=1,
                monitoring_version=1, expected_monitoring_version=0, enabled=True), bearer)
            call("/v1/monitoring/persons", dict(scope(activity), person_id="PERSON", category="NORMAL",
                revision=1, expected_revision=0, evidence_id="SYNTHETIC:enrolled", templates=[{"template_id": "REGISTERED",
                "feature_space": SPACE, "embedding": vec(0)}]), bearer)
            if activity == "A":
                until(lambda values: any(value["event"]["activity_id"] == "A" and value["status"] == 503 for value in values))
        call("/v1/monitoring/observe", observation("A_SCOPE_1_84d9"), A)
        call("/v1/monitoring/observe", observation("A_SCOPE_2_740a", t=T0 + 100), A)
        submitted_b = time.monotonic()
        call("/v1/monitoring/observe", observation("B_SCOPE_1_16a5", a="B"), B)
        delivery = until(lambda values: any(value["event"].get("operation") == "monitoring_observe" for value in accepted(values, "B")), timeout=.7)
        b_push = next(value for value in accepted(delivery, "B") if value["event"]["operation"] == "monitoring_observe")
        check("real_Go_Cpp_failed_A_does_not_block_B_push", b_push["monotonic"] - submitted_b < .7 and
              not accepted(delivery, "A"), {"B_delivery_delay_seconds": b_push["monotonic"] - submitted_b})
        check("B_notification_has_activity_versions_and_deadline", all(field in b_push["event"] for field in
              ("activity_id", "activity_num", "config_version", "binding_version", "monitoring_version", "retention_deadline_unix_ms")))
        monitor_b = call("/v1/monitoring/get", dict(scope("B"), target_id="PERSON"), B)
        check("B_monitoring_stays_active_during_A_delivery_failure", monitor_b["monitoring"]["continue_monitoring"])
        for _ in range(100):
            with sqlite3.connect(database) as connection:
                pending_rows = [json.loads(value[0]) for value in connection.execute("SELECT value FROM state WHERE ns='push_outbox'")]
            if len(pending_rows) == 3 and all(value["activity_id"] == "A" for value in pending_rows):
                break
            time.sleep(.01)
        check("only_A_unacknowledged_scope_heads_remain_before_restart",
              len(pending_rows) == 3 and all(value["activity_id"] == "A" for value in pending_rows))
        failed_before_restart = len([value for value in snapshot() if value["event"]["activity_id"] == "A" and value["status"] == 503])
        process.terminate()
        process.wait(15)
        process = None
        start_service(T0 + 10000)
        delivery = until(lambda values: len([value for value in values if value["event"]["activity_id"] == "A" and value["status"] == 503]) > failed_before_restart)
        monitor_a = call("/v1/monitoring/get", dict(scope(), target_id="PERSON"), A)
        monitor_b = call("/v1/monitoring/get", dict(scope("B"), target_id="PERSON"), B)
        check("Go_Cpp_restart_preserves_independent_A_B_lifecycles_during_failed_delivery",
              monitor_a["monitoring"]["continue_monitoring"] and monitor_b["monitoring"]["continue_monitoring"] and
              monitor_a["activity_id"] == "A" and monitor_b["activity_id"] == "B")
        resumed_a = [value for value in delivery if value["event"]["activity_id"] == "A" and value["status"] == 503]
        check("restart_replays_same_A_head_and_scoped_idempotency_key", all(
              value["event"]["event_id"] == resumed_a[0]["event"]["event_id"] and
              value["idempotency_key"] == resumed_a[0]["idempotency_key"] for value in resumed_a))
        submitted_b = time.monotonic()
        call("/v1/monitoring/observe", observation("B_SCOPE_2_RESTART_56d8", a="B", t=T0 + 200), B)
        delivery = until(lambda values: any(value["event"]["result"].get("observation_id") == "B_SCOPE_2_RESTART_56d8"
                                           for value in accepted(values, "B")), timeout=.7)
        restarted_b = next(value for value in accepted(delivery, "B") if value["event"]["result"].get("observation_id") == "B_SCOPE_2_RESTART_56d8")
        check("restarted_failed_A_does_not_block_new_B_push", restarted_b["monotonic"] - submitted_b < .7 and
              not accepted(delivery, "A"), {"B_delivery_delay_seconds": restarted_b["monotonic"] - submitted_b})
        recover_a.set()
        delivery = until(lambda values: len(accepted(values, "A")) == 3)
        a_pushes = accepted(delivery, "A")
        check("real_Cpp_A_scope_commit_order_survives_retry", [value["event"]["operation"] for value in a_pushes] ==
              ["monitoring_person_put", "monitoring_observe", "monitoring_observe"] and
              [value["event"]["result"].get("observation_id") for value in a_pushes[1:]] ==
              ["A_SCOPE_1_84d9", "A_SCOPE_2_740a"])
        first_a = next(value for value in delivery if value["event"]["activity_id"] == "A")
        check("A_retry_preserves_scoped_idempotency_key", first_a["event"]["event_id"] == a_pushes[0]["event"]["event_id"] and
              first_a["idempotency_key"] == a_pushes[0]["idempotency_key"] and bool(first_a["idempotency_key"]))
        latest_failed_a = max(value["monotonic"] for value in delivery if value["event"]["activity_id"] == "A" and value["status"] == 503)
        check("A_retry_keeps_independent_backoff", a_pushes[0]["monotonic"] - latest_failed_a >= 1)
        for _ in range(100):
            status = call("/v1/push/status", bearer=A)
            if status["push"]["pending"] == 0:
                break
            time.sleep(.02)
        check("all_activities_durable_pushes_acknowledged_without_loss", status["push"]["pending"] == 0 and
              len(accepted(snapshot(), "A")) == 3 and len(accepted(snapshot(), "B")) == 3, status)
        with sqlite3.connect(database) as connection:
            pending = connection.execute("SELECT count(*) FROM state WHERE ns='push_outbox'").fetchone()[0]
        check("real_SQLite_outbox_has_no_unacknowledged_rows", pending == 0)
    except BaseException as error:
        failures.append(repr(error))
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(15)
        receiver.shutdown()
        receiver.server_close()
        receiver_thread.join(5)
        result = {"scope": "Local real Go/C++/SQLite/backend receiver, synthetic keys/reference; no production/capacity acceptance",
                  "passed": sum(check["passed"] for check in checks), "failed": sum(not check["passed"] for check in checks),
                  "checks": checks, "failures": failures, "service_sha256": hashlib.sha256(service.read_bytes()).hexdigest(),
                  "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest()}
        (args.output / "calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output / "backend-attempts.json").write_text(json.dumps(snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": result["passed"], "failed": result["failed"], "failures": failures,
                          "summary": str((args.output / "summary.json").resolve())}))
    if failures or result["failed"]:
        raise RuntimeError("real push fairness integration failed")


if __name__ == "__main__":
    main()
