import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from face_engine.acquisition import Frame
from face_engine.continuous_capture import ContinuousCamera
from face_engine.main_photo import pack_burst, unpack_burst, select_best
from face_engine.monitoring_delivery import DeliveryStore
from face_engine.monitoring_service import MonitoringRuntime


def metadata(number, camera='C'):
    return dict(source_id=camera, camera_id=camera, frame_id=str(number), stream_kind='main',
        received_at_unix_ms=100000 + number * 400, time_source='UNKNOWN', clock_domain='UNKNOWN',
        clock_uncertainty_ms=-1, activity_id='A', activity_num='A', config_version=1,
        binding_version=1, monitoring_version=1, route_rule_id='R')


def observation(number, identity=0, sharp=100):
    embedding = [0.] * 512; embedding[identity] = 1.
    return dict(observation_id='o' + str(number), embedding=embedding,
        detection_score=.9, face_size_px=50,
        quality=dict(metrics=dict(face_size_px=50, laplacian_variance=sharp, yaw_proxy=0)))


def result(observations, boxes):
    return dict(observations=observations, observation_boxes={o['observation_id']: b for o, b in zip(observations, boxes)},
                rejected_faces=[], detected_faces=len(observations), needs_high_resolution=False)


class MainPhotoTests(unittest.TestCase):
    def test_burst_retains_original_bytes_frame_times_and_committed_route(self):
        inputs = [(b'original-' + str(i).encode(), metadata(i)) for i in range(3)]
        body, meta = pack_burst(inputs)
        meta.update(activity_id='COMMITTED', camera_name='Bound UUID')
        records = unpack_burst(body, meta)
        self.assertEqual([b for b, _ in records], [b for b, _ in inputs])
        self.assertEqual([m['frame_id'] for _, m in records], ['0', '1', '2'])
        self.assertEqual([m['received_at_unix_ms'] for _, m in records], [100000, 100400, 100800])
        self.assertTrue(all(m['activity_id'] == 'COMMITTED' for _, m in records))
        with self.assertRaises(ValueError):
            unpack_burst(body[:-1] + b'x', meta)
        with self.assertRaises(ValueError):
            unpack_burst(body, dict(meta, camera_id='OTHER'))
        with self.assertRaises(ValueError):
            pack_burst([inputs[0], (b'x', metadata(20))])
        with self.assertRaises(ValueError):
            pack_burst([inputs[0], inputs[0]])

    def test_best_sharp_frame_per_person_not_largest_face_or_face_index(self):
        a0, b0 = observation('a0', 0, 20), observation('b0', 1, 250)
        a1, b1 = observation('a1', 0, 300), observation('b1', 1, 30)
        # Detector indices change; two separate people retain their own photo.
        results = [result([a0, b0], [[0, 0, 50, 70], [100, 0, 150, 70]]),
                   result([b1, a1], [[102, 0, 152, 70], [2, 0, 52, 70]])]
        selected = select_best(results)
        self.assertEqual({o['observation_id'] for _, o in selected}, {'oa1', 'ob0'})

    def test_disjoint_and_ambiguous_faces_are_not_suppressed(self):
        a, b, c = observation('a'), observation('b'), observation('c')
        self.assertEqual(len(select_best([result([a], [[0, 0, 50, 70]]),
            result([b], [[200, 0, 250, 70]])])), 2)
        self.assertEqual(len(select_best([result([a, b], [[0, 0, 50, 70], [5, 0, 55, 70]]),
            result([c], [[2, 0, 52, 70]])])), 3)
        different = observation('other', 1)
        self.assertEqual(len(select_best([result([a], [[0, 0, 50, 70]]),
            result([different], [[0, 0, 50, 70]])])), 2)

    def test_clear_original_preferred_but_tiny_only_passage_not_discarded(self):
        tiny, clear = observation('tiny', sharp=400), observation('clear', sharp=45)
        tiny['quality']['metrics']['face_size_px'] = 24
        clear['quality']['metrics']['face_size_px'] = 60
        selected = select_best([result([tiny], [[0, 0, 50, 70]]),
                                result([clear], [[2, 0, 52, 70]])])
        self.assertEqual(selected[0][1]['observation_id'], 'oclear')
        self.assertEqual(len(select_best([result([tiny], [[0, 0, 50, 70]])])), 1)

    def test_keyframe_main_motion_requests_full_decode_burst(self):
        camera = ContinuousCamera('', 'rtsp://main', 'C', 'C', on_capture=lambda *args: None)
        with patch.object(camera, '_start_hd') as request:
            camera._accept(b'main', np.zeros((80, 100, 3), np.uint8),
                           dict(metadata(0), decode_keyframes_only=True), 0)
            request.assert_called_once()

    def test_processing_atomically_uses_photo_embedding_and_time_from_winner(self):
        raw = []
        for i in range(3):
            ok, encoded = cv2.imencode('.jpg', np.full((80, 100, 3), i * 50, np.uint8))
            self.assertTrue(ok); raw.append((encoded.tobytes(), metadata(i)))
        results = []
        for i, sharp in enumerate([20, 300, 50]):
            obs = dict(observation(i, sharp=sharp), **metadata(i),
                       capture_photo_base64=base64.b64encode(raw[i][0]).decode())
            results.append(result([obs], [[10, 10, 60, 70]]))
        class Extractor:
            def extract_detailed(self, frames):
                return copy.deepcopy(results)
        class Clock:
            def record(self, image, meta):
                return dict(source_kind='SERVER_FRAME_RECEIVED', source_timestamp=meta['received_at_unix_ms'])
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(Path(directory)/'p1.sqlite')
            runtime = MonitoringRuntime(dict(policy={}, graph={}, trajectory_options={}, endpoint='http://127.0.0.1:9'), Extractor(), store)
            runtime.capture_clock = Clock()
            try:
                body, meta = pack_burst(raw); key = store.capture(body, meta)
                report = runtime.process_burst(body, meta, capture_key=key)
                self.assertEqual(report['selected_faces'], 1)
                self.assertEqual(report['selected_original_frame_ids'], ['1'])
                payload = json.loads(store.db.execute('SELECT payload FROM deliveries').fetchone()[0])
                obs = payload['observation']
                self.assertEqual(obs['frame_id'], '1')
                self.assertEqual(base64.b64decode(obs['capture_photo_base64']), raw[1][0])
                self.assertEqual(obs['time_record']['source_timestamp'], 100400)
                self.assertEqual(store.status()['captures_pending'], 0)
                self.assertNotIn('capture_photo_base64', json.dumps(report))
                self.assertNotIn('embedding', json.dumps(report))
            finally:
                runtime.main_pool.shutdown(); store.close()

    def test_sub_capture_continues_while_main_connection_is_waiting(self):
        entered, release = threading.Event(), threading.Event()
        originals = []
        class Reader:
            def __init__(self, *args, **kwargs):
                self.n = 0
            def start(self):
                return self
            def wait_latest(self, *args, **kwargs):
                entered.set(); release.wait(3)
                self.n += 1
                meta = metadata(self.n)
                return Frame(np.zeros((60, 80, 3), np.uint8), meta, time.monotonic(), b'JPEG' + str(self.n).encode())
            def stop(self):
                release.set()
        camera = ContinuousCamera('rtsp://sub', 'rtsp://main', 'C', 'C',
            on_capture=lambda b, m, t: originals.append((b, m)))
        with patch('face_engine.uuid_sources.PrivateRTSPReader', Reader):
            try:
                start = time.monotonic()
                camera._accept(b'sub1', np.zeros((60, 80, 3), np.uint8), dict(metadata(0), stream_kind='sub'), start)
                self.assertTrue(entered.wait(1))
                self.assertEqual(originals[0][0], b'sub1')
                self.assertLess(time.monotonic() - start, 1)
                release.set(); camera.hd_thread.join(3)
                self.assertFalse(camera.hd_thread.is_alive())
                self.assertEqual(camera.metrics()['hd_frames'], 3)
                body, meta = originals[1]
                self.assertEqual(len(unpack_burst(body, meta)), 3)
                self.assertEqual(meta['stream_kind'], 'main')
            finally:
                release.set(); camera.stop()

    def test_main_capture_groups_three_originals_and_flushes_short_tail(self):
        output = []
        camera = ContinuousCamera('', 'rtsp://main', 'C', 'C',
            on_capture=lambda b, m, t: output.append((b, m)))
        image = np.zeros((80, 100, 3), np.uint8)
        for i in range(3):
            camera._accept(b'original' + str(i).encode(), image + i * 30, metadata(i), i * .4)
        self.assertEqual(len(output), 1)
        self.assertEqual(len(unpack_burst(*output[0])), 3)
        camera._accept(b'tail', image + 90, metadata(3), 1.2)
        camera._flush_photo(3)
        self.assertEqual(output[1][0], b'tail')


if __name__ == '__main__':
    unittest.main()
