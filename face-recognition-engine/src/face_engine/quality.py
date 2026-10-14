"""Face quality gates measured before alignment or enlargement.

These defaults are engineering starting points, not calibrated identity accuracy.
Landmark geometry is a pose proxy; it is not a certified 3D pose or occlusion model.
"""
from dataclasses import asdict, dataclass, fields
from collections.abc import Mapping
import math

import cv2
import numpy as np


@dataclass(frozen=True)
class QualityPolicy:
    min_face_size_px: float = 64.0
    min_inter_eye_distance_px: float = 20.0
    min_detection_score: float = 0.6
    min_laplacian_variance: float = 40.0
    max_clipped_fraction: float = 0.08
    max_yaw_proxy: float = 0.6
    min_pitch_proxy: float = 0.1
    max_pitch_proxy: float = 0.9

    def __post_init__(self):
        for key, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"quality.{key} must be finite numeric")
        if min(self.min_face_size_px, self.min_inter_eye_distance_px) <= 0:
            raise ValueError("quality pixel thresholds must be positive")
        if not 0 <= self.min_detection_score <= 1 or self.min_laplacian_variance < 0:
            raise ValueError("invalid detection or sharpness threshold")
        if not 0 <= self.max_clipped_fraction < 1 or self.max_yaw_proxy <= 0:
            raise ValueError("invalid boundary or yaw threshold")
        if not 0 <= self.min_pitch_proxy < self.max_pitch_proxy <= 1:
            raise ValueError("pitch proxy thresholds must satisfy 0 <= min < max <= 1")

    @classmethod
    def from_config(cls, value=None):
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("quality must be a mapping or QualityPolicy")
        unknown = set(value) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError("unknown quality settings: " + ", ".join(sorted(unknown)))
        return cls(**value)


