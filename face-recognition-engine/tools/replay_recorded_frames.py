"""Replay original camera frame archives through the unchanged monitoring API.

Use a dedicated activity with original enrollment templates only. No attribution
correction, person assertion in observations, receiver-time substitution, or
camera-specific matching threshold is performed.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import time
import urllib.error
import urllib.request

import cv2
import numpy as np

from face_engine.camera_clock import CameraClock
from face_engine.config import load_config
from face_engine.monitoring_service import association_policy_for_stream
from face_engine.trajectory_bridge import TrajectoryExtractor, activity_context


def namespace(activity, kind):
    return json.dumps(['activity', activity, kind], separators=(',', ':'))


def atomic_json(path, value):
    temporary = path.with_suffix('.writing')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def frames(path):
    """Read a fixed prefix; a live archive's incomplete final record is excluded."""
    with path.open('rb') as stream:
        limit = os.fstat(stream.fileno()).st_size
        first = stream.read(8)
        if first != b'FACEBT01':
            stream.seek(0)
        while stream.tell() + 8 <= limit:
            lengths = stream.read(8)
            hs, bs = struct.unpack('>II', lengths)
            if not 0 < hs <= 262144 or not 0 < bs <= 12582912:
                raise ValueError('invalid original frame archive lengths')
            if stream.tell() + hs + bs > limit:
                break
            raw = stream.read(hs)
            header = json.loads(raw)
            body = stream.read(bs)
            if hashlib.sha256(body).hexdigest() != header['image_sha256']:
                raise ValueError('original frame hash mismatch')
            yield header, body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--monitoring-config', type=Path, required=True)
    parser.add_argument('--activity-id', required=True)
    parser.add_argument('--pack', type=Path, action='append', required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.report_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    config = json.loads(args.monitoring_config.read_text(encoding='utf-8'))
    state = Path(config['platform_bindings']['state_file'])
    with closing(sqlite3.connect(state.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        activity = json.loads(db.execute("SELECT value FROM state WHERE ns='activity' AND key=?",
                                         (args.activity_id,)).fetchone()[0])
        templates = [json.loads(row[0]) for row in db.execute('SELECT value FROM state WHERE ns=?',
                      (namespace(args.activity_id, 'template'),))]
        if not templates or any(t['template_id'].startswith('assoc-template:') for t in templates):
            raise ValueError('dedicated replay requires original enrollment templates only')
        if db.execute('SELECT 1 FROM state WHERE ns=? LIMIT 1',
                      (namespace(args.activity_id, 'monitoring_observation'),)).fetchone():
            raise ValueError('dedicated replay activity must not contain earlier observations')
    bindings = {(b['source_id'], b['device_id']): b for b in activity['bindings'] if b['kind'] == 'CAMERA'}
    context = {k: activity[k] for k in ['activity_id', 'activity_num', 'config_version',
                                      'binding_version', 'monitoring_version']}
    references = [(t['person_entity_id'], np.asarray(t['embedding'], np.float32)) for t in templates]
    authorization = os.environ[config.get('p2_authorization_env', 'FACE_TRAJECTORY_AUTHORIZATION')]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    extractor = TrajectoryExtractor.from_config(load_config(args.model_config))
    clock = CameraClock(dict(config['camera_clock'], state_file=str(state)), config['endpoint'], authorization)
    cv2.setNumThreads(1)

    def post(path, payload):
        # Retain the exact request bytes and IDs after ambiguous transport
        # failures. The server's immutable intake makes a repeated observe
        # idempotent; a new observation must never be invented for a retry.
        encoded = json.dumps(payload).encode()
        for attempt in range(3):
            request = urllib.request.Request(config['endpoint'] + path, data=encoded,
                headers={'Authorization': authorization, 'Content-Type': 'application/json'})
            try:
                with opener.open(request, timeout=25) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in (502, 503, 504) or attempt == 2:
                    raise
                error.close()
            except (urllib.error.URLError, TimeoutError, socket.timeout):
                if attempt == 2:
                    raise
            time.sleep(0.5 * (attempt + 1))

    stats = {}
    observations = []
    errors = []
    seen = {}
    processed = 0
    started = time.monotonic()
    report = dict(activity_id=args.activity_id, real_original_frames=True, synthetic=False,
                  original_enrollment_only=True, human_corrections_used=False,
                  stream_match_thresholds=config.get('stream_match_thresholds'),
                  template_count=len(templates), complete=False,
                  snapshots=[dict(path=str(p),bytes=p.stat().st_size) for p in args.pack])
    try:
        for path in args.pack:
            for header, body in frames(path):
                source = header['metadata']
                if source.get('activity_id') == args.activity_id:
                    raise ValueError('replay must use a dedicated activity, preserving source history')
                source_key = (source.get('activity_id'), source['source_id'], source['camera_id'], source['frame_id'])
                digest = header['image_sha256']
                if source_key in seen:
                    if seen[source_key] != digest:
                        raise ValueError('conflicting original frame identity')
                    continue
                seen[source_key] = digest
                binding = bindings.get((source['source_id'], source['camera_id']))
                if binding is None:
                    raise ValueError('original source/camera UUID is outside replay bindings')
                meta = dict(source, **context, route_rule_id=binding['route_rule_id'], camera_name=binding['camera_name'])
                s = stats.setdefault(meta['camera_id'], dict(camera_name=meta['camera_name'], frames=0,
                    detected_faces=0, qualified_faces=0, matched=0, max_similarity=None))
                image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError('original camera image cannot be decoded')
                detail = extractor.extract_detailed([(image, meta)])[0]
                s['frames'] += 1
                s['detected_faces'] += detail['detected_faces']
                s['qualified_faces'] += len(detail['observations'])
                record = None
                for observation in detail['observations']:
                    vector = np.asarray(observation['embedding'], np.float32)
                    top_id, score = max(((person, float(vector @ ref)) for person, ref in references), key=lambda item:item[1])
                    s['max_similarity'] = score if s['max_similarity'] is None else max(score, s['max_similarity'])
                    policy = association_policy_for_stream(config, meta.get('stream_kind'))
                    # Freeze enrollment during validation: no test frame may
                    # become a reference for another test frame.
                    policy.update(template_update_threshold=1.0, max_template_similarity=1.0)
                    entry = dict(camera_id=meta['camera_id'], camera_name=meta['camera_name'],
                        original_activity_id=source.get('activity_id'), original_frame_id=source['frame_id'],
                        original_image_sha256=digest, stream_kind=meta.get('stream_kind'),
                        observation_id=observation['observation_id'], similarity=score,
                        match_threshold=policy['match_threshold'], face_size_px=observation['face_size_px'])
                    try:
                        if record is None:
                            record = clock.record(image, source)
                        observation.update(time_record=record, time_source='DEVICE_CAPTURE',
                                           clock_domain='CAMERA:' + source['camera_id'])
                        payload = dict(observation=observation, time_record=record, policy=policy,
                                       graph=config['graph'], options=config['trajectory_options'])
                        payload.update(activity_context(meta))
                        reply = post('/v1/monitoring/observe', payload)
                        association = reply.get('association') or {}
                        entry.update(camera_time=record['raw_time'], status=reply.get('status'),
                            time_admitted=reply.get('time_admitted'), decision=association.get('decision'),
                            target_id=association.get('target_id'), reason_codes=reply.get('reason_codes'))
                        if entry['status'] == 'ACCEPTED' and entry['decision'] == 'MATCHED':
                            s['matched'] += 1
                    except Exception as error:
                        entry['error'] = type(error).__name__ + ': ' + str(error)[:250]
                        errors.append(dict(camera_id=meta['camera_id'], observation_id=entry['observation_id'], error=entry['error']))
                    observations.append(entry)
                processed += 1
                if processed % 250 == 0:
                    report.update(processed_frames=processed, elapsed_seconds=round(time.monotonic()-started, 2),
                                  camera_statistics=stats, observations=observations, errors=errors)
                    atomic_json(args.report_dir/'progress.json', report)
                    print(json.dumps(dict(processed=processed,matched=sum(x['matched'] for x in stats.values()),
                        matched_cameras=sum(x['matched']>0 for x in stats.values())),ensure_ascii=False),flush=True)
        timelines = {}
        for person in sorted({t['person_entity_id'] for t in templates}):
            query = {k:context[k] for k in ['activity_id','config_version','binding_version','monitoring_version']}
            query.update(person_id=person, from_unix_ms=0, to_unix_ms=int(time.time()*1000)+1, limit=1000)
            timeline = post('/v1/monitoring/timeline/query', query)
            rows = timeline.get('timeline', [])
            matched_ids = {o['observation_id'] for o in observations if o.get('decision')=='MATCHED' and o.get('status')=='ACCEPTED'}
            assert all(row['observation_id'] in matched_ids for row in rows)
            timelines[person] = dict(records=len(rows), distinct_cameras=len({row['camera_id'] for row in rows}),
                timeline=rows, has_more=timeline.get('has_more'))
        report.update(complete=True, timelines=timelines,
            automatic_multicamera_chain=any(t['distinct_cameras']>=2 for t in timelines.values()))
    finally:
        clock.refresh_pool.shutdown(wait=False)
        report.update(processed_frames=processed, elapsed_seconds=round(time.monotonic()-started,2),
                      camera_statistics=stats, observations=observations, errors=errors,
                      production_backend_push_verified=False)
        atomic_json(args.report_dir/'result.json', report)
    print(json.dumps(dict(complete=report['complete'],processed_frames=processed,
        automatic_multicamera_chain=report.get('automatic_multicamera_chain',False),report=str(args.report_dir/'result.json'))),flush=True)


if __name__ == '__main__':
    main()
