"""P2 503 CAPACITY for one activity (its backend push queue is full): real loopback
HTTP and durable SQLite; no models or production services."""
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from face_engine.monitoring_delivery import DeliveryStore, delivery_key

ROUTE = '/v1/monitoring/gate-event'
FIELDS = ('activity_id', 'activity_num', 'config_version', 'binding_version', 'monitoring_version', 'route_rule_id')


def gate(activity, event):
    return dict(activity_id=activity, activity_num='NUM-' + activity, config_version=1, binding_version=1,
                monitoring_version=1, route_rule_id='route-' + activity, source_id='S', event_id=event,
                gate_id='G', direction='EXIT')


def capture(store, activity, frame):
    return store.capture(b'image', dict({k: v for k, v in gate(activity, frame).items() if k in FIELDS},
                                        source_id='S', camera_id='C', frame_id=frame))


class P2(BaseHTTPRequestHandler):
    """Answers like P2: 503 CAPACITY for activities whose backend push queue is full."""
    capacity, clock, received, accepted = set(), set(), [], []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        activity, event = body['activity_id'], body['event_id']
        self.received.append((activity, event))
        if activity in self.capacity:
            code, reply = 503, dict(ok=False, error='CAPACITY: backend push quota exhausted; retry after delivery')
        elif activity in self.clock:
            code, reply = 503, dict(ok=False, error='CLOCK_UNTRUSTED: trusted UTC lease expired')
        else:
            code, reply = 200, dict({k: body[k] for k in FIELDS}, ok=True, event_id=event)
            self.accepted.append((activity, event))
        self.send_response(code)
        self.end_headers()
        self.wfile.write(json.dumps(reply).encode())


class CapacityIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), P2)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        P2.capacity, P2.clock = set(), set()
        P2.received[:], P2.accepted[:] = [], []
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def store(self, **kwargs):
        store = DeliveryStore(Path(self.temp.name) / 'p1.sqlite', **kwargs)
        self.addCleanup(store.close)
        return store

    def enqueue(self, store, activity, event):
        body = gate(activity, event)
        store.enqueue(delivery_key(ROUTE, body), ROUTE, body)

    def sent(self, activity, log):
        return [event for name, event in log if name == activity]

    def test_capacity_refusal_pauses_only_that_activity_and_keeps_its_order(self):
        store = self.store()
        P2.capacity, P2.clock = {'A'}, {'C'}
        for i in range(3):
            for activity in 'ABC':
                self.enqueue(store, activity, f'{activity}{i}')
        while store.send_one(self.url):
            pass
        # A blocked activity is probed with its oldest delivery only; each refusal
        # would cost P2 a full recognition and rollback.
        self.assertEqual(set(self.sent('A', P2.received)), {'A0'})
        self.assertEqual(self.sent('B', P2.accepted), ['B0', 'B1', 'B2'])
        # Other 503s (clock, overload) keep the existing per-delivery retry.
        self.assertEqual(self.sent('C', P2.received)[:3], ['C0', 'C1', 'C2'])
        rows = dict(store.db.execute('SELECT event_id,status FROM deliveries WHERE activity_id=?', ('A',)))
        self.assertEqual(rows, {'A0': 'PENDING', 'A1': 'PENDING', 'A2': 'PENDING'})  # retained, not dropped
        P2.capacity = set()  # the receiver accepted A's head event; P2 has room again
        deadline = time.time() + 10
        while len(self.sent('A', P2.accepted)) < 3 and time.time() < deadline:
            if not store.send_one(self.url):
                time.sleep(.05)
        self.assertEqual(self.sent('A', P2.accepted), ['A0', 'A1', 'A2'])

    def test_blocked_activities_use_at_most_half_of_the_queue(self):
        store = self.store(max_pending=8)
        P2.capacity = {'A'}
        early = capture(store, 'A', 'early')  # admitted before A was blocked
        self.enqueue(store, 'A', 'A0')
        self.assertTrue(store.send_one(self.url))  # 503 CAPACITY: A is now blocked
        for i in range(1, 4):
            self.enqueue(store, 'A', f'A{i}')
        with self.assertRaisesRegex(RuntimeError, '^CAPACITY:'):
            self.enqueue(store, 'A', 'A4')
        with self.assertRaisesRegex(RuntimeError, '^CAPACITY:'):
            capture(store, 'A', 'late')
        self.assertFalse(store.accepts('A'))
        self.assertTrue(store.accepts('B'))
        for i in range(4):  # the other half stays usable by every other activity
            self.enqueue(store, 'B', f'B{i}')
        later = capture(store, 'B', 'later')
        # A's admitted capture waits unprocessed: its observations could not be queued.
        self.assertEqual(store.next_capture()[0], later)
        with store.db:
            store.db.execute("UPDATE captures SET status='PROCESSED' WHERE id=?", (later,))
        self.assertIsNone(store.next_capture())
        self.assertEqual(store.db.execute("SELECT status FROM captures WHERE id=?", (early,)).fetchone()[0], 'PENDING')
        self.assertEqual(store.status()['deliveries'], {'PENDING': 8})
        P2.capacity = set()
        deadline = time.time() + 10
        while store.status()['deliveries'].get('ACKNOWLEDGED', 0) < 8 and time.time() < deadline:
            if not store.send_one(self.url):
                time.sleep(.05)
        self.assertEqual(store.status()['deliveries'], {'ACKNOWLEDGED': 8})
        self.assertTrue(store.accepts('A'))
        self.assertEqual(store.next_capture()[0], early)


if __name__ == '__main__':
    unittest.main()
