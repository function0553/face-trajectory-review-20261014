"""Local durable capture inbox and independent retryable P2 deliveries."""
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from collections import deque

from face_engine.trajectory_bridge import (encoded, activity_context, scoped_event_key,
    verify_activity_receipt, ActivityContextError, EventConflict, RetentionExpired,
    retention_tombstone, retention_write_guard, consumer_lease)
from face_engine.scheduling import CATEGORIES


class FairRLock:
    """FIFO handoff prevents camera writers starving inference and health."""
    def __init__(self):
        self.guard = threading.Lock()
        self.waiting = deque()
        self.owner = None
        self.depth = 0

    def __enter__(self):
        identity = threading.get_ident()
        with self.guard:
            if self.owner == identity:
                self.depth += 1
                return self
            if self.owner is None:
                self.owner, self.depth = identity, 1
                return self
            ticket = (identity, threading.Event())
            self.waiting.append(ticket)
        try:
            ticket[1].wait()
        except BaseException:
            with self.guard:
                if ticket in self.waiting:
                    self.waiting.remove(ticket)
                elif self.owner == identity:
                    self._handoff()
            raise
        return self

    def _handoff(self):
        if self.waiting:
            self.owner, event = self.waiting.popleft()
            self.depth = 1
            event.set()
        else:
            self.owner, self.depth = None, 0

    def __exit__(self, *args):
        with self.guard:
            if self.owner != threading.get_ident():
                raise RuntimeError('store mutex is not owned by this thread')
            self.depth -= 1
            if not self.depth:
                self._handoff()


def _p2_capacity(text):
    """True for P2's own 503 CAPACITY answer, e.g. its per-activity push quota is full."""
    try:
        reply = json.loads(text)
    except ValueError:
        return False
    return isinstance(reply, dict) and isinstance(reply.get('error'), str) and reply['error'].startswith('CAPACITY:')


def delivery_key(route, payload):
    if route.endswith('/observe'):
        observation = payload.get('observation') or {}
        return scoped_event_key('observe', {**payload, 'source_id': observation.get('source_id'),
            'event_id': observation.get('observation_id')})
    return scoped_event_key('person' if route.endswith('/persons') else 'gate', payload,
        'event_id')


