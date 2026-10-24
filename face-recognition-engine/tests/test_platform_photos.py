"""V4.1 URL enrollment and actual capture bytes; synthetic identities only."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest

import cv2
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from face_engine.platform_photos import capture_base64, prepare_people, read_photo
from face_engine.monitoring_service import decode_image
from face_engine.trajectory_bridge import verify_activity_receipt, ActivityContextError


class Extractor:
    feature_space = {'recognition_model_sha256': 'SYNTHETIC'}
    def __init__(self, count=1, accepted=True):
        self.count, self.accepted = count, accepted
    def extract_detailed(self, frames):
        return [dict(detected_faces=self.count, observations=[dict(embedding=[1.] + [0.] * 511)] if self.accepted else [])]


class PlatformPhotosTest(unittest.TestCase):
    def setUp(self):
        self.raw = cv2.imencode('.png', np.full((128, 128, 3), 127, np.uint8))[1].tobytes()
        self.person = dict(personId='P', personName='模拟人员', personIdNum='SYNTHETIC-ID',
                           personPhoto='https://photos.example.test/profile.png', seat=None)

    def test_url_download_and_real_image_decode(self):
        raw = self.raw
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(raw)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            person = dict(self.person, personPhoto=f'http://127.0.0.1:{server.server_port}/photo.png')
            result = prepare_people([person], Extractor(), decode_image)[0]
            self.assertEqual(result['status'], 'SUCCESS')
            self.assertEqual(result['person']['personIdNum'], 'SYNTHETIC-ID')
            self.assertEqual(len(result['person']['templates'][0]['embedding']), 512)
            retry = prepare_people([person], Extractor(), decode_image)[0]
            self.assertEqual(retry, result)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_each_person_failure_is_separate(self):
        people = [self.person, dict(self.person, personId='MISSING', personIdNum=None)]
        results = prepare_people(people, Extractor(), decode_image, lambda _: self.raw)
        self.assertEqual([r['status'] for r in results], ['SUCCESS', 'FAILED'])
        self.assertEqual(results[1]['personId'], 'MISSING')
        self.assertNotIn('templates', results[1])

    def test_no_face_multiple_faces_low_quality_and_corruption(self):
        for extractor in (Extractor(0), Extractor(2), Extractor(1, False)):
            with self.subTest(count=extractor.count, accepted=extractor.accepted):
                self.assertEqual(prepare_people([self.person], extractor, decode_image, lambda _: self.raw)[0]['status'], 'FAILED')
        self.assertEqual(prepare_people([self.person], Extractor(), decode_image, lambda _: b'broken')[0]['status'], 'FAILED')

    def test_urls_and_errors_do_not_leak_credentials(self):
        for url in ('file:///private', 'http://user:password@host/photo', 'https://host/with space', None):
            with self.assertRaises(ValueError):
                read_photo(url)
        def unavailable(_):
            raise RuntimeError('https://host/private?token=SECRET')
        result = prepare_people([self.person], Extractor(), decode_image, unavailable)[0]
        self.assertNotIn('SECRET', json.dumps(result))

    def test_capture_is_decodable_base64_of_corresponding_face(self):
        image = np.zeros((240, 320, 3), np.uint8); image[:, :, 2] = 255
        encoded = capture_base64(image, [20, 30, 100, 130])
        self.assertFalse(encoded.startswith('data:'))
        raw = base64.b64decode(encoded, validate=True)
        frame = decode_image(raw)
        self.assertEqual(frame.shape, (100, 80, 3))
        self.assertGreater(float(frame[:, :, 2].mean()), 240)
        self.assertLessEqual(len(raw), 5 * 1024 * 1024)

    def test_two_faces_in_same_frame_have_their_own_photos(self):
        image = np.zeros((240, 320, 3), np.uint8)
        image[20:120, 10:90, 2] = 255
        image[30:150, 180:280, 0] = 255
        first = decode_image(base64.b64decode(capture_base64(image, [10, 20, 90, 120])))
        second = decode_image(base64.b64decode(capture_base64(image, [180, 30, 280, 150])))
        self.assertEqual(first.shape, (100, 80, 3))
        self.assertEqual(second.shape, (120, 100, 3))
        self.assertGreater(float(first[:, :, 2].mean()), 240)
        self.assertLess(float(first[:, :, 0].mean()), 10)
        self.assertGreater(float(second[:, :, 0].mean()), 240)
        self.assertLess(float(second[:, :, 2].mean()), 10)

    def test_invalid_face_box_never_falls_back_to_full_frame(self):
        image = np.zeros((120, 120, 3), np.uint8)
        for box in (None, [], [0, 0, 0, 1], [20, 0, 10, 10], [200, 200, 210, 210], [0, 0, float('nan'), 5]):
            with self.subTest(box=box), self.assertRaises(ValueError):
                capture_base64(image, box)

    def test_v41_ack_uses_authoritative_activity_not_stale_cas(self):
        original = dict(activity_id='A', activity_num='old', config_version=1,
                        binding_version=1, monitoring_version=1, route_rule_id='old-route')
        verify_activity_receipt(original, dict(platform_protocol=4, activity_id='A', config_version=2))
        with self.assertRaises(ActivityContextError):
            verify_activity_receipt(original, dict(platform_protocol=4, activity_id='B'))


if __name__ == '__main__':
    unittest.main()
