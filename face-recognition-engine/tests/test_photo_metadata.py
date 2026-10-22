"""URL association only; synthetic image/model doubles, real alignment and queue."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import cv2
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from face_engine.trajectory_bridge import TrajectoryExtractor
from face_engine.monitoring_service import enroll_manifest
from face_engine.monitoring_delivery import DeliveryStore

class Detector:
    def detect_faces(self,image):
        template=np.array([[38.2946,51.6963],[73.5318,51.5014],[56.0252,71.7366],[41.5493,92.3655],[70.7299,92.2041]],dtype=np.float32)
        return [dict(bbox=np.array([x,4,x+112,116]),landmarks=template+[x,4],score=.99) for x in [4,124]]

class Recognizer:
    def extract_embeddings(self,images):
        result=np.zeros((len(images),512),dtype=np.float32);result[:,0]=1
        return result

class PhotoMetadata(unittest.TestCase):
    def setUp(self):
        self.image=np.random.default_rng(123).integers(0,256,size=(120,240,3),dtype=np.uint8)
        self.meta=dict(activity_id='A',activity_num='NUM-A',config_version=1,binding_version=1,monitoring_version=1,
                       route_rule_id='R',source_id='S',camera_id='C',frame_id='FRAME')
        self.extractor=TrajectoryExtractor(Detector(),Recognizer(),{'synthetic':True})
    def test_frame_photo_and_per_face_photo_remain_distinct(self):
        metadata=dict(self.meta,capture_photo_url='https://photos.example.test/frame.jpg',
                      faces={'1':{'capture_photo_url':'https://photos.example.test/face-1.jpg'}})
        rows=self.extractor.extract([(self.image,metadata)])[0]
        self.assertEqual(len(rows),2)
        self.assertEqual([r.get('capture_photo_url') for r in rows],['https://photos.example.test/frame.jpg','https://photos.example.test/face-1.jpg'])
    def test_absent_url_stays_absent_and_face_null_overrides_frame(self):
        rows=self.extractor.extract([(self.image,self.meta)])[0]
        self.assertTrue(all('capture_photo_url' not in row for row in rows))
        rows=self.extractor.extract([(self.image,dict(self.meta,capture_photo_url='https://photos.example.test/frame.jpg',faces={'0':{'capture_photo_url':None}}))])[0]
        self.assertIsNone(rows[0].get('capture_photo_url'))
    def test_enrollment_forwards_uploaded_person_display_fields(self):
        class EnrollmentExtractor:
            feature_space={'synthetic':True}
            def extract_detailed(self,frames):
                return [dict(detected_faces=1,observations=[dict(embedding=[1.0]+[0.0]*511)])]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'photo.png').write_bytes(cv2.imencode('.png',self.image)[1].tobytes())
            person=dict(self.meta,person_id='P',category='PRIORITY',revision=1,expected_revision=0,evidence_id='SYNTHETIC',event_id='ENROLL',
                        person_name='模拟人员甲',profile_photo_url='https://photos.example.test/profile.jpg',photos=['photo.png'])
            manifest=root/'persons.json';manifest.write_text(json.dumps({'persons':[person]}),encoding='utf-8')
            store=DeliveryStore(root/'queue.sqlite')
            try:
                enroll_manifest(manifest,EnrollmentExtractor(),store)
                payload=json.loads(store.db.execute('SELECT payload FROM deliveries').fetchone()[0])
                self.assertEqual(payload.get('person_name'),person['person_name'])
                self.assertEqual(payload.get('profile_photo_url'),person['profile_photo_url'])
            finally:store.close()

if __name__=='__main__':unittest.main()
