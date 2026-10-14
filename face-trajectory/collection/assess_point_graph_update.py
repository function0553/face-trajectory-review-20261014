"""Compare immutable point-graph packages offline before a model update.

This reports changes and replay scope. It never selects the current model,
rewrites observations, registers graphs, or promotes topology verification.
"""
import argparse
import json
from pathlib import Path

from build_point_graph import digest, read_json, sha256


def indexed(items, key):
    result = {item[key]: item for item in items}
    if len(result) != len(items):
        raise ValueError("Duplicate " + key)
    return result


def difference(before, after):
    return {"added": sorted(after.keys() - before.keys()),
            "removed": sorted(before.keys() - after.keys()),
            "changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}


def camera_positions(packet):
    result = {}
    for key, camera in indexed(packet["cameras"], "camera_id").items():
        result[key] = {"position_status": camera["position_status"], "markers": {
            marker_id: {field: marker.get(field) for field in
                        ("camera_id", "source_scene_id", "shape", "points", "visible")}
            for marker_id, marker in indexed(camera["position_candidates"], "annotation_id").items()}}
    return result


def assess(previous, candidate):
    if previous["schema"] != "camera-point-candidate-graph@1" or candidate["schema"] != previous["schema"]:
        raise ValueError("Expected camera-point-candidate-graph@1 packages")
    same_version = previous["graph_version"] == candidate["graph_version"]
    if same_version and previous != candidate:
        raise ValueError("Graph content changed without a new graph_version")
    spatial_fields = ("coordinate_frame", "unit", "to_world_matrix")
    spatial_changes = [k for k in spatial_fields if previous[k] != candidate[k]]
    positions = difference(camera_positions(previous), camera_positions(candidate))
    edges = difference(indexed(previous["edges"], "edge_id"), indexed(candidate["edges"], "edge_id"))
    old_edges, new_edges = indexed(previous["edges"], "edge_id"), indexed(candidate["edges"], "edge_id")
    direct = set(positions["added"] + positions["removed"] + positions["changed"])
    for group in (edges["added"], edges["removed"], edges["changed"]):
        for key in group:
            for table in (old_edges, new_edges):
                if key in table:
                    direct.update((table[key]["from_camera"], table[key]["to_camera"]))
    if spatial_changes:
        direct.update(camera_positions(previous))
        direct.update(camera_positions(candidate))
    changed_fields = [k for k in ("scene_version", "model_binding", "source_hashes", "policy",
                                  "topology_verified", "topology_is_complete", "model_provenance")
                      if previous.get(k) != candidate.get(k)]
    qualifications = {k: {"before": previous[k], "after": candidate[k]}
                      for k in ("topology_verified", "topology_is_complete")}
    return {
        "schema": "point-graph-update-assessment@1",
        "status": "NO_CHANGE" if same_version else "REBUILD_REQUIRED",
        "previous_graph_version": previous["graph_version"], "candidate_graph_version": candidate["graph_version"],
        "previous_scene_version": previous["scene_version"], "candidate_scene_version": candidate["scene_version"],
        "spatial_reference_changes": spatial_changes, "metadata_changes": changed_fields,
        "camera_position_changes": positions, "operational_edge_changes": edges,
        "directly_changed_camera_ids": sorted(direct), "topology_qualification": qualifications,
        "retained_candidate_changes": {k: {"before_count": len(previous[k]), "after_count": len(candidate[k]),
                                             "content_changed": digest(previous[k]) != digest(candidate[k])}
                                       for k in ("position_conflicts", "ambiguous_connection_candidates",
                                                 "unresolved_connection_candidates", "unbound_camera_markers")},
        "replay_scope": {"required": not same_version,
                         "selector": None if same_version else {"graph_version": previous["graph_version"]},
                         "target_ids": None,
                         "reason": "Targets must be selected from the caller's state. Changes can affect multi-hop routes beyond the directly changed cameras."},
        "actions": [] if same_version else [
            "Keep the old scene, graph, trajectory versions and predictions immutable.",
            "Register the candidate scene, then its independently generated graph under new versions.",
            "Replay complete authoritative raw record sets for targets using the previous graph; retain HOLD admissions and all raw evidence.",
            "Read the rebuilt point trace and predict using its new version. Do not carry forward an old prediction or geometry binding.",
            "When comparing across graph versions, retain GRAPH_CHANGED_REBUILD_REQUIRED as unresolved, not a contradictory observation.",
        ],
        "limitations": [
            "Added/removed cameras and markers describe the supplied packages only; they do not prove physical installation/removal or completeness of a server snapshot.",
            "A changed hash or publication label is a change signal, not proof of new field geometry or accuracy.",
            "Matching package content does not prove the remote model files have remained unchanged.",
            "This tool cannot establish graph-to-model truth or infer the scope of an unrecorded asset change.",
            "No personnel records, identity links, clock profiles, topology qualifications or server state are modified.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = assess(read_json(args.previous), read_json(args.candidate))
    report["input_sha256"] = {"previous": sha256(args.previous), "candidate": sha256(args.candidate)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"status": report["status"], "directly_changed_cameras": len(report["directly_changed_camera_ids"]),
                      "replay_scope": report["replay_scope"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
