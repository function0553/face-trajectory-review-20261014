"""Decoded images -> existing SCRFD/ArcFace -> durable P2 observation outbox.

Run: python -m face_engine.trajectory_bridge --help
Capture metadata belongs to the acquisition system. This bridge never turns
processing time into UTC capture evidence or guesses a clock uncertainty.
"""
import argparse
import errno
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout, contextmanager
from pathlib import Path

import numpy as np
from face_engine.alignment.face_align import align_face


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


ACTIVITY_FIELDS = ("activity_id", "activity_num", "config_version", "binding_version", "monitoring_version")


class ActivityContextError(ValueError):
    """An input/receipt has no unambiguous current activity context."""


class EventConflict(ValueError):
    """The same scoped logical event was submitted with different content."""


class RetentionExpired(ValueError):
    """C++ maintenance expired this logical event; replay cannot recreate it."""


class MaintenancePaused(RuntimeError):
    """The C++ owner requested quiescence; no input or old payload may proceed."""


class ConsumerLeaseBusy(RuntimeError):
    """Another consumer or maintenance owner still holds the OS lease."""


def maintenance_request(path):
    return Path(str(Path(path).resolve())+'.maintenance.request')


def retention_tombstone(db, table, key, content_hash=None):
    """Compatibility only: C++ owns time qualification and expiration policy.

    The guard keeps only irreversible hashes, never camera/event/time fields.
    It is checked under the caller's SQLite transaction for every insertion.
    """
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_tombstone'").fetchone() is None:
        return False
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    old = db.execute('SELECT content_hash FROM retention_tombstone WHERE table_name=? AND key_hash=?',
        (table, key_hash)).fetchone()
    if old is None:
        return False
    if content_hash is not None and old[0] != content_hash:
        raise EventConflict('conflicting expired logical event')
    return True


def retention_write_guard(db):
    """Serialize the tombstone check with C++ deletion, before any intake write."""
    path = db.execute('PRAGMA database_list').fetchone()[2]
    if path and maintenance_request(path).exists():
        raise MaintenancePaused('P1 maintenance requested; retry the same logical event')
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')


@contextmanager
def consumer_lease(path, *, wait=False):
    """P1 consumers and C++ external cleanup use the same one-byte lease."""
    stream = Path(str(Path(path).resolve())+'.consumer.lock').open('a+b')
    locked = False
    try:
        stream.seek(0)
        while True:
            if maintenance_request(path).exists():
                if not wait:
                    raise MaintenancePaused('P1 maintenance requested')
                time.sleep(.05)
                continue
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if not wait:
                    raise ConsumerLeaseBusy('P1 consumer or retention maintenance owns the state') from error
                time.sleep(.05)
                continue
            # The request can arrive between checking and acquiring. Errors in
            # this check or in unlock are not expected lock contention.
            if maintenance_request(path).exists():
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
                if not wait:
                    raise MaintenancePaused('P1 maintenance requested')
                time.sleep(.05)
                continue
            break
        locked = True
        # C++ may create and lock an empty lease file. Initialize it only while
        # owning the lock, so concurrent startup can wait instead of writing
        # through another process's locked byte before checking the request.
        if stream.seek(0, 2) == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def activity_context(value, *, require_route=True):
    """Copy explicit backend context; never infer an activity or fan out an input."""
    if not isinstance(value, dict):
        raise ActivityContextError("activity context must be an object")
    fields = ACTIVITY_FIELDS + (("route_rule_id",) if require_route else ())
    for field in fields:
        item = value.get(field)
        if field.endswith("_version"):
            if type(item) is not int or item < 1:
                raise ActivityContextError(f"positive {field} required")
        elif not isinstance(item, str) or not item.strip():
            raise ActivityContextError(f"explicit {field} required")
    return {field: value[field] for field in fields}


def scoped_event_key(kind, value, event_field="event_id"):
    """Activity/source/event is the logical key; a content digest is not a key."""
    for field in ("source_id", event_field):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"{field} required")
    activity = value.get("activity_id")
    if not isinstance(activity, str) or not activity.strip():
        activity = None  # Legacy data remains explicitly unassigned.
    return kind + ":" + encoded([activity, value["source_id"], value[event_field]])


def verify_activity_receipt(original, reply, *, require_route=True):
    context = activity_context(original, require_route=require_route)
    if any(reply.get(field) != item for field, item in context.items()):
        raise ActivityContextError("P2 acknowledgement activity/version mismatch")
    monitor = reply.get("monitoring")
    if isinstance(monitor, dict) and monitor.get("activity_id", context["activity_id"]) != context["activity_id"]:
        raise ActivityContextError("P2 monitoring activity mismatch")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


