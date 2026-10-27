import io
import json
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from face_engine.continuous_capture import MotionAdmission
from face_engine.uuid_sources import UUIDSources, UUIDSourceUnavailable
from face_engine.monitoring_delivery import DeliveryStore, FairRLock
from face_engine.monitoring_service import MonitoringRuntime


class ContinuousCoverage(unittest.TestCase):
    def test_short_registry_outage_keeps_only_known_sources_then_expires(self):
        now=[0.0]
        resolver=UUIDSources(dict(registry_url='http://127.0.0.1:7600/api/devices',
            rtsp_base_url='rtsp://127.0.0.1:554',live_app='hiklive',ffmpeg_path='ffmpeg',
            registry_grace_seconds=60),clock=lambda:now[0])
        raw=json.dumps([dict(online=True,channels=[dict(uuid='C1',online=True)])]).encode()
        class Reply(io.BytesIO):status=200
        with patch.object(resolver.opener,'open',return_value=Reply(raw)):
            self.assertEqual(set(resolver.resolve(['C1'])),{'C1'})
        with patch.object(resolver.opener,'open',side_effect=TimeoutError()):
            now[0]=11
            self.assertEqual(set(resolver.resolve(['C1','NEW_UNKNOWN'])),{'C1'})
            self.assertEqual(resolver.status['status'],'REGISTRY_DEGRADED')
            now[0]=61
            with self.assertRaises(UUIDSourceUnavailable):resolver.resolve(['C1'])
    def test_waiting_inference_is_not_overtaken_by_another_capture_writer(self):
        mutex=FairRLock();order=[];workers=[]
        def enter(number):
            with mutex:order.append(number)
        with mutex:
            with mutex:self.assertEqual(mutex.depth,2)
            for number in [1,2]:
                thread=threading.Thread(target=enter,args=(number,));thread.start();workers.append(thread)
                deadline=time.monotonic()+2
                while len(mutex.waiting)<number and time.monotonic()<deadline:time.sleep(.001)
                self.assertEqual(len(mutex.waiting),number)
        with mutex:order.append(3)
        for worker in workers:worker.join(2)
        self.assertEqual(order,[1,2,3])
    def test_continuous_selection_never_rotates_out_an_eligible_camera(self):
        settings=dict(registry_url='http://127.0.0.1:7600/api/devices',rtsp_base_url='rtsp://127.0.0.1:554',
                      live_app='hiklive',ffmpeg_path='ffmpeg',max_readers=20,continuous=True)
        resolver=UUIDSources(settings)
        cameras=['C'+str(i) for i in range(20)]
        raw=json.dumps([dict(online=True,channels=[dict(uuid=c,online=True) for c in cameras])]).encode()
        class Reply(io.BytesIO):status=200
        with patch.object(resolver.opener,'open',side_effect=lambda *a,**k:Reply(raw)):
            self.assertEqual(set(resolver.resolve(cameras)),set(cameras))
            resolver.next_rotation=0
            self.assertEqual(set(resolver.resolve(cameras)),set(cameras))
            self.assertEqual(resolver.status['deferred'],0)
            self.assertEqual(resolver.status['preferred_stream'],'main')
            self.assertTrue(all(v['preferred_stream']=='main' for v in resolver.resolve(cameras).values()))
            resolver.limit=19
            with self.assertRaises(UUIDSourceUnavailable):resolver.resolve(cameras)

    def test_motion_keeps_a_brief_walk_and_post_motion_frames(self):
        gate=MotionAdmission();empty=np.zeros((540,960,3),np.uint8)
        self.assertTrue(gate.admit(empty,0))
        self.assertFalse(gate.admit(empty,3))
        walk=empty.copy();walk[120:420,300:420]=180
        self.assertTrue(gate.admit(walk,3.5))
        self.assertTrue(gate.admit(walk,4))
        self.assertTrue(gate.admit(empty,4.5))
        self.assertFalse(gate.admit(empty,7))
        self.assertTrue(gate.admit(empty,70))

    def test_parallel_inference_claims_durable_captures_once(self):
        seen=[];lock=threading.Lock()
        class Extractor:
            def extract_detailed(self,frames):
                with lock:seen.append(frames[0][1]['frame_id'])
                time.sleep(.04)
                return [dict(observations=[],rejected_faces=[],detected_faces=0,needs_high_resolution=False)]
        with tempfile.TemporaryDirectory() as d:
            store=DeliveryStore(Path(d)/'inbox.sqlite')
            runtime=MonitoringRuntime(dict(policy={},graph={},trajectory_options={},endpoint='http://127.0.0.1:9',
                scheduling=dict(inference_workers=4)),Extractor(),store)
            ok,body=cv2.imencode('.jpg',np.zeros((32,32,3),np.uint8));self.assertTrue(ok)
            for i in range(12):
                store.capture(body.tobytes(),dict(camera_id='C',source_id='C',frame_id=str(i),stream_kind='main',
                    activity_id='A',activity_num='A',config_version=1,binding_version=1,monitoring_version=1,route_rule_id='R'))
            runtime.start()
            try:
                end=time.monotonic()+4
                while store.status()['captures_pending'] and time.monotonic()<end:time.sleep(.02)
                self.assertEqual(store.status()['captures_pending'],0)
                self.assertEqual(sorted(seen),sorted(str(i) for i in range(12)))
            finally:runtime.stop()


if __name__=='__main__':unittest.main()
