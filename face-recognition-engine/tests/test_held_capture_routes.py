from pathlib import Path
import tempfile,unittest
from face_engine.monitoring_delivery import DeliveryStore

def meta(frame,camera='C'):
    return dict(activity_id='A',activity_num='N',config_version=1,binding_version=1,monitoring_version=1,
        source_id=camera,camera_id=camera,route_rule_id='R',frame_id=frame)

class HeldRoutes(unittest.TestCase):
    def test_revoked_original_survives_and_current_input_can_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            store=DeliveryStore(Path(folder)/'p1.sqlite',max_pending=1)
            try:
                key=store.capture(b'original-old',meta('old'))
                store.set_capture_routes([('A','N','D','D','R')])
                self.assertEqual(store.capture_room(),1)
                self.assertEqual(store.db.execute('select status,image from captures where id=?',(key,)).fetchone(),('HELD_ROUTE',b'original-old'))
                new=store.capture(b'current',meta('new','D'))
                self.assertEqual(store.next_capture()[0],new)
                self.assertEqual(store.status()['captures_held_route'],1)
            finally:store.close()
    def test_delayed_revoked_import_is_held_and_reactivation_preserves_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            store=DeliveryStore(Path(folder)/'p1.sqlite',max_pending=1)
            try:
                store.set_capture_routes([])
                key=store.capture(b'untouched',meta('1'))
                self.assertIsNone(store.next_capture())
                store.set_capture_routes([('A','N','C','C','R')])
                self.assertEqual(store.next_capture()[:2],(key,b'untouched'))
                self.assertEqual(store.status()['captures_held_route'],0)
            finally:store.close()
