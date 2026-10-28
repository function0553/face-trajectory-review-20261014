"""Durable best-shot gates; fixtures are synthetic, not field acceptance."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import threading

import cv2
import numpy as np

from face_engine.photo_window import recognition_candidates, select_window
from face_engine.monitoring_delivery import DeliveryStore
from face_engine.monitoring_service import MonitoringRuntime
from face_engine.trajectory_bridge import encoded


def meta(i, camera='C', activity='A', stream='main', stamp=None):
    return dict(activity_id=activity, activity_num=activity, source_id=camera, camera_id=camera,
        config_version=1, binding_version=1, monitoring_version=1, route_rule_id='R',
        frame_id=str(i), stream_kind=stream, photo_window_managed=True,
        received_at_unix_ms=stamp if stamp is not None else 100000 + i * 400)


def face(name, identity=0, size=50, sharp=100):
    v=[0.]*512;v[identity]=1.
    return dict(observation_id=name, embedding=v, face_size_px=size, detection_score=.99,
        quality=dict(metrics=dict(face_size_px=size, laplacian_variance=sharp, yaw_proxy=0)))


def frame(observations, stamp=100000, stream='main', scope='A:C', box=None, dimensions=None):
    return dict(observations=observations, rejected_faces=[], detected_faces=len(observations),
        needs_high_resolution=False, frame_info=dict(scope=scope, received_at_unix_ms=stamp,
            stream_kind=stream, dimensions=dimensions or [100,100]),
        observation_boxes={o['observation_id']: b for o,b in zip(observations, box or [[10,10,60,80]]*len(observations))})


class PhotoWindowTests(unittest.TestCase):
    def test_main_disagreement_vetoes_low_identity_even_without_feature_agreement(self):
        low0,low1,high=face('l0',0,24),face('l1',0,24),face('h',1,80)
        results=[frame([low0],stream='sub'), frame([low1],stamp=100800,stream='sub'),
                 frame([high],stamp=101000)]
        selected=select_window(results)
        self.assertEqual([o['observation_id'] for _,o in selected],['h'])
        self.assertTrue(results[0]['low_resolution_identity_suppressed'])

    def test_main_requirement_propagates_along_low_track_before_pair_time(self):
        results=[frame([face('l'+str(i),0,24)],stamp=100000+i*1000,stream='sub') for i in range(5)]
        results.append(frame([face('main',1,80)],stamp=104500))
        self.assertEqual([o['observation_id'] for _,o in select_window(results)],['main'])

    def test_main_is_not_used_to_veto_another_person_camera_or_stale_frame(self):
        low=face('low',0,24);high=face('main',1,80)
        for high_frame in [frame([high],scope='OTHER'),frame([high],stamp=110000),
                           frame([high],box=[[70,10,95,80]])]:
            self.assertEqual(len(select_window([frame([low],stream='sub'),high_frame])),2)
        # No usable main face: keep the actual low-res fallback for normal P2 checks.
        self.assertEqual(len(select_window([frame([low],stream='sub'),frame([],stamp=100200)])),1)

    def test_normalized_main_sub_boxes_and_quality_first_delivery_order(self):
        low=face('low',0,24);high=face('high',0,80);other=face('other',1,35)
        results=[frame([low,other],stream='sub',box=[[10,10,30,40],[70,10,90,40]]),
                 frame([high],stamp=100500,box=[[20,20,60,80]],dimensions=[200,200])]
        self.assertEqual([o['observation_id'] for _,o in select_window(results)],['high','other'])

    def test_pre_embedding_skip_is_bounded_scoped_and_forces_refresh(self):
        targets=[face(str(i)) for i in range(7)]
        results=[frame([o],stamp=100000+i*400) for i,o in enumerate(targets)]
        crops=[np.full((112,112,3),80,np.uint8) for _ in targets]
        keep=recognition_candidates(results,crops,targets)
        self.assertEqual(len(keep),2) # refresh after 2 seconds, not permanent identity reuse
        self.assertEqual(sum(r['repeated_embeddings_avoided'] for r in results),5)
        targets=[face('one'),face('changed'),face('sub'),face('another-camera')]
        results=[frame([targets[0]]),frame([targets[1]],stamp=100400),
                 frame([targets[2]],stamp=100800,stream='sub'),frame([targets[3]],stamp=101000,scope='B:C')]
        crops=[np.full((112,112,3),x,np.uint8) for x in [50,150,150,150]]
        self.assertEqual(len(recognition_candidates(results,crops,targets)),4)

    def test_ambiguous_crossing_does_not_reuse_face_identity(self):
        targets=[face(str(i)) for i in range(4)]
        results=[frame(targets[:2],box=[[10,10,60,80],[12,10,62,80]]),
                 frame(targets[2:],stamp=100400,box=[[11,10,61,80],[13,10,63,80]])]
        self.assertEqual(len(recognition_candidates(results,[np.full((112,112,3),80,np.uint8)]*4,targets)),4)

    def test_durable_wait_survives_restart_and_ignores_newest_seed_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'p1.sqlite';store=DeliveryStore(path)
            start=int(time.time()*1000)
            records=[]
            for i in range(3):
                m=meta(i,stamp=start+i*500);key=store.capture(b'original'+str(i).encode(),m);records.append((key,b'original'+str(i).encode(),m))
            rows,delay=store.photo_window(records[-1],now=start/1000+1)
            self.assertFalse(rows);self.assertGreater(delay,0);store.close()
            store=DeliveryStore(path)
            try:
                rows,delay=store.photo_window(records[-1],now=start/1000+7)
                self.assertEqual([r[0] for r in rows],[r[0] for r in records])
                self.assertEqual(store.status()['captures_pending'],3)
                store.finish_photo_window([r[0] for r in rows],dict(selected_faces=0),[])
                self.assertEqual(store.status()['captures_pending'],0)
            finally:store.close()

    def test_group_commit_failure_rolls_back_every_original(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DeliveryStore(Path(directory)/'p1.sqlite')
            try:
                keys=[store.capture(b'original',meta(i)) for i in range(2)]
                with patch.object(store,'_capacity',side_effect=RuntimeError('CAPACITY')):
                    with self.assertRaises(RuntimeError):store.finish_photo_window(keys,{},[])
                self.assertEqual(store.status()['captures_pending'],2)
                self.assertEqual(store.next_capture(exclude_scopes=[('A','C')]),None)
            finally:store.close()

    def test_real_extraction_path_computes_only_winning_original_features(self):
        from face_engine.trajectory_bridge import TrajectoryExtractor
        class Detector:
            def detect_faces(self,image):
                points=np.array([[38.2946,51.6963],[73.5318,51.5014],[56.0252,71.7366],
                                 [41.5493,92.3655],[70.7299,92.2041]],np.float32)+[4,4]
                return [dict(bbox=np.array([4,4,116,116]),landmarks=points,score=.99)]
        class Recognizer:
            calls=[]
            def extract_embeddings(self,images):
                self.calls.append(len(images));v=np.zeros((len(images),512),np.float32);v[:,0]=1;return v
        recognizer=Recognizer();extractor=TrajectoryExtractor(Detector(),recognizer,{'synthetic':True})
        image=np.random.default_rng(17).integers(0,256,(120,120,3),np.uint8)
        results=extractor.extract_selected((image,meta(i)) for i in range(3))
        self.assertEqual(recognizer.calls,[1])
        self.assertEqual(sum(r['repeated_embeddings_avoided'] for r in results),2)
        self.assertEqual(sum(len(r['observations']) for r in results),1)

    def test_concurrent_workers_reserve_one_camera_window_once(self):
        entered,release=threading.Event(),threading.Event();calls=[]
        class Extractor:
            def extract_detailed(self,frames):
                frames=list(frames);calls.append(len(frames));entered.set();release.wait(3)
                return [frame([]) for _ in frames]
        raw=cv2.imencode('.jpg',np.zeros((100,100,3),np.uint8))[1].tobytes()
        with tempfile.TemporaryDirectory() as directory:
            store=DeliveryStore(Path(directory)/'p1.sqlite')
            runtime=MonitoringRuntime(dict(policy={},graph={},trajectory_options={},endpoint='http://127.0.0.1:9'),Extractor(),store)
            try:
                for i in range(8):store.capture(raw,meta(i))
                with ThreadPoolExecutor(max_workers=4) as pool:
                    first=pool.submit(runtime._infer_cycle,True)
                    self.assertTrue(entered.wait(2))
                    others=[pool.submit(runtime._infer_cycle,True) for _ in range(3)]
                    for task in others:task.result(timeout=2)
                    release.set();first.result(timeout=3)
                self.assertEqual(calls,[8]);self.assertEqual(store.status()['captures_pending'],0)
                self.assertFalse(runtime.photo_scope_reservations)
                self.assertFalse(runtime.capture_inflight)
            finally:release.set();runtime.main_pool.shutdown();store.close()

    def test_accepted_main_and_weak_main_never_borrow_low_person_identity(self):
        raw=cv2.imencode('.jpg',np.zeros((100,100,3),np.uint8))[1].tobytes()
        low,high=face('low',0,24),face('high',1,80)
        records=[(raw,meta(0,stream='sub')),(raw,meta(1,stream='main'))]
        outputs=[frame([low],stream='sub'),frame([high],stamp=100400)]
        for output,(_,m) in zip(outputs,records):
            for o in output['observations']:o.update(m)
        class Extractor:
            def extract_detailed(self,frames):
                list(frames);return copy.deepcopy(outputs)
        with tempfile.TemporaryDirectory() as directory:
            store=DeliveryStore(Path(directory)/'p1.sqlite')
            runtime=MonitoringRuntime(dict(policy={},graph={},trajectory_options={},endpoint='http://127.0.0.1:9'),Extractor(),store)
            try:
                captures=[(store.capture(body,m),body,m) for body,m in records]
                report=runtime.process_photo_window(captures)
                self.assertEqual(report['observation_ids'],['high'])
                payload=json.loads(store.db.execute('SELECT payload FROM deliveries').fetchone()[0])
                self.assertEqual(payload['observation']['embedding'],high['embedding'])
                self.assertEqual(payload['observation']['observation_id'],'high')
                self.assertNotIn('"embedding":',json.dumps(report))
            finally:runtime.main_pool.shutdown();store.close()


if __name__=='__main__':unittest.main()
