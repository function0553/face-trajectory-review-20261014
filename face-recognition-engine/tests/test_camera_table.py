"""Catches losing platform identity, guessing camera endpoints, and joining by names."""
import tempfile
import unittest
from pathlib import Path
from face_engine.camera_table import compile_camera_rows, read_camera_table

SCOPE=dict(activity_id='A',activity_num='N',config_version=1,binding_version=1,monitoring_version=1,route_rule_id='R')

def row(camera='C1',ip='192.0.2.1',name='同名入口',**more):
    return dict({'平台摄像头ID':camera,'视频设备ID':'NVR','平台设备名称':name,
                 '已核实设备IP':ip,'DB active标志':1,'DB deleted标志':0},**more)

class CameraTableTests(unittest.TestCase):
    def test_same_named_cameras_keep_platform_ids_and_separate_connections(self):
        result=compile_camera_rows([row(),row('C2')],SCOPE,'stable-source')
        self.assertEqual([c['camera_id'] for c in result['config']['cameras']],['C1','C2'])
        self.assertEqual([b['device_id'] for b in result['bindings']],['C1','C2'])
        keys=[c['sub_url_env'] for c in result['config']['cameras']]
        self.assertEqual(len(set(keys)),2)
        self.assertFalse(result['runtime_ready'])
        self.assertNotIn('rtsp://',str(result))
        self.assertTrue(all(b['camera_name'] is None for b in result['bindings']))
    def test_unverified_ip_is_pending_and_deleted_row_is_excluded(self):
        result=compile_camera_rows([row(ip=None),row('C2',**{'DB deleted标志':1}),row('C3',ip='未核实')],SCOPE,'S')
        self.assertEqual(result['config']['cameras'],[])
        self.assertEqual([x['camera_id'] for x in result['pending']],['C1','C3'])
        self.assertEqual(result['deleted'],1)
    def test_snapshot_offline_status_does_not_remove_camera(self):
        result=compile_camera_rows([row(**{'DB active标志':0})],SCOPE,'S')
        self.assertEqual(len(result['config']['cameras']),1)
    def test_conflicting_duplicate_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'conflicting camera_id'):
            compile_camera_rows([row(),row(ip='192.0.2.2')],SCOPE,'S')
    def test_missing_platform_id_is_not_replaced_by_name_or_ip(self):
        with self.assertRaisesRegex(ValueError,'platform camera ID'):
            compile_camera_rows([row(camera=None)],SCOPE,'S')
    def test_without_backend_context_only_catalog_is_prepared(self):
        result=compile_camera_rows([row()],None,'S')
        self.assertIsNone(result['config'])
        self.assertFalse(result['runtime_ready'])
        self.assertEqual(result['catalog'][0]['camera_id'],'C1')
    def test_csv_preserves_case_and_opaque_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'cameras.csv'
            p.write_text('平台摄像头ID,视频设备ID,平台设备名称,已核实设备IP,DB active标志,DB deleted标志\nCAM-AbC,NVR,入口,192.0.2.1,1,0\n',encoding='utf-8-sig')
            result=compile_camera_rows(read_camera_table(p),SCOPE,'S')
            self.assertEqual(result['config']['cameras'][0]['camera_id'],'CAM-AbC')
    def test_xlsx_reads_named_sheet_and_header_row(self):
        try:from openpyxl import Workbook
        except ImportError:self.skipTest('optional openpyxl is unavailable')
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'cameras.xlsx';book=Workbook();s=book.active;s.title='平台注册通道'
            s.append(['标题']);s.append(list(row()));s.append(list(row().values()));book.save(p);book.close()
            result=compile_camera_rows(read_camera_table(p,header_row=2),SCOPE,'S')
            self.assertEqual(result['config']['cameras'][0]['camera_id'],'C1')

if __name__=='__main__':unittest.main()
