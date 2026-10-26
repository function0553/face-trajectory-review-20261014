"""Committed algorithm SQLite + real delivery store; synthetic physical readers."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from face_engine.acquisition import Frame
from face_engine.monitoring_delivery import DeliveryStore, delivery_key
from face_engine.platform_runtime import PlatformMonitoringRuntime, binding_snapshot
from face_engine.trajectory_bridge import ActivityContextError


class Reader:
    made = []
    def __init__(self, *args, **kwargs):
        self.started = self.stopped = 0
        self.frame = None
        self.kwargs = kwargs
        self.made.append(self)
    def start(self): self.started += 1
    def stop(self): self.stopped += 1
    def latest(self, **kwargs): return self.frame
    def request_main(self, **kwargs): return self.frame


class PlatformRuntime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root/'p2.sqlite'
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute('CREATE TABLE state(ns TEXT,key TEXT,value TEXT,PRIMARY KEY(ns,key))')
        self.catalog = self.root/'cameras.json'
        self.catalog.write_text(json.dumps(dict(schema_version=1,cameras=[dict(
            camera_id='BACKEND-UUID',source_id='ORIGINAL-SOURCE',sub_url_env='TEST_PLATFORM_SUB')])) )
        self.store = DeliveryStore(self.root/'p1.sqlite')
        config = dict(platform_bindings=dict(state_file=str(self.state)),cameras_file=str(self.catalog),
            policy={},graph={},trajectory_options={},scheduling=dict(fps=[100,100,100]))
        self.runtime = PlatformMonitoringRuntime(config, object(), self.store)
        Reader.made = []
        self.env = patch.dict(os.environ,TEST_PLATFORM_SUB='rtsp://synthetic.invalid/sub')
        self.env.start()
        self.reader = patch('face_engine.acquisition.CameraAcquisition',Reader)
        self.reader.start()
    def tearDown(self):
        self.runtime.stop()
        self.reader.stop()
        self.env.stop()
        self.temp.cleanup()
    def activity(self, identity, *, enabled=True, end=None, camera='BACKEND-UUID', name='后端摄像头名称'):
        now=int(time.time()*1000)
        value=dict(activity_id=identity,activity_num='NUM-'+identity,config_version=1,binding_version=1,
            monitoring_version=1,enabled=enabled,configuration=dict(platform_protocol=4,
            activity_state='1',start_unix_ms=now-10000,end_unix_ms=end or now+100000),
            bindings=[dict(kind='CAMERA',source_id=camera,device_id=camera,camera_name=name,route_rule_id='PLATFORM-V4')])
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute('INSERT OR REPLACE INTO state VALUES(?,?,?)',('activity',identity,json.dumps(value)))
        return value
    def frame(self, identity='FRAME', when=None):
        return Frame(np.zeros((20,40,3),dtype=np.uint8),dict(source_id='ORIGINAL-SOURCE',camera_id='BACKEND-UUID',
            frame_id=identity,stream_kind='sub',time_record=dict(source_id='ORIGINAL-SOURCE',camera_id='BACKEND-UUID',
            capture_unix_ms=1792650000000,time_source='CAMERA',clock_domain='UTC',clock_uncertainty_ms=17)),
            time.monotonic() if when is None else when)
    def test_single_reader_two_activities_and_original_clock(self):
        self.activity('A');self.activity('B')
        before=self.state.read_bytes()
        self.runtime.refresh_bindings()
        self.assertEqual(len(Reader.made),1)
        self.assertEqual(Reader.made[0].started,1)
        frame=self.frame()
        routes=self.runtime._routed_frames(frame)
        self.assertEqual([x.metadata['activity_id'] for x in routes],['A','B'])
        self.assertEqual([x.metadata['camera_name'] for x in routes],['后端摄像头名称']*2)
        self.assertEqual([x.metadata['source_id'] for x in routes],['ORIGINAL-SOURCE']*2)
        self.assertTrue(all(x.metadata['time_record']==frame.metadata['time_record'] for x in routes))
        routes[0].metadata['time_record']['capture_unix_ms']=0
        self.assertEqual(routes[1].metadata['time_record']['capture_unix_ms'],1792650000000)
        self.assertEqual(self.state.read_bytes(),before)
    def test_committed_rebinding_reuses_reader_and_isolates_revoke(self):
        self.activity('A');self.runtime.refresh_bindings()
        old=self.runtime._routed_frames(self.frame())[0].metadata
        self.activity('B');self.runtime.refresh_bindings()
        self.assertEqual(len(Reader.made),1)
        self.assertEqual(len(self.runtime._routed_frames(self.frame())),2)
        self.activity('A',enabled=False);self.runtime.refresh_bindings()
        self.assertEqual([x.metadata['activity_id'] for x in self.runtime._routed_frames(self.frame())],['B'])
        with self.assertRaises(ActivityContextError):
            self.runtime.process_frame(np.zeros((1,1,3)),old)
        self.activity('B',enabled=False);self.runtime.refresh_bindings()
        self.assertFalse(self.runtime.cameras)
        self.assertEqual(Reader.made[0].stopped,1)
    def test_cached_frame_cannot_enter_newly_bound_activity(self):
        self.activity('A');self.runtime.refresh_bindings()
        cached=self.frame()
        self.activity('B');self.runtime.refresh_bindings()
        self.assertEqual([x.metadata['activity_id'] for x in self.runtime._routed_frames(cached)],['A'])
        self.assertEqual(len(self.runtime._routed_frames(self.frame())),2)
    def test_poll_fans_out_without_starving_shared_activity(self):
        self.activity('A');self.activity('B');self.runtime.refresh_bindings()
        self.runtime.cameras['BACKEND-UUID'].frame=self.frame()
        self.runtime._poll_cycle()
        jobs=[self.runtime.queue.pop(),self.runtime.queue.pop()]
        self.assertEqual({j.metadata['activity_id'] for j in jobs},{'A','B'})
        self.runtime._poll_cycle()
        self.assertIsNone(self.runtime.queue.pop())
    def test_capture_is_durable_once_per_bound_activity(self):
        self.activity('A');self.activity('B');self.runtime.refresh_bindings()
        self.runtime._hikvision_frame(self.frame())
        rows=self.store.db.execute('SELECT metadata FROM captures').fetchall()
        self.assertEqual({json.loads(row[0])['activity_id'] for row in rows},{'A','B'})
    def test_activity_waiting_for_p2_capacity_does_not_block_shared_camera(self):
        self.activity('A');self.activity('B');self.runtime.refresh_bindings()
        self.store.max_pending=2  # blocked activities may then hold one pending delivery
        body=dict(activity_id='A',activity_num='NUM-A',config_version=1,binding_version=1,monitoring_version=1,
            route_rule_id='PLATFORM-V4',source_id='ORIGINAL-SOURCE',event_id='E',gate_id='G',direction='EXIT')
        self.store.enqueue(delivery_key('/v1/monitoring/gate-event',body),'/v1/monitoring/gate-event',body)
        self.store.blocked['A']=time.time()+60  # as after P2 answered 503 CAPACITY for A
        with self.assertRaisesRegex(RuntimeError,'^CAPACITY:'):
            self.runtime._hikvision_frame(self.frame())
        self.assertEqual(self.store.db.execute('SELECT activity_id FROM captures').fetchall(),[('B',)])
        self.runtime.cameras['BACKEND-UUID'].frame=self.frame('LIVE')
        self.runtime._poll_cycle()
        processed=[]
        with patch.object(self.runtime,'process_frame',lambda image,metadata,**kwargs:processed.append(metadata['activity_id'])):
            self.runtime._infer_cycle(False);self.runtime._infer_cycle(False)
        self.assertEqual(processed,['B'])
        self.assertEqual(self.runtime.counters['inference_or_capacity_errors'],1)
    def test_missing_video_does_not_claim_a_running_camera(self):
        self.activity('A',camera='UNKNOWN-UUID');self.runtime.refresh_bindings()
        self.assertFalse(self.runtime.cameras)
        self.assertEqual(self.runtime.binding_status['status'],'MISSING_VIDEO_SOURCE')
        self.assertEqual(self.runtime.binding_status['missing_camera_ids'],['UNKNOWN-UUID'])
    def test_failed_snapshot_revokes_previous_readers(self):
        self.activity('A');self.runtime.refresh_bindings()
        self.state.unlink();self.runtime.refresh_bindings()
        self.assertFalse(self.runtime.cameras)
        self.assertFalse(self.runtime._routed_frames(self.frame()))
        self.assertEqual(self.runtime.binding_status['status'],'BINDING_SOURCE_UNAVAILABLE')
        self.assertFalse(self.state.exists())
        self.assertEqual(Reader.made[0].stopped,1)
    def test_expired_or_legacy_activities_are_not_routes(self):
        self.activity('A',end=int(time.time()*1000)-1)
        value=self.activity('B');value['configuration']['platform_protocol']=3
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute('UPDATE state SET value=? WHERE key=?',(json.dumps(value),'B'))
        self.assertEqual(binding_snapshot(self.state,int(time.time()*1000)),{})
    def test_static_activity_catalog_is_rejected(self):
        self.activity('A')
        catalog=json.loads(self.catalog.read_text());catalog['cameras'][0]['activity_context']={}
        self.catalog.write_text(json.dumps(catalog));self.runtime.refresh_bindings()
        self.assertEqual(self.runtime.binding_status['status'],'BINDING_SOURCE_UNAVAILABLE')
        self.assertFalse(Reader.made)

    def test_shared_hikvision_subscription_survives_one_activity_stop(self):
        self.activity('A');self.activity('B')
        self.catalog.write_text(json.dumps(dict(schema_version=1,cameras=[dict(camera_id='BACKEND-UUID',
            source_id='ORIGINAL-SOURCE',hikvision=dict(enabled=True,expected_channel_id='1',
            alert_url_env='TEST_HIK_URL',username_env='TEST_HIK_USER',password_env='TEST_HIK_PASSWORD'))])))
        with patch.dict(os.environ,TEST_HIK_URL='http://synthetic.invalid/ISAPI/Event/notification/alertStream',
                TEST_HIK_USER='SYNTHETIC',TEST_HIK_PASSWORD='SYNTHETIC'), patch('face_engine.acquisition.HikvisionEventReader',Reader):
            self.runtime.refresh_bindings()
            self.assertEqual(len(self.runtime.hikvision),1)
            self.assertEqual(len(Reader.made),1)
            self.assertEqual(Reader.made[0].kwargs['channel_bindings']['1']['camera_id'],'BACKEND-UUID')
            self.activity('A',enabled=False);self.runtime.refresh_bindings()
            self.assertEqual(len(Reader.made),1)
            self.assertEqual(len(self.runtime._routed_frames(self.frame())),1)
            self.activity('B',enabled=False);self.runtime.refresh_bindings()
            self.assertEqual(Reader.made[0].stopped,1)
            self.assertFalse(self.runtime.hikvision)

    def test_activity_end_is_exclusive(self):
        end=int(time.time()*1000)+1000
        self.activity('A',end=end)
        self.assertTrue(binding_snapshot(self.state,end-1))
        self.assertEqual(binding_snapshot(self.state,end),{})

    def test_maintenance_resume_rebuilds_dynamic_readers_while_ingress_paused(self):
        self.activity('A');self.runtime.configure_cameras()
        lease=self.store.consumer_lease(wait=True);lease.__enter__()
        self.runtime.consumer_lease=lease
        self.runtime.maintenance_state='RUNNING'
        self.runtime.cameras['BACKEND-UUID'].start()
        self.runtime._pause_for_maintenance()
        self.assertTrue(self.runtime.maintenance_paused)
        self.assertEqual(self.runtime.maintenance_state,'PAUSED')
        self.runtime._resume_after_maintenance()
        self.assertFalse(self.runtime.maintenance_paused)
        self.assertEqual(self.runtime.maintenance_state,'RUNNING')
        self.assertEqual(len(self.runtime.cameras),1)
        self.assertEqual(len(Reader.made),2)
        self.assertEqual((Reader.made[0].stopped,Reader.made[1].started),(1,1))

    def test_stop_timeout_is_unavailable_retained_and_retried(self):
        self.activity('A');self.runtime.refresh_bindings()
        reader=self.runtime.cameras['BACKEND-UUID']
        self.activity('A',enabled=False)
        with patch.object(reader,'stop',side_effect=TimeoutError('SYNTHETIC blocked native reader')):
            self.runtime.refresh_bindings()
            self.assertEqual(self.runtime.binding_status['status'],'BINDING_SOURCE_UNAVAILABLE')
            self.assertTrue(self.runtime.cleanup_pending)
            self.assertIs(self.runtime.cameras['BACKEND-UUID'],reader)
            self.assertFalse(self.runtime.platform_routes)
            self.assertGreater(self.runtime.next_binding_poll,time.monotonic())
            self.runtime.next_binding_poll=0
            self.runtime._poll_cycle()  # A repeated timeout must not kill polling.
            self.assertIs(self.runtime.cameras['BACKEND-UUID'],reader)
        self.runtime.refresh_bindings()
        self.assertFalse(self.runtime.cameras)
        self.assertFalse(self.runtime.cleanup_pending)
        self.assertEqual(self.runtime.binding_status['status'],'IDLE')

    def test_other_hikvision_channel_activity_does_not_restart_shared_connection(self):
        self.activity('A',camera='CAM1');self.activity('B',camera='CAM2')
        cameras=[dict(camera_id='CAM'+str(n),hikvision=dict(enabled=True,expected_channel_id=str(n),
            alert_url_env='TEST_HIK_URL',username_env='TEST_HIK_USER',password_env='TEST_HIK_PASSWORD')) for n in (1,2)]
        self.catalog.write_text(json.dumps(dict(schema_version=1,cameras=cameras)))
        with patch.dict(os.environ,TEST_HIK_URL='http://synthetic.invalid/ISAPI/Event/notification/alertStream',
                TEST_HIK_USER='SYNTHETIC',TEST_HIK_PASSWORD='SYNTHETIC'), patch('face_engine.acquisition.HikvisionEventReader',Reader):
            self.runtime.refresh_bindings()
            self.assertEqual(len(Reader.made),1)
            self.assertEqual(set(Reader.made[0].kwargs['channel_bindings']),{'1','2'})
            self.activity('A',camera='CAM1',enabled=False);self.runtime.refresh_bindings()
            self.assertEqual(len(Reader.made),1)
            self.assertEqual(Reader.made[0].stopped,0)
            self.assertEqual(set(self.runtime.platform_routes),{'CAM2'})
            frame=Frame(np.zeros((20,40,3),np.uint8),dict(camera_id='CAM1',source_id='CAM1',frame_id='IGNORED'),time.monotonic())
            self.assertFalse(self.runtime._routed_frames(frame))

    @unittest.skipUnless(os.environ.get('FACE_TRAJECTORY_TEST_WORKER'), 'real C++ worker path required')
    def test_real_worker_bindings_to_two_person_rosters_and_capture_reports(self):
        p2=Path(__file__).resolve().parents[2]/'face-trajectory'
        sys.path.insert(0,str(p2/'tools'))
        from verify_multi_person_faces import Detector, Recognizer
        from verify_core import T0, SPACE, POLICY, clock, iso, profile
        from face_engine.platform_photos import prepare_people
        from face_engine.monitoring_service import decode_image
        from face_engine.trajectory_bridge import TrajectoryExtractor
        import cv2
        worker=subprocess.Popen([os.environ['FACE_TRAJECTORY_TEST_WORKER'],'--state-file',str(self.root/'real-p2.sqlite'),
            '--push-results','true'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        def call(body):
            worker.stdin.write(json.dumps(dict(body,_trusted_clock=clock(T0+100000)))+'\n');worker.stdin.flush()
            response=json.loads(worker.stdout.readline())
            self.assertTrue(response.get('ok'),response)
            return response
        runtime=None
        try:
            extractor=TrajectoryExtractor(Detector(),Recognizer(),SPACE)
            rng=np.random.default_rng(23)
            red=rng.integers(0,80,(128,128,3),dtype=np.uint8);red[:,:,2]+=170
            blue=rng.integers(0,80,(128,128,3),dtype=np.uint8);blue[:,:,0]+=170
            people={}
            for activity in ('A','B'):
                call(dict(op='platform_v4_sync',activity_id=activity,body=dict(activityName='SYNTHETIC',activityNum='NUM-'+activity,
                    activityState='1',start_unix_ms=T0,end_unix_ms=T0+86400000,
                    cameras=[dict(deviceId='C1',deviceName='BACKEND-NAME')])) )
                for index,image in enumerate((red,blue)):
                    person=dict(personId=activity+'-PERSON-'+str(index),personName=activity+'-NAME-'+str(index),
                        personIdNum=activity+'-SYNTHETIC-ID-'+str(index),personPhoto='https://synthetic.invalid/photo.png')
                    prepared=prepare_people([person],extractor,decode_image,
                        photo_reader=lambda _,image=image: cv2.imencode('.png',image)[1].tobytes())[0]
                    self.assertEqual(prepared['status'],'SUCCESS')
                    call(dict(op='platform_v4_person',activity_id=activity,person=prepared['person']))
                    people[person['personId']]=person
                call(dict(op='platform_v4_monitoring',activity_id=activity,body=dict(enabled=True)))
            calibration=profile();calibration['profile']['camera_id']='C1';call(calibration)
            self.catalog.write_text(json.dumps(dict(schema_version=1,cameras=[dict(camera_id='C1',source_id='LOCAL-SOURCE',sub_url_env='TEST_PLATFORM_SUB')])))
            store=DeliveryStore(self.root/'real-p1.sqlite')
            runtime=PlatformMonitoringRuntime(dict(platform_bindings=dict(state_file=str(self.root/'real-p2.sqlite')),
                cameras_file=str(self.catalog),policy=POLICY,graph={},trajectory_options={}),extractor,store)
            with patch('face_engine.platform_runtime.time.time',return_value=(T0+100000)/1000):
                runtime.refresh_bindings()
            self.assertEqual(runtime.binding_status['activity_route_count'],2)
            self.assertEqual(len(runtime.cameras),1)
            image=np.zeros((128,264,3),np.uint8);image[:,:128]=red;image[:,132:260]=blue
            frame=Frame(image,dict(source_id='LOCAL-SOURCE',camera_id='C1',frame_id='TWO-PEOPLE',stream_kind='sub',
                time_record=dict(source_id='LOCAL-SOURCE',camera_id='C1',profile_version='good',raw_time=iso(T0+1000))),time.monotonic())
            for routed in runtime._routed_frames(frame):
                runtime.process_frame(routed.image_bgr,routed.metadata)
            deliveries=store.db.execute('SELECT payload FROM deliveries').fetchall()
            self.assertEqual(len(deliveries),4)
            for (payload,) in deliveries:
                request=json.loads(payload)
                response=call(dict(request,op='monitoring_observe'))
                runtime._receipt(request,response)
            reported=[]
            while True:
                envelope=call(dict(op='push_next'))['event']
                if envelope is None:break
                event=envelope['person_track_event'];reported.append(event)
                person=people[event['person_id']]
                self.assertEqual((event['person_name'],event['person_id_num']),(person['personName'],person['personIdNum']))
                self.assertTrue(event['person_id'].startswith(event['activity_id']+'-'))
                self.assertEqual((event['camera_id'],event['device_id'],event['captured_at_unix_ms']),('C1','C1',T0+1000))
                import base64
                crop=decode_image(base64.b64decode(event['capture_photo_url'],validate=True))
                self.assertEqual(crop.shape,(112,112,3))
                dominant=2 if event['person_id'].endswith('0') else 0
                self.assertGreater(crop[:,:,dominant].mean(),crop[:,:,2-dominant].mean()+100)
                call(dict(op='push_ack',event_id=event['event_id']))
            self.assertEqual({event['person_id'] for event in reported},set(people))
            call(dict(op='platform_v4_monitoring',activity_id='A',body=dict(enabled=False)))
            with patch('face_engine.platform_runtime.time.time',return_value=(T0+100000)/1000):
                runtime.refresh_bindings()
            self.assertEqual([r.metadata['activity_id'] for r in runtime._routed_frames(self._fresh_frame(frame))],['B'])
        finally:
            if runtime:runtime.stop()
            worker.stdin.close();worker.wait(timeout=20)
            self.assertEqual(worker.returncode,0,worker.stderr.read())
            worker.stdout.close();worker.stderr.close()

    @staticmethod
    def _fresh_frame(frame):
        return Frame(frame.image_bgr,frame.metadata,time.monotonic())


if __name__=='__main__': unittest.main()
