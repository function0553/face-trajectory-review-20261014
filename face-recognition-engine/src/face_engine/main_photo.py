"""Bounded original-frame bursts and conservative per-face photo selection.

Internal capture format only. Every selected photo, embedding and timestamp
come from ONE original frame; sub-stream coordinates never crop a main frame.
"""
import hashlib
import json
import math
import struct

import numpy as np

MAGIC = b'FACEHD01'
MAX_BYTES = 12 * 1024 * 1024
MAX_FRAMES = 3
FRAME_FIELDS = ('source_id', 'camera_id', 'frame_id', 'stream_kind',
                'received_at_unix_ms', 'captured_at_unix_ms', 'time_source',
                'clock_domain', 'clock_uncertainty_ms', 'time_record',
                'camera_clock_profile', 'decode_keyframes_only')


def pack_burst(frames):
    """frames: original encoded image bytes and their own acquisition metadata."""
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError('invalid HD burst count')
    first = frames[0][1]
    times = []
    parts = [MAGIC]
    for body, meta in frames:
        if (not isinstance(body, bytes) or not body or
                any(meta.get(k) != first.get(k) for k in ('source_id', 'camera_id')) or
                meta.get('stream_kind') != 'main' or not meta.get('frame_id')):
            raise ValueError('invalid HD burst camera or frame')
        stamp = meta.get('received_at_unix_ms')
        if type(stamp) is not int:
            raise ValueError('HD burst needs original receipt timestamps')
        times.append(stamp)
        record = {k: meta[k] for k in FRAME_FIELDS if k in meta}
        record['image_sha256'] = hashlib.sha256(body).hexdigest()
        header = json.dumps(record, separators=(',', ':'), allow_nan=False).encode()
        if len(header) > 65536:
            raise ValueError('HD burst header too large')
        parts.extend((struct.pack('>II', len(header), len(body)), header, body))
    if (times != sorted(times) or times[-1] - times[0] > 2000 or
            len({m['frame_id'] for _, m in frames}) != len(frames)):
        raise ValueError('HD burst crosses time window or repeats a frame')
    packed = b''.join(parts)
    if len(packed) > MAX_BYTES:
        raise ValueError('HD burst exceeds capture bound')
    meta = dict(first, frame_id='hd:' + hashlib.sha256(packed).hexdigest(),
                capture_format='main-photo-burst-v1', hd_upgrade_managed=True)
    return packed, meta


def unpack_burst(body, metadata):
    if not isinstance(body, bytes) or len(body) > MAX_BYTES or not body.startswith(MAGIC):
        raise ValueError('invalid HD burst envelope')
    records, offset = [], len(MAGIC)
    while offset < len(body):
        if len(records) >= MAX_FRAMES or offset + 8 > len(body):
            raise ValueError('invalid HD burst length')
        hs, bs = struct.unpack('>II', body[offset:offset + 8]); offset += 8
        if not 0 < hs <= 65536 or not 0 < bs <= MAX_BYTES or offset + hs + bs > len(body):
            raise ValueError('invalid HD burst sizes')
        header = json.loads(body[offset:offset + hs]); offset += hs
        raw = body[offset:offset + bs]; offset += bs
        if not isinstance(header, dict) or hashlib.sha256(raw).hexdigest() != header.get('image_sha256'):
            raise ValueError('HD burst image hash mismatch')
        if any(header.get(k) != metadata.get(k) for k in ('source_id', 'camera_id')):
            raise ValueError('HD burst camera identity mismatch')
        # Route and activity always come from the committed outer capture.
        meta = {k: v for k, v in metadata.items() if k not in FRAME_FIELDS and k != 'capture_format'}
        meta.update({k: header[k] for k in FRAME_FIELDS if k in header})
        records.append((raw, meta))
    # Reuse all count, time-window and uniqueness checks on untrusted storage.
    pack_burst(records)
    return records


def _iou(a, b):
    x = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    y = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = lambda r: max(0, r[2] - r[0]) * max(0, r[3] - r[1])
    return x * y / max(1e-9, area(a) + area(b) - x * y)


def photo_score(observation):
    q = observation.get('quality', {}).get('metrics', {})
    size = max(0, float(q.get('face_size_px', observation.get('face_size_px', 0))))
    sharp = max(0, float(q.get('laplacian_variance', 0)))
    pose = 1 / (1 + max(0, float(q.get('yaw_proxy', 0))))
    return size * math.sqrt(min(sharp, 400) / 400) * pose * observation.get('detection_score', 0)


def photo_is_clear(observation):
    """Delivery quality only; detection and enrollment policy stay unchanged."""
    q = observation.get('quality', {}).get('metrics', {})
    return (q.get('face_size_px', 0) >= 48 and q.get('laplacian_variance', 0) >= 40
            and q.get('yaw_proxy', 0) <= .6 and q.get('clipped_fraction', 0) <= .08)


def photo_rank(observation):
    return (photo_is_clear(observation), photo_score(observation), observation['observation_id'])


def select_best(results):
    """Return (frame index, observation) for each conservative short face track.

    Association requires spatial overlap AND normalized feature agreement, with
    no ambiguous alternatives on either side. Ambiguity retains both people;
    never choose the largest face globally or merge only by frame face-index.
    No identity is assigned here. P2 performs the unchanged gallery matching.
    """
    tracks = []
    for fi, result in enumerate(results):
        observations = result['observations']
        boxes = result.get('observation_boxes', {})
        edges = []
        for oi, obs in enumerate(observations):
            box = boxes.get(obs['observation_id'])
            if box is None:
                continue
            for ti, track in enumerate(tracks):
                last_fi, previous = track[-1]
                prev_box = results[last_fi].get('observation_boxes', {}).get(previous['observation_id'])
                if prev_box is None or last_fi >= fi or _iou(box, prev_box) < .15:
                    continue
                a, b = np.asarray(obs['embedding']), np.asarray(previous['embedding'])
                cosine = float(np.dot(a, b) / max(1e-9, np.linalg.norm(a) * np.linalg.norm(b)))
                if math.isfinite(cosine) and cosine >= .55:
                    edges.append((oi, ti))
        assigned = set()
        for oi, ti in edges:
            if sum(x == oi for x, _ in edges) == 1 and sum(y == ti for _, y in edges) == 1:
                tracks[ti].append((fi, observations[oi])); assigned.add(oi)
        tracks.extend([[(fi, o)] for oi, o in enumerate(observations) if oi not in assigned])
    winners = [max(track, key=lambda item: photo_rank(item[1])) for track in tracks]
    return [item for item in winners if photo_is_clear(item[1])]
