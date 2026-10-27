"""Durable camera ingress independent of the inference SQLite mutex.

An acknowledged file survives a restart. Queue capacity never overwrites an
older event; failed imports retain the original evidence for retry or review.
"""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import threading
import time
import uuid

from face_engine.trajectory_bridge import encoded, scoped_event_key, EventConflict


MAGIC = b'FACEIN01'


class CaptureIngress:
    def __init__(self, directory, *, max_mb=8192, min_free_mb=1024):
        if type(max_mb) is not int or max_mb < 1 or type(min_free_mb) is not int or min_free_mb < 0:
            raise ValueError('positive ingress capacity and nonnegative disk reserve required')
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes, self.min_free = max_mb*1024*1024, min_free_mb*1024*1024
        self.lock = threading.Lock()
        self.shards = [threading.Lock() for _ in range(64)]
        self.pending = deque()
        self.sizes = {}
        self.quarantined = 0
        self.last_error = None
        self.ready = threading.Event()
        # Capture files have an exclusive atomic publication step. Interrupted
        # temporary writes are not acknowledged and never enter the import queue.
        files = sorted(self.root.glob('*/*.cap'), key=lambda p:p.stat().st_mtime_ns)
        for path in files:
            self.pending.append(path)
            self.sizes[path] = path.stat().st_size
        self.quarantined = sum(1 for _ in self.root.glob('*/*.quarantined'))
        self.bytes = sum(self.sizes.values()) + sum(p.stat().st_size for p in self.root.glob('*/*.quarantined'))
        if self.pending:self.ready.set()

    @staticmethod
    def _sync_directory(path):
        if os.name == 'nt':return
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:os.fsync(fd)
        finally:os.close(fd)

    @staticmethod
    def read(path):
        with Path(path).open('rb') as stream:
            if stream.read(8) != MAGIC:raise ValueError('invalid ingress file magic')
            size=stream.read(4)
            if len(size)!=4:raise ValueError('truncated ingress header')
            length = struct.unpack('>I', size)[0]
            if length > 256*1024:raise ValueError('ingress metadata exceeds limit')
            header = json.loads(stream.read(length))
            body = stream.read(12*1024*1024+1)
        if not body or len(body)>12*1024*1024 or hashlib.sha256(body).hexdigest()!=header['image_sha256']:
            raise ValueError('ingress image integrity check failed')
        if scoped_event_key('capture',header['metadata'],'frame_id') != header['key']:
            raise ValueError('ingress activity/source/event identity differs')
        return header, body

    def push(self, body, metadata, category='NORMAL'):
        if not isinstance(body,bytes) or not 0<len(body)<=12*1024*1024:
            raise ValueError('ingress image size outside limit')
        key = scoped_event_key('capture',metadata,'frame_id')
        immutable = {k:v for k,v in metadata.items() if k not in ('received_at_unix_ms','camera_name')}
        digest = hashlib.sha256(encoded(immutable).encode()+body).hexdigest()
        identity = hashlib.sha256(key.encode()).hexdigest()
        folder = self.root/identity[:2]
        try:
            folder.mkdir(mode=0o700)
            self._sync_directory(self.root)
        except FileExistsError:pass
        path = folder/(identity+'.cap')
        header = dict(key=key,metadata=metadata,category=category,hash=digest,
                      image_sha256=hashlib.sha256(body).hexdigest(),created=time.time())
        raw = encoded(header).encode()
        if len(raw)>256*1024:raise ValueError('ingress metadata exceeds limit')
        size = 12+len(raw)+len(body)
        with self.shards[int(identity[:2],16)%len(self.shards)]:
            if path.exists():
                previous,_ = self.read(path)
                if previous['hash']!=digest:raise EventConflict('conflicting ingress event')
                return key
            with self.lock:
                if self.bytes+size>self.max_bytes or shutil.disk_usage(self.root).free < self.min_free+size:
                    self.last_error='CAPACITY: durable ingress storage is full'
                    raise RuntimeError(self.last_error)
                self.bytes+=size
            temporary=folder/(identity+'.'+uuid.uuid4().hex+'.partial')
            published=False
            try:
                with temporary.open('xb') as stream:
                    if os.name!='nt':os.chmod(temporary,0o600)
                    stream.write(MAGIC+struct.pack('>I',len(raw))+raw+body)
                    stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,path)
                published=True
                self._sync_directory(folder)
                with self.lock:
                    self.sizes[path]=size
                    self.pending.append(path)
                    self.ready.set()
                return key
            except BaseException:
                if not published:
                    with self.lock:self.bytes-=size
                else:
                    with self.lock:
                        self.sizes[path]=size
                        self.pending.append(path)
                        self.ready.set()
                # A published file with a directory-sync failure is recovered
                # on restart. Never erase possibly acknowledged evidence.
                if temporary.exists():temporary.unlink()
                raise

    def claim(self):
        with self.lock:
            if not self.pending:
                self.ready.clear()
                return None
            return self.pending.popleft()

    def retry(self,path):
        with self.lock:
            self.pending.append(path)
            self.ready.set()

    def acknowledge(self,path):
        path=Path(path)
        path.unlink()
        self._sync_directory(path.parent)
        with self.lock:self.bytes-=self.sizes.pop(path)

    def quarantine(self,path,reason):
        path=Path(path)
        os.replace(path,path.with_suffix('.quarantined'))
        self._sync_directory(path.parent)
        with self.lock:
            self.sizes.pop(path,None)
            self.quarantined+=1
            self.last_error=reason

    def status(self):
        with self.lock:
            return dict(pending=len(self.sizes),queued=len(self.pending),bytes=self.bytes,
                        max_bytes=self.max_bytes,quarantined=self.quarantined,last_error=self.last_error)