class TrajectoryExtractor:
    """Quality-qualified embeddings; scheduling and clock normalization stay outside."""
    def __init__(self, detector, recognizer, feature_space, *, quality_policy=None):
        from face_engine.quality import QualityPolicy
        self.detector, self.recognizer = detector, recognizer
        self.feature_space = feature_space
        self.quality_policy = QualityPolicy.from_config(quality_policy)

    @classmethod
    def from_config(cls, config):
        from face_engine.cli import _models, _model_path
        with redirect_stdout(sys.stderr):
            detector, recognizer = _models(config)
        d, r = config["detector"], config["recognizer"]
        profile = {k: d[k] for k in ("input_size", "confidence_threshold", "nms_threshold")}
        for key in ("small_image_input_size", "small_image_max_side"):
            if key in d:
                profile[key] = d[key]
        space = dict(recognition_model_sha256=file_sha256(_model_path(r["model_path"])),
                     detector_model_sha256=file_sha256(_model_path(d["model_path"])),
                     detector_profile="p1-scrfd:" + encoded(profile),
                     alignment_version="p1-five-point-112-v1",
                     preprocessing_version="p1-rgb-127.5-v1", output_version="l2-512-v1")
        return cls(detector, recognizer, space, quality_policy=config.get("quality"))

    def extract(self, frames):
        """Compatibility wrapper. Use extract_detailed to retain rejected evidence."""
        return [frame["observations"] for frame in self.extract_detailed(frames)]

    def extract_detailed(self, frames):
        """Each (BGR image, acquisition metadata) returns observations/rejected_faces.

        face_index is sorted detection order BEFORE quality filtering, keeping IDs
        and caller-provided per-face tracking metadata stable when a face is rejected.
        time_record is passed through as immutable JSON data, never interpreted here.
        DEVICE_CAPTURE is not promoted to UTC_CAPTURE; P2 must qualify the clock.
        """
        from face_engine.quality import evaluate_face_quality, alignment_rejection
        outputs, aligned, targets = [], [], []
        for image, meta in frames:
            if not isinstance(meta, dict):
                raise ValueError("acquisition metadata must be a dictionary")
            for key in ("source_id", "camera_id", "frame_id"):
                if not isinstance(meta.get(key), str) or not meta[key]:
                    raise ValueError(f"{key} is required")
            context = activity_context(meta, require_route=meta.get('purpose') != 'GALLERY_ENROLLMENT')
            time_source = meta.get("time_source", "UNKNOWN")
            capture = meta.get("captured_at_unix_ms", 0)
            uncertainty = meta.get("clock_uncertainty_ms", -1)
            domain = meta.get("clock_domain", "UNKNOWN")
            if time_source not in ("UNKNOWN", "UTC_CAPTURE", "DEVICE_CAPTURE", "MEDIA_OFFSET", "RECEIVED_AT"):
                raise ValueError("invalid time source")
            if type(capture) is not int or type(uncertainty) is not int:
                raise ValueError("capture and uncertainty must be integers")
            if not -(2**63) <= capture < 2**63 or not -(2**63) <= uncertainty < 2**63:
                raise ValueError("capture metadata outside int64")
            if not isinstance(domain, str) or not domain:
                raise ValueError("clock_domain must be a nonempty string")
            if time_source == "UTC_CAPTURE" and ("captured_at_unix_ms" not in meta or uncertainty < 0 or domain != "UTC"):
                time_source = "UNKNOWN"
            time_record = None
            if "time_record" in meta:
                if not isinstance(meta["time_record"], dict):
                    raise ValueError("time_record must be an object")
                time_record = json.loads(encoded(meta["time_record"]))
            per_face_meta = meta.get("faces", {})
            if not isinstance(per_face_meta, dict):
                raise ValueError("faces metadata must be a dictionary keyed by detection index")
            faces = sorted(self.detector.detect_faces(image), key=lambda face: tuple(face["bbox"]))
            output = dict(observations=[], rejected_faces=[], needs_high_resolution=False,
                          detected_faces=len(faces))
            outputs.append(output)
            for index, face in enumerate(faces):
                quality = evaluate_face_quality(image, face, self.quality_policy, metadata=meta)
                crop = None
                if quality["accepted"]:
                    try:
                        crop = align_face(image, face["landmarks"])
                    except ValueError:
                        quality = alignment_rejection(quality)
                if not quality["accepted"]:
                    output["rejected_faces"].append(dict(face_index=index,
                        bbox=np.asarray(face["bbox"]).tolist(), landmarks=np.asarray(face["landmarks"]).tolist(),
                        detection_score=float(face["score"]), status="QUALITY_REJECTED",
                        quality=quality, needs_high_resolution=quality["needs_high_resolution"]))
                    output["needs_high_resolution"] |= quality["needs_high_resolution"]
                    continue
                identity = [context["activity_id"], meta["source_id"], meta["camera_id"], meta["frame_id"], index]
                oid = "p1:" + hashlib.sha256(encoded(identity).encode()).hexdigest()
                observation = dict(observation_id=oid, source_id=meta["source_id"], camera_id=meta["camera_id"],
                    frame_id=meta["frame_id"], face_index=index, captured_at_unix_ms=capture,
                    clock_uncertainty_ms=uncertainty, time_source=time_source, clock_domain=domain,
                    feature_space=self.feature_space, detection_score=float(face["score"]),
                    face_size_px=quality["metrics"]["face_size_px"], quality=quality)
                observation.update(context)
                if time_record is not None:
                    observation["time_record"] = json.loads(encoded(time_record))
                face_meta = per_face_meta.get(str(index), {})
                if not isinstance(face_meta, dict):
                    raise ValueError("per-face metadata must be a dictionary")
                for key in ("local_track_id", "track_session_id", "reentry_evidence_id"):
                    if key in face_meta:
                        if not isinstance(face_meta[key], str):
                            raise ValueError(f"{key} must be a string")
                        observation[key] = face_meta[key]
                if bool(observation.get("local_track_id")) != bool(observation.get("track_session_id")):
                    raise ValueError("local track ID and track session must be supplied together")
                output["observations"].append(observation)
                aligned.append(crop)
                targets.append(observation)
        if aligned:
            vectors = np.asarray(self.recognizer.extract_embeddings(aligned), dtype=np.float32)
            if vectors.shape != (len(aligned), 512) or not np.isfinite(vectors).all():
                raise ValueError("recognizer must return finite [N,512] vectors")
            norms = np.linalg.norm(vectors, axis=1)
            if not np.isfinite(norms).all() or (norms <= 0).any():
                raise ValueError("nonzero finite embeddings required")
            for observation, vector, norm in zip(targets, vectors, norms):
                observation["embedding"] = (vector / norm).tolist()
        return outputs


