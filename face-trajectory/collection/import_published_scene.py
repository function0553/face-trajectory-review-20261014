"""Offline import of a read-only audit; no SSH, image decoding or inference."""
import argparse
import collections
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

CAMERA_KINDS = {"cctv-fixed", "cctv-dome", "panorama", "anpr"}


def audit_objects(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        result = json.loads(line).get("result", {})
        if not isinstance(result, dict):
            continue
        for text in result.get("stdout", "").splitlines():
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def import_site(audit, model_id, scene_url, profile, model_revision=None):
    objects = list(audit_objects(audit))
    catalog = next(o["camera_catalog"] for o in reversed(objects) if "camera_catalog" in o)
    camera_ids = {c["camera_id"] for c in catalog}
    # Use only the last recorded table snapshot; do not merge older markers.
    # Missing completeness metadata remains unknown for historical audits.
    snapshot = next(o for o in reversed(objects) if o.get("scene_annotation_table") == "studio_scene")
    complete_flags = ("complete", "snapshot_complete", "list_complete")
    partial_flags = ("truncated", "partial")
    for flag in complete_flags + partial_flags:
        if flag in snapshot and not isinstance(snapshot[flag], bool):
            raise ValueError("Snapshot completeness flag must be boolean: " + flag)
    if (any(snapshot.get(flag) is False for flag in complete_flags) or
            any(snapshot.get(flag) is True for flag in partial_flags)):
        raise ValueError("Latest published scene snapshot is explicitly incomplete or partial")
    publication_snapshot = {
        "completeness": "DECLARED_COMPLETE" if any(snapshot.get(flag) is True for flag in complete_flags)
                        else "LEGACY_COMPLETENESS_UNVERIFIED",
        "selection": "LAST_RECORDED_TABLE_SNAPSHOT",
        "note": "Source completeness is not field verification; absent records do not prove physical removal.",
    }
    scenes = [s for s in snapshot["scenes"]
              if s["status"] == "published" and s["model_id"] == model_id
              and s["binding"].get("sceneUrl") == scene_url and s["binding"].get("calibrationProfile") == profile
              and s["binding"].get("activityId") == ""]
    if not scenes:
        raise ValueError("No published baseline scene matches the explicit binding")
    if len(camera_ids) != len(catalog) or len({s["id"] for s in scenes}) != len(scenes):
        raise ValueError("Duplicate camera or published scene IDs in latest snapshot")
    matrices = {json.dumps(s["binding"]["designerMatrix"]) for s in scenes}
    tilesets = {s["binding"]["tilesetId"] for s in scenes}
    if len(matrices) != 1 or len(tilesets) != 1:
        raise ValueError("Published scenes do not share a coordinate transform")
    annotations = []
    for scene in scenes:
        for a in scene["annotations"]:
            anchor, shape = a["anchor"], a["shape"]
            # Matches the active frontend MAe: toWorld(anchor + offset).
            points = [anchor] if shape["type"] == "point" else [[anchor[d] + p[d] for d in range(3)] for p in shape["offsets"]]
            if a["kindId"] in CAMERA_KINDS:
                role = "CAMERA"
            elif a["kindId"] == "fence":
                role = "BARRIER"
            elif a["kindId"] == "pedestrian-route":
                role = "ROUTE_CANDIDATE"
            else:
                role = "LANDMARK"
            device_id = a.get("link", {}).get("deviceId", "")
            annotations.append({"annotation_id": scene["id"] + ":" + a["id"], "source_scene_id": scene["id"],
                                "name": a["name"], "kind_id": a["kindId"], "role": role,
                                "shape": shape["type"], "points": points, "visible": a["visible"],
                                "camera_id": device_id, "device_name": a.get("link", {}).get("deviceName", ""),
                                "matches_camera_catalog": bool(device_id and device_id in camera_ids),
                                "declared_floor": a.get("floor"), "remark": a.get("remark", "")})
    site = {"scene_version": "pending", "model_id": model_id, "scene_url": scene_url,
            "tileset_id": next(iter(tilesets)), "calibration_profile": profile,
            "coordinate_frame": model_id + ":" + profile, "world_coordinate_frame": "ECEF_WGS84", "unit": "m",
            "to_world_matrix": json.loads(next(iter(matrices))), "duplicate_tolerance_m": 1.0,
            "source_evidence_id": "readonly-audit-sha256:" + hashlib.sha256(audit.read_bytes()).hexdigest(),
            "publication_snapshot": publication_snapshot,
            "cameras": sorted(catalog, key=lambda c: c["camera_id"]),
            "annotations": sorted(annotations, key=lambda a: a["annotation_id"])}
    if model_revision is not None:
        if not isinstance(model_revision, str) or not model_revision.strip():
            raise ValueError("model_revision must identify an explicit model publication or manifest")
        site["model_revision"] = model_revision
    digest = hashlib.sha256(json.dumps(site, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    site["scene_version"] = "published-readonly-" + digest[:16]
    bound = collections.defaultdict(list)
    for a in annotations:
        if a["role"] == "CAMERA" and a["matches_camera_catalog"]:
            bound[a["camera_id"]].append(a)
    duplicates = [{"camera_id": key, "annotation_ids": [a["annotation_id"] for a in values],
                   "max_separation_m": max(math.dist(a["points"][0], b["points"][0]) for a in values for b in values)}
                  for key, values in bound.items() if len(values) > 1]
    summary = {"scene_version": site["scene_version"], "published_scenes": [s["id"] for s in scenes],
               "publication_snapshot": publication_snapshot,
               "annotation_count": len(annotations), "kind_counts": dict(collections.Counter(a["kind_id"] for a in annotations)),
               "camera_markers": sum(a["role"] == "CAMERA" for a in annotations), "bound_camera_ids": len(bound),
               "unbound_camera_markers": [a["annotation_id"] for a in annotations if a["role"] == "CAMERA" and not a["camera_id"]],
               "invalid_camera_bindings": [a for a in annotations if a["role"] == "CAMERA" and a["camera_id"] and not a["matches_camera_catalog"]],
               "duplicate_bindings": duplicates, "landmarks": [a for a in annotations if a["role"] != "CAMERA"],
               "navigation_verified": False, "person_localization_verified": False,
               "note": "All declared_floor values are copied metadata, not verified floors; no name-based device matching."}
    return site, summary


def review_times(baseline):
    snapshot = json.loads((baseline / "snapshot-summary.json").read_text(encoding="utf-8"))
    def millis(text):
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("Explicit offset required for timestamp diagnostics")
        return round(dt.timestamp() * 1000)
    samples = []
    for query in snapshot["historical_timestamp_samples"]:
        result = query["result"]
        columns = [c[0] for c in result.get("column_meta", [])]
        for values in result.get("data", []):
            row = dict(zip(columns, values))
            if not all(k in row for k in ["image_time", "utc_time", "device_id"]):
                continue
            delta = millis(row["image_time"]) - millis(row["utc_time"])
            samples.append({"camera_id": row["device_id"], "raw_image_time": row["image_time"], "raw_utc_time": row["utc_time"],
                            "raw_delta_ms": delta, "residual_after_hypothetical_utc8_ms": delta - 8 * 3600 * 1000,
                            "admission": "HOLD", "reasons": ["CAPTURE_TIME_SEMANTICS_UNVERIFIED", "CLOCK_BOUND_UNVERIFIED", "IDENTITY_EVIDENCE_UNAVAILABLE"],
                            "diagnostic_only": True})
    return {"clock_estimates": snapshot["clock_estimates"], "timestamp_samples": samples,
            "automatic_time_corrections": 0, "confirmed_sightings_generated": 0,
            "note": "Eight-hour subtraction is a diagnostic hypothesis, not a timestamp conversion contract."}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--baseline", type=Path)
    p.add_argument("--model-id", required=True)
    p.add_argument("--scene-url", required=True)
    p.add_argument("--calibration-profile", required=True)
    p.add_argument("--model-revision", help="Explicit publication/manifest identity; not proof of geometric accuracy")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    site, summary = import_site(args.audit, args.model_id, args.scene_url, args.calibration_profile, args.model_revision)
    # A new publication goes to a new directory; never overwrite prior evidence.
    outputs = ["site-scene.json", "site-index.json"] + (["capture-time-review.json"] if args.baseline else [])
    if any((args.output / name).exists() for name in outputs):
        raise FileExistsError("Output evidence exists; use a new output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    save(args.output / "site-scene.json", site)
    save(args.output / "site-index.json", summary)
    if args.baseline:
        save(args.output / "capture-time-review.json", review_times(args.baseline))
    print(json.dumps({k: summary[k] for k in ["scene_version", "annotation_count", "camera_markers", "bound_camera_ids", "duplicate_bindings"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
