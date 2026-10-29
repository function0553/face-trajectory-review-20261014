import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from face_engine.capture_ingress import CaptureIngress
from face_engine.monitoring_delivery import DeliveryStore
from face_engine.uuid_sources import PrivateRTSPReader


def metadata(frame):
    return dict(activity_id='A',activity_num='A',config_version=1,binding_version=1,
        monitoring_version=1,route_rule_id='R',source_id='C',camera_id='C',frame_id=frame)


class BoundedCache(unittest.TestCase):
    def test_age_and_capacity_never_unlink_pending_input(self):
        with tempfile.TemporaryDirectory() as directory:
            ingress=CaptureIngress(directory,min_free_mb=0)
            try:
                for i in range(3):ingress.push(bytes([i])*700000,metadata(str(i)))
                claimed=ingress.claim()
                result=ingress.prune_cache(max_mb=1,max_age_seconds=3600)
                self.assertEqual(result['files'],0)
                self.assertTrue(claimed.exists())
                self.assertGreater(ingress.status()['bytes'],1024*1024)
                self.assertFalse((Path(directory)/'cache-evictions.jsonl').exists())
                ingress.acknowledge(claimed)
                self.assertGreater(ingress.status()['bytes'],0)
            finally:ingress.close()

    def test_old_database_capture_remains_available_with_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DeliveryStore(Path(directory)/'p1.sqlite',max_pending=2)
            try:
                key=store.capture(b'original-image',metadata('one'))
                with store.lock,store.db:store.db.execute('UPDATE captures SET created=? WHERE id=?',(time.time()-4000,key))
                self.assertEqual(store.prune_capture_cache(1800),0)
                self.assertEqual(store.capture_room(),1)
                self.assertEqual(store.capture(b'original-image',metadata('one')),key)
                self.assertEqual(store.next_capture()[1],b'original-image')
            finally:store.close()

    def test_delayed_import_does_not_renew_the_original_cache_age(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DeliveryStore(Path(directory)/'p1.sqlite')
            try:
                meta=dict(metadata('delayed'),received_at_unix_ms=int((time.time()-4000)*1000))
                key=store.capture(b'original-image',meta)
                self.assertEqual(store.prune_capture_cache(1800,exclude_keys=[key]),0)
                self.assertEqual(store.prune_capture_cache(1800),0)
                self.assertEqual(store.next_capture()[1],b'original-image')
            finally:store.close()

    def test_unacknowledged_video_is_never_wrapped_or_pruned(self):
        with tempfile.TemporaryDirectory() as directory:
            reader=PrivateRTSPReader('rtsp://127.0.0.1/example','C','C',main_archive_directory=directory)
            command=reader._command()
            self.assertNotIn('-segment_wrap',command)
            old=reader.archive_directory/'old-session-000000.mkv';old.write_bytes(b'old')
            os.utime(old,(time.time()-120,time.time()-120))
            active=reader.archive_directory/(reader.archive_session+'-000000.mkv');active.write_bytes(b'active')
            reader._prune_archive()
            self.assertTrue(old.exists())
            self.assertTrue(active.exists())


if __name__=='__main__':unittest.main()
