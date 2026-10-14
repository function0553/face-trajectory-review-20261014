"""Shared original-pixel quality, batched embeddings and person-level decisions."""
from collections.abc import Sequence
import math

import numpy as np

from face_engine.alignment.face_align import align_face
from face_engine.detector.base import FaceDetector
from face_engine.index.base import FaceIndex
from face_engine.recognizer.base import FaceRecognizer
from face_engine.quality import QualityPolicy, evaluate_face_quality, alignment_rejection


def distinct_person_hits(hits):
    """Aggregate templates by person before measuring top1/top2 ambiguity."""
    best = {}
    for hit in hits:
        if not isinstance(hit.subject_id, str) or not hit.subject_id:
            raise ValueError("index returned invalid subject_id")
        score = float(hit.similarity)
        if not math.isfinite(score) or not -1.000001 <= score <= 1.000001:
            raise ValueError("index returned invalid cosine similarity")
        score = max(-1., min(1., score))
        best[hit.subject_id] = max(score, best.get(hit.subject_id, -1.))
    return [{"subject_id": sid, "similarity": score}
            for sid, score in sorted(best.items(), key=lambda row: (-row[1], row[0]))]


class FaceRecognitionEngine:
    """No scheduling or priority input can change identity/quality thresholds."""
    def __init__(self, detector: FaceDetector, recognizer: FaceRecognizer, index: FaceIndex,
                 low_threshold: float, high_threshold: float, top_k: int = 5, *,
                 min_top1_top2_margin: float = 0.05, quality_policy=None):
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in (low_threshold, high_threshold, min_top1_top2_margin)):
            raise ValueError("matching thresholds must be finite numeric")
        if not -1 <= low_threshold < high_threshold <= 1:
            raise ValueError("Require -1 <= low_threshold < high_threshold <= 1")
        if not 0 <= min_top1_top2_margin <= 2:
            raise ValueError("min_top1_top2_margin must be in [0,2]")
        if type(top_k) is not int or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        self.detector, self.recognizer, self.index = detector, recognizer, index
        self.low_threshold, self.high_threshold = low_threshold, high_threshold
        self.top_k, self.min_top1_top2_margin = top_k, min_top1_top2_margin
        self.quality_policy = QualityPolicy.from_config(quality_policy)

    @classmethod
    def from_config(cls, detector, recognizer, index, config):
        matching = config["matching"]
        return cls(detector, recognizer, index, matching["low_threshold"], matching["high_threshold"],
                   config["face_index"]["top_k"],
                   min_top1_top2_margin=matching.get("min_top1_top2_margin", 0.05),
                   quality_policy=config.get("quality"))

    def recognize_image(self, image: np.ndarray, source_id: str | None = None, *, metadata=None) -> list[dict]:
        return self.recognize_batch([image], [source_id], metadata=[metadata])[0]

    def recognize_batch(self, images: Sequence[np.ndarray], source_ids: Sequence[str | None] | None = None,
                        *, metadata=None) -> list[list[dict]]:
        if source_ids is None:
            source_ids = [None] * len(images)
        if metadata is None:
            metadata = [None] * len(images)
        if len(source_ids) != len(images) or len(metadata) != len(images):
            raise ValueError("source_ids, metadata and images length must match")
        if any(sid is not None and not isinstance(sid, str) for sid in source_ids):
            raise ValueError("source_id must be a string or None")
        outputs, aligned, targets = [], [], []
        for image, source_id, meta in zip(images, source_ids, metadata):
            frame_results = []
            for face in self.detector.detect_faces(image):
                quality = evaluate_face_quality(image, face, self.quality_policy, metadata=meta)
                crop = None
                if quality["accepted"]:
                    try:
                        crop = align_face(image, face["landmarks"])
                    except ValueError:
                        quality = alignment_rejection(quality)
                row = dict(source_id=source_id, bbox=face["bbox"], landmarks=face["landmarks"],
                           detection_score=face["score"], subject_id=None, similarity=None,
                           status="UNKNOWN" if quality["accepted"] else "QUALITY_REJECTED",
                           top_k=[], second_similarity=None, top1_top2_margin=None,
                           quality=quality, needs_high_resolution=quality["needs_high_resolution"],
                           thresholds_calibrated=False)
                frame_results.append(row)
                if quality["accepted"]:
                    aligned.append(crop)
                    targets.append(row)
            outputs.append(frame_results)
        if not aligned:
            return outputs
        embeddings = np.asarray(self.recognizer.extract_embeddings(aligned), dtype=np.float32)
        if embeddings.shape != (len(aligned), 512) or not np.isfinite(embeddings).all():
            raise ValueError("recognizer must return finite [N,512] embeddings")
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        if not np.isfinite(norms).all() or (norms <= 0).any():
            raise ValueError("recognizer returned zero or non-finite embedding norm")
        embeddings = embeddings / norms
        # At least two DIFFERENT people must be considered even if caller asks top_k=1.
        query_k = max(self.top_k, 2)
        while True:
            candidates = self.index.search_batch(embeddings, top_k=query_k)
            if len(candidates) != len(targets):
                raise ValueError("index returned incorrect batch length")
            people = [distinct_person_hits(hits) for hits in candidates]
            size = self.index.size()
            if query_k >= size or all(len(rows) >= 2 or len(raw) < query_k for rows, raw in zip(people, candidates)):
                break
            query_k = min(size, query_k * 2)
        for row, hits in zip(targets, people):
            best = hits[0] if hits else None
            score = best["similarity"] if best else None
            second = hits[1]["similarity"] if len(hits) > 1 else None
            margin = score - second if second is not None else None
            if best is None or score < self.low_threshold:
                status, subject_id = "UNKNOWN", None
            elif score >= self.high_threshold and (margin is None or margin >= self.min_top1_top2_margin):
                status, subject_id = "MATCH", best["subject_id"]
            else:
                status, subject_id = "REVIEW", best["subject_id"]
            row.update(status=status, subject_id=subject_id, similarity=score,
                       top_k=hits[:self.top_k], second_similarity=second, top1_top2_margin=margin)
        return outputs


ImagePipeline = FaceRecognitionEngine
