from collections import deque
from pathlib import Path
import io,json,tempfile,threading,time,unittest
from unittest.mock import patch
import numpy as np
from face_engine.motion_video import MotionVideoCamera,_write,_spool_room
from face_engine.analysis_admission import MotionAdmission,SceneMotionGate


def context():
    return dict(context=dict(activity_id='A',activity_num='A',config_version=1,binding_version=1,
        monitoring_version=1,route_rule_id='R',source_id='C'),since_monotonic=time.monotonic()-60,category='NORMAL')


class MotionVideoDurability(unittest.TestCase):
    def camera(self,directory,callback=lambda *a:None):
        return MotionVideoCamera('rtsp://127.0.0.1/sub','rtsp://127.0.0.1/main','C','C',
            on_capture=callback,motion_video=dict(directory=directory),activity_routes=[context()])
    def job(self,camera):
        epoch=int(time.time())-20;video=camera.folder/('session-'+str(epoch)+'.mkv');video.write_bytes(b'original-video')
        stat=video.stat();job=dict(video=video.name,start_unix_ms=epoch*1000,duration_ms=2000,
            size=stat.st_size,mtime_ns=stat.st_mtime_ns,stream_kind='main',routes=camera.routes)
        path=video.with_suffix('.job.json');_write(path,job);return path,job,video
    def test_motion_gap_or_missing_history_cannot_discard_main_video(self):
        with tempfile.TemporaryDirectory() as d:
            camera=self.camera(d);self.assertTrue(camera._active(5000,6000)[0])
            camera.history=deque((t,False) for t in range(0,11000,500))
            self.assertFalse(camera._active(5000,6000)[0])
            camera.history=deque((t,False) for t in [0,1000,2000,9000,10000])
            self.assertTrue(camera._active(5000,6000)[0])
    def test_small_local_movement_is_admitted_without_global_scene_change(self):
        gate=MotionAdmission(idle_seconds=30,thumbnail_size=(320,180),min_changed_pixels=4,pixel_threshold=8)
        first=np.zeros((576,704,3),np.uint8);self.assertTrue(gate.admit(first,0));self.assertFalse(gate.admit(first,4))
        moving=first.copy();moving[300:320,400:408]=200
        self.assertTrue(gate.admit(moving,4.5))
    def test_static_background_settles_but_local_walk_is_motion(self):
        gate=SceneMotionGate();image=np.zeros((576,704,3),np.uint8)
        for i in range(24):gate.admit(image,i*.5)
        self.assertFalse(gate.admit(image,12))
        moving=image.copy();moving[280:320,400:416]=200
        self.assertTrue(gate.admit(moving,12.5))
    def test_storage_pressure_preserves_old_video(self):
        with tempfile.TemporaryDirectory() as d:
            camera=self.camera(d);_,_,video=self.job(camera)
            self.assertFalse(_spool_room(Path(d),1,0));self.assertTrue(video.exists())
    def test_decode_retries_same_frame_then_commits_all_sampled_frames(self):
        class Process:
            stdout=io.BytesIO();returncode=0
            def wait(self,**kw):return 0
            def poll(self):return 0
        with tempfile.TemporaryDirectory() as d:
            attempts=[];saved=[]
            def callback(body,meta,received):
                attempts.append(meta['frame_id'])
                if len(attempts)==1:raise RuntimeError('durable inbox temporarily full')
                saved.append((body,dict(meta)))
            camera=self.camera(d,callback);path,job,video=self.job(camera)
            image=np.zeros((40,40,3),np.uint8)
            outputs=[(image,dict(_encoded_image=b'frame0')),(image,dict(_encoded_image=b'frame1')),(None,{})]
            with patch('face_engine.motion_video.subprocess.Popen',return_value=Process()),patch('face_engine.motion_video.PrivateRTSPReader._read_output',side_effect=outputs):camera._decode(path,job)
            self.assertEqual(attempts[0],attempts[1]);self.assertEqual([b for b,_ in saved],[b'frame0',b'frame1'])
            self.assertEqual(saved[0][1]['received_at_unix_ms'],job['start_unix_ms'])
            self.assertEqual(saved[1][1]['received_at_unix_ms']-saved[0][1]['received_at_unix_ms'],500)
            self.assertFalse(video.exists());self.assertFalse(path.exists())
    def test_stop_during_inbox_refusal_keeps_original_segment_and_job(self):
        class Process:
            stdout=io.BytesIO()
            def wait(self,**kw):return 0
            def poll(self):return 0
        with tempfile.TemporaryDirectory() as d:
            camera=self.camera(d)
            def refuse(*args):camera.stop_event.set();raise RuntimeError('full')
            camera.callback=refuse;path,job,video=self.job(camera)
            with patch('face_engine.motion_video.subprocess.Popen',return_value=Process()),patch('face_engine.motion_video.PrivateRTSPReader._read_output',return_value=(np.zeros((10,10,3),np.uint8),dict(_encoded_image=b'frame'))):camera._decode(path,job)
            self.assertTrue(video.exists());self.assertTrue(path.exists())
    def test_restart_finishes_only_durably_committed_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            camera=self.camera(d);path,job,video=self.job(camera)
            marker=video.with_suffix('.done.json');_write(marker,dict(processing_completed=True,video=video.name,size=job['size'],mtime_ns=job['mtime_ns']))
            camera._recover_commits();self.assertFalse(video.exists());self.assertFalse(path.exists());self.assertFalse(marker.exists())


if __name__=='__main__':unittest.main()
