import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from types import SimpleNamespace

from face_engine.camera_names import CameraNames, ISAPINameReader, channel_name
from face_engine.monitoring_delivery import DeliveryStore
from face_engine.monitoring_service import MonitoringRuntime


def xml(name='真实入口', channel='102'):
    return ('<StreamingChannel xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            f'<id>{channel}</id><channelName>{name}</channelName></StreamingChannel>').encode()


class CameraNameTests(unittest.TestCase):
    def test_namespaced_channel_and_invalid_responses(self):
        self.assertEqual(channel_name(xml(), 102), '真实入口')
        for body in (xml(channel='202'), xml(name=' '), xml(name='x'*1025), b'<html>login</html>',
                     b'<!DOCTYPE x [<!ENTITY a "x">]><StreamingChannel/>', xml().replace(b'</id>', b'</id><id>102</id>')):
            with self.assertRaises(ValueError):
                channel_name(body, 102)

    def test_config_uses_device_endpoint_not_table_name_and_matches_stream_channel(self):
        camera=dict(camera_id='C', camera_name='WRONG', sub_url_env='SUB', device_name=dict(
            url_env='NAME', username_env='USER_TEST', password_env='PASS_TEST'))
        env={'NAME':'http://127.0.0.1/ISAPI/Streaming/channels/101','USER_TEST':'test','PASS_TEST':'test',
             'SUB':'rtsp://127.0.0.1/Streaming/Channels/102'}
        with patch.dict(os.environ, env):
            self.assertEqual(ISAPINameReader.from_camera(camera).stream_id,101)
            with patch.dict(os.environ, {'SUB':'rtsp://127.0.0.1/Streaming/Channels/202'}):
                with self.assertRaisesRegex(ValueError,'video channel'):
                    ISAPINameReader.from_camera(camera)
        self.assertIsNone(ISAPINameReader.from_camera({'camera_name':'WRONG'}))

    def test_alert_subscription_reuses_authoritative_channel_connection(self):
        camera=dict(hikvision=dict(enabled=True,expected_channel_id=3,alert_url_env='ALERT',
            username_env='USER_TEST',password_env='PASS_TEST'))
        with patch.dict(os.environ, {'ALERT':'https://127.0.0.1:8443/ISAPI/Event/notification/alertStream',
                                    'USER_TEST':'test','PASS_TEST':'test'}):
            reader=ISAPINameReader.from_camera(camera)
            self.assertEqual(reader.url,'https://127.0.0.1:8443/ISAPI/Streaming/channels/301')

    def test_digest_http_rename_failure_and_async_refresh(self):
        state={'name':'设备入口甲','status':200,'authorized':0}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if not self.headers.get('Authorization','').startswith('Digest '):
                    self.send_response(401)
                    self.send_header('WWW-Authenticate','Digest realm="camera", nonce="test-nonce", qop="auth", algorithm=MD5')
                    self.end_headers();return
                state['authorized']+=1
                self.send_response(state['status'])
                if state['status']==302:self.send_header('Location','/other-channel')
                self.end_headers()
                self.wfile.write(xml(state['name']))
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        names=CameraNames()
        reader=ISAPINameReader(f'http://127.0.0.1:{server.server_port}/ISAPI/Streaming/channels/102','test','test',1,.2)
        names.add('S','C',reader)
        try:
            self.assertIsNone(names.snapshot('S','C'))
            names.refresh(('S','C'));self.assertEqual(names.snapshot('S','C'),'设备入口甲')
            state['name']='设备新名称'
            names.refresh(('S','C'));self.assertEqual(names.snapshot('S','C'),'设备新名称')
            state['status']=503
            names.refresh(('S','C'));self.assertIsNone(names.snapshot('S','C'))
            self.assertEqual(names.status()[0]['status'],'HTTP_503')
            state['status']=302
            names.refresh(('S','C'));self.assertIsNone(names.snapshot('S','C'))
            self.assertEqual(names.status()[0]['status'],'HTTP_302')
            state['status']=200
            names.entries[('S','C')]['due']=0
            names.start()
            deadline=time.monotonic()+2
            while names.snapshot('S','C') is None and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(names.snapshot('S','C'),'设备新名称')
            self.assertGreater(state['authorized'],0)
            self.assertIsNone(names.snapshot('WRONG_SOURCE','C'))
            with names.lock:names.entries[('S','C')]['checked']=time.monotonic()-10
            self.assertIsNone(names.snapshot('S','C'))
        finally:
            names.stop();server.shutdown();server.server_close();thread.join()

    def test_capture_retry_keeps_first_device_name_snapshot(self):
        meta=dict(activity_id='A',activity_num='N',config_version=1,binding_version=1,monitoring_version=1,
                  route_rule_id='R',source_id='S',camera_id='C',frame_id='F',camera_name='设备旧名')
        with tempfile.TemporaryDirectory() as directory:
            store=DeliveryStore(Path(directory)/'state.sqlite')
            try:
                key=store.capture(b'synthetic',meta)
                self.assertEqual(store.capture(b'synthetic',dict(meta,camera_name='设备新名')),key)
                row=store.db.execute('SELECT metadata FROM captures WHERE id=?',(key,)).fetchone()
                self.assertEqual(json.loads(row[0])['camera_name'],'设备旧名')
            finally:store.close()

    def test_runtime_attaches_only_configured_device_snapshot(self):
        context=dict(activity_id='A',activity_num='N',config_version=1,binding_version=1,
                     monitoring_version=1,route_rule_id='R',source_id='S')
        runtime=MonitoringRuntime({},SimpleNamespace(),None)
        try:
            runtime.camera_contexts['C']=context
            reader=SimpleNamespace(read=lambda:'设备通道',refresh_seconds=5,timeout=2)
            runtime.camera_names.add('S','C',reader)
            runtime.camera_names.refresh(('S','C'))
            frame=SimpleNamespace(metadata=dict(source_id='S',camera_id='C',camera_name='表格假名'))
            self.assertEqual(runtime._configured_metadata(frame)['camera_name'],'设备通道')
            with runtime.camera_names.lock:runtime.camera_names.entries[('S','C')]['name']=None
            self.assertIsNone(runtime._configured_metadata(frame)['camera_name'])
        finally:
            runtime.camera_names.stop();runtime.main_pool.shutdown()

if __name__=='__main__':unittest.main()
