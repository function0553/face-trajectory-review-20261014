import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

from face_engine.capture_ingress import CaptureIngress
from face_engine.monitoring_delivery import DeliveryStore
from face_engine.monitoring_service import MonitoringRuntime
from face_engine.trajectory_bridge import EventConflict


def metadata(frame='1'):
    return dict(activity_id='A',activity_num='A',config_version=1,binding_version=1,
        monitoring_version=1,route_rule_id='R',camera_id='C',source_id='C',frame_id=frame)


class DurableIngress(unittest.TestCase):
    def test_camera_persistence_continues_while_inference_database_is_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DeliveryStore(Path(directory)/'p1.sqlite')
            runtime=MonitoringRuntime(dict(policy={},graph={},trajectory_options={},
                uuid_sources={'continuous':True},capture_ingress={'min_free_mb':0}),object(),store)
            failures=[]
            def camera():
                try:
                    for i in range(12):runtime.persist_camera_capture(b'original-frame-'+str(i).encode(),metadata(str(i)))
                except Exception as error:failures.append(error)
            try:
                with store.lock:
                    worker=threading.Thread(target=camera);worker.start();worker.join(3)
                    self.assertFalse(worker.is_alive(),'camera writer waited on inference SQL mutex')
                    self.assertEqual(runtime.capture_ingress.status()['pending'],12)
                    self.assertEqual(store._pending_count('captures'),0)
                self.assertFalse(failures)
                drain=threading.Thread(target=runtime._ingress_loop);drain.start()
                deadline=time.monotonic()+3
                while runtime.capture_ingress.status()['pending'] and time.monotonic()<deadline:time.sleep(.01)
                runtime.stop_event.set();drain.join(3)
                self.assertEqual(store._pending_count('captures'),12)
                self.assertEqual(runtime.capture_ingress.status()['pending'],0)
            finally:runtime.stop()

    def test_restart_and_duplicate_vendor_delivery_preserve_original_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            first=CaptureIngress(directory,min_free_mb=0)
            first.push(b'raw',dict(metadata(),camera_name='old',received_at_unix_ms=1))
            recovered=CaptureIngress(directory,min_free_mb=0)
            recovered.push(b'raw',dict(metadata(),camera_name='renamed',received_at_unix_ms=2))
            self.assertEqual(recovered.status()['pending'],1)
            path=recovered.claim();header,body=recovered.read(path)
            self.assertEqual(body,b'raw');self.assertEqual(header['metadata']['camera_name'],'old')
            with self.assertRaises(EventConflict):recovered.push(b'different',metadata())
            recovered.acknowledge(path)
            self.assertEqual(CaptureIngress(directory,min_free_mb=0).status()['pending'],0)

    def test_parallel_publication_does_not_overwrite_or_duplicate_same_event(self):
        with tempfile.TemporaryDirectory() as directory:
            ingress=CaptureIngress(directory,min_free_mb=0)
            with ThreadPoolExecutor(max_workers=8) as pool:
                keys=list(pool.map(lambda _:ingress.push(b'original',metadata()),range(20)))
            self.assertEqual(len(set(keys)),1);self.assertEqual(ingress.status()['pending'],1)
            self.assertEqual(len(list(Path(directory).glob('*/*.cap'))),1)

    def test_capacity_and_corruption_keep_original_file_for_review(self):
        with tempfile.TemporaryDirectory() as directory:
            ingress=CaptureIngress(directory,max_mb=1,min_free_mb=0)
            ingress.push(b'x'*700000,metadata())
            with self.assertRaisesRegex(RuntimeError,'CAPACITY:'):ingress.push(b'y'*700000,metadata('2'))
            path=ingress.claim();header,body=ingress.read(path);self.assertEqual(body,b'x'*700000)
            path.write_bytes(b'FACEIN01')
            with self.assertRaises(ValueError):ingress.read(path)
            ingress.quarantine(path,'CORRUPT')
            self.assertTrue(path.with_suffix('.quarantined').exists())
            self.assertEqual(ingress.status()['quarantined'],1)

    def test_database_counts_follow_rollback_and_external_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'p1.sqlite';store=DeliveryStore(path,max_pending=2)
            try:
                first=store.capture(b'first',metadata('1'));second=store.capture(b'second',metadata('2'))
                with self.assertRaisesRegex(RuntimeError,'CAPACITY:'):store.capture(b'third',metadata('3'))
                self.assertEqual(store._pending_count('captures'),2)
                with closing(sqlite3.connect(path)) as external,external:
                    external.execute('DELETE FROM captures WHERE id=?',(first,))
                self.assertEqual(store._pending_count('captures'),1)
                store.finish_capture(second,{'detected_faces':0},[])
                self.assertEqual(store._pending_count('captures'),0)
            finally:store.close()


if __name__=='__main__':unittest.main()
