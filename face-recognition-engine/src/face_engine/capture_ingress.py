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
    def __init__(self, directory, *, max_mb=8192, min_free_mb=1024, bucket_digits=2):
        if type(max_mb) is not int or max_mb < 1 or type(min_free_mb) is not int or min_free_mb < 0:
            raise ValueError('positive ingress capacity and nonnegative disk reserve required')
        self.root = Path(directory).resolve()
        if bucket_digits not in (1,2):raise ValueError('invalid ingress bucket size')
        self.bucket_digits = bucket_digits
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes, self.min_free = max_mb*1024*1024, min_free_mb*1024*1024
        self.lock = threading.Lock()
        self.shards = [threading.Lock() for _ in range(64)]
        self.pending = deque()
        self.available=set()
        self.external_pending={}
        self.fresh_root=0
        self.fresh_turn=False
        self.sizes = {}
        self.quarantined = 0
        self.last_error = None
        self.ready = threading.Event()
        self.external_roots = []
        self.next_discovery = 0
        # Capture files have an exclusive atomic publication step. Interrupted
        # temporary writes are not acknowledged and never enter the import queue.
        files = sorted(self.root.glob('*/*.cap'), key=lambda p:p.stat().st_mtime_ns)
        for path in files:
            self.pending.append(path)
            self.available.add(path)
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
        folder = self.root/identity[:self.bucket_digits]
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
                # An external importer may have acknowledged this producer's
                # files. Reconcile only when approaching its storage quota.
                if self.bytes+size>self.max_bytes:
                    removed=[p for p in self.sizes if not p.exists()]
                    for p in removed:self.bytes-=self.sizes.pop(p)
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
                    self.available.add(path)
                    self.ready.set()
                return key
            except BaseException:
                if not published:
                    with self.lock:self.bytes-=size
                else:
                    with self.lock:
                        self.sizes[path]=size
                        self.pending.append(path)
                        self.available.add(path)
                        self.ready.set()
                # A published file with a directory-sync failure is recovered
                # on restart. Never erase possibly acknowledged evidence.
                if temporary.exists():temporary.unlink()
                raise

    def claim(self):
        with self.lock:
            self.fresh_turn=not self.fresh_turn
            roots=list(self.external_pending)
            if self.fresh_turn and roots:
                for _ in roots:
                    root=roots[self.fresh_root%len(roots)];self.fresh_root+=1
                    queue=self.external_pending[root]
                    while queue:
                        path=queue.pop()
                        if path in self.available:
                            self.available.remove(path);return path
            while self.pending:
                path=self.pending.popleft()
                if path in self.available:
                    self.available.remove(path);return path
            if not self.available:
                self.ready.clear()
            return None

    def discover_external(self):
        """Import atomic files from acquisition-only processes, never pixels over IPC."""
        now=time.monotonic()
        if now<self.next_discovery:return
        self.next_discovery=now+2
        found=[]
        with self.lock:known_paths=set(self.sizes)
        for root in list(self.external_roots):
            # One directory pass, and one stat only for an unknown file. A
            # large flat batch directory must not be walked twice per import.
            try:entries=os.scandir(root)
            except FileNotFoundError:continue
            with entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith('.batch'):
                        paths=[Path(entry.path)]
                    elif entry.is_dir(follow_symlinks=False):
                        paths=Path(entry.path).glob('*.cap')
                    else:
                        continue
                    for path in paths:
                        if path in known_paths:continue
                        try:
                            stat=path.stat();found.append((stat.st_mtime_ns,path,stat.st_size))
                        except FileNotFoundError:pass
        with self.lock:
            for _,path,size in sorted(found):
                if path in self.sizes:continue
                self.sizes[path]=size;self.bytes+=size;self.pending.append(path)
                self.available.add(path)
                root=next((Path(root) for root in self.external_roots if path.is_relative_to(root)),None)
                if root is not None:self.external_pending.setdefault(root,deque()).append(path)
            if found:self.ready.set()
        # A slow scan must still leave time to import its discovered batches.
        self.next_discovery=time.monotonic()+2

    @staticmethod
    def read_batch(path):
        records=[];total=0
        with Path(path).open('rb') as stream:
            if stream.read(8)!=b'FACEBT01':raise ValueError('invalid acquisition batch')
            while True:
                size=stream.read(8)
                if not size:break
                if len(size)!=8 or len(records)>=128:raise ValueError('invalid batch framing')
                header_size,body_size=struct.unpack('>II',size)
                total+=header_size+body_size
                if not 0<header_size<=256*1024 or not 0<body_size<=12*1024*1024 or total>64*1024*1024:
                    raise ValueError('acquisition batch size exceeded')
                header=json.loads(stream.read(header_size));body=stream.read(body_size)
                if len(body)!=body_size or hashlib.sha256(body).hexdigest()!=header['image_sha256']:
                    raise ValueError('acquisition batch image integrity differs')
                if scoped_event_key('capture',header['metadata'],'frame_id')!=header['key']:
                    raise ValueError('acquisition batch identity differs')
                records.append((header,body))
        if not records:raise ValueError('empty acquisition batch')
        return records

    def retry(self,path):
        with self.lock:
            self.pending.append(path)
            self.available.add(path)
            root=next((Path(root) for root in self.external_roots if path.is_relative_to(root)),None)
            if root is not None:self.external_pending.setdefault(root,deque()).append(path)
            self.ready.set()

    def acknowledge(self,path):
        path=Path(path)
        path.unlink()
        # The SQLite import has already committed durably. If this deletion
        # reappears after power loss, its scoped ID/hash or retention tombstone
        # makes another import safe; no original image evidence can be lost.
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
            return dict(pending=len(self.sizes),queued=len(self.available),bytes=self.bytes,
                        max_bytes=self.max_bytes,quarantined=self.quarantined,last_error=self.last_error)
