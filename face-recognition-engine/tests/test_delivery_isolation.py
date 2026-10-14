"""Real loopback HTTP and durable SQLite; no models or production services."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from face_engine.trajectory_bridge import Outbox, activity_context, scoped_event_key, ActivityContextError
from face_engine.monitoring_delivery import DeliveryStore, delivery_key

def context():
    return dict(activity_id='A', activity_num='NUM-A', config_version=1,
                binding_version=1, monitoring_version=1, route_rule_id='route-A')

class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        event=(body.get('observation') or body)
        code=int(event.get('observation_id',event.get('event_id')))
        if code!=200:
            self.send_response(code); self.end_headers(); self.wfile.write(b'{"error":"bad event detail"}');return
        reply=dict(context(), ok=True, observation_id='200', event_id='200')
        self.send_response(200);self.end_headers();self.wfile.write(json.dumps(reply).encode())

class DeliveryIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def test_bridge_activity_context(self):
        self.assertEqual(activity_context(context()), context())
        for key in context():
            value=context();value.pop(key)
            with self.assertRaises(ActivityContextError): activity_context(value)
    def test_bridge_permanent_errors_continue_fifo(self):
        for code in (400,404,409,410,413,422):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as d:
                box=Outbox(Path(d)/'box.sqlite')
                try:
                    for status in (code,200):
                        meta=dict(context(),source_id='S',camera_id='C',frame_id=str(status))
                        observation=dict(meta,observation_id=str(status))
                        box.enqueue(scoped_event_key('capture',meta,'frame_id'),str(status),[observation],{}, {},metadata=meta)
                    self.assertEqual(box.flush(self.url),1)
                    rows=box.db.execute('SELECT status,payload,error FROM outbox ORDER BY enqueue_seq').fetchall()
                    self.assertEqual([r[0] for r in rows],['QUARANTINED','ACKNOWLEDGED'])
                    self.assertIsNotNone(rows[0][1]);self.assertIn(str(code),rows[0][2]);self.assertIn('bad event detail',rows[0][2])
                    self.assertEqual(box.flush(self.url),0)
                finally: box.close()
    def test_bridge_retryable_errors_preserve_fifo(self):
        for code in (401,403,429,500,503):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as d:
                box=Outbox(Path(d)/'box.sqlite')
                try:
                    meta=dict(context(),source_id='S',camera_id='C',frame_id=str(code))
                    box.enqueue(scoped_event_key('capture',meta,'frame_id'),str(code),[dict(meta,observation_id=str(code))],{}, {},metadata=meta)
                    with self.assertRaises(RuntimeError):box.flush(self.url)
                    self.assertEqual(box.db.execute('SELECT status FROM outbox').fetchone()[0],'PENDING')
                finally:box.close()
    def test_delivery_410_is_quarantined(self):
        with tempfile.TemporaryDirectory() as d:
            store=DeliveryStore(Path(d)/'delivery.sqlite')
            try:
                for code in (410,200):
                    body=dict(context(),source_id='S',event_id=str(code),gate_id='G',direction='EXIT')
                    store.enqueue(delivery_key('/v1/monitoring/gate-event',body),'/v1/monitoring/gate-event',body)
                self.assertTrue(store.send_one(self.url));self.assertTrue(store.send_one(self.url))
                self.assertEqual(store.status()['deliveries'],{'ACKNOWLEDGED':1,'QUARANTINED':1})
            finally:store.close()
    def test_empty_priority_does_not_starve_auxiliary(self):
        with tempfile.TemporaryDirectory() as d:
            store=DeliveryStore(Path(d)/'delivery.sqlite')
            try:
                for i,category in enumerate(('NORMAL','UNREGISTERED')):
                    for j in range(15):
                        store.capture(b'image',dict(context(),source_id='S',camera_id='C',frame_id=f'{i}-{j}'),category)
                seen=[]
                for _ in range(10):
                    key,_,meta=store.next_capture();seen.append(meta['frame_id'].split('-')[0])
                    with store.db:store.db.execute("UPDATE captures SET status='PROCESSED' WHERE id=?",(key,))
                self.assertIn('1',seen[:4])
            finally:store.close()

if __name__=='__main__':unittest.main()
