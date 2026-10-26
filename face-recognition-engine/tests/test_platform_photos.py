"""V4.1 URL enrollment and actual capture bytes; synthetic identities only."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote

import cv2
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from face_engine import platform_photos
from face_engine.platform_photos import capture_base64, normalize_photo_url, prepare_people, read_photo
from face_engine.monitoring_service import decode_image
from face_engine.trajectory_bridge import verify_activity_receipt, ActivityContextError


class Extractor:
    feature_space = {'recognition_model_sha256': 'SYNTHETIC'}
    def __init__(self, count=1, accepted=True):
        self.count, self.accepted = count, accepted
    def extract_detailed(self, frames):
        return [dict(detected_faces=self.count, observations=[dict(embedding=[1.] + [0.] * 511)] if self.accepted else [])]


class QualityRejectingExtractor(Extractor):
    def __init__(self, reasons, metrics, thresholds):
        super().__init__(1, False)
        self.quality = dict(reasons=reasons, metrics=metrics, thresholds=thresholds)
    def extract_detailed(self, frames):
        return [dict(detected_faces=1, observations=[], rejected_faces=[dict(
            face_index=0, status='QUALITY_REJECTED', quality=dict(self.quality, accepted=False))])]


class PhotoServer:
    """Local file server that records the exact request target it received."""
    def __init__(self, body=b'', status=200, delay=0.0):
        self.paths = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.paths.append(self.path)
                time.sleep(delay)
                try:
                    self.send_response(status); self.end_headers(); self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client already gave up (timeout test)
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = 'http://127.0.0.1:%d' % self.server.server_port
    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        return self
    def __exit__(self, *_):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


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

    def test_chinese_url_path_and_query_are_utf8_percent_encoded(self):
        with PhotoServer(self.raw) as server:
            person = dict(self.person, personPhoto=server.base + '/人员照片/张三_正面.png?name=李四&v=1')
            result = prepare_people([person], Extractor(), decode_image)[0]
            self.assertEqual(result['status'], 'SUCCESS', result.get('message'))
            self.assertEqual(server.paths, ['/%E4%BA%BA%E5%91%98%E7%85%A7%E7%89%87/%E5%BC%A0%E4%B8%89_%E6%AD%A3%E9%9D%A2.png'
                                            '?name=%E6%9D%8E%E5%9B%9B&v=1'])
            self.assertEqual(unquote(server.paths[0]), '/人员照片/张三_正面.png?name=李四&v=1')
            # Already-encoded URLs are sent unchanged, never double-encoded.
            encoded = dict(self.person, personPhoto=server.base + server.paths[0])
            self.assertEqual(prepare_people([encoded], Extractor(), decode_image)[0]['status'], 'SUCCESS')
            self.assertEqual(server.paths[1], server.paths[0])

    def test_url_normalization_preserves_escapes_and_still_validates(self):
        self.assertEqual(normalize_photo_url('http://h/a%20b/%E5%BC%A0.jpg?x=%2F'), 'http://h/a%20b/%E5%BC%A0.jpg?x=%2F')
        self.assertEqual(normalize_photo_url('http://h/50%.jpg'), 'http://h/50%25.jpg')
        self.assertEqual(normalize_photo_url('https://照片.例子.测试/张.jpg'), 'https://xn--2uxqs.xn--fsqu00a.xn--0zwm56d/%E5%BC%A0.jpg')
        self.assertEqual(normalize_photo_url('http://[::1]:8080/张.jpg'), 'http://[::1]:8080/%E5%BC%A0.jpg')
        self.assertEqual(normalize_photo_url('HTTPS://files.example.test:8443/a.png'), 'https://files.example.test:8443/a.png')
        # Acceptance rules are the original ones: only the request encoding changed.
        self.assertEqual(normalize_photo_url('http://@h/a'), 'http://@h/a')
        for url in ('http://user@h/a', 'http://:pw@h/a', 'http://h/a#frag', 'ftp://h/a', 'http://h/张 三.jpg',
                    'http://h/a\tb', 'http:///a', 'x' * 4097, None):
            with self.subTest(url=str(url)[:40]), self.assertRaisesRegex(ValueError, '^personPhoto必须是完整HTTP\\(S\\) URL$'):
                normalize_photo_url(url)

    def test_download_failures_name_the_cause_without_leaking_url(self):
        secret = '/张三.png?token=SECRET'
        def message(url):
            result = prepare_people([dict(self.person, personPhoto=url)], Extractor(), decode_image)[0]
            self.assertEqual(result['status'], 'FAILED')
            self.assertNotIn('SECRET', json.dumps(result, ensure_ascii=False))
            return result['message']
        # The original message stays as the prefix; only the cause is appended.
        with PhotoServer(b'missing', status=404) as server:
            self.assertEqual(message(server.base + secret), '登记照片无法读取或处理：下载失败，服务器返回HTTP 404')
        with PhotoServer(b'') as server:
            self.assertEqual(message(server.base + secret), '登记照片为空或超过5MiB')
        with PhotoServer(self.raw, delay=1.0) as server, patch.object(platform_photos, 'DOWNLOAD_TIMEOUT_SECONDS', 0.2):
            self.assertEqual(message(server.base + secret), '登记照片无法读取或处理：下载失败，连接或读取超时')
        closed = socket.socket(); closed.bind(('127.0.0.1', 0)); port = closed.getsockname()[1]; closed.close()
        self.assertEqual(message('http://127.0.0.1:%d%s' % (port, secret)), '登记照片无法读取或处理：下载失败，连接被拒绝')

    def test_face_count_decode_and_quality_messages_explain_the_reason(self):
        def message(extractor, raw=None):
            return prepare_people([self.person], extractor, decode_image, lambda _: raw or self.raw)[0]['message']
        self.assertTrue(message(Extractor(0)).startswith('登记照片必须恰好包含一张人脸：未检测到人脸，'))
        self.assertEqual(message(Extractor(2)), '登记照片必须恰好包含一张人脸：检测到2张人脸，请使用单人照片')
        self.assertTrue(message(Extractor(), b'broken').startswith('登记照片无法读取或解码：'))
        self.assertEqual(message(Extractor(1, False)), '登记照片人脸质量不合格')
        thresholds = dict(min_face_size_px=64.0, min_inter_eye_distance_px=20.0, min_detection_score=0.6,
                          min_laplacian_variance=40.0, max_clipped_fraction=0.08, max_yaw_proxy=0.6,
                          min_pitch_proxy=0.1, max_pitch_proxy=0.9)
        small = QualityRejectingExtractor(['FACE_TOO_SMALL', 'INTER_EYE_DISTANCE_TOO_SMALL'],
                                          dict(face_size_px=46.64, inter_eye_distance_px=19.6), thresholds)
        self.assertEqual(message(small), '登记照片人脸质量不合格：FACE_TOO_SMALL 人脸过小（47px，要求≥64px）；'
                                         'INTER_EYE_DISTANCE_TOO_SMALL 双眼间距过小（20px，要求≥20px）')
        blurred = QualityRejectingExtractor(['BLURRED', 'EXTREME_PITCH_PROXY', 'ALIGNMENT_FAILED', 'FACE_OUTSIDE_IMAGE'],
                                            dict(laplacian_variance=15.75, pitch_proxy=0.95), thresholds)
        self.assertEqual(message(blurred), '登记照片人脸质量不合格：BLURRED 清晰度不足（拉普拉斯方差16，要求≥40）；'
                                           'EXTREME_PITCH_PROXY 抬头或低头角度过大（0.95，要求0.10～0.90）；'
                                           'ALIGNMENT_FAILED 人脸关键点对齐失败；FACE_OUTSIDE_IMAGE 人脸框或关键点无效')
        hostile = QualityRejectingExtractor(['https://x/?token=SECRET', 'FACE_CLIPPED'], dict(clipped_fraction=0.125), thresholds)
        self.assertEqual(message(hostile), '登记照片人脸质量不合格：FACE_CLIPPED 人脸被图片边缘截断（12.5%，上限8.0%）')

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
