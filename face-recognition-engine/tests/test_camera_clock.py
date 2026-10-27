import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

import numpy as np
from face_engine.camera_clock import osd_datetime, CameraClock


class ClockEvidence(unittest.TestCase):
    def test_date_time_requires_real_complete_unambiguous_fields(self):
        self.assertEqual(osd_datetime('2026年10月27日 星期二 11：24：25'), datetime(2026,10,27,11,24,25))
        for text in ['11:24:25', '2026-02-30 11:24:25', '2026-10-27 11:24:25 12:00:00',
                     '2026-10-27 11:2479', '2026-10-27 25:00:00']:
            with self.subTest(text=text), self.assertRaises(ValueError): osd_datetime(text)

    def test_same_image_time_survives_delayed_processing_and_receiver_clock(self):
        class OCR:
            def read(self, image, camera):
                return datetime(2026,10,27,11,24,25), dict(text='2026-10-27 11:24:25',confidence=.99,regions=[])
        with tempfile.TemporaryDirectory() as d:
            clock=object.__new__(CameraClock);clock.ocr=OCR();clock.evidence=Path(d)
            start=1793071465000
            metadata=dict(camera_id='C',source_id='C',frame_id='frame-1',received_at_unix_ms=start+50000,
                          camera_clock_profile=dict(profile_version='measured-profile',utc_offset_minutes=480,
                          valid_from_utc_ms=start-5000,valid_until_utc_ms=start+300000))
            record=clock.record(np.zeros((10,10,3),np.uint8),metadata)
            self.assertEqual(record['raw_time'],'2026-10-27T11:24:25+08:00')
            self.assertEqual(record['profile_version'],'measured-profile')
            self.assertEqual(record['source_kind'],'CAMERA_OSD_SAME_IMAGE_OCR')
            self.assertEqual(len(list(Path(d).glob('*.json'))),1)
            del metadata['camera_clock_profile']
            with self.assertRaises(ValueError):clock.record(np.zeros((10,10,3),np.uint8),metadata)


if __name__=='__main__':unittest.main()
