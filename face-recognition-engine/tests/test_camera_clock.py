import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
import threading
import time
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import numpy as np
from face_engine.camera_clock import osd_datetime, CameraClock


class ClockEvidence(unittest.TestCase):
    def test_slow_host_ntp_query_does_not_hold_camera_cache(self):
        from types import SimpleNamespace
        clock=object.__new__(CameraClock)
        clock.lock=threading.RLock();clock.profiles={};clock.retry_at={};clock.refreshing={'C'}
        clock.ntp_checked_at=0;clock.ntp_synced=False;clock.endpoint='http://unused'
        entered=threading.Event();release=threading.Event();errors=[]
        def slow(*args,**kwargs):
            entered.set();release.wait(3);return SimpleNamespace(returncode=0,stdout='yes')
        clock._json=lambda _:dict(clock=dict(trusted=True,stamp=dict(verified=True,now_unix_ms=time.time_ns()//1000000)))
        def reference():
            try:clock._reference()
            except Exception as error:errors.append(error)
        with patch('face_engine.camera_clock.subprocess.run',side_effect=slow):
            thread=threading.Thread(target=reference);thread.start()
            try:
                self.assertTrue(entered.wait(1))
                start=time.monotonic();self.assertIsNone(clock.for_capture('C'))
                self.assertLess(time.monotonic()-start,.2)
            finally:release.set();thread.join(3)
        self.assertFalse(errors)

    def test_delayed_frame_recovers_only_its_camera_and_valid_historical_profile(self):
        class OCR:
            def read(self,image,camera):return datetime(2026,10,27,11,24,25),dict(text='2026-10-27 11:24:25')
        with tempfile.TemporaryDirectory() as directory:
            clock=object.__new__(CameraClock);clock.ocr=OCR();clock.evidence=Path(directory);clock.state_file=Path(directory)/'p2.sqlite'
            start=1793071465000
            with closing(sqlite3.connect(clock.state_file)) as db,db:
                db.execute('CREATE TABLE state(ns TEXT,key TEXT,value TEXT)')
                value=dict(camera_id='C',profile_version='historical',verified=True,fixed_utc_offset_minutes=480,
                    calibrations=[dict(offset_ms=0,valid_from_utc_ms=start-5000,valid_until_utc_ms=start+5000)])
                db.execute('INSERT INTO state VALUES(?,?,?)',('clock_profile','historical',json.dumps(value)))
            metadata=dict(camera_id='C',source_id='C',frame_id='F',received_at_unix_ms=start+9999999)
            self.assertEqual(clock.record(np.zeros((2,2,3),np.uint8),metadata)['profile_version'],'historical')
            metadata['camera_id']='OTHER'
            with self.assertRaises(ValueError):clock.record(np.zeros((2,2,3),np.uint8),metadata)
    def test_unreachable_clock_probe_never_blocks_camera_acquisition(self):
        clock=object.__new__(CameraClock)
        clock.lock=threading.RLock();clock.profiles={};clock.retry_at={};clock.refreshing=set()
        clock.refresh_pool=ThreadPoolExecutor(max_workers=1)
        entered=threading.Event();release=threading.Event()
        def prepare(camera,connection):entered.set();release.wait(3)
        clock.prepare=prepare
        try:
            start=time.monotonic()
            self.assertIsNone(clock.for_capture('C'))
            self.assertLess(time.monotonic()-start,.5)
            self.assertTrue(entered.wait(1))
            self.assertIsNone(clock.for_capture('C'))
            self.assertEqual(clock.refreshing,{'C'})
        finally:release.set();clock.close()

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
