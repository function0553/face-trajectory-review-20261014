"""Independent producer files and acquisition-process lifecycle boundaries."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from face_engine.capture_ingress import CaptureIngress
from face_engine.capture_process import CaptureProcesses,atomic_json,BatchProducer
from face_engine.monitoring_delivery import DeliveryStore


def metadata(event):
    return dict(activity_id='A',activity_num='A',config_version=1,binding_version=1,
        monitoring_version=1,route_rule_id='PLATFORM-V4',source_id='UUID',camera_id='UUID',frame_id=event)


class IndependentCapture(unittest.TestCase):
    def test_old_queue_and_new_frames_share_inference_across_cameras(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DeliveryStore(Path(directory)/'p1.sqlite')
            try:
                expected={}
                for camera in ['A','B']:
                    for i in range(3):
                        meta=dict(metadata(str(i)),camera_id=camera,source_id=camera)
                        expected[camera+str(i)]=store.capture(b'original',meta)
                excluded=[];order=[]
                for _ in range(6):
                    row=store.next_capture(excluded);excluded.append(row[0]);order.append(row[2]['camera_id']+row[2]['frame_id'])
                self.assertEqual(order,['A0','B0','A2','B2','A1','B1'])
                self.assertEqual(store._pending_count('captures'),6)
            finally:store.close()

    def test_group_ack_waits_for_durable_publication_and_import_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);producer=BatchProducer(root/'process-0',max_mb=8,min_free_mb=0)
            entered=threading.Event();release=threading.Event()
            original=CaptureIngress._sync_directory
            def sync(path):entered.set();release.wait(3);original(path)
            try:
                with patch.object(CaptureIngress,'_sync_directory',side_effect=sync),ThreadPoolExecutor(max_workers=12) as pool:
                    futures=[pool.submit(producer.push,b'original-'+str(i).encode(),metadata(str(i))) for i in range(12)]
                    try:
                        self.assertTrue(entered.wait(2));self.assertFalse(any(f.done() for f in futures))
                    finally:release.set()
                    keys=[f.result(timeout=4) for f in futures]
                records=[]
                for path in (root/'process-0').glob('*.batch'):records.extend(CaptureIngress.read_batch(path))
                self.assertEqual(len(records),12);self.assertEqual({h['key'] for h,b in records},set(keys))
                store=DeliveryStore(root/'p1.sqlite',max_pending=1)
                try:
                    with self.assertRaisesRegex(RuntimeError,'CAPACITY:'):store.capture_batch(records[:2])
                    self.assertEqual(store._pending_count('captures'),0)
                    store.max_pending=20
                    self.assertEqual(len(store.capture_batch(records)),12)
                    self.assertEqual(len(store.capture_batch(records)),12)
                    self.assertEqual(store._pending_count('captures'),12)
                finally:store.close()
            finally:release.set();producer.close()

    def test_external_import_restart_and_producer_quota_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=CaptureIngress(directory,min_free_mb=0)
            child_root=Path(directory)/'process-0'
            producer=CaptureIngress(child_root,max_mb=1,min_free_mb=0,bucket_digits=1)
            producer.push(b'x'*700000,metadata('one'))
            parent.external_roots=[child_root];parent.discover_external()
            path=parent.claim();self.assertEqual(parent.read(path)[1],b'x'*700000)
            parent.next_discovery=0;parent.discover_external();self.assertIsNone(parent.claim())
            parent.acknowledge(path)
            producer.push(b'y'*700000,metadata('two'))
            recovered=CaptureIngress(directory,min_free_mb=0);recovered.external_roots=[child_root]
            recovered.discover_external();path=recovered.claim()
            self.assertEqual(recovered.read(path)[0]['metadata']['frame_id'],'two')
            recovered.acknowledge(path);self.assertEqual(recovered.status()['pending'],0)

    def test_grouping_depends_only_on_uuid_and_private_config_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            ingress=CaptureIngress(Path(directory)/'ingress',min_free_mb=0)
            manager=CaptureProcesses(Path(directory)/'control',ingress,count=16)
            self.assertEqual(manager.group('UUID'),manager.group('UUID'))
            self.assertEqual(len(manager.roots),16)
            path=Path(directory)/'private.json';atomic_json(path,dict(password='private-test-value'))
            self.assertEqual(json.loads(path.read_text())['password'],'private-test-value')
            if os.name!='nt':self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertFalse(list(Path(directory).glob('*.partial')))

    @unittest.skipUnless(sys.platform=='linux','Linux acquisition owner/process-group contract')
    def test_worker_control_refresh_and_owner_stop_leave_no_running_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'process.json'
            atomic_json(path,dict(schema_version=1,parent_pid=os.getpid(),cameras={},
                ingress_directory=str(Path(directory)/'ingress'),max_mb=1,min_free_mb=0))
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'),
                     OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
            process=subprocess.Popen([sys.executable,'-m','face_engine.capture_process','--config',str(path)],
                env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            try:
                status=path.with_suffix('.status.json');deadline=time.monotonic()+5
                while not status.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
                self.assertTrue(status.exists())
                self.assertEqual(json.loads(status.read_text())['pid'],process.pid)
                process.terminate();self.assertEqual(process.wait(timeout=5),-signal.SIGKILL)
            finally:
                if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)


if __name__=='__main__':unittest.main()
