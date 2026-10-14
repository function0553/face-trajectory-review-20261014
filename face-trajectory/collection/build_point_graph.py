"""Build a general camera-point candidate graph from local read-only evidence.

No network, person records, selected trajectory, or coordinate/name-based device
matching is used. Model connections remain unverified and incomplete.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def point(value):
    if (not isinstance(value, list) or len(value) != 3 or
            any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value)):
        raise ValueError("Camera marker requires three finite coordinates")
    return value


def build(scene_path, coverage_path, max_speed_mps=2.5, slow_speed_mps=0.3,
          dwell_allowance_ms=900000):
    if not 0 < slow_speed_mps <= max_speed_mps or not math.isfinite(max_speed_mps):
        raise ValueError("Require finite 0 < slow_speed_mps <= max_speed_mps")
    if not isinstance(dwell_allowance_ms, int) or dwell_allowance_ms < 0:
        raise ValueError("dwell_allowance_ms must be a nonnegative integer")
    scene, coverage = read_json(scene_path), read_json(coverage_path)
    source_hashes = {"site_scene_sha256": sha256(scene_path),
                     "model_coverage_sha256": sha256(coverage_path),
                     "navmesh_sha256": coverage["source_navmesh_sha256"]}
    if coverage["source_scene_sha256"] != source_hashes["site_scene_sha256"]:
        raise ValueError("Model coverage was generated from a different scene; do not mix versions")
    model_binding = {k: scene[k] for k in ("model_id", "scene_url", "tileset_id", "calibration_profile")}
    if "model_revision" in scene:
        model_binding["model_revision"] = scene["model_revision"]
    if "model_binding" in coverage and coverage["model_binding"] != model_binding:
        raise ValueError("Model diagnostic publication binding differs from scene")
    if "model_revision" in scene and coverage.get("model_binding") != model_binding:
        raise ValueError("Revision-tagged scene requires matching model diagnostic publication evidence")
    if "source_geometry_report_sha256" in coverage:
        source_hashes["geometry_report_sha256"] = coverage["source_geometry_report_sha256"]
    if scene["unit"] != "m":
        raise ValueError("Model candidate times require explicit metre units")
    policy = {"id": "camera-point-model-candidates@2", "max_speed_mps": max_speed_mps,
              "slow_speed_mps": slow_speed_mps, "dwell_allowance_ms": dwell_allowance_ms,
              "min_travel_rule": "floor(model_path_length_m / max_speed_mps * 1000)",
              "max_travel_rule": "ceil(model_path_length_m / slow_speed_mps * 1000) + dwell_allowance_ms",
              "travel_time_bound_verified": False, "hard_rejection_allowed": False,
              "conflict_rule": "EXCLUDE_CONFLICT_DEVICE_EDGES_AND_RETAIN_ALL_POSITION_CANDIDATES",
              "connection_rule": "ALL_COMPLETE_MODEL_PAIRS_NO_PERSON_OR_NEAREST_K_SELECTION"}
    catalog = {c["camera_id"]: c for c in scene["cameras"]}
    if len(catalog) != len(scene["cameras"]):
        raise ValueError("Duplicate camera IDs in device catalog")
    annotations = {a["annotation_id"]: a for a in scene["annotations"]}
    if len(annotations) != len(scene["annotations"]):
        raise ValueError("Duplicate annotation IDs")
    diagnostics = {m["annotation_id"]: m for m in coverage["markers"]}
    if len(diagnostics) != len(coverage["markers"]):
        raise ValueError("Duplicate model diagnostic marker IDs")
    camera_annotations = [a for a in scene["annotations"] if a["role"] == "CAMERA"]
    if set(diagnostics) != {a["annotation_id"] for a in camera_annotations}:
        raise ValueError("Model diagnostic markers do not match scene camera annotations")
    bound, unbound, invalid = defaultdict(list), [], []
    for a in camera_annotations:
        if a["shape"] != "point" or len(a["points"]) != 1:
            raise ValueError("Camera annotation is not a point")
        point(a["points"][0])
        m = diagnostics[a["annotation_id"]]
        if (m["camera_id"] or "") != (a["camera_id"] or "") or m["marker_local_xyz"] != a["points"][0]:
            raise ValueError("Model marker binding or coordinates differ from source annotation")
        if a["camera_id"] and a["camera_id"] in catalog and a["matches_camera_catalog"]:
            bound[a["camera_id"]].append(a)
        elif not a["camera_id"]:
            unbound.append(a)
        else:
            invalid.append(a)

    conflicts = []
    for camera_id, candidates in sorted(bound.items()):
        separation = max(math.dist(a["points"][0], b["points"][0]) for a in candidates for b in candidates)
        if separation > scene["duplicate_tolerance_m"]:
            conflicts.append({"camera_id": camera_id,
                              "annotation_ids": sorted(a["annotation_id"] for a in candidates),
                              "max_separation_m": separation,
                              "handling": "KEEP_ALL_CANDIDATES_EXCLUDE_OPERATIONAL_EDGES",
                              "reason": "POSITION_CANDIDATES_REQUIRE_CORRELATED_PATH_STATE"})
    conflict_ids = {c["camera_id"] for c in conflicts}
    # Cross-check the independent model audit, never silently remove a conflict.
    if conflict_ids != {c["camera_id"] for c in coverage["conflicts"]}:
        raise ValueError("Scene conflict detection differs from model audit")

    edges, ambiguous, failed, seen = [], [], [], set()
    for p in sorted(coverage["pairs"], key=lambda p: (p["from_annotation_id"], p["to_annotation_id"])):
        pair_key = (p["from_annotation_id"], p["to_annotation_id"])
        if pair_key in seen:
            raise ValueError("Duplicate ordered annotation pair in model audit")
        seen.add(pair_key)
        a, b = annotations[pair_key[0]], annotations[pair_key[1]]
        if (p["from_camera_id"] != a["camera_id"] or p["to_camera_id"] != b["camera_id"] or
                a["camera_id"] not in bound or b["camera_id"] not in bound or a["camera_id"] == b["camera_id"]):
            raise ValueError("Invalid model pair camera binding")
        if not p["complete_model_path"]:
            failed.append({**p, "classification": "MODEL_CONNECTION_UNRESOLVED",
                           "physical_connection_impossible": False})
            continue
        length = p["model_path_length_m"]
        if not p["reached_destination"] or isinstance(length, bool) or not isinstance(length, (int, float)) or not math.isfinite(length) or length < 0:
            raise ValueError("Complete model pair lacks finite length or reached-destination evidence")
        edge = {"edge_id": "model-candidate-" + digest(pair_key)[:20],
                "from_camera": a["camera_id"], "to_camera": b["camera_id"],
                "min_travel_ms": math.floor(length / max_speed_mps * 1000),
                "max_travel_ms": math.ceil(length / slow_speed_mps * 1000) + dwell_allowance_ms,
                "cost": length, "evidence_status": "MODEL_CANDIDATE_UNVERIFIED",
                "from_annotation_id": pair_key[0], "to_annotation_id": pair_key[1],
                "model_path_length_m": length, "travel_time_bound_verified": False,
                "hard_rejection_allowed": False,
                "camera_intermediate_passages_observed": False,
                "directionality_verified": False,
                "geometry_role": "GENERAL_CONNECTIVITY_DIAGNOSTIC_NOT_PERSON_ROUTE",
                "real_pedestrian_route_verified": False,
                "source_path_status": p["path_status"], "source_straight_path_status": p["straight_path_status"]}
        if "waypoints" in p:
            points = p["waypoints"]
            if (not isinstance(points, list) or not 2 <= len(points) <= 4096 or
                any(not isinstance(q, dict) or set(q) != {"x", "y", "z"} or
                    any(isinstance(q[k], bool) or not isinstance(q[k], (int, float)) or
                        not math.isfinite(q[k]) for k in ("x", "y", "z")) for q in points)):
                raise ValueError("Complete model path needs bounded finite scene-local waypoints")
            edge["waypoints"] = points
            edge["route_geometry_kind"] = "TOPOLOGY_INFERRED_PATH"
            edge["route_coordinate_frame"] = scene["coordinate_frame"]
        if p.get("model_path_local_xyz"):
            points = p["model_path_local_xyz"]
            if (not isinstance(points, list) or not 2 <= len(points) <= 4096 or
                any(not isinstance(q, list) or len(q) != 3 or
                    any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                        not math.isfinite(value) for value in q) for q in points)):
                raise ValueError("Complete model path needs bounded finite local xyz waypoints")
            edge["model_path_local_xyz"] = points
        if a["camera_id"] in conflict_ids or b["camera_id"] in conflict_ids:
            ambiguous.append({**edge, "excluded_reason": "POSITION_CONFLICT_REQUIRES_CORRELATED_CANDIDATES"})
        else:
            edges.append(edge)

    # The source diagnostic covers all pairs of eligible marker projections.
    projected = [a for values in bound.values() for a in values if diagnostics[a["annotation_id"]]["nearby_surface"]]
    expected_pairs = {(a["annotation_id"], b["annotation_id"]) for a in projected for b in projected
                      if a["annotation_id"] != b["annotation_id"] and a["camera_id"] != b["camera_id"]}
    if seen != expected_pairs:
        raise ValueError("Model diagnostic omits or adds eligible ordered pairs")
    outgoing, incoming = Counter(e["from_camera"] for e in edges), Counter(e["to_camera"] for e in edges)
    cameras = []
    for camera_id, candidates in sorted(bound.items()):
        positions = []
        for a in sorted(candidates, key=lambda a: a["annotation_id"]):
            m = diagnostics[a["annotation_id"]]
            positions.append({**a, "position_role": "CAMERA_MARKER_NOT_PERSON_FOOTPOINT",
                              "model_diagnostic": m})
        reasons = []
        if camera_id in conflict_ids:
            reasons.append("POSITION_CONFLICT_OPERATIONAL_EDGES_EXCLUDED")
        if not any(p["model_diagnostic"]["inside_copied_roi"] for p in positions):
            reasons.append("OUTSIDE_COPIED_MODEL_ROI")
        if not any(p["model_diagnostic"]["nearby_surface"] for p in positions):
            reasons.append("NO_NEARBY_MODEL_SURFACE_NOT_A_CAMERA_VALIDITY_FAILURE")
        if not outgoing[camera_id] and not incoming[camera_id]:
            reasons.append("NO_OPERATIONAL_CONNECTION_CANDIDATE")
        cameras.append({**catalog[camera_id], "position_candidates": positions,
                        "position_status": "AMBIGUOUS_POSITION" if camera_id in conflict_ids else "BOUND_MARKER",
                        "observation_usable_without_navigation": True,
                        "outgoing_candidate_count": outgoing[camera_id], "incoming_candidate_count": incoming[camera_id],
                        "coverage_reasons": reasons})
    limitations = [
        "Connection candidates use all complete ordered model-query results, independently of any person or test sequence.",
        "The graph is incomplete and unverified. Missing or failed model edges do not establish physical impossibility.",
        "A model path can pass other cameras without an observed capture. It is not verified physical adjacency or a complete channel topology.",
        "Marker installation coordinates are camera points, not pedestrian footpoints or verified camera observation regions.",
        "Model distances and assumed speed bounds are informational; no field-calibrated hard travel-time bounds are available.",
        "The maximum travel time includes a configurable dwell allowance, not a measured maximum; longer dwell remains possible.",
        "Conflicting camera positions are retained. Their edges are excluded to prevent combining incompatible position candidates across a path.",
        "Direction, opening hours, access restrictions, floor labels, model omissions and terrain accuracy remain unverified.",
        "No capture record, unified identity, observed person trajectory, target-specific path, or future event is generated here.",
    ]
    packet = {"schema": "camera-point-candidate-graph@1",
              "graph_version": "camera-points-" + digest({"sources": source_hashes, "policy": policy})[:20],
              "scene_version": scene["scene_version"], "source_evidence_id": scene["source_evidence_id"],
              "model_binding": model_binding,
              "model_provenance": coverage.get("provenance", {"status": "LEGACY_UNVERIFIED",
                  "note": "Historical diagnostic lacks an explicit model publication and geometry-to-navmesh provenance binding."}),
              "source_hashes": source_hashes, "coordinate_frame": scene["coordinate_frame"],
              "unit": scene["unit"], "to_world_matrix": scene["to_world_matrix"],
              "topology_verified": False, "topology_is_complete": False,
              "incomplete_graph_missing_edge_means": "UNKNOWN_NOT_IMPOSSIBLE",
              "policy": policy, "cameras": cameras, "edges": edges,
              "position_conflicts": conflicts, "ambiguous_connection_candidates": ambiguous,
              "unresolved_connection_candidates": failed,
              "unbound_camera_markers": sorted(unbound, key=lambda a: a["annotation_id"]),
              "invalid_camera_bindings": sorted(invalid, key=lambda a: a["annotation_id"]),
              "catalog_cameras_without_published_markers": [catalog[c] for c in sorted(set(catalog) - set(bound))],
              "venue_annotations": [a for a in scene["annotations"] if a["role"] != "CAMERA"],
              "model_diagnostic_scope": {"source_roi": coverage["source_roi"],
                                         "projection_search_half_extents_m": coverage["projection_search_half_extents_m"],
                                         "source_summary": coverage["summary"],
                                         "source_limitations": coverage["limitations"]},
              "limitations": limitations}
    counts = {"catalog_cameras": len(catalog), "published_camera_markers": len(camera_annotations),
              "bound_camera_markers": sum(map(len, bound.values())), "bound_devices": len(bound),
              "unbound_camera_markers": len(unbound), "catalog_without_published_markers": len(catalog) - len(bound),
              "position_conflict_devices": len(conflicts), "ordered_pairs_preserved": len(seen),
              "complete_model_path_candidates": len(edges) + len(ambiguous),
              "operational_unverified_edges": len(edges), "ambiguous_edges_excluded": len(ambiguous),
              "unresolved_model_pairs_preserved": len(failed),
              "devices_with_operational_edges": len(set(outgoing) | set(incoming)),
              "bound_devices_without_operational_edges": len(bound) - len(set(outgoing) | set(incoming)),
              "venue_annotations": len(packet["venue_annotations"])}
    checks = {
        "source_scene_sha_matches_model_audit": True,
        "every_eligible_ordered_pair_preserved": seen == expected_pairs,
        "complete_and_failed_candidates_partition_input": len(edges) + len(ambiguous) + len(failed) == len(coverage["pairs"]),
        "every_bound_marker_preserved": sum(len(c["position_candidates"]) for c in cameras) == sum(map(len, bound.values())),
        "conflict_devices_excluded_from_operational_edges": all(e["from_camera"] not in conflict_ids and e["to_camera"] not in conflict_ids for e in edges),
        "all_complete_nonconflicting_pairs_included": len(edges) == sum(p["complete_model_path"] and p["from_camera_id"] not in conflict_ids and p["to_camera_id"] not in conflict_ids for p in coverage["pairs"]),
        "no_implicit_name_or_position_bindings": len(bound) == coverage["summary"]["bound_devices"],
        "no_claim_of_complete_or_verified_topology": not packet["topology_is_complete"] and not packet["topology_verified"],
        "edge_ids_unique": len({e["edge_id"] for e in edges + ambiguous}) == len(edges) + len(ambiguous),
    }
    if not all(checks.values()):
        raise ValueError("Graph import consistency checks failed")
    report = {"schema": "camera-point-graph-import-report@1", "graph_version": packet["graph_version"],
              "source_hashes": source_hashes, "policy": policy, "counts": counts,
              "import_consistency_checks": checks, "production_accessed": False,
              "person_trajectory_accuracy_tested": False, "capture_records_associated": 0,
              "note": "Checks validate faithful offline import, not real route accuracy or field navigability."}
    return packet, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-speed-mps", type=float, default=2.5)
    parser.add_argument("--slow-speed-mps", type=float, default=0.3)
    parser.add_argument("--dwell-allowance-ms", type=int, default=900000)
    args = parser.parse_args()
    packet, report = build(args.scene, args.coverage, args.max_speed_mps, args.slow_speed_mps, args.dwell_allowance_ms)
    if any((args.output / name).exists() for name in ("graph-packet.json", "graph-build-report.json")):
        raise FileExistsError("Output evidence exists; use a new output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    packet_path = args.output / "graph-packet.json"
    save(packet_path, packet)
    report["graph_packet_sha256"] = sha256(packet_path)
    save(args.output / "graph-build-report.json", report)
    print(json.dumps({"graph_version": packet["graph_version"], **report["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
