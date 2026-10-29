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
import sys
import ctypes

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
        self.published = {}
        self.evicted_files = 0
        self.evicted_bytes = 0
        self.quarantined = 0
        self.last_error = None
        self.ready = threading.Event()
        self.external_roots = []
        self.next_discovery = 0
        self.notification_fd=None
        self.notification_roots={}
        self.notification_paths={}
        self.notification_rescan=True
        if sys.platform=='linux':
            try:
                libc=ctypes.CDLL(None,use_errno=True)
                libc.inotify_init1.argtypes=[ctypes.c_int];libc.inotify_init1.restype=ctypes.c_int
                libc.inotify_add_watch.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_uint32]
                libc.inotify_add_watch.restype=ctypes.c_int
                fd=libc.inotify_init1(os.O_NONBLOCK|os.O_CLOEXEC)
                if fd>=0:self.notification_fd=fd;self.notification_libc=libc
            except (OSError,AttributeError):pass
        # Capture files have an exclusive atomic publication step. Interrupted
        # temporary writes are not acknowledged and never enter the import queue.
        files = sorted(self.root.glob('*/*.cap'), key=lambda p:p.stat().st_mtime_ns)
        for path in files:
            self.pending.append(path)
            self.available.add(path)
            self.sizes[path] = path.stat().st_size
            self.published[path] = path.stat().st_mtime
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
                    self.published[path]=time.time()
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
                        self.published[path]=time.time()
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

    def _watch_publications(self,root):
        root=Path(root)
        if root in self.notification_paths:return True
        # The producer publishes by atomic rename. Directory creation matters
        # only for legacy bucketed .cap producers.
        wd=self.notification_libc.inotify_add_watch(self.notification_fd,os.fsencode(root),0x80|0x100)
        if wd<0:return False
        self.notification_paths[root]=wd;self.notification_roots[wd]=root
        self.notification_rescan=True
        return True

    def _publication_events(self):
        paths=[]
        for _ in range(8):
            try:raw=os.read(self.notification_fd,65536)
            except BlockingIOError:break
            except OSError:
                self.close();self.notification_rescan=True;break
            position=0
            while position+16<=len(raw):
                wd,mask,_,length=struct.unpack_from('iIII',raw,position);position+=16
                name=os.fsdecode(raw[position:position+length].split(b'\0',1)[0]);position+=length
                if mask&0x4000:self.notification_rescan=True;continue
                root=self.notification_roots.get(wd)
                if root is None:continue
                if mask&0x8000:
                    self.notification_roots.pop(wd,None);self.notification_paths.pop(root,None)
                    self.notification_rescan=True;continue
                path=root/name
                if mask&0x40000000:
                    self._watch_publications(path);self.notification_rescan=True
                elif mask&0x80 and path.suffix in ('.batch','.cap'):
                    paths.append(path)
        return paths

    def close(self):
        if self.notification_fd is not None:
            os.close(self.notification_fd);self.notification_fd=None

    def discover_external(self):
        """Import atomic files from acquisition-only processes, never pixels over IPC."""
        now=time.monotonic()
        event_paths=[]
        notifications_ready=self.notification_fd is not None and bool(self.external_roots)
        if self.notification_fd is not None:
            for root in list(self.external_roots):
                if not self._watch_publications(root):notifications_ready=False
            event_paths=self._publication_events()
        scan=self.notification_rescan if notifications_ready else now>=self.next_discovery
        if not scan and not event_paths:return
        self.next_discovery=now+2
        found=[]
        with self.lock:known_paths=set(self.sizes)
        for path in event_paths:
            if path in known_paths:continue
            try:
                stat=path.stat();found.append((stat.st_mtime_ns,path,stat.st_size));known_paths.add(path)
            except FileNotFoundError:pass
        for root in list(self.external_roots) if scan else []:
            # One directory pass, and one stat only for an unknown file. A
            # large flat batch directory must not be walked twice per import.
            try:entries=os.scandir(root)
            except FileNotFoundError:continue
            with entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith('.batch'):
                        paths=[Path(entry.path)]
                    elif entry.is_dir(follow_symlinks=False):
                        if self.notification_fd is not None:self._watch_publications(Path(entry.path))
                        paths=Path(entry.path).glob('*.cap')
                    else:
                        continue
                    for path in paths:
                        if path in known_paths:continue
                        try:
                            stat=path.stat();found.append((stat.st_mtime_ns,path,stat.st_size))
                        except FileNotFoundError:pass
        with self.lock:
            for modified,path,size in sorted(found):
                if path in self.sizes:continue
                self.sizes[path]=size;self.bytes+=size;self.pending.append(path)
                self.published[path]=modified/1e9
                self.available.add(path)
                root=next((Path(root) for root in self.external_roots if path.is_relative_to(root)),None)
                if root is not None:self.external_pending.setdefault(root,deque()).append(path)
            if found:self.ready.set()
        # A slow scan must still leave time to import its discovered batches.
        self.notification_rescan=False
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
        with self.lock:
            self.bytes-=self.sizes.pop(path)
            self.published.pop(path,None)

    def prune_cache(self, *, max_mb, max_age_seconds, max_files=4096):
        """Pending evidence is not disposable cache, regardless of age/quota.

        Storage pressure must backpressure producers. Only acknowledge() after
        durable SQLite import may unlink an ingress original.
        """
        if type(max_mb) is not int or max_mb < 1 or max_age_seconds <= 0:
            raise ValueError('positive bounded cache retention required')
        return dict(files=0,bytes=0,pending_protected=True)


    def quarantine(self,path,reason):
        path=Path(path)
        os.replace(path,path.with_suffix('.quarantined'))
        self._sync_directory(path.parent)
        with self.lock:
            self.sizes.pop(path,None)
            self.published.pop(path,None)
            self.quarantined+=1
            self.last_error=reason

    def status(self):
        with self.lock:
            return dict(pending=len(self.sizes),queued=len(self.available),bytes=self.bytes,
                        max_bytes=self.max_bytes,quarantined=self.quarantined,last_error=self.last_error,
                        cache_evicted_files=self.evicted_files,cache_evicted_bytes=self.evicted_bytes)