def evaluate_face_quality(image, face, policy=None, *, metadata=None):
    """Return JSON-safe measurements and explicit rejection reasons.

    Coordinates refer to the supplied image. If this image was resized, callers
    may supply source_width/source_height (or resized_from_width/height) for
    the SAME field of view. A declared upscaled=True image without those dimensions
    is rejected. Downsampling never earns extra effective face pixels. Crop origins
    and full-sensor dimensions must not be substituted for this resize provenance.
    Without provenance, measurements describe the supplied decoded image only.
    """
    policy = QualityPolicy.from_config(policy)
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) < 1:
        raise ValueError("quality input must be nonempty uint8 BGR image")
    metadata = {} if metadata is None else metadata
    if not isinstance(metadata, Mapping):
        raise ValueError("image metadata must be a mapping")
    reasons, metrics = [], {}
    height, width = image.shape[:2]
    metrics.update(input_width=width, input_height=height)
    sx = sy = 1.0
    declared = None
    for xkey, ykey in (("source_width", "source_height"), ("resized_from_width", "resized_from_height")):
        if xkey in metadata or ykey in metadata:
            x, y = metadata.get(xkey), metadata.get(ykey)
            if type(x) is not int or type(y) is not int or x <= 0 or y <= 0:
                raise ValueError(f"{xkey}/{ykey} must be positive integer dimensions together")
            if declared is not None and declared != (x, y):
                raise ValueError("conflicting source image dimensions")
            declared = (x, y)
    if "upscaled" in metadata and type(metadata["upscaled"]) is not bool:
        raise ValueError("upscaled must be a boolean")
    if declared is not None:
        sx, sy = min(1.0, declared[0] / width), min(1.0, declared[1] / height)
        metrics.update(source_width=declared[0], source_height=declared[1])
        if metadata.get("upscaled") is True and sx == sy == 1.0:
            reasons.append("UPSCALE_PROVENANCE_INCONSISTENT")
    elif metadata.get("upscaled") is True:
        reasons.append("ORIGINAL_RESOLUTION_UNKNOWN")
    metrics.update(pixel_basis="declared_source_capped_by_input" if declared else "decoded_input_only",
                   effective_scale_x=sx, effective_scale_y=sy)

    def result():
        return dict(status="QUALITY_REJECTED" if reasons else "QUALITY_ACCEPTED",
                    accepted=not reasons, reasons=list(dict.fromkeys(reasons)), metrics=metrics,
                    needs_high_resolution=any(reason in reasons for reason in (
                        "FACE_TOO_SMALL", "INTER_EYE_DISTANCE_TOO_SMALL", "BLURRED",
                        "ORIGINAL_RESOLUTION_UNKNOWN", "UPSCALE_PROVENANCE_INCONSISTENT")),
                    policy_version="p1-original-pixel-quality-v1", thresholds_calibrated=False,
                    thresholds=asdict(policy))

    try:
        box = np.asarray(face["bbox"], dtype=np.float64)
        raw_box = np.asarray(face.get("raw_bbox", face["bbox"]), dtype=np.float64)
        points = np.asarray(face["landmarks"], dtype=np.float64)
        score = float(face["score"])
    except (KeyError, TypeError, ValueError, OverflowError):
        reasons.append("INVALID_DETECTION")
        return result()
    if (box.shape != (4,) or not np.isfinite(box).all() or
            np.max(np.abs(box)) > max(width, height) * 1024 or
            raw_box.shape != (4,) or not np.isfinite(raw_box).all() or
            np.max(np.abs(raw_box)) > max(width, height) * 1024 or
            not math.isfinite(score) or not 0 <= score <= 1):
        reasons.append("INVALID_DETECTION")
        return result()
    x1, y1, x2, y2 = box
    fw, fh = x2 - x1, y2 - y1
    if fw <= 0 or fh <= 0:
        reasons.append("INVALID_FACE_BOX")
        return result()
    rx1, ry1, rx2, ry2 = raw_box
    raw_width, raw_height = rx2 - rx1, ry2 - ry1
    if raw_width <= 0 or raw_height <= 0:
        reasons.append("INVALID_FACE_BOX")
        return result()
    if "raw_bbox" in face:
        expected_visible = np.clip(raw_box, [0., 0., 0., 0.], [width, height, width, height])
        if not np.allclose(box, expected_visible, rtol=0., atol=1e-6):
            reasons.append("INVALID_DETECTION")
            return result()
    native_size = float(min(fw * sx, fh * sy))
    metrics.update(face_width_px=float(fw * sx), face_height_px=float(fh * sy),
                   face_size_px=native_size, detection_score=score)
    if native_size < policy.min_face_size_px:
        reasons.append("FACE_TOO_SMALL")
    if score < policy.min_detection_score:
        reasons.append("LOW_DETECTION_SCORE")
    visible_area = max(0., min(width, rx2) - max(0., rx1)) * max(0., min(height, ry2) - max(0., ry1))
    clipped = float(max(0., min(1., 1. - visible_area / (raw_width * raw_height))))
    metrics["clipped_fraction"] = clipped
    if clipped > policy.max_clipped_fraction:
        reasons.append("FACE_CLIPPED")
    if (points.shape != (5, 2) or not np.isfinite(points).all() or
            np.max(np.abs(points)) > max(width, height) * 1024):
        reasons.append("INVALID_LANDMARKS")
        return result()
    if ((points[:, 0] < max(0., x1)) | (points[:, 0] >= min(width, x2)) |
            (points[:, 1] < max(0., y1)) | (points[:, 1] >= min(height, y2))).any():
        reasons.append("LANDMARK_OUTSIDE_FACE")
    native = points * np.array([sx, sy])
    eye = native[1] - native[0]
    ipd = float(np.linalg.norm(eye))
    metrics["inter_eye_distance_px"] = ipd
    if ipd < policy.min_inter_eye_distance_px:
        reasons.append("INTER_EYE_DISTANCE_TOO_SMALL")
    if ipd <= 1e-6:
        reasons.append("DEGENERATE_LANDMARKS")
        return result()
    axis = eye / ipd
    down = np.array([-axis[1], axis[0]])
    eye_mid = (native[0] + native[1]) / 2
    mouth_mid = (native[3] + native[4]) / 2
    mouth_depth = float(np.dot(mouth_mid - eye_mid, down))
    yaw = abs(float(np.dot(native[2] - eye_mid, axis) / ipd))
    metrics.update(yaw_proxy=yaw, eye_roll_degrees=float(math.degrees(math.atan2(eye[1], eye[0]))))
    if mouth_depth <= 1e-6 or np.dot(native[4] - native[3], axis) <= 0:
        reasons.append("DEGENERATE_LANDMARKS")
    else:
        pitch = float(np.dot(native[2] - eye_mid, down) / mouth_depth)
        metrics["pitch_proxy"] = pitch
        if not policy.min_pitch_proxy <= pitch <= policy.max_pitch_proxy:
            reasons.append("EXTREME_PITCH_PROXY")
    if yaw > policy.max_yaw_proxy:
        reasons.append("EXTREME_YAW_PROXY")
    left, top = max(0, int(math.floor(x1))), max(0, int(math.floor(y1)))
    right, bottom = min(width, int(math.ceil(x2))), min(height, int(math.ceil(y2)))
    if left >= right or top >= bottom:
        reasons.append("FACE_OUTSIDE_IMAGE")
    else:
        crop = image[top:bottom, left:right]
        if sx < 1 or sy < 1:
            # Measure at the declared native pixel scale, never on the enlarged crop.
            crop = cv2.resize(crop, (max(1, round(crop.shape[1] * sx)), max(1, round(crop.shape[0] * sy))), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        metrics["laplacian_variance"] = sharpness
        if sharpness < policy.min_laplacian_variance:
            reasons.append("BLURRED")
    return result()


def alignment_rejection(quality):
    """Convert an otherwise accepted measurement when five-point fitting fails."""
    return {**quality, "accepted": False, "status": "QUALITY_REJECTED",
            "reasons": [*quality["reasons"], "ALIGNMENT_FAILED"]}
