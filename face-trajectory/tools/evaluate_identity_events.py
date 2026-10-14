"""Offline evaluation of saved worker decisions against independent event labels.

This tool never calls a worker, generates identities, changes a model or policy,
or infers truth from file names, automatic GIDs, or predictions. Every input row
must have a complete independent label, including rejected/HOLD observations.
Scope is P2_FEATURE_EVENTS: already-extracted feature events and their worker
intake/association outcomes. Image-level missed detections and P1 quality-rejected
images are outside this dataset; this cannot prove end-to-end accuracy gains.
The independence declaration and evidence reference must be supplied by the
label owner; validating the declaration cannot prove that review took place.

Truth JSONL: activity_id, source_id, observation_id, episode_id, truth_person_id,
enrolled_target_id (string or null), truth_complete=true, truth_evidence_id,
label_origin=INDEPENDENT_REVIEW or SYNTHETIC_INDEPENDENT_FIXTURE.
Prediction JSONL: the same three event key fields, input_sha256, feature_space,
and result (the saved worker response, including status and association).
Manifest JSON: schema_version=1, dataset_id, truth_sha256, worker_sha256,
evidence_kind=INDEPENDENT_LABELLED or SYNTHETIC_TOOL_CONTRACT,
ground_truth_independent=true, input_stage=P2_FEATURE_EVENTS, embedding_dim=512,
feature_space, identity_policy, complete eight-field quality_policy,
enrollment_sha256, graph_sha256, clock_profiles_sha256,
replay_mode=AUTOMATIC_OBSERVATION_REPLAY, request_timeline_sha256,
operation_inventory (operation -> positive integer count),
registered_target_ids (activity -> all explicitly enrolled target IDs).
Hashes freeze the independently recorded complete enrollment/config requests
(including every template vector), graph and clock profile artifacts. They are
audit declarations; reviewers must check them against the saved setup I/O and
artifacts. A supplied hash or response cannot prove worker execution or GT review.
The complete ordered request timeline includes setup, clocks and observations,
without GT fields. Only the six setup/observation operations listed below are
allowed; CORRECTED decisions and manual attribution corrections are excluded
from automatic algorithm evaluation. The reader cannot detect operations omitted
from a supplied inventory or prove a supplied timeline digest was executed.

Anonymous pair counts are computed within (activity_id, episode_id), allowing
independently labelled cross-source events in an episode, never across activities.
Abstention breaks a same-person pair and counts as a false split. Missing rows
are validation errors, never implicit abstentions. Rates with zero denominators
are null. Comparison freezes model, feature-space, policies, roster, labels and
event inputs. Worker code hashes may differ, permitting a code-fix comparison.

The pair confusion-count definitions follow the independent same/different-label
evaluation pattern in InsightFace's MIT-licensed verification.py. No upstream
inference, PCA, flipping, threshold selection, or framework dependency is used.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sys


class EvaluationError(ValueError):
    pass


KEY_FIELDS = ("activity_id", "source_id", "observation_id")
SPACE_FIELDS = ("recognition_model_sha256", "detector_model_sha256",
                "detector_profile", "alignment_version", "preprocessing_version",
                "output_version")
ACCEPTED_DECISIONS = {"MATCHED", "CREATED"}
DECISIONS = ACCEPTED_DECISIONS | {"UNASSIGNED", "PROVISIONAL", "CONFLICT"}
# monitoring_observe uses ACCEPTED/HOLD (worker.cpp 539-550); the activity
# retention admission adds EXPIRED (activity.cpp 172/189). Gate-event statuses
# such as BOUND are not identity-observation results.
INTAKE_STATUSES = {"ACCEPTED", "HOLD", "EXPIRED"}
AUTOMATIC_OPERATIONS = {"clock_profile_put", "activity_sync", "activity_bindings",
                        "activity_monitoring", "monitoring_person_put", "monitoring_observe"}
QUALITY_FIELDS = {"min_face_size_px", "min_inter_eye_distance_px", "min_detection_score",
                  "min_laplacian_variance", "max_clipped_fraction", "max_yaw_proxy",
                  "min_pitch_proxy", "max_pitch_proxy"}
FROZEN_FIELDS = ("dataset_id", "truth_sha256", "evidence_kind", "feature_space",
                 "identity_policy", "quality_policy", "registered_target_ids")


def _require(condition, message):
    if not condition:
        raise EvaluationError(message)


def _string(value, name):
    _require(isinstance(value, str) and bool(value.strip()), name + " must be nonempty text")
    return value


def _sha(value, name):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
             name + " must be a lowercase SHA256")
    return value


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON field: " + key)
        result[key] = value
    return result


def _finite_float(text):
    value = float(text)
    _require(math.isfinite(value), "nonfinite JSON number: " + text)
    return value


def _json(text, context):
    try:
        return json.loads(text, object_pairs_hook=_object_pairs,
                          parse_float=_finite_float,
                          parse_constant=lambda x: (_ for _ in ()).throw(
                              EvaluationError("nonfinite JSON number: " + x)))
    except (ValueError, TypeError) as exc:
        raise EvaluationError(context + ": " + str(exc)) from exc


def _same_json(left, right):
    # Python equates True with 1; an audit must preserve JSON field types too.
    return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == \
        json.dumps(right, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _key(row):
    _require(isinstance(row, dict), "record must be an object")
    return tuple(_string(row.get(field), field) for field in KEY_FIELDS)


def _rows(path):
    rows = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        _require(bool(line.strip()), f"{path}:{number}: blank JSONL record")
        row = _json(line, f"{path}:{number}")
        key = _key(row)
        _require(key not in rows, "duplicate scoped event: " + repr(key))
        rows[key] = row
    _require(bool(rows), str(path) + ": empty event dataset")
    return rows


def _manifest(path, truth_path):
    manifest = _json(Path(path).read_text(encoding="utf-8"), str(path))
    _require(isinstance(manifest, dict), "manifest must be an object")
    _require(manifest.get("schema_version") == 1, "unsupported manifest schema")
    _string(manifest.get("dataset_id"), "dataset_id")
    _sha(manifest.get("worker_sha256"), "worker_sha256")
    for name in ("enrollment_sha256", "graph_sha256", "clock_profiles_sha256", "request_timeline_sha256"):
        _sha(manifest.get(name), name)
    _require(manifest.get("replay_mode") == "AUTOMATIC_OBSERVATION_REPLAY",
             "automatic evaluation must exclude manual attribution correction replays")
    inventory = manifest.get("operation_inventory")
    _require(isinstance(inventory, dict) and bool(inventory), "complete operation inventory required")
    for operation, count in inventory.items():
        _require(operation in AUTOMATIC_OPERATIONS,
                 "manual correction or unsupported replay operation: " + operation)
        _require(type(count) is int and count > 0, "operation counts must be positive integers")
    _require(inventory.get("monitoring_observe", 0) > 0,
             "automatic feature-event replay must contain monitoring_observe")
    _sha(manifest.get("truth_sha256"), "truth_sha256")
    _require(manifest["truth_sha256"] == hashlib.sha256(Path(truth_path).read_bytes()).hexdigest(),
             "truth SHA256 does not match the independent label file")
    _require(manifest.get("ground_truth_independent") is True,
             "manifest must explicitly declare independent ground truth")
    _require(manifest.get("input_stage") == "P2_FEATURE_EVENTS",
             "this tool only evaluates already-extracted P2 feature events")
    _require(type(manifest.get("embedding_dim")) is int and manifest["embedding_dim"] == 512,
             "this existing-worker evaluation requires the frozen 512-dimensional feature contract")
    _require(manifest.get("evidence_kind") in {"INDEPENDENT_LABELLED", "SYNTHETIC_TOOL_CONTRACT"},
             "manifest must declare the evidence kind")
    space = manifest.get("feature_space")
    _require(isinstance(space, dict) and set(space) == set(SPACE_FIELDS),
             "feature_space must contain exactly the complete existing contract")
    for field in SPACE_FIELDS:
        _string(space[field], "feature_space." + field)
    _sha(space["recognition_model_sha256"], "recognition_model_sha256")
    _sha(space["detector_model_sha256"], "detector_model_sha256")
    for name in ("identity_policy", "quality_policy"):
        _require(isinstance(manifest.get(name), dict) and bool(manifest[name]),
                 name + " must be recorded, not inferred")
    quality = manifest["quality_policy"]
    _require(set(quality) == QUALITY_FIELDS, "complete existing eight-field QualityPolicy required")
    for name, value in quality.items():
        _require(type(value) in (int, float) and math.isfinite(value),
                 "quality policy must contain finite numeric values: " + name)
    _require(min(quality["min_face_size_px"], quality["min_inter_eye_distance_px"]) > 0 and
             0 <= quality["min_detection_score"] <= 1 and quality["min_laplacian_variance"] >= 0 and
             0 <= quality["max_clipped_fraction"] < 1 and quality["max_yaw_proxy"] > 0 and
             0 <= quality["min_pitch_proxy"] < quality["max_pitch_proxy"] <= 1,
             "invalid quality policy bounds")
    _string(manifest["identity_policy"].get("policy_id"), "identity_policy.policy_id")
    for field in ("match_threshold", "new_target_threshold", "min_margin",
                  "template_update_threshold", "max_template_similarity",
                  "min_detection_score", "min_face_size_px", "max_clock_uncertainty_ms",
                  "max_templates_per_target", "require_topology", "topology_is_complete",
                  "overlapping_cameras"):
        _require(field in manifest["identity_policy"], "incomplete identity policy: " + field)
    for field in ("match_threshold", "new_target_threshold", "min_margin",
                  "template_update_threshold", "max_template_similarity",
                  "min_detection_score", "min_face_size_px", "max_clock_uncertainty_ms",
                  "max_templates_per_target"):
        _require(type(manifest["identity_policy"][field]) in (int, float),
                 "identity policy numeric field must not be a boolean or text: " + field)
    for field in ("require_topology", "topology_is_complete"):
        _require(type(manifest["identity_policy"][field]) is bool,
                 "identity policy must record an explicit boolean: " + field)
    roster = manifest.get("registered_target_ids")
    _require(isinstance(roster, dict) and bool(roster), "explicit activity enrollment roster required")
    for activity, targets in roster.items():
        _string(activity, "roster activity_id")
        _require(isinstance(targets, list), "activity roster must be an array")
        for target in targets:
            _string(target, "registered target ID")
            _require(not target.startswith("auto:"), "automatic target cannot be an enrolled roster ID")
        _require(len(set(targets)) == len(targets), "duplicate registered target ID")
    return manifest


def _validate_truth(rows, manifest):
    person_enrollment = {}
    enrolled_persons = {}
    roster = manifest["registered_target_ids"]
    required_origin = ("SYNTHETIC_INDEPENDENT_FIXTURE" if
                       manifest["evidence_kind"] == "SYNTHETIC_TOOL_CONTRACT" else
                       "INDEPENDENT_REVIEW")
    for key, row in rows.items():
        _require(key[0] in roster, "truth activity has no independent roster: " + key[0])
        _require(row.get("truth_complete") is True, "incomplete independent truth: " + repr(key))
        _require(row.get("label_origin") == required_origin,
                 "truth origin is missing or conflicts with the evidence kind: " + repr(key))
        _string(row.get("episode_id"), "episode_id")
        person = _string(row.get("truth_person_id"), "truth_person_id")
        _require(not person.startswith(("auto:", "assoc-template:")),
                 "automatic algorithm identity cannot be independent truth")
        _string(row.get("truth_evidence_id"), "truth_evidence_id")
        _require("enrolled_target_id" in row, "explicit enrollment state required: " + repr(key))
        enrolled = row["enrolled_target_id"]
        _require(enrolled is None or isinstance(enrolled, str) and enrolled in roster[key[0]],
                 "truth enrollment conflicts with the independent roster: " + repr(key))
        person_key = (key[0], person)
        _require(person_key not in person_enrollment or person_enrollment[person_key] == enrolled,
                 "conflicting enrollment label for the same scoped truth person")
        person_enrollment[person_key] = enrolled
        if enrolled is not None:
            enrolled_key = (key[0], enrolled)
            _require(enrolled_key not in enrolled_persons or enrolled_persons[enrolled_key] == person,
                     "independent truth maps different people to one enrolled target")
            enrolled_persons[enrolled_key] = person


def _validate_predictions(rows, truth, manifest):
    _require(set(rows) == set(truth), "prediction/GT event keys differ; missing=" +
             repr(sorted(set(truth) - set(rows))) + "; extra=" + repr(sorted(set(rows) - set(truth))))
    for key, row in rows.items():
        _sha(row.get("input_sha256"), "input_sha256")
        _require(row.get("feature_space") == manifest["feature_space"],
                 "event feature-space differs from frozen manifest: " + repr(key))
        result = row.get("result")
        _require(isinstance(result, dict), "saved worker result required: " + repr(key))
        _require(result.get("ok") is True, "worker error must not be hidden as an evaluated identity event")
        _require(result.get("activity_id") == key[0],
                 "saved worker result activity conflicts with scoped event")
        status = _string(result.get("status"), "result.status")
        _require(status in INTAKE_STATUSES, "unknown worker identity intake status: " + status)
        for field, expected in zip(("source_id", "observation_id"), key[1:]):
            _require((field not in result and status != "ACCEPTED") or result.get(field) == expected,
                     "saved worker result source/observation conflicts with scoped event")
        association = result.get("association")
        if association is None:
            _require(status != "ACCEPTED", "ACCEPTED intake requires its association result")
            continue
        _require(isinstance(association, dict), "association must be an object")
        decision = association.get("decision")
        _require(decision != "CORRECTED", "manual CORRECTED attribution is not automatic algorithm performance")
        _require(decision in DECISIONS, "unknown saved worker association decision: " + repr(decision))
        _require(association.get("observation_id") == key[2],
                 "association observation ID conflicts with scoped event")
        _require(association.get("source_id") == key[1],
                 "association source ID conflicts with scoped event")
        _require(association.get("policy_id") == manifest["identity_policy"]["policy_id"],
                 "association used another identity policy")
        target = association.get("target_id")
        if decision in ACCEPTED_DECISIONS:
            _require(status == "ACCEPTED" and isinstance(target, str) and bool(target),
                     "confirmed association requires ACCEPTED intake and a target")
        else:
            _require(target is None, "unconfirmed association cannot claim a target")


def _rate(numerator, denominator):
    return numerator / denominator if denominator else None


def _pairs(counts):
    return sum(n * (n - 1) // 2 for n in counts.values())


def _metrics(truth, predictions, manifest):
    counts = Counter(total_events=len(truth))
    statuses, decisions = Counter(), Counter()
    anonymous = defaultdict(list)
    events = []
    for key in sorted(truth):
        label, result = truth[key], predictions[key]["result"]
        status = result["status"]
        association = result.get("association") or {}
        decision = association.get("decision")
        target = association.get("target_id") if decision in ACCEPTED_DECISIONS else None
        statuses[status] += 1
        decisions[decision or "NO_ASSOCIATION"] += 1
        counts["intake_accepted"] += status == "ACCEPTED"
        counts["confirmed_associations"] += target is not None
        expected = label["enrolled_target_id"]
        if expected is not None:
            counts["enrolled_events"] += 1
            counts["enrolled_correct_accepts"] += target == expected
            counts["enrolled_wrong_associations"] += target is not None and target != expected
            counts["enrolled_wrong_registered_accepts"] += (target is not None and target != expected and
                                                            target in manifest["registered_target_ids"][key[0]])
            counts["enrolled_abstentions"] += target is None
        else:
            counts["unenrolled_events"] += 1
            counts["unknown_false_registered_accepts"] += target in manifest["registered_target_ids"][key[0]]
            counts["anonymous_confirmed_events"] += target is not None
            anonymous[(key[0], label["episode_id"])].append((label["truth_person_id"], target))
        events.append({**dict(zip(KEY_FIELDS, key)), "status": status, "decision": decision,
                       "predicted_target_id": target, "truth_person_id": label["truth_person_id"],
                       "enrolled_target_id": expected, "reason": association.get("reason", result.get("reason"))})
    pair_counts = Counter(total_pairs=0, truth_same_pairs=0, predicted_same_pairs=0,
                          true_same_pairs=0, false_merge_pairs=0, false_split_pairs=0)
    for group in anonymous.values():
        gt = Counter(person for person, _ in group)
        predicted = Counter(target for _, target in group if target is not None)
        joint = Counter((person, target) for person, target in group if target is not None)
        same_truth, same_prediction, correct = _pairs(gt), _pairs(predicted), _pairs(joint)
        pair_counts["total_pairs"] += len(group) * (len(group) - 1) // 2
        pair_counts["truth_same_pairs"] += same_truth
        pair_counts["predicted_same_pairs"] += same_prediction
        pair_counts["true_same_pairs"] += correct
        pair_counts["false_merge_pairs"] += same_prediction - correct
        pair_counts["false_split_pairs"] += same_truth - correct
    for field in ("enrolled_events", "enrolled_correct_accepts", "enrolled_wrong_associations",
                  "enrolled_wrong_registered_accepts", "enrolled_abstentions", "unenrolled_events",
                  "unknown_false_registered_accepts", "anonymous_confirmed_events"):
        counts.setdefault(field, 0)
    return {"counts": dict(counts), "statuses": dict(statuses), "decisions": dict(decisions),
            "rates": {"enrolled_correct_recall": _rate(counts["enrolled_correct_accepts"], counts["enrolled_events"]),
                      "enrolled_wrong_association_rate": _rate(counts["enrolled_wrong_associations"], counts["enrolled_events"]),
                      "unknown_false_registered_accept_rate": _rate(counts["unknown_false_registered_accepts"], counts["unenrolled_events"]),
                      "confirmed_coverage": _rate(counts["confirmed_associations"], len(truth)),
                      "hold_rate": _rate(statuses["HOLD"], len(truth))},
            "anonymous_pairs": {**dict(pair_counts),
                                "precision": _rate(pair_counts["true_same_pairs"], pair_counts["predicted_same_pairs"]),
                                "recall": _rate(pair_counts["true_same_pairs"], pair_counts["truth_same_pairs"])},
            "events": events}


def evaluate_files(truth_path, predictions_path, manifest_path, *,
                   baseline_predictions_path=None, baseline_manifest_path=None):
    _require(bool(baseline_predictions_path) == bool(baseline_manifest_path),
             "baseline predictions and manifest must be provided together")
    truth = _rows(truth_path)
    manifest = _manifest(manifest_path, truth_path)
    _validate_truth(truth, manifest)
    predictions = _rows(predictions_path)
    _validate_predictions(predictions, truth, manifest)
    def metrics_by_activity(values, meta):
        metrics = _metrics(truth, values, meta)
        metrics["by_activity"] = {}
        for activity in sorted({key[0] for key in truth}):
            selected = {key: row for key, row in truth.items() if key[0] == activity}
            detail = _metrics(selected, values, meta)
            detail.pop("events")
            metrics["by_activity"][activity] = detail
        return metrics

    report = {"schema_version": 1, "evidence_kind": manifest["evidence_kind"],
              "scope": "offline supplied P2 feature-event worker responses; excludes missed detections and P1 quality-rejected images",
              "field_accuracy_claim": False, "worker_execution_verified": False,
              "label_independence_verified": False, "timeline_execution_verified": False,
              "manifest": manifest, "candidate": metrics_by_activity(predictions, manifest),
              "comparison": None}
    if baseline_predictions_path:
        baseline_manifest = _manifest(baseline_manifest_path, truth_path)
        for field in FROZEN_FIELDS:
            _require(_same_json(baseline_manifest[field], manifest[field]),
                     "no-downgrade audit rejected changed " + field)
        _require(_same_json({k: v for k, v in baseline_manifest.items() if k != "worker_sha256"},
                            {k: v for k, v in manifest.items() if k != "worker_sha256"}),
                 "no-downgrade audit rejected changed additional dataset metadata")
        baseline = _rows(baseline_predictions_path)
        _validate_predictions(baseline, truth, baseline_manifest)
        for key in predictions:
            _require(predictions[key]["input_sha256"] == baseline[key]["input_sha256"],
                     "baseline/candidate event input changed: " + repr(key))
        previous = metrics_by_activity(baseline, baseline_manifest)
        current = report["candidate"]
        checks = []
        rules = (
                ("counts", "enrolled_wrong_associations", "nonincrease"),
                ("counts", "unknown_false_registered_accepts", "nonincrease"),
                ("anonymous_pairs", "false_merge_pairs", "nonincrease"),
                ("anonymous_pairs", "false_split_pairs", "nonincrease"),
                ("counts", "enrolled_correct_accepts", "nondecrease"),
                ("counts", "anonymous_confirmed_events", "nondecrease"))
        for scope in [None] + sorted(previous["by_activity"]):
            old_metrics = previous if scope is None else previous["by_activity"][scope]
            new_metrics = current if scope is None else current["by_activity"][scope]
            for group, field, direction in rules:
                old, new = old_metrics[group][field], new_metrics[group][field]
                passed = new <= old if direction == "nonincrease" else new >= old
                checks.append({"activity_id": scope, "metric": field, "baseline": old, "candidate": new,
                               "requirement": direction, "passed": passed})
        report["baseline"] = previous
        report["comparison"] = {"passed": all(check["passed"] for check in checks), "checks": checks,
                                "baseline_worker_sha256": baseline_manifest["worker_sha256"],
                                "candidate_worker_sha256": manifest["worker_sha256"],
                                "hold_count_delta": current["statuses"].get("HOLD", 0) - previous["statuses"].get("HOLD", 0),
                                "confirmed_coverage_delta": current["rates"]["confirmed_coverage"] - previous["rates"]["confirmed_coverage"]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline-predictions", type=Path)
    parser.add_argument("--baseline-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        _require(not args.output.exists(), "fresh output file required")
        report = evaluate_files(args.truth, args.predictions, args.manifest,
                                baseline_predictions_path=args.baseline_predictions,
                                baseline_manifest_path=args.baseline_manifest)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        passed = report["comparison"] is None or report["comparison"]["passed"]
        print(json.dumps({"report": str(args.output), "evidence_kind": report["evidence_kind"],
                          "comparison_passed": None if report["comparison"] is None else passed}))
        return 0 if passed else 1
    except (EvaluationError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
