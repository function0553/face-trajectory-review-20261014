"""Bounded durable-outbox fairness checks using real C++/fresh SQLite.

Person revision writes produce actual durable pushes without an artificial
direct-SQL queue fixture or thousands of expensive trajectory rebuilds.
"""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

from verify_core import observation, scope
from verify_retention import Runner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-flood", type=int, default=10002)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("fresh output required")
    args.output.mkdir(parents=True)
    frozen = args.output / "worker-under-audit.exe"
    shutil.copyfile(args.worker, frozen)
    checks, calls, failures = [], [], []
    r = Runner(frozen, args.output / "isolated", checks, calls)
    accepted = 0
    revision = 1
    last_request = None
    blocked = None
    try:
        r.setup(("A", "B"))
        for _ in range(args.max_flood):
            request = dict(scope(), op="monitoring_person_put", person_id="PERSON", category="PRIORITY",
                           revision=revision + 1, expected_revision=revision,
                           evidence_id="SYNTHETIC:fairness:%d" % (revision + 1))
            result = r.call(request, ok=None)
            if not result["ok"]:
                blocked = result
                break
            accepted += 1
            revision += 1
            last_request = request
        status = r.call({"op": "push_status"})
        maximum = status["push"]["max_pending"]
        checks.append({"name": "A_scope_backpressure_preserves_global_reserve",
                       "passed": blocked is not None and accepted < maximum - 2,
                       "evidence": {"accepted_A": accepted, "blocked": blocked, "push_status": status}})
        b_request = observation("B_RESERVE_EVENT_67fd")
        b_request.update(scope("B"))
        b_request["route_rule_id"] = "ROUTE-B"
        b = r.call(b_request, ok=None)
        checks.append({"name": "B_intake_remains_accepted_after_A_durable_queue_backpressure",
                       "passed": b["ok"] and b.get("status") == "ACCEPTED", "evidence": b})
        if last_request:
            last_retry = r.call(last_request, ok=None)
            checks.append({"name": "A_committed_same_content_retry_survives_backpressure",
                           "passed": last_retry["ok"] and last_retry.get("duplicate", False),
                           "evidence": last_retry})
        if b["ok"]:
            b_before = r.rows()
            retry = r.call(b_request, ok=None)
            checks.append({"name": "B_committed_event_retry_is_idempotent_at_A_backpressure",
                           "passed": retry["ok"] and retry.get("duplicate", False) and b_before == r.rows(),
                           "evidence": retry})
        r.restart()
        b_current = r.call(dict(scope("B"), op="monitoring_get", target_id="PERSON"), ok=None)
        checks.append({"name": "B_valid_runtime_restores_after_A_outbox_flood",
                       "passed": b_current["ok"] and b_current["monitoring"]["continue_monitoring"],
                       "evidence": b_current})
    except BaseException as error:
        failures.append(repr(error))
    finally:
        r.stop()
    summary = {"scope": "SYNTHETIC real C++ and fresh local SQLite; durable queue fairness only",
               "checks": checks, "passed": sum(check["passed"] for check in checks),
               "failed": sum(not check["passed"] for check in checks), "failures": failures,
               "accepted_A": accepted, "worker_sha256": hashlib.sha256(frozen.read_bytes()).hexdigest()}
    (args.output / "calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": summary["passed"], "failed": summary["failed"], "failures": failures,
                      "accepted_A": accepted, "summary": str((args.output / "summary.json").resolve())}))
    if failures or summary["failed"]:
        raise RuntimeError("durable outbox fairness verification failed")


if __name__ == "__main__":
    main()
