"""Sharpness as the recognizer sees it, close-up re-detection and the dominant enrollment face."""
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from face_engine.alignment.face_align import _TEMPLATE, alignment_scale
from face_engine.detector.scrfd import SizeAwareSCRFDDetector
from face_engine.platform_photos import DOMINANT_FACE_MAX_OTHER_RATIO, dominant_observation, prepare_people
from face_engine.quality import _MIN_CONTRAST, _REFERENCE_CONTRAST, QualityPolicy, evaluate_face_quality
from face_engine.trajectory_bridge import TrajectoryExtractor


def face_box(size, offset=0):
    """A detection whose five points sit on the ArcFace template scaled to a size x size box."""
    points = _TEMPLATE * (size / 112) + offset
    return dict(bbox=[offset, offset, offset + size, offset + size], landmarks=points.tolist(), score=.9)


def texture(size, sigma, seed=7):
    image = np.random.default_rng(seed).integers(0, 256, size=(size, size, 3), dtype=np.uint8)
    return cv2.GaussianBlur(image, (0, 0), sigma) if sigma else image


def blocks(size, block, sigma, low=0, high=255, seed=3):
    """Random light/dark blocks: contrast is set by low/high, focus by sigma."""
    cells = np.random.default_rng(seed).integers(0, 2, size=(size // block, size // block)).astype(np.float32)
    image = cv2.resize(cells * (high - low) + low, (size, size), interpolation=cv2.INTER_NEAREST)
    image = cv2.GaussianBlur(image, (0, 0), sigma) if sigma else image
    return np.repeat(np.clip(image, 0, 255).astype(np.uint8)[:, :, None], 3, axis=2)


def native_variance(image):
    return float(cv2.Laplacian(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def recognizer_view(gray, size=None):
    """Independent restatement of the re-measure: optional reduction, then exposure normalization."""
    if size is not None:
        gray = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)
    value, contrast = float(cv2.Laplacian(gray, cv2.CV_64F).var()), float(gray.std())
    return value * (_REFERENCE_CONTRAST / contrast) ** 2 if _MIN_CONTRAST <= contrast < _REFERENCE_CONTRAST else value


class RecognizerScaleSharpness(unittest.TestCase):
    def test_alignment_scale_is_the_factor_used_by_alignment(self):
        self.assertAlmostEqual(alignment_scale(_TEMPLATE * 4), .25, places=6)
        self.assertAlmostEqual(alignment_scale(_TEMPLATE), 1., places=6)

    def test_large_soft_face_is_judged_at_recognizer_scale(self):
        image = blocks(448, 16, 6)
        self.assertLess(native_variance(image), 40)
        quality = evaluate_face_quality(image, face_box(448))
        self.assertTrue(quality['accepted'], quality['reasons'])
        reduced = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (112, 112), interpolation=cv2.INTER_AREA)
        self.assertGreaterEqual(reduced.std(), _REFERENCE_CONTRAST)  # normally exposed: no contrast boost
        self.assertEqual(quality['metrics']['laplacian_variance'], float(cv2.Laplacian(reduced, cv2.CV_64F).var()))

    def test_sharp_face_keeps_original_pixel_measurement(self):
        image = texture(448, 0)
        quality = evaluate_face_quality(image, face_box(448))
        self.assertTrue(quality['accepted'])
        self.assertEqual(quality['metrics']['laplacian_variance'], native_variance(image))

    def test_small_face_is_never_enlarged_for_measurement(self):
        image = blocks(100, 8, 4)
        quality = evaluate_face_quality(image, face_box(100))
        self.assertIn('BLURRED', quality['reasons'])
        self.assertEqual(quality['metrics']['laplacian_variance'], native_variance(image))

    def test_large_face_blurred_at_recognizer_scale_is_still_rejected(self):
        quality = evaluate_face_quality(blocks(448, 16, 14), face_box(448))
        self.assertIn('BLURRED', quality['reasons'])
        self.assertTrue(quality['needs_high_resolution'])
        self.assertLess(quality['metrics']['laplacian_variance'], 40)

    def test_dim_face_is_measured_at_reference_contrast(self):
        for size, image in ((448, blocks(448, 16, 4, 100, 150)), (100, blocks(100, 8, 1, 110, 150))):
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            self.assertLess(native_variance(image), 40)
            quality = evaluate_face_quality(image, face_box(size))
            self.assertTrue(quality['accepted'], (size, quality['reasons']))
            expected = recognizer_view(gray, 112 if size > 112 else None)
            self.assertAlmostEqual(quality['metrics']['laplacian_variance'], expected, places=6)

    def test_dim_blurred_face_is_still_rejected(self):
        quality = evaluate_face_quality(blocks(448, 16, 14, 100, 150), face_box(448))
        self.assertIn('BLURRED', quality['reasons'])

    def test_flat_crop_is_not_normalized(self):
        image = blocks(448, 16, 4, 118, 135)
        reduced = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (112, 112), interpolation=cv2.INTER_AREA)
        self.assertLess(reduced.std(), _MIN_CONTRAST)
        quality = evaluate_face_quality(image, face_box(448))
        self.assertIn('BLURRED', quality['reasons'])
        self.assertEqual(quality['metrics']['laplacian_variance'], float(cv2.Laplacian(reduced, cv2.CV_64F).var()))

    def test_declared_source_pixels_cap_the_measurement_scale(self):
        # A 448px face declared to come from a 2x enlargement is measured at <=224px native
        # pixels, then at the recognizer scale; never at the enlarged decoded size.
        image = blocks(448, 16, 10)
        quality = evaluate_face_quality(image, face_box(448), metadata=dict(source_width=224, source_height=224))
        self.assertEqual(quality['metrics']['face_size_px'], 224.)
        native = cv2.cvtColor(cv2.resize(image, (224, 224), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        self.assertLess(float(cv2.Laplacian(native, cv2.CV_64F).var()), 40)
        self.assertAlmostEqual(quality['metrics']['laplacian_variance'], recognizer_view(native, 112), places=6)

    def test_policy_and_reported_fields_are_unchanged(self):
        quality = evaluate_face_quality(blocks(448, 16, 4, 100, 150), face_box(448))
        self.assertEqual(set(quality['thresholds']), {field for field in QualityPolicy.__dataclass_fields__})
        self.assertEqual(len(quality['thresholds']), 8)
        self.assertEqual(quality['policy_version'], 'p1-original-pixel-quality-v1')
        self.assertEqual(set(quality), {'status', 'accepted', 'reasons', 'metrics', 'needs_high_resolution',
                                        'policy_version', 'thresholds_calibrated', 'thresholds'})


class Session:
    def __init__(self, faces):
        self.faces, self.calls, self.backend = faces, 0, 'TEST'

    def detect_faces(self, image):
        self.calls += 1
        return list(self.faces)


class CloseUpDetection(unittest.TestCase):
    def test_close_up_pass_uses_small_input_only_for_large_images(self):
        standard, small = Session([]), Session([face_box(112)])
        detector = SizeAwareSCRFDDetector(standard, small, 576)
        self.assertEqual(detector.detect_faces(np.zeros((800, 600, 3), np.uint8)), [])
        self.assertEqual((standard.calls, small.calls), (1, 0))
        self.assertEqual(len(detector.detect_close_up_faces(np.zeros((800, 600, 3), np.uint8))), 1)
        self.assertEqual(detector.detect_close_up_faces(np.zeros((400, 300, 3), np.uint8)), [])
        self.assertEqual((standard.calls, small.calls), (1, 1))


class CloseUpDetector:
    def __init__(self):
        self.close_up_calls = 0

    def detect_faces(self, image):
        return []

    def detect_close_up_faces(self, image):
        self.close_up_calls += 1
        return [face_box(112, 40)]


class Recognizer:
    def extract_embeddings(self, images):
        result = np.zeros((len(images), 512), dtype=np.float32)
        result[:, 0] = 1
        return result


class ExtractorCloseUps(unittest.TestCase):
    META = dict(activity_id='A', activity_num='NUM-A', config_version=1, binding_version=1, monitoring_version=1,
                route_rule_id='R', source_id='S', camera_id='C', frame_id='F')

    def run_one(self, detector, **meta):
        image = texture(200, 0)
        return TrajectoryExtractor(detector, Recognizer(), {'synthetic': True}).extract_detailed([(image, dict(self.META, **meta))])[0]

    def test_photos_and_camera_captures_get_the_close_up_pass(self):
        for meta in (dict(stream_kind='hikvision'), dict(), dict(purpose='GALLERY_ENROLLMENT')):
            detector = CloseUpDetector()
            result = self.run_one(detector, **meta)
            self.assertEqual((result['detected_faces'], len(result['observations']), detector.close_up_calls), (1, 1, 1), meta)

    def test_video_frames_skip_the_close_up_pass(self):
        for kind in ('sub', 'main'):
            detector = CloseUpDetector()
            result = self.run_one(detector, stream_kind=kind)
            self.assertEqual((result['detected_faces'], detector.close_up_calls), (0, 0), kind)

    def test_detectors_without_close_up_pass_are_unchanged(self):
        class Plain:
            def detect_faces(self, image):
                return []
        self.assertEqual(self.run_one(Plain(), stream_kind='hikvision')['detected_faces'], 0)


def accepted_face(width, height, embedding_head=1.):
    return dict(embedding=[embedding_head] + [0.] * 511,
                quality=dict(accepted=True, reasons=[], metrics=dict(face_width_px=float(width), face_height_px=float(height))))


def rejected_face(width, height, reasons=('FACE_TOO_SMALL',)):
    return dict(face_index=1, bbox=[0., 0., float(width), float(height)], status='QUALITY_REJECTED',
                quality=dict(accepted=False, reasons=list(reasons), metrics=dict(face_width_px=float(width), face_height_px=float(height))))


class DetailExtractor:
    feature_space = {'recognition_model_sha256': 'SYNTHETIC'}

    def __init__(self, observations, rejected):
        self.detail = dict(detected_faces=len(observations) + len(rejected), observations=observations, rejected_faces=rejected)

    def extract_detailed(self, frames):
        return [self.detail]


class DominantEnrollmentFace(unittest.TestCase):
    PERSON = dict(personId='p', personName='n', personIdNum='i', personPhoto='https://photos.test/p.jpg')

    def enroll(self, observations, rejected):
        return prepare_people([self.PERSON], DetailExtractor(observations, rejected),
                              lambda raw: np.zeros((8, 8, 3), np.uint8), photo_reader=lambda url: b'photo')[0]

    def test_small_or_cut_off_background_faces_do_not_block_enrollment(self):
        main = accepted_face(120, 140, .5)
        result = self.enroll([main], [rejected_face(60, 70), rejected_face(30, 40, ('FACE_CLIPPED',))])
        self.assertEqual(result['status'], 'SUCCESS', result.get('message'))
        self.assertEqual(result['person']['templates'][0]['embedding'], main['embedding'])

    def test_comparable_second_face_keeps_the_photo_ambiguous(self):
        side = 120 * DOMINANT_FACE_MAX_OTHER_RATIO + 1
        result = self.enroll([accepted_face(120, 120)], [rejected_face(side, side, ('EXTREME_YAW_PROXY',))])
        self.assertEqual(result['message'], '登记照片必须恰好包含一张人脸：检测到2张人脸，请使用单人照片')

    def test_two_usable_faces_or_a_larger_rejected_face_are_refused(self):
        self.assertEqual(self.enroll([accepted_face(120, 120), accepted_face(60, 60)], [])['message'],
                         '登记照片必须恰好包含一张人脸：检测到2张人脸，请使用单人照片')
        self.assertEqual(self.enroll([accepted_face(80, 80)], [rejected_face(160, 160, ('BLURRED',))])['message'],
                         '登记照片必须恰好包含一张人脸：检测到2张人脸，请使用单人照片')

    def test_a_dominant_rejected_face_reports_its_own_quality_problem(self):
        main = rejected_face(150, 150, ('BLURRED',))
        main['quality'].update(thresholds=dict(min_laplacian_variance=40.))
        main['quality']['metrics']['laplacian_variance'] = 23.4
        self.assertEqual(self.enroll([], [main, rejected_face(50, 50)])['message'],
                         '登记照片必须恰好包含一张人脸：检测到2张人脸，其中最大的人脸质量不合格：BLURRED 清晰度不足（拉普拉斯方差23，要求≥40）')
        self.assertEqual(self.enroll([], [rejected_face(150, 150, ('BLURRED',)), rejected_face(40, 40, ('FACE_CLIPPED',))])['message'],
                         '登记照片必须恰好包含一张人脸：检测到2张人脸，其中最大的人脸质量不合格：BLURRED 清晰度不足')
        side = 150 * DOMINANT_FACE_MAX_OTHER_RATIO + 1
        self.assertEqual(self.enroll([], [main, rejected_face(side, side)])['message'],
                         '登记照片必须恰好包含一张人脸：检测到2张人脸，请使用单人照片')

    def test_single_face_rules_are_unchanged(self):
        self.assertEqual(self.enroll([accepted_face(120, 120)], [])['status'], 'SUCCESS')
        self.assertTrue(self.enroll([], [])['message'].startswith('登记照片必须恰好包含一张人脸：未检测到人脸'))
        self.assertTrue(self.enroll([], [rejected_face(50, 50)])['message'].startswith('登记照片人脸质量不合格：FACE_TOO_SMALL'))

    def test_inconsistent_or_incomplete_details_are_not_trusted(self):
        self.assertIsNone(dominant_observation(dict(detected_faces=3, observations=[accepted_face(120, 120)],
                                                    rejected_faces=[rejected_face(10, 10)])))
        self.assertIsNone(dominant_observation(dict(detected_faces=2, observations=[dict(embedding=[1.] + [0.] * 511)],
                                                    rejected_faces=[rejected_face(10, 10)])))


if __name__ == '__main__':
    unittest.main()