class DeliveryStore:
    def __init__(self, path, *, max_pending=10000, max_database_mb=1024, max_faces_per_capture=1024, weights=(6, 3, 1)):
        if max_pending < 1 or max_database_mb < 1:
            raise ValueError("positive storage limits required")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path).resolve()
        with consumer_lease(self.path, wait=True):
            self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
            self.lock = FairRLock()
            self.max_pending = max_pending
            self.max_faces_per_capture = max_faces_per_capture
            if type(max_faces_per_capture) is not int or max_faces_per_capture < 1:
                raise ValueError('positive max_faces_per_capture required')
            if len(weights) != 3 or any(type(w) is not int or w < 1 for w in weights):
                raise ValueError('all three durable classes require positive integer weights')
            self.cycle = [category for i in range(max(weights)) for category, weight in zip(CATEGORIES, weights) if i < weight]
            self.cursors = {'captures': 0, 'deliveries': 0}
            self.activity_cursors = {}
            self.capture_source_cursors = {}
            self.capture_fresh_turns = {}
            # activity -> earliest next probe. P2 refuses an activity's new results with 503
            # CAPACITY while its backend push queue is full; by contract that queue waits on
            # its oldest event until the receiver accepts it or it expires. Probing with one delivery at a
            # time keeps those refusals (each a full P2 recognition and rollback) from
            # crowding out other activities; see _blocked_share for their queue share.
            self.blocked = {}
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            # Image-bearing imports otherwise evict the tiny default page
            # cache and repeatedly checkpoint while every infer worker waits.
            self.db.execute('PRAGMA cache_size=-262144')
            self.db.execute('PRAGMA wal_autocheckpoint=16384')
            self.db.execute('PRAGMA journal_size_limit=67108864')
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            limit = max_database_mb * 1024 * 1024 // page_size
            self.page_limit = limit
            if self.db.execute("PRAGMA page_count").fetchone()[0] > limit:
                self.db.close()
                raise ValueError("existing database exceeds limit")
            self.db.execute(f"PRAGMA max_page_count={limit}")
            self.db.execute("CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY, hash TEXT NOT NULL, image BLOB, metadata TEXT NOT NULL, result TEXT, created REAL NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, hash TEXT NOT NULL, route TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, receipt TEXT, error TEXT, created REAL NOT NULL)")
            for table in ('captures', 'deliveries'):
                columns = {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}
                for name, definition in {'category': "TEXT NOT NULL DEFAULT 'NORMAL'",
                        'activity_id': 'TEXT', 'source_id': 'TEXT', 'event_id': 'TEXT', 'context': 'TEXT'}.items():
                    if name not in columns:
                        self.db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            if 'status' not in {r[1] for r in self.db.execute('PRAGMA table_info(captures)')}:
                self.db.execute("ALTER TABLE captures ADD COLUMN status TEXT NOT NULL DEFAULT 'PENDING'")
                self.db.execute("UPDATE captures SET status='PROCESSED' WHERE image IS NULL")
            # Legacy payloads are neither assigned nor replayed to a default activity.
            for key, metadata in self.db.execute('SELECT id,metadata FROM captures WHERE context IS NULL').fetchall():
                value = json.loads(metadata)
                try:
                    context = activity_context(value)
                    if key != scoped_event_key('capture', value, 'frame_id'):
                        raise ActivityContextError('legacy unscoped capture key')
                except ActivityContextError:
                    self.db.execute("UPDATE captures SET status='QUARANTINED',result=coalesce(result,?) WHERE id=?",
                        (encoded({'status': 'QUARANTINED', 'reason': 'ACTIVITY_CONTEXT_REQUIRED'}), key))
                else:
                    self.db.execute('UPDATE captures SET activity_id=?,source_id=?,event_id=?,context=? WHERE id=?',
                        (context['activity_id'], value['source_id'], value['frame_id'], encoded(context), key))
            for key, route, body in self.db.execute('SELECT id,route,payload FROM deliveries WHERE context IS NULL').fetchall():
                value = json.loads(body)
                try:
                    context = activity_context(value, require_route=not route.endswith('/persons'))
                    if key != delivery_key(route, value):
                        raise ActivityContextError('legacy unscoped delivery key')
                except (ActivityContextError, ValueError):
                    self.db.execute("UPDATE deliveries SET status='QUARANTINED',error='ACTIVITY_CONTEXT_REQUIRED' WHERE id=?", (key,))
                else:
                    source = (value.get('observation') or value)['source_id']
                    event = (value.get('observation') or value).get('observation_id', value.get('event_id'))
                    self.db.execute('UPDATE deliveries SET activity_id=?,source_id=?,event_id=?,context=? WHERE id=?',
                        (context['activity_id'], source, event, encoded(context), key))
            self.db.execute("CREATE INDEX IF NOT EXISTS pending_delivery ON deliveries(status,next_attempt,created)")
            self.db.execute("CREATE INDEX IF NOT EXISTS pending_capture ON captures(created) WHERE image IS NOT NULL")
            self.db.execute("CREATE INDEX IF NOT EXISTS capture_status ON captures(status)")
            self.db.execute("CREATE INDEX IF NOT EXISTS pending_capture_scope ON captures(category,activity_id,created,id) WHERE status='PENDING' AND image IS NOT NULL")
            self.db.execute("CREATE INDEX IF NOT EXISTS pending_capture_source ON captures(category,activity_id,source_id,created,id) WHERE status='PENDING' AND image IS NOT NULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS inbox_counts(kind TEXT PRIMARY KEY,n INTEGER NOT NULL CHECK(n>=0))")
            # Reconcile once under the exclusive consumer lease. SQL triggers
            # also see C++ retention deletes and roll back with the owning write.
            for table,condition in [('captures',"status='PENDING' AND image IS NOT NULL"),('deliveries',"status='PENDING'")]:
                self.db.execute('INSERT OR REPLACE INTO inbox_counts VALUES(?,(SELECT count(*) FROM '+table+' WHERE '+condition+'))',(table,))
                fresh=condition.replace('status','NEW.status').replace('image','NEW.image')
                old=condition.replace('status','OLD.status').replace('image','OLD.image')
                for operation,delta,when in [('INSERT','1',fresh),('DELETE','-1',old)]:
                    self.db.execute('CREATE TRIGGER IF NOT EXISTS count_'+table+'_'+operation.lower()+' AFTER '+operation+' ON '+table+' WHEN '+when+" BEGIN UPDATE inbox_counts SET n=n+("+delta+") WHERE kind='"+table+"'; END")
                self.db.execute('CREATE TRIGGER IF NOT EXISTS count_'+table+'_update AFTER UPDATE ON '+table+' WHEN ('+fresh+') != ('+old+") BEGIN UPDATE inbox_counts SET n=n+CAST(("+fresh+") AS INTEGER)-CAST(("+old+") AS INTEGER) WHERE kind='"+table+"'; END")
            self.db.commit()

    def _pending_count(self,table):
        return self.db.execute('SELECT n FROM inbox_counts WHERE kind=?',(table,)).fetchone()[0]

    @contextmanager
    def consumer_lease(self, *, wait=False):
        """One consumer or C++ maintenance owner, while enrollment may enqueue."""
        with consumer_lease(self.path, wait=wait):
            yield

    def _blocked_held(self):
        """Pending deliveries of every activity P2 currently refuses for capacity."""
        names = list(self.blocked)
        if not names:
            return 0
        return self.db.execute(f"SELECT count(*) FROM deliveries WHERE status='PENDING' AND activity_id IN ({','.join('?' * len(names))})", names).fetchone()[0]

    def _blocked_share(self):
        # Together blocked activities may use at most half of the pending budget, so one
        # receiver refusing an activity's events cannot stop every other activity and camera.
        return max(1, self.max_pending // 2)

    def _blocked_full(self):
        return bool(self.blocked) and self._blocked_held() >= self._blocked_share()

    def accepts(self, activity):
        """False while this activity waits on P2 capacity and the blocked share is used up."""
        with self.lock:
            return activity not in self.blocked or not self._blocked_full()

    def _capacity(self, fanout=False, activities=()):
        captures = self._pending_count('captures')
        deliveries = self._pending_count('deliveries')
        # Separate ingress/egress budgets leave room to turn one image into many
        # observations even when the capture inbox is completely full.
        allowance = self.max_faces_per_capture if fanout else 0
        if captures > self.max_pending or deliveries > self.max_pending + allowance:
            raise RuntimeError("CAPACITY: durable pending queue is full")
        if self.blocked.keys() & set(activities) and self._blocked_held() > self._blocked_share() + allowance:
            raise RuntimeError("CAPACITY: activity is waiting for P2 push capacity")

    def capture(self, image_bytes, metadata, category='NORMAL'):
        with self.lock, self.db:
            return self._capture_locked(image_bytes,metadata,category)

    def capture_room(self):
        with self.lock:return max(0,self.max_pending-self._pending_count('captures'))

    def prune_capture_cache(self, max_age_seconds, *, max_rows=256, exclude_keys=()):
        """Drop expired queue images under an explicit cache-retention policy.

        Keep immutable IDs/hashes and a QUARANTINED receipt, so a duplicate
        import cannot recreate an evicted frame or claim it was recognized.
        Recognition receipts and queued deliveries are never removed here.
        """
        if max_age_seconds<=0:raise ValueError('positive capture cache age required')
        cutoff=time.time()-max_age_seconds
        with self.lock,self.db:
            retention_write_guard(self.db)
            excluded=tuple(exclude_keys)
            if len(excluded)>512:raise ValueError('too many in-flight capture exclusions')
            condition="status='PENDING' AND image IS NOT NULL AND created<?"
            if excluded:condition+=' AND id NOT IN ('+','.join('?'*len(excluded))+')'
            rows=self.db.execute('SELECT id,created FROM captures INDEXED BY pending_capture_scope WHERE '+condition+' ORDER BY created,id LIMIT ?',
                (cutoff,*excluded,max_rows)).fetchall()
            for key,created in rows:
                report=encoded(dict(status='QUARANTINED',reason='CACHE_RETENTION_UNPROCESSED',
                    recognition_completed=False,received_created_unix_ms=int(created*1000)))
                self.db.execute("UPDATE captures SET image=NULL,result=?,status='QUARANTINED' WHERE id=? AND status='PENDING'",(report,key))
            return len(rows)

    def capture_batch(self, records):
        # Publish small durable chunks so a large multi-camera batch cannot
        # monopolize the infer/delivery mutex. The producer file is acknowledged
        # only after all chunks commit; a retry verifies prior committed hashes.
        keys=[]
        for offset in range(0,len(records),4):
            with self.lock, self.db:
                for header,body in records[offset:offset+4]:
                    try:key=self._capture_locked(body,header['metadata'],header['category'])
                    except RetentionExpired:key=header['key']
                    if key!=header['key']:raise EventConflict('batch event identity differs')
                    keys.append(key)
        return keys

    def _capture_locked(self, image_bytes, metadata, category):
        if not isinstance(metadata, dict):
            raise ValueError('capture metadata must be an object')
        if category not in CATEGORIES:
            raise ValueError('invalid capture scheduling category')
        for key in ("source_id", "camera_id", "frame_id"):
            if not isinstance(metadata.get(key), str) or not metadata[key]:
                raise ValueError(f"{key} required")
        key = scoped_event_key('capture', metadata, 'frame_id')
        try:
            context = activity_context(metadata)
            status, report = 'PENDING', None
        except ActivityContextError:
            context, status = {}, 'QUARANTINED'
            report = encoded({'status': status, 'reason': 'ACTIVITY_CONTEXT_REQUIRED'})
        body = encoded(metadata)
        # A retransmitted vendor event has the same acquisition evidence but a
        # new receiver timestamp. Keep the first arrival without conflicting it.
        # Device names are receiver-enriched snapshots, not vendor event identity.
        # Retain the first stored snapshot when a vendor retries after a rename.
        immutable = {k: v for k, v in metadata.items() if k not in ('received_at_unix_ms', 'camera_name')}
        digest = hashlib.sha256(encoded(immutable).encode() + image_bytes).hexdigest()
        retention_write_guard(self.db)
        if retention_tombstone(self.db, 'captures', key, digest):
            raise RetentionExpired('EXPIRED: capture cannot be recreated')
        old = self.db.execute("SELECT hash FROM captures WHERE id=?", (key,)).fetchone()
        if old:
            if old[0] != digest:
                raise EventConflict("conflicting capture event")
            return key
        if status == 'PENDING' and context['activity_id'] in self.blocked and self._blocked_full():
            raise RuntimeError('CAPACITY: activity is waiting for P2 push capacity')
        created=time.time()
        received=metadata.get('received_at_unix_ms')
        if type(received) is int and 0<received<=int(created*1000)+60000:
            # A delayed batch must not renew an old frame's cache lifetime.
            created=received/1000
        self.db.execute("INSERT INTO captures(id,hash,image,metadata,result,created,category,activity_id,source_id,event_id,context,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, digest, image_bytes, body, report, created, category, context.get('activity_id'), metadata['source_id'], metadata['frame_id'], encoded(context), status))
        if self._pending_count('captures') > self.max_pending:
            raise RuntimeError('CAPACITY: capture inbox full')
        return key

    def next_capture(self, exclude_keys=()):
        with self.lock:
            # Captures of a blocked activity over its share wait unprocessed; their
            # observations could not be queued, so do not spend inference on them.
            full = list(self.blocked) if self._blocked_full() else []
            condition = "status='PENDING' AND image IS NOT NULL" + (
                f" AND activity_id NOT IN ({','.join('?' * len(full))})" if full else '')
            excluded = tuple(exclude_keys)
            if len(excluded)>512:
                raise ValueError('too many in-flight capture exclusions')
            if excluded:
                condition += f" AND id NOT IN ({','.join('?' * len(excluded))})"
            row = self._next('captures', 'id,image,metadata', condition, tuple(full)+excluded)
            return (row[0], row[1], json.loads(row[2])) if row else None

    def _next(self, table, columns, condition, params):
        source = table + (' INDEXED BY pending_capture_scope' if table=='captures' else '')
        for _ in self.cycle:
            category = self.cycle[self.cursors[table]]
            self.cursors[table] = (self.cursors[table]+1) % len(self.cycle)
            previous = self.activity_cursors.get((table, category), '')
            if table == 'captures':
                # Seek the next eligible scope instead of reading every image
                # row to enumerate DISTINCT values while owning the store lock.
                row = self.db.execute(f'SELECT activity_id FROM {source} WHERE {condition} AND category=? AND activity_id>? ORDER BY activity_id LIMIT 1',
                    (*params, category, previous)).fetchone()
                if row is None:
                    row = self.db.execute(f'SELECT activity_id FROM {source} WHERE {condition} AND category=? ORDER BY activity_id LIMIT 1',
                        (*params, category)).fetchone()
                activities = [row[0]] if row else []
            else:
                activities = [r[0] for r in self.db.execute(f'SELECT DISTINCT activity_id FROM {source} WHERE {condition} AND category=? ORDER BY activity_id', (*params, category))]
            if activities:
                activity = next((item for item in activities if item > previous), activities[0])
                self.activity_cursors[table, category] = activity
                if table=='captures':
                    scope=(category,activity)
                    previous_source=self.capture_source_cursors.get(scope,'')
                    row=self.db.execute(
                        f'SELECT source_id FROM captures INDEXED BY pending_capture_source WHERE {condition} AND category=? AND activity_id=? AND source_id>? ORDER BY source_id LIMIT 1',
                        (*params,category,activity,previous_source)).fetchone()
                    if row is None:
                        row=self.db.execute(
                            f'SELECT source_id FROM captures INDEXED BY pending_capture_source WHERE {condition} AND category=? AND activity_id=? ORDER BY source_id LIMIT 1',
                            (*params,category,activity)).fetchone()
                    if row is None:
                        continue
                    source_id=row[0]
                    self.capture_source_cursors[scope]=source_id
                    key=scope+(source_id,);fresh=self.capture_fresh_turns.get(key,False)
                    self.capture_fresh_turns[key]=not fresh
                    direction='DESC' if fresh else 'ASC'
                    return self.db.execute(f'SELECT {columns} FROM captures INDEXED BY pending_capture_source WHERE {condition} AND category=? AND activity_id=? AND source_id=? ORDER BY created {direction},id {direction} LIMIT 1',
                        (*params,category,activity,source_id)).fetchone()
                return self.db.execute(f'SELECT {columns} FROM {source} WHERE {condition} AND category=? AND activity_id=? ORDER BY created,id LIMIT 1', (*params, category, activity)).fetchone()
        return None

    def _enqueue(self, key, route, payload, category='NORMAL'):
        retention_write_guard(self.db)
        if category not in CATEGORIES:
            raise ValueError('invalid delivery scheduling category')
        if route not in ("/v1/monitoring/observe", "/v1/monitoring/gate-event", "/v1/monitoring/persons"):
            raise ValueError("unsupported delivery route")
        if key != delivery_key(route, payload):
            raise ValueError('delivery key must be the scoped logical event key')
        try:
            context = activity_context(payload, require_route=not route.endswith('/persons'))
            if route.endswith('/observe') and context != activity_context(payload['observation']):
                raise ActivityContextError('observation activity context conflict')
            status, error = 'PENDING', None
        except ActivityContextError:
            context, status, error = {}, 'QUARANTINED', 'ACTIVITY_CONTEXT_REQUIRED'
        body = encoded(payload)
        digest = hashlib.sha256((route + body).encode()).hexdigest()
        if retention_tombstone(self.db, 'deliveries', key, digest):
            raise RetentionExpired('EXPIRED: delivery cannot be re-enqueued')
        old = self.db.execute("SELECT hash FROM deliveries WHERE id=?", (key,)).fetchone()
        if old:
            if old[0] != digest:
                raise EventConflict("conflicting delivery event")
            return
        source = (payload.get('observation') or payload)['source_id']
        event = (payload.get('observation') or payload).get('observation_id', payload.get('event_id'))
        self.db.execute("INSERT INTO deliveries(id,hash,route,payload,status,created,category,activity_id,source_id,event_id,context,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, digest, route, body, status, time.time(), category, context.get('activity_id'), source, event, encoded(context), error))

    def enqueue(self, key, route, payload, category='NORMAL'):
        with self.lock, self.db:
            self._enqueue(key, route, payload, category)
            self._capacity(activities=[payload.get('activity_id')] if isinstance(payload, dict) else ())

    def quarantine_capture(self, key, report):
        """Isolate invalid input without erasing its original image evidence."""
        with self.lock,self.db:
            retention_write_guard(self.db)
            self.db.execute("UPDATE captures SET result=?,status='QUARANTINED' WHERE id=? AND status='PENDING'",
                            (encoded(report),key))

    def finish_capture(self, key, report, deliveries):
        if len(deliveries) > self.max_faces_per_capture:
            raise ValueError('capture face fanout exceeds configured bound')
        # Receipt is committed only with every extracted observation queued.
        with self.lock, self.db:
            retention_write_guard(self.db)
            if retention_tombstone(self.db, 'captures', key):
                raise RetentionExpired('EXPIRED: deleted capture cannot create deliveries')
            for delivery in deliveries:
                self._enqueue(*delivery)
            self.db.execute("UPDATE captures SET image=NULL,result=?,status='PROCESSED' WHERE id=? AND status='PENDING'", (encoded(report), key))
            self._capacity(fanout=True, activities=[delivery[2].get('activity_id') for delivery in deliveries])

    def status(self, key=None):
        with self.lock:
            if key:
                row = self.db.execute("SELECT status,receipt,error FROM deliveries WHERE id=?", (key,)).fetchone()
                return dict(status=row[0], receipt=json.loads(row[1]) if row[1] else None, error=row[2]) if row else None
            counts = dict(self.db.execute("SELECT status,count(*) FROM deliveries GROUP BY status"))
            age = self.db.execute("SELECT min(created) FROM deliveries WHERE status='PENDING'").fetchone()[0]
            capture_age = self.db.execute("SELECT min(created) FROM captures INDEXED BY pending_capture_scope WHERE status='PENDING' AND image IS NOT NULL").fetchone()[0]
            return {"deliveries": counts, "captures_pending": self._pending_count('captures'),
                    "captures_quarantined": self.db.execute("SELECT count(*) FROM captures WHERE status='QUARANTINED'").fetchone()[0],
                    "oldest_capture_age_seconds": max(0, time.time()-capture_age) if capture_age else 0,
                    "oldest_delivery_age_seconds": max(0, time.time()-age) if age else 0}

    def capture_status(self, key):
        """Per-capture progress without returning images or biometric vectors.

        P2 receipts describe their original transaction; callers obtain current
        monitoring state from P2. An observation ID is not a gate binding proof.
        """
        with self.lock:
            row = self.db.execute("SELECT status,metadata,result FROM captures WHERE id=?", (key,)).fetchone()
            if row is None:
                return None
            capture_state, metadata, report = row
            metadata = json.loads(metadata)
            report = json.loads(report) if report is not None else None
            observations = []
            for observation_id in ([] if capture_state == 'QUARANTINED' else (report or {}).get('observation_ids', [])):
                observation_key = scoped_event_key('observe', {**metadata, 'event_id': observation_id})
                delivery = self.db.execute("SELECT status,receipt,error FROM deliveries WHERE id=?", (observation_key,)).fetchone()
                if delivery is None:
                    raise RuntimeError('capture report references a missing durable delivery')
                status, receipt, error = delivery
                receipt = json.loads(receipt) if receipt is not None else {}
                association = receipt.get('association') or {}
                observations.append(dict(observation_id=observation_id, delivery_status=status, error=error,
                    p2_status=receipt.get('status'), association_decision=association.get('decision'),
                    target_id=association.get('target_id'), monitoring=receipt.get('monitoring'),
                    receipt_scope=receipt.get('receipt_scope')))
            return dict(capture_id=key, state=capture_state,
                **{field: metadata.get(field) for field in ('activity_id', 'activity_num', 'config_version', 'binding_version', 'monitoring_version', 'route_rule_id')},
                source_id=metadata['source_id'], camera_id=metadata['camera_id'], frame_id=metadata['frame_id'],
                report=report, observations=observations)

    def send_one(self, endpoint, *, timeout=3, authorization=None, on_receipt=None):
        # A single sender owns this method. Network I/O does not hold the DB lock.
        with self.lock:
            now = time.time()
            paused = [activity for activity, probe in self.blocked.items() if probe > now]
            condition = "status='PENDING' AND next_attempt<=?" + (
                f" AND activity_id NOT IN ({','.join('?' * len(paused))})" if paused else '')
            row = self._next('deliveries', 'id,route,payload,attempts,activity_id', condition, (now, *paused))
        if row is None:
            return False
        key, route, body, attempts, activity = row
        with self.lock:
            if retention_tombstone(self.db, 'deliveries', key):
                return False
        headers = {"Content-Type": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        request = urllib.request.Request(endpoint.rstrip('/')+route, body.encode(), headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(16*1024*1024+1)
            if len(raw) > 16*1024*1024:
                raise ValueError("oversized P2 response")
            reply = json.loads(raw)
            if reply.get("ok") is not True:
                raise ValueError("P2 reply was not an acknowledgement")
            if route.endswith('/observe') and reply.get("observation_id", (reply.get("association") or {}).get("observation_id")) != json.loads(body)["observation"]["observation_id"]:
                raise ActivityContextError("P2 observation acknowledgement ID mismatch")
            original = json.loads(body)
            verify_activity_receipt(original, reply, require_route=not route.endswith('/persons'))
            if route.endswith('/persons') and (reply.get('person', {}).get('person_id') != original['person_id'] or reply.get('submitted_revision') != original['revision']):
                raise ActivityContextError('P2 person acknowledgement ID/revision mismatch')
            if route.endswith('/gate-event') and reply.get('event_id') != original['event_id']:
                raise ActivityContextError('P2 gate acknowledgement ID mismatch')
        except Exception as error:
            status = "QUARANTINED" if isinstance(error, ActivityContextError) else "PENDING"
            if isinstance(error, urllib.error.HTTPError):
                # Permanent bad events are isolated, retaining their original payload.
                # A full/dead peer or unavailable credentials can recover later.
                if error.code in (400, 404, 409, 410, 413, 422):
                    status = "QUARANTINED"
                text = error.read(2048).decode('utf-8', errors='replace')
                capacity = error.code == 503 and _p2_capacity(text)
                detail = f"P2 HTTP {error.code}: {text}"
            else:
                capacity, detail = False, type(error).__name__
            retry = time.time()+min(30, 0.5*2**min(attempts, 6))
            with self.lock, self.db:
                self.db.execute("UPDATE deliveries SET status=?,attempts=attempts+1,next_attempt=?,error=? WHERE id=?",
                                (status, retry, detail, key))
                if capacity:
                    # Pause the whole activity until this retry; then one due delivery probes.
                    self.blocked[activity] = retry
                if isinstance(error, ActivityContextError):
                    self.db.execute('UPDATE deliveries SET receipt=? WHERE id=?', (encoded(reply), key))
            return True
        with self.lock, self.db:
            retention_write_guard(self.db)
            if retention_tombstone(self.db, 'deliveries', key) or self.db.execute('SELECT 1 FROM deliveries WHERE id=?', (key,)).fetchone() is None:
                return True
            self.db.execute("UPDATE deliveries SET status='ACKNOWLEDGED',receipt=?,error=NULL WHERE id=?", (encoded(reply), key))
            self.blocked.pop(activity, None)
        if on_receipt:
            on_receipt(json.loads(body), reply)
        return True

    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None

    def reopen(self):
        """Resume after C++ maintenance with no old SQLite or scheduling cache."""
        with self.lock:
            if self.db is not None:
                raise RuntimeError('state is already open')
            self.db = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
            self.db.execute('PRAGMA synchronous=FULL')
            if self.db.execute('PRAGMA page_count').fetchone()[0] > self.page_limit:
                self.db.close()
                self.db = None
                raise ValueError('state exceeds the configured storage limit after maintenance')
            self.db.execute(f'PRAGMA max_page_count={self.page_limit}')
            self.cursors = {'captures': 0, 'deliveries': 0}
            self.activity_cursors.clear()
            self.blocked.clear()