class Outbox:
    def __init__(self, path, max_pending=10000, max_pending_bytes=128*1024*1024, max_database_bytes=512*1024*1024):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path).resolve()
        with consumer_lease(self.path, wait=True):
            self.db = sqlite3.connect(path, timeout=5)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            pages = max_database_bytes // page_size
            self.page_limit = pages
            if pages < 16 or self.db.execute("PRAGMA page_count").fetchone()[0] > pages:
                self.db.close()
                raise ValueError("outbox database exceeds configured capacity or limit too small")
            self.db.execute(f"PRAGMA max_page_count={pages}")
            self.db.execute("CREATE TABLE IF NOT EXISTS frames(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,payload TEXT,receipt TEXT,payload_hash TEXT NOT NULL)")
            for table in ('frames', 'outbox'):
                names = {row[1] for row in self.db.execute(f'PRAGMA table_info({table})')}
                for name, definition in {'activity_id': 'TEXT', 'source_id': 'TEXT', 'event_id': 'TEXT',
                        'context': 'TEXT', 'status': "TEXT NOT NULL DEFAULT 'QUARANTINED'", 'created': 'REAL'}.items():
                    if name not in names:
                        self.db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            if 'metadata' not in {row[1] for row in self.db.execute('PRAGMA table_info(frames)')}:
                self.db.execute('ALTER TABLE frames ADD COLUMN metadata TEXT')
            columns={row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
            if "enqueue_seq" not in columns:
                self.db.execute("ALTER TABLE outbox ADD COLUMN enqueue_seq INTEGER")
                self.db.execute("UPDATE outbox SET enqueue_seq=rowid")
            self.db.execute("CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(enqueue_seq) WHERE payload IS NOT NULL")
            self.db.execute("UPDATE outbox SET status='QUARANTINED' WHERE context IS NULL AND payload IS NOT NULL")
            self.db.commit()
            self.max_pending, self.max_pending_bytes = max_pending, max_pending_bytes
            if min(max_pending, max_pending_bytes) < 1:
                raise ValueError("positive outbox limits required")

    def frame_seen(self, key, fingerprint):
        if retention_tombstone(self.db, 'frames', key, fingerprint):
            return True
        row = self.db.execute("SELECT fingerprint FROM frames WHERE id=?", (key,)).fetchone()
        if row and row[0] != fingerprint:
            raise EventConflict("conflicting duplicate frame metadata/image/feature contract")
        return row is not None

    def quarantine_frame(self, key, fingerprint, metadata):
        with self.db:
            retention_write_guard(self.db)
            if self.frame_seen(key, fingerprint):
                return 0
            self.db.execute("INSERT INTO frames(id,fingerprint,source_id,event_id,status,created,metadata) VALUES(?,?,?,?,'QUARANTINED',strftime('%s','now'),?)",
                (key, fingerprint, metadata.get('source_id'), metadata.get('frame_id'), encoded(metadata)))
        return 0

    def enqueue(self, key, fingerprint, observations, policy, graph, *, metadata=None):
        metadata = metadata if metadata is not None else (observations[0] if observations else {})
        context = activity_context(metadata)
        if key != scoped_event_key('capture', metadata, 'frame_id'):
            raise ValueError('frame key must use activity/source/frame scope')
        with self.db:
            retention_write_guard(self.db)
            if self.frame_seen(key, fingerprint):
                return 0
            for observation in observations:
                if activity_context(observation) != context:
                    raise ActivityContextError('frame observation activity mismatch')
                payload = encoded(dict(context, observation=observation,
                    time_record=observation.get('time_record'), policy=policy, graph=graph))
                oid = scoped_event_key('observe', {**context, 'source_id': observation['source_id'], 'event_id': observation['observation_id']})
                digest = hashlib.sha256(payload.encode()).hexdigest()
                if retention_tombstone(self.db, 'outbox', oid, digest):
                    raise RetentionExpired('EXPIRED: observation cannot be re-enqueued')
                old = self.db.execute("SELECT payload_hash FROM outbox WHERE id=?", (oid,)).fetchone()
                if old and old[0] != digest:
                    raise EventConflict("conflicting duplicate observation ID")
                self.db.execute("INSERT OR IGNORE INTO outbox(id,payload,payload_hash,enqueue_seq,activity_id,source_id,event_id,context,status,created) VALUES(?,?,?,(SELECT coalesce(max(rowid),0)+1 FROM outbox),?,?,?,?,'PENDING',strftime('%s','now'))",
                    (oid, payload, digest, context['activity_id'], observation['source_id'], observation['observation_id'], encoded(context)))
            count, size = self.db.execute("SELECT count(*),coalesce(sum(length(cast(payload AS BLOB))),0) FROM outbox WHERE payload IS NOT NULL AND status='PENDING'").fetchone()
            if count > self.max_pending or size > self.max_pending_bytes:
                raise RuntimeError("outbox capacity reached; pause acquisition or flush before retry")
            self.db.execute("INSERT INTO frames(id,fingerprint,activity_id,source_id,event_id,context,status,created,metadata) VALUES(?,?,?,?,?,?,'PROCESSED',strftime('%s','now'),?)",
                (key, fingerprint, context['activity_id'], metadata['source_id'], metadata['frame_id'], encoded(context), encoded(metadata)))
        return len(observations)

    def flush(self, endpoint, limit=1000, timeout=20, authorization=None):
        sent = 0
        for _ in range(limit):
            row = self.db.execute("SELECT id,payload FROM outbox INDEXED BY outbox_pending WHERE payload IS NOT NULL AND status='PENDING' ORDER BY enqueue_seq LIMIT 1").fetchone()
            if row is None:
                break
            original = json.loads(row[1])
            if retention_tombstone(self.db, 'outbox', row[0]):
                with self.db:
                    self.db.execute('DELETE FROM outbox WHERE id=?', (row[0],))
                continue
            try:
                activity_context(original)
            except ActivityContextError:
                with self.db:
                    self.db.execute("UPDATE outbox SET status='QUARANTINED' WHERE id=?", (row[0],))
                continue
            headers = {"Content-Type": "application/json"}
            if authorization:
                headers['Authorization'] = authorization
            request = urllib.request.Request(endpoint.rstrip("/")+"/v1/monitoring/observe",
                data=row[1].encode(), headers=headers, method="POST")
            # Timeout/network failure leaves the durable payload pending. Retrying
            # reuses the exact body and ID even if P2 committed before disconnection.
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    reply = json.load(response)
            except urllib.error.HTTPError as error:
                detail=error.read(2048).decode("utf-8",errors="replace")
                raise RuntimeError(f"P2 HTTP {error.code}: {detail}; observation remains pending") from error
            if reply.get("ok") is not True or reply.get('observation_id', (reply.get('association') or {}).get('observation_id')) != original['observation']['observation_id']:
                raise RuntimeError("invalid association acknowledgement")
            try:
                verify_activity_receipt(original, reply)
            except ActivityContextError:
                with self.db:
                    self.db.execute("UPDATE outbox SET status='QUARANTINED',receipt=? WHERE id=?", (encoded(reply), row[0]))
                continue
            with self.db:
                retention_write_guard(self.db)
                if retention_tombstone(self.db, 'outbox', row[0]):
                    continue
                self.db.execute("UPDATE outbox SET payload=NULL,receipt=?,status='ACKNOWLEDGED' WHERE id=?", (encoded(reply), row[0]))
            sent += 1
        return sent

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def reopen(self):
        if self.db is not None:
            raise RuntimeError('outbox is already open')
        self.db = sqlite3.connect(str(self.path), timeout=5)
        self.db.execute('PRAGMA synchronous=FULL')
        if self.db.execute('PRAGMA page_count').fetchone()[0] > self.page_limit:
            self.db.close()
            self.db = None
            raise ValueError('outbox exceeds configured storage capacity after maintenance')
        self.db.execute(f'PRAGMA max_page_count={self.page_limit}')

    def consumer_lease(self, *, wait=False):
        return consumer_lease(self.path, wait=wait)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--events", help="JSONL: image_path plus source/camera/frame and capture metadata")
    parser.add_argument("--policy", help="explicit P2 policy JSON file")
    parser.add_argument("--graph", help="explicit P2 graph JSON file")
    parser.add_argument("--outbox", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8081")
    parser.add_argument("--enqueue-only", action="store_true")
    parser.add_argument("--flush-only", action="store_true")
    parser.add_argument("--max-pending", type=int, default=10000)
    parser.add_argument("--max-outbox-mb", type=int, default=512)
    args = parser.parse_args()
    box = Outbox(args.outbox, args.max_pending, max_database_bytes=args.max_outbox_mb*1024*1024)
    box.close()
    queued = sent = 0
    try:
        def consume(fn):
            # Each complete inference or send owns the lease. No image/body or
            # SQLite page cache survives releasing it; a request prevents reuse.
            with box.consumer_lease(wait=True):
                box.reopen()
                try:
                    return fn()
                finally:
                    box.close()

        authorization = os.environ.get('FACE_TRAJECTORY_AUTHORIZATION')
        def flush_once():
            return consume(lambda: box.flush(args.endpoint, limit=1, authorization=authorization))

        if not args.flush_only:
            if not all((args.config, args.events, args.policy, args.graph)):
                parser.error("--config, --events, --policy and --graph required for extraction")
            from face_engine.config import load_config
            from face_engine.cli import _image
            extractor = TrajectoryExtractor.from_config(load_config(args.config))
            policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
            graph = json.loads(Path(args.graph).read_text(encoding="utf-8"))
            with Path(args.events).open(encoding="utf-8") as stream:
                def process_one():
                    line = stream.readline()
                    if not line:
                        return None
                    event = json.loads(line)
                    image_path = Path(event["image_path"])
                    if not image_path.is_absolute():
                        image_path = Path(args.events).resolve().parent / image_path
                    key = scoped_event_key('capture', event, 'frame_id')
                    image_hash = file_sha256(image_path)
                    fingerprint = hashlib.sha256(encoded([event, image_hash, extractor.feature_space, policy, graph]).encode()).hexdigest()
                    if box.frame_seen(key, fingerprint):
                        return 0
                    try:
                        activity_context(event)
                    except ActivityContextError:
                        return box.quarantine_frame(key, fingerprint, event)
                    observations = extractor.extract([(_image(image_path), event)])[0]
                    return box.enqueue(key, fingerprint, observations, policy, graph, metadata=event)
                while True:
                    count = consume(process_one)
                    if count is None:
                        break
                    queued += count
                    if not args.enqueue_only:
                        sent += flush_once()
        if not args.enqueue_only:
            while True:
                count = flush_once()
                sent += count
                if not count:
                    break
        pending = consume(lambda: box.db.execute("SELECT count(*) FROM outbox WHERE payload IS NOT NULL AND status='PENDING'").fetchone()[0])
        print(encoded(dict(queued=queued, sent=sent, pending=pending)))
    finally:
        box.close()


if __name__ == "__main__":
    main()
