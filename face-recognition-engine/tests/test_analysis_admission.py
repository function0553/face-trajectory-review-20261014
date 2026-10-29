import threading
import unittest

import numpy as np

from face_engine.analysis_admission import AnalysisBudget, MotionAdmission
from face_engine.continuous_capture import ContinuousCamera


class AdmissionEvidence(unittest.TestCase):
    def test_slow_movement_accumulates_without_cross_camera_state(self):
        first = MotionAdmission(idle_seconds=15)
        other = MotionAdmission(idle_seconds=15)
        image = np.zeros((54, 96, 3), np.uint8)
        self.assertTrue(first.admit(image, 0))
        self.assertTrue(other.admit(image, 0))
        image[20, 30:38] = 200  # Eight changed pixels, below the single-frame gate.
        self.assertFalse(first.admit(image, 3))
        image[21, 30:38] = 200
        self.assertTrue(first.admit(image, 3.5))
        self.assertEqual(first.stats()['cumulative_motion'], 1)
        self.assertFalse(other.admit(np.zeros_like(image), 3.5))
        self.assertTrue(other.admit(np.zeros_like(image), 15))
        self.assertEqual(other.stats()['idle_deadline'], 1)

    def test_failed_persistence_retries_an_unchanged_scene_immediately(self):
        attempts = []
        def capture(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise RuntimeError('inbox temporarily full')
        camera = ContinuousCamera('sub', 'main', 'S', 'C', on_capture=capture,main_photo_enabled=False)
        image = np.zeros((54, 96, 3), np.uint8)
        camera._accept(b'original', image, {'stream_kind': 'sub'}, 0)
        camera._accept(b'original', image, {'stream_kind': 'sub'}, .5)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(camera.metrics()['persisted_candidates'], 1)
        self.assertEqual(attempts[0][0],attempts[1][0])
        self.assertEqual(camera.motion.stats().get('persistence_retry_resets',0),0)

    def test_stream_switch_checks_first_frame_of_new_stream(self):
        captures = []
        camera = ContinuousCamera('sub', 'main', 'S', 'C', on_capture=lambda *args: captures.append(args),main_photo_enabled=False)
        image = np.zeros((54, 96, 3), np.uint8)
        camera._accept(b'sub-original', image, {'stream_kind': 'sub'}, 0)
        camera._accept(b'sub-next', image, {'stream_kind': 'sub'}, 3)
        camera._accept(b'main-original', image, {'stream_kind': 'main'}, 3.5)
        self.assertEqual([x[0] for x in captures], [b'sub-original', b'main-original'])

    def test_shared_budget_does_not_multiply_capacity_by_worker_count(self):
        now = [0.0]
        budget = AnalysisBudget(5, 2, clock=lambda: now[0])
        values = []
        lock = threading.Lock()
        def take():
            answer = budget.delay()
            with lock: values.append(answer)
        threads = [threading.Thread(target=take) for _ in range(12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual(values.count(0), 2)
        now[0] = .2
        self.assertEqual(budget.delay(), 0)
        self.assertGreater(budget.delay(), 0)
        stop = threading.Event()
        stop.set()
        self.assertFalse(budget.wait(stop))

    def test_invalid_configuration_and_unrestricted_default(self):
        self.assertEqual(AnalysisBudget().delay(), 0)
        for rate in (-1, float('inf'), True):
            with self.assertRaises(ValueError): AnalysisBudget(rate)
        with self.assertRaises(ValueError): MotionAdmission(idle_seconds=0)
        with self.assertRaises(ValueError): MotionAdmission(thumbnail_size=(160, 0))


if __name__ == '__main__':
    unittest.main()
