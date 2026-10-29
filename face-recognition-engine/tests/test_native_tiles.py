import unittest
import numpy as np
from face_engine.native_tiles import detect_native_tiles,motion_region_faces


class NativeTiles(unittest.TestCase):
    def test_same_frame_coordinates_and_overlap_are_preserved(self):
        class Detector:
            def detect_faces(self,image):
                points=np.argwhere(image[:,:,0]>0)
                if not len(points):return []
                y,x=points.min(axis=0);Y,X=points.max(axis=0)+1
                return [dict(bbox=[x,y,X,Y],raw_bbox=[x,y,X,Y],
                    landmarks=[[x+1,y+1],[X-1,y+1],[(x+X)/2,(y+Y)/2],[x+1,Y-1],[X-1,Y-1]],score=.9)]
        image=np.zeros((1080,1920,3),np.uint8);image[400:440,900:930,0]=200
        faces=detect_native_tiles(Detector(),image)
        self.assertEqual(len(faces),1);self.assertEqual(faces[0]['bbox'],[900,400,930,440])
        self.assertEqual(faces[0]['landmarks'][0],[901,401])
    def test_small_images_do_not_pay_for_tiles(self):
        class Detector:
            def detect_faces(self,image):raise AssertionError('unneeded detector invocation')
        self.assertEqual(detect_native_tiles(Detector(),np.zeros((480,640,3),np.uint8)),[])
    def test_unchanged_frame_does_not_repeat_full_image_tiles(self):
        class Detector:
            def detect_faces(self,image):raise AssertionError('idle region should not invoke detector')
        self.assertEqual(motion_region_faces(Detector(),np.zeros((1080,1920,3),np.uint8),np.zeros((180,320),np.uint8)),[])


if __name__=='__main__':unittest.main()
