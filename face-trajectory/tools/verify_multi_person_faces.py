"""Real crop/alignment + synthetic models + real worker: two people in one frame.

Downloads two distinct enrollment photos from loopback URLs. Verifies that the
matched backend ID, name, ID number, capture face and camera/time stay together.
No real identities, production endpoints or accuracy claims.
"""
import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading

import cv2
import numpy as np

P2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P2 / 'evidence/2026-10-10-activity-retention'))
sys.path.insert(0, str(P2.parent / 'face-recognition-engine/src'))
from verify_core import T0, SPACE, POLICY, clock, iso, profile, scope
from face_engine.monitoring_service import decode_image
from face_engine.platform_photos import prepare_people
from face_engine.trajectory_bridge import TrajectoryExtractor


class Detector:
    def detect_faces(self, image):
        landmarks = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                              [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)
        positions = [4] if image.shape[1] < 200 else [4, 136]
        return [dict(bbox=np.array([x, 4, x + 112, 116]), landmarks=landmarks + [x, 4], score=.99) for x in positions]


class Recognizer:
    def extract_embeddings(self, images):
        vectors = np.zeros((len(images), 512), np.float32)
        for index, image in enumerate(images):
            channel = 0 if image[:, :, 2].mean() > image[:, :, 0].mean() else 1
            vectors[index, channel] = 1
        return vectors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rng = np.random.default_rng(20261023)
    red = rng.integers(0, 80, (128, 128, 3), dtype=np.uint8); red[:, :, 2] += 170
    blue = rng.integers(0, 80, (128, 128, 3), dtype=np.uint8); blue[:, :, 0] += 170
    files = {key: cv2.imencode('.png', image)[1].tobytes() for key, image in (('/A.png', red), ('/B.png', blue))}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(files[self.path])
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    process = subprocess.Popen([str(args.worker.resolve()), '--state-file', str((args.output / 'isolated.sqlite').resolve()),
                                '--push-results', 'true'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8')
    checks = []
    def check(name, passed):
        checks.append(dict(name=name, passed=bool(passed)))
        if not passed:
            raise AssertionError(name)
    def call(body):
        process.stdin.write(json.dumps(dict(body, _trusted_clock=clock(T0 + 100000)), ensure_ascii=False) + '\n'); process.stdin.flush()
        response = json.loads(process.stdout.readline())
        if not response.get('ok'):
            raise AssertionError((body['op'], response))
        return response
    failure = None
    try:
        people = [dict(personId='PERSON-' + label, personName='模拟人员' + label, personIdNum='SYNTHETIC-ID-' + label,
                       personPhoto=f'http://127.0.0.1:{server.server_port}/{label}.png') for label in ('A', 'B')]
        extractor = TrajectoryExtractor(Detector(), Recognizer(), SPACE)
        prepared = prepare_people(people, extractor, decode_image)
        check('two_people_url_photos_individually_enrolled', len(prepared) == 2 and all(p['status'] == 'SUCCESS' for p in prepared))
        check('enrollment_preserves_backend_person_id_name_and_id_number', all(
            all(p['person'][field] == incoming[field] for field in ('personId', 'personName', 'personIdNum'))
            for p, incoming in zip(prepared, people)))
        call(dict(op='platform_v4_sync', activity_id='A', body=dict(activityName='SYNTHETIC', activityNum='NUM-A',
            activityState='0', start_unix_ms=T0, end_unix_ms=T0 + 86400000,
            cameras=[dict(deviceId=c, deviceName='后端' + c) for c in ('C1', 'C2')])))
        for person in prepared:
            call(dict(op='platform_v4_person', activity_id='A', person=person['person']))
        call(dict(op='platform_v4_monitoring', activity_id='A', body=dict(enabled=True)))
        for camera in ('C1', 'C2'):
            calibration = profile(); calibration['profile']['camera_id'] = camera; call(calibration)
        frame = np.zeros((128, 264, 3), np.uint8); frame[:, :128] = red; frame[:, 132:260] = blue
        def observe(camera, time):
            metadata = dict(scope(), route_rule_id='LOCAL-ROUTE', source_id='LOCAL-SOURCE', camera_id=camera,
                            frame_id='FRAME-AB', time_record=dict(profile_version='good', raw_time=iso(time)))
            rows = extractor.extract_detailed([(frame, metadata)])[0]['observations']
            for observation in rows:
                call(dict(scope(), op='monitoring_observe', route_rule_id='LOCAL-ROUTE', observation=observation,
                          time_record=observation['time_record'], policy=POLICY))
            return rows
        observations = observe('C1', T0 + 1000)
        check('same_frame_two_faces_have_distinct_observation_ids', len(observations) == 2 and len({o['observation_id'] for o in observations}) == 2)
        by_id = {person['personId']: person for person in people}
        emitted = []
        while True:
            envelope = call(dict(op='push_next'))['event']
            if envelope is None:
                break
            event = envelope['person_track_event']; emitted.append(event)
            person = by_id[event['person_id']]
            check('original_person_fields_' + event['person_id'], event['person_name'] == person['personName'] and event['person_id_num'] == person['personIdNum'])
            face = decode_image(base64.b64decode(event['capture_photo_url'], validate=True))
            check('capture_is_face_crop_not_full_frame_or_enrollment_' + event['person_id'], face.shape == (112, 112, 3))
            dominant = 2 if event['person_id'] == 'PERSON-A' else 0
            other = 0 if dominant == 2 else 2
            check('capture_face_matches_that_person_' + event['person_id'], face[:, :, dominant].mean() > face[:, :, other].mean() + 100)
            check('original_time_and_camera_' + event['person_id'], event['captured_at_unix_ms'] == T0 + 1000 and event['device_id'] == event['camera_id'] == 'C1')
            call(dict(op='push_ack', event_id=event['event_id']))
        check('two_distinct_priority_people_are_both_reported', {e['person_id'] for e in emitted} == {'PERSON-A', 'PERSON-B'} and len(emitted) == 2)
        check('two_people_do_not_share_the_same_capture_photo', emitted[0]['capture_photo_url'] != emitted[1]['capture_photo_url'])
        observe('C2', T0 + 2000)
        check('five_second_cross_camera_dedup_is_per_person', call(dict(op='push_next'))['event'] is None)
    except Exception as error:
        failure = repr(error)
        raise
    finally:
        process.stdin.close(); process.wait(timeout=20)
        server.shutdown(); server.server_close(); thread.join()
        report = dict(scope='Local real URL/image decode/crop/alignment and C++ worker; synthetic detector and embeddings only',
                      passed=sum(c['passed'] for c in checks), total=len(checks), checks=checks, failure=failure)
        (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(dict(passed=report['passed'], total=report['total'], failure=failure), ensure_ascii=False))


if __name__ == '__main__':
    main()
