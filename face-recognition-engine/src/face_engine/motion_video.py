"""Continuous copy-only video spool, lightweight motion screening, durable decode.

Scheduling design adapted from AegisVision camera_manager/analysis_scheduler
(CC0-1.0, 7ad92210bcfb491dc2090ef7fc73ab27cab8c03d). Identity stays in P1/P2.
Closed video is never overwritten. A moving/uncertain segment is retired only
after every sampled native-size frame has committed to the durable ingress.
"""
from collections import deque
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
import traceback
import cv2

cv2.setNumThreads(1)

from face_engine.acquisition import Frame, _base_metadata
from face_engine.analysis_admission import SceneMotionGate
from face_engine.uuid_sources import FailoverCamera, PrivateRTSPReader


def _write(path, value):
    path=Path(path);temporary=path.with_name(path.name+'.partial')
    with temporary.open('w',encoding='utf-8') as stream:
        if os.name!='nt':os.chmod(temporary,0o600)
        json.dump(value,stream,separators=(',',':'));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)
    if os.name!='nt':
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)


class _SegmentDecodeFailure(RuntimeError):
    def __init__(self, reason, frames_consumed=0):
        super().__init__(reason)
        # Number of original frame indices already committed or excluded by
        # the route start. CPU retry must not replace their encoded bytes.
        self.frames_consumed=frames_consumed


class _HardwareDecodeFailure(_SegmentDecodeFailure):
    pass


class _FairSlots:
    def __init__(self, count):
        self.count=count;self.active=0;self.waiting=deque();self.condition=threading.Condition()
    def acquire(self, stop, maintenance=None):
        ticket=object();next_maintenance=time.monotonic()+1
        with self.condition:self.waiting.append(ticket)
        try:
            while not stop.is_set():
                with self.condition:
                    if self.waiting[0] is ticket and self.active<self.count:
                        self.waiting.popleft();self.active+=1;self.condition.notify_all();return True
                    self.condition.wait(.2)
                # Keep the FIFO ticket while discovering new closed files.
                # Disk IO never holds the shared decode-slot condition lock.
                if maintenance is not None and time.monotonic()>=next_maintenance:
                    maintenance();next_maintenance=time.monotonic()+1
            return False
        finally:
            with self.condition:
                if ticket in self.waiting:self.waiting.remove(ticket);self.condition.notify_all()
    def release(self):
        with self.condition:self.active-=1;self.condition.notify_all()


_SLOTS=_FairSlots(2)  # Per acquisition process; no unbounded executor/future queue.
_FREE_SPACE={}
_CUDA_LOCK=threading.Lock()
_CUDA_READERS={}


def _spool_room(root, maximum, reserve):
    # Recording must never wait on a venue-wide file count or a shared scan
    # lock. max_mb is no longer a recording quota; actual disk reserve remains.
    # The short per-process statvfs cache does not walk or delete any video.
    now=time.monotonic()
    key=str(root);saved=_FREE_SPACE.get(key)
    if saved is None or now-saved[0]>1:
        saved=(now,shutil.disk_usage(root).free);_FREE_SPACE[key]=saved
    return saved[1]>reserve


class MotionVideoCamera:
    def __init__(self, sub_url, main_url, source_id, camera_id, *, on_capture,
                 motion_video, activity_routes=(), ffmpeg='ffmpeg', sample_fps=2,
                 camera_clock=None, connection=None, on_live_frame=None, **legacy):
        self.source_id,self.camera_id=source_id,camera_id;self.sub_url=sub_url;self.main_url=main_url
        self.callback=on_capture;self.ffmpeg=ffmpeg;self.settings=dict(motion_video)
        self.main_only=self.settings.get('main_only',False)
        if type(self.main_only) is not bool:raise ValueError('main_only must be boolean')
        enabled=self.settings.get('live_camera_ids')
        if enabled is not None and (not isinstance(enabled,list) or any(not isinstance(v,str) for v in enabled)):
            raise ValueError('live_camera_ids must be a camera UUID list')
        # Cached source records may predate rollout settings. Only an explicit
        # selection enables native live decoding; '*' expands to all cameras.
        self.live_enabled=enabled is not None and ('*' in enabled or camera_id in enabled)
        self.on_live_frame=on_live_frame;self.live_frame=None;self.live_last_published=0;self.live_consumed_id=None
        self.camera_clock=camera_clock;self.connection=connection
        self.live_preferred_stream='main' if self.main_only else self.settings.get('live_preferred_stream','main')
        if self.live_preferred_stream not in ('main','sub'):raise ValueError('live_preferred_stream must be main or sub')
        self.cuda_reader_capacity=self.settings.get('cuda_readers_per_device_per_process',1)
        if type(self.cuda_reader_capacity) is not int or not 1<=self.cuda_reader_capacity<=16:
            raise ValueError('cuda_readers_per_device_per_process must be 1..16')
        devices=self.settings.get('cuda_decode_devices',[])
        if not isinstance(devices,list) or any(type(d) is not int or d<0 for d in devices):raise ValueError('invalid CUDA decode devices')
        self.cuda_device=devices[int(hashlib.sha256(camera_id.encode()).hexdigest()[-8:],16)%len(devices)] if devices else None
        self.hardware_ffmpeg=self.settings.get('hardware_ffmpeg',ffmpeg)
        self.fps=float(self.settings.get('sample_fps',2));self.segment_seconds=float(self.settings.get('segment_seconds',4))
        if not .5<=self.fps<=5 or not 2<=self.segment_seconds<=10:raise ValueError('invalid motion-video cadence')
        self.root=Path(self.settings['directory'])
        if not self.root.is_absolute():raise ValueError('motion video spool must be absolute')
        self.folder=self.root/hashlib.sha256(camera_id.encode()).hexdigest();self.folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        identity=dict(schema=1,camera_id=camera_id,source_id=source_id)
        path=self.folder/'camera.json'
        if path.exists():
            if json.loads(path.read_text())!=identity:raise ValueError('motion video UUID identity conflict')
        else:_write(path,identity)
        self.routes=self._routes(activity_routes);self.route_signature=json.dumps(self.routes,sort_keys=True)
        self.stop_event=threading.Event();self.rotate=threading.Event();self.lock=threading.RLock()
        self.threads=[];self.recorder=None;self.motion_reader=None;self.decoder=None
        self.recording_session=None;self.next_recover=0;self.last_forced_check=0
        self.last_forced_old_check=0;self.prefer_recent=True
        self.history=deque(maxlen=4096);self.offsets={};self.last_frame_at=None
        self.discovery_cursor=0;self.recovery_scan=None
        self.stats=dict(state='NEW',frames=0,persisted_candidates=0,idle_frames=0,inbox_errors=0,
            capture_errors=0,last_error=None,stream_kind='main',recorded_segments=0,
            motion_segments=0,idle_segments=0,decoded_segments=0,decoded_frames=0,
            pending_video_segments=0,retained_unprocessed=True,durable_candidates=True)
    @staticmethod
    def _routes(rows):
        now=time.time()*1000;mono=time.monotonic();result=[]
        for row in rows:
            result.append(dict(context=dict(row['context']),
                since_unix_ms=int(row.get('since_unix_ms',now+(row['since_monotonic']-mono)*1000)),
                category=row.get('category','NORMAL')))
        return result
    def update_routes(self, rows):
        routes=self._routes(rows)
        # Millisecond conversion noise must not rotate an unchanged route.
        signature=json.dumps([dict(context=r['context'],category=r['category']) for r in routes],sort_keys=True)
        with self.lock:
            old=json.dumps([dict(context=r['context'],category=r['category']) for r in self.routes],sort_keys=True)
            if signature!=old:self.routes=routes;self.rotate.set()
    def start(self):
        if self.threads:return self
        for label,fn in [('record',self._record),('motion',self._motion),('segments',self._manage)]:
            thread=threading.Thread(target=fn,name='motion-video-'+label,daemon=True);self.threads.append(thread);thread.start()
        return self
    @staticmethod
    def _terminate(process):
        if process and process.poll() is None:
            try:process.terminate()
            except ProcessLookupError:pass
            try:process.wait(timeout=4)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
        if process and process.stdout:process.stdout.close()
    def _record(self):
        attempt=0
        self.stop_event.wait(int(hashlib.sha256(self.camera_id.encode()).hexdigest()[:4],16)/65535*5)
        while not self.stop_event.is_set():
            process=None;transport=None
            try:
                if not _spool_room(self.root,int(self.settings.get('max_mb',524288))*1024**2,
                                   int(self.settings.get('min_free_mb',65536))*1024**2):
                    with self.lock:self.stats.update(state='STORAGE_BACKPRESSURE',last_error='VIDEO_SPOOL_FULL_PENDING_RETAINED')
                    self.stop_event.wait(2);continue
                if self.main_only:
                    kind='main';url=self.main_url
                    if not url:
                        with self.lock:self.stats.update(state='MISSING_MAIN_SOURCE',last_error='MAIN_URL_MISSING')
                        self.stop_event.wait(2);continue
                else:
                    kind='main' if self.main_url and attempt%3!=2 else 'sub';url=self.main_url if kind=='main' else self.sub_url
                    if not url:kind='main';url=self.main_url
                session=uuid.uuid4().hex;self.rotate.clear()
                with self.lock:routes=list(self.routes)
                _write(self.folder/(session+'.session.json'),dict(schema=1,camera_id=self.camera_id,
                    source_id=self.source_id,stream_kind=kind,routes=routes,session_id=session,
                    started_unix_ms=int(time.time()*1000)))
                command=[self.ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-threads','1',
                    '-protocol_whitelist','pipe,rtsp,rtsps,tcp,tls,udp,rtp','-f','concat','-safe','0','-i','pipe:0',
                    '-map','0:v:0','-an','-sn','-dn','-c:v','copy','-f','segment','-segment_format','matroska',
                    '-segment_time',str(self.segment_seconds),'-reset_timestamps','1','-strftime','1',
                    '-segment_list',str(self.folder/(session+'.csv')),'-segment_list_type','csv',
                    str(self.folder/(session+'-%s.mkv'))]
                transport=PrivateRTSPReader(url,self.source_id,self.camera_id,kind,ffmpeg=self.ffmpeg,
                    first_frame_timeout_s=20,stall_timeout_s=20)
                process=transport._spawn(command);transport._release_connect()
                with self.lock:self.recorder=process;self.recording_session=session;self.stats.update(state='CONNECTING',stream_kind=kind)
                started=time.monotonic();seen=False
                while not self.stop_event.wait(.5) and not self.rotate.is_set() and process.poll() is None:
                    seen=seen or any(p.stat().st_size>4096 for p in self.folder.glob(session+'-*.mkv'))
                    if seen:
                        with self.lock:self.stats['state']='RECORDING'
                    if not seen and time.monotonic()-started>22:break
                    if kind=='sub' and time.monotonic()-started>60:break
                    if not _spool_room(self.root,int(self.settings.get('max_mb',524288))*1024**2,
                                       int(self.settings.get('min_free_mb',65536))*1024**2):break
                if seen:attempt=0
                else:
                    attempt+=1
                    with self.lock:self.stats.update(state='RECONNECTING',last_error='NO_VIDEO_BYTES_MAIN_RETRY' if self.main_only else 'NO_VIDEO_BYTES_MAIN_SUB_RETRY')
            except Exception:
                attempt+=1
                with self.lock:self.stats['capture_errors']+=1;self.stats['last_error']='VIDEO_RECORDING_RETRY'
            finally:
                self._terminate(process)
                if transport:transport._release_connect()
                with self.lock:self.recorder=None;self.recording_session=None
            self.stop_event.wait(1)
    def _motion(self):
        if not self.live_enabled:return self._motion_preview()
        reader=None;last=None;gate=SceneMotionGate();cuda_slot=None;device=None
        if self.main_only and not self.main_url:
            with self.lock:self.stats.update(state='MISSING_MAIN_SOURCE',last_error='MAIN_URL_MISSING')
            return
        try:
            if self.cuda_device is not None:
                with _CUDA_LOCK:slot=_CUDA_READERS.setdefault(self.cuda_device,threading.BoundedSemaphore(self.cuda_reader_capacity))
                if slot.acquire(blocking=False):cuda_slot=slot;device=self.cuda_device
            reader=FailoverCamera(None if self.main_only else self.sub_url,self.main_url,self.source_id,self.camera_id,
                ffmpeg=self.ffmpeg,sample_fps=self.fps,preferred_stream=self.live_preferred_stream,decode_keyframes_only=False)
            if device is not None:
                def native_reader(*args,**kwargs):
                    kwargs.update(ffmpeg=self.hardware_ffmpeg,decode_cuda_device=device,cpu_ffmpeg=self.ffmpeg,
                        concat_input_options=self.settings.get('hardware_concat_options',True))
                    return PrivateRTSPReader(*args,**kwargs)
                reader.reader_type=native_reader
            with self.lock:self.motion_reader=reader
            reader.start()
            idle_seconds=float(self.settings.get('idle_face_check_seconds',60))
            while not self.stop_event.is_set():
                frame=reader.latest(after_frame_id=last,max_age_s=2)
                if frame is None:
                    self.stop_event.wait(.02);continue
                last=frame.metadata['frame_id'];active=gate.admit(frame.image_bgr,frame.received_monotonic)
                with self.lock:
                    self.history.append((frame.metadata['received_at_unix_ms'],active));self.stats['frames']+=1
                    self.stats['idle_frames']+=not active;self.last_frame_at=frame.received_monotonic
                if not active and frame.received_monotonic-self.live_last_published<idle_seconds:continue
                if frame.encoded_image is None:continue
                meta=dict(frame.metadata,capture_origin='LIVE_MOTION_VIDEO',motion_scene_active=active,
                    detection_native_tiles=frame.metadata.get('stream_kind')=='main')
                if self.camera_clock:
                    profile=self.camera_clock.for_capture(self.camera_id,self.connection)
                    if profile:meta['camera_clock_profile']=profile
                # Keep the decoder's JPEG. MotionGate alone shrinks its own working image.
                current=Frame(None,meta,frame.received_monotonic,frame.encoded_image)
                with self.lock:
                    self.live_frame=current;self.live_last_published=frame.received_monotonic
                    self.stats['live_published']=self.stats.get('live_published',0)+1
                if self.on_live_frame:self.on_live_frame(current)
        except Exception:
            with self.lock:self.stats['last_error']='LIVE_FEED_UNAVAILABLE_MAIN_VIDEO_RETAINED'
        finally:
            if reader:reader.stop()
            if cuda_slot:cuda_slot.release()
    def _motion_preview(self):
        reader=None;last=None;gate=SceneMotionGate();cuda_slot=None;device=None
        if self.main_only and not self.main_url:
            with self.lock:self.stats.update(state='MISSING_MAIN_SOURCE',last_error='MAIN_URL_MISSING')
            return
        try:
            if self.cuda_device is not None:
                # A configured context budget per device/process bounds decoder VRAM.
                # Remaining cameras keep acquiring with the software decoder.
                with _CUDA_LOCK:slot=_CUDA_READERS.setdefault(self.cuda_device,threading.BoundedSemaphore(self.cuda_reader_capacity))
                if slot.acquire(blocking=False):cuda_slot=slot;device=self.cuda_device
            preview_url=self.main_url if self.main_only else (self.sub_url or self.main_url)
            preview_kind='main' if self.main_only or not self.sub_url else 'sub'
            reader=PrivateRTSPReader(preview_url,self.source_id,self.camera_id,preview_kind,
                ffmpeg=self.hardware_ffmpeg if device is not None else self.ffmpeg,
                decode_cuda_device=device,cpu_ffmpeg=self.ffmpeg,motion_preview=True,
                concat_input_options=self.settings.get('hardware_concat_options',True) if device is not None else True,
                sample_fps=2,decode_keyframes_only=False,first_frame_timeout_s=20,stall_timeout_s=20)
            with self.lock:self.motion_reader=reader
            reader.start()
            while not self.stop_event.is_set():
                frame=reader.wait_latest(.5,after_frame_id=last)
                if frame is None:continue
                last=frame.metadata['frame_id'];active=gate.admit(frame.image_bgr,frame.received_monotonic)
                with self.lock:
                    self.history.append((frame.metadata['received_at_unix_ms'],active));self.stats['frames']+=1
                    self.stats['idle_frames']+=not active;self.last_frame_at=frame.received_monotonic
        except Exception:
            with self.lock:self.stats['last_error']='MOTION_FEED_UNAVAILABLE_MAIN_VIDEO_RETAINED'
        finally:
            if reader:reader.stop()
            if cuda_slot:cuda_slot.release()
    def _active(self, start, end):
        # Missing motion history is not evidence of an empty scene.
        with self.lock:history=list(self.history)
        lower,upper=start-3000,end+3000
        if not history or history[0][0]>lower or history[-1][0]<upper:return True,'MOTION_COVERAGE_UNCERTAIN'
        selected=[x for x in history if lower-1500<=x[0]<=upper+1500]
        if any(b[0]-a[0]>1500 for a,b in zip(selected,selected[1:])):return True,'MOTION_FEED_GAP'
        return any(active and lower<=at<=upper for at,active in selected),'MOTION_SCREENED'
    def _discover(self):
        # A large old CSV must not hide today's closed video. Give the active
        # session a bounded turn, then rotate through historical sessions.
        with self.lock:active=self.recording_session
        listings=list(self.folder.glob('*.csv'))
        current=self.folder/(active+'.csv') if active else None
        history=sorted(listings,key=lambda p:p.name)
        selected=[(current,True)] if current is not None and current.exists() else []
        if history:
            self.discovery_cursor%=len(history)
            selected.append((history[self.discovery_cursor],False));self.discovery_cursor+=1
        deadline=time.monotonic()+.25
        for listing,tail in selected:
            session_path=listing.with_suffix('.session.json')
            if not session_path.exists():continue
            data=json.loads(session_path.read_text())
            position=self.offsets.get(str(listing),0)
            with listing.open('rb') as stream:
                if tail:
                    # Register the newest closed rows immediately while the
                    # normal cursor still retains every older row for catchup.
                    offset=max(0,listing.stat().st_size-32768);stream.seek(offset)
                    if offset:stream.readline()
                    lines=iter([line for line in stream.readlines() if line.endswith(b'\n')][-16:])
                else:stream.seek(position)
                for index in range(16):
                    # Always allow each selected session one row so old work
                    # advances even when disk sync takes the entire budget.
                    if index and time.monotonic()>=deadline:break
                    line=next(lines,b'') if tail else stream.readline()
                    if not line or not line.endswith(b'\n'):break
                    fields=next(csv.reader([line.decode('utf-8')]));video=Path(fields[0]);video=video if video.is_absolute() else self.folder/video
                    if video.parent!=self.folder or video.is_symlink():raise ValueError('segment escaped source folder')
                    if video.exists():
                        job=video.with_suffix('.job.json')
                        if not job.exists() and not video.with_suffix('.done.json').exists():
                            duration=max(0,float(fields[2])-float(fields[1]));epoch=int(video.stem.rsplit('-',1)[1])*1000
                            with video.open('rb') as original:os.fsync(original.fileno())
                            stat=video.stat()
                            _write(job,dict(data,video=video.name,start_unix_ms=max(epoch,data.get('started_unix_ms',epoch)),
                                duration_ms=int(duration*1000),size=stat.st_size,mtime_ns=stat.st_mtime_ns,status='PENDING',decode_backend='pending'))
                            with self.lock:self.stats['recorded_segments']+=1
                    if not tail:self.offsets[str(listing)]=stream.tell()
        with self.lock:self.stats['discovery_at_unix_ms']=int(time.time()*1000)
    def _retire(self, path, job, state, frames=0):
        video=self.folder/job['video'];stat=video.stat()
        if stat.st_size!=job['size'] or stat.st_mtime_ns!=job['mtime_ns']:raise ValueError('closed segment changed')
        done=video.with_suffix('.done.json');_write(done,dict(status=state,video=job['video'],frames=frames,
            size=job['size'],mtime_ns=job['mtime_ns'],processing_completed=True))
        video.unlink();path.unlink();done.unlink()
    def _recover_commits(self):
        for marker in self.folder.glob('*.done.json'):
            value=json.loads(marker.read_text())
            if value.get('processing_completed') is not True:continue
            video=self.folder/value['video']
            if video.parent.resolve()!=self.folder.resolve():raise ValueError('invalid completion path')
            if video.exists():
                stat=video.stat()
                if stat.st_size!=value['size'] or stat.st_mtime_ns!=value['mtime_ns']:raise ValueError('completed video changed')
                video.unlink()
            job=video.with_suffix('.job.json')
            if job.exists():job.unlink()
            marker.unlink()
    def _recover_unlisted(self):
        # A crash can leave a readable final MKV before the CSV close record.
        # Retain unreadable orphans and conservatively analyze readable ones.
        if time.monotonic()<self.next_recover:return
        self.next_recover=time.monotonic()+30
        with self.lock:active=self.recording_session
        if self.recovery_scan is None:self.recovery_scan=iter(self.folder.glob('*.mkv'))
        # One probe per turn; resume the directory walk next time rather than
        # blocking closed-segment dispatch on every orphan in the archive.
        for _ in range(64):
            video=next(self.recovery_scan,None)
            if video is None:self.recovery_scan=None;break
            session=video.stem.split('-',1)[0]
            try:modified=video.stat().st_mtime
            except FileNotFoundError:continue  # A resumed scan may pass a committed, retired file.
            if session==active or time.time()-modified<30:continue
            path=video.with_suffix('.job.json')
            if path.exists() or video.with_suffix('.done.json').exists():continue
            session_path=self.folder/(session+'.session.json')
            if not session_path.exists():continue
            sibling=Path(self.ffmpeg).with_name('ffprobe')
            probe_binary=str(sibling) if sibling.is_file() else shutil.which('ffprobe')
            if not probe_binary:continue  # Preserve the orphan; ordinary closed clips still progress.
            probe=subprocess.run([probe_binary,'-v','error',
                '-show_entries','format=duration','-of','json',str(video)],capture_output=True,text=True,timeout=8)
            if probe.returncode:break
            duration=float(json.loads(probe.stdout).get('format',{}).get('duration',0))
            if not math.isfinite(duration) or duration<=0:break
            data=json.loads(session_path.read_text());stat=video.stat();epoch=int(video.stem.rsplit('-',1)[1])*1000
            _write(path,dict(data,video=video.name,start_unix_ms=max(epoch,data.get('started_unix_ms',epoch)),
                duration_ms=int(duration*1000),size=stat.st_size,mtime_ns=stat.st_mtime_ns,status='PENDING',decode_backend='pending',
                active=True,motion_decision='RECOVERED_VIDEO_MOTION_UNKNOWN'))
            break
    def _decode(self, path, job):
        cuda_slot=None;device=None
        # Legacy jobs may already contain CPU-encoded committed frames.
        # New jobs explicitly opt into selecting their first decode backend.
        backend=job.get('decode_backend','cpu')
        resume_frame=int(job.get('resume_frame',0))
        wanted=job.get('decode_cuda_device') if backend=='cuda' else self.cuda_device
        try:
            if backend=='cuda' or (backend=='pending' and job.get('stream_kind')=='main' and wanted is not None):
                with _CUDA_LOCK:
                    slot=_CUDA_READERS.setdefault(wanted,threading.BoundedSemaphore(self.cuda_reader_capacity))
                if slot.acquire(blocking=False):cuda_slot=slot;device=wanted
                elif backend=='cuda':
                    # A persisted CUDA prefix must never silently become CPU
                    # output under the same frame IDs after a restart.
                    with self.lock:self.stats['hardware_durable_slot_waits']=self.stats.get('hardware_durable_slot_waits',0)+1
                    return
                else:
                    with self.lock:self.stats['hardware_durable_capacity_fallbacks']=self.stats.get('hardware_durable_capacity_fallbacks',0)+1
            selected='cuda' if device is not None else 'cpu'
            if backend=='pending' or 'decode_backend' not in job:
                job.update(decode_backend=selected,decode_cuda_device=device,resume_frame=resume_frame)
                _write(path,job)
            if device is not None:
                with self.lock:self.stats['hardware_durable_attempts']=self.stats.get('hardware_durable_attempts',0)+1
                try:
                    completed=self._decode_segment(path,job,device,start_frame=resume_frame)
                    if completed:
                        with self.lock:self.stats['hardware_durable_decoded_segments']=self.stats.get('hardware_durable_decoded_segments',0)+1
                    return
                except _HardwareDecodeFailure as error:
                    if backend=='cuda':
                        # A prior attempt may have committed farther than its
                        # last checkpoint before process termination. Keep that
                        # attempt's encoder rather than overwrite an unknown
                        # committed prefix with different CPU-encoded bytes.
                        with self.lock:
                            self.stats['hardware_durable_failures']=self.stats.get('hardware_durable_failures',0)+1
                            self.stats['hardware_durable_last_error']=str(error)
                        raise
                    # Persist the backend switch and committed prefix before
                    # CPU can emit anything, including across process restarts.
                    resume_frame=max(resume_frame,error.frames_consumed)
                    job.update(decode_backend='cpu',decode_cuda_device=None,resume_frame=resume_frame)
                    _write(path,job)
                    with self.lock:
                        self.stats['hardware_durable_failures']=self.stats.get('hardware_durable_failures',0)+1
                        self.stats['hardware_durable_last_error']=str(error)
                        self.stats['hardware_durable_last_error_at_unix_ms']=int(time.time()*1000)
                    cuda_slot.release();cuda_slot=None
                    if self.stop_event.is_set():return
            with self.lock:self.stats['software_durable_attempts']=self.stats.get('software_durable_attempts',0)+1
            self._decode_segment(path,job,None,start_frame=resume_frame)
        except _SegmentDecodeFailure as error:
            job['resume_frame']=max(resume_frame,error.frames_consumed)
            _write(path,job)
            raise
        finally:
            if cuda_slot:cuda_slot.release()
    def _decode_segment(self, path, job, device, start_frame=0):
        video=self.folder/job['video']
        binary=self.hardware_ffmpeg if device is not None else self.ffmpeg
        command=[binary,'-hide_banner','-loglevel','error','-nostdin','-threads','1','-filter_threads','1']
        if device is not None:command+=['-hwaccel','cuda','-hwaccel_device',str(device),'-hwaccel_output_format','cuda']
        command+=['-i',str(video),'-map','0:v:0','-an','-sn','-dn',
            '-vf','fps='+str(self.fps)+(',hwdownload,format=nv12' if device is not None else ''),
            '-threads','1','-c:v','mjpeg','-q:v','2','-f','image2pipe','pipe:1']
        if job.get('sample_limit'):
            command[-1:-1]=['-frames:v',str(job['sample_limit'])]
        try:process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        except OSError as error:
            if device is not None:raise _HardwareDecodeFailure('NVDEC_START_'+type(error).__name__) from error
            raise
        parser=PrivateRTSPReader('rtsp://127.0.0.1:1/unused',self.source_id,self.camera_id,job['stream_kind'])
        with self.lock:self.decoder=process;self.stats['durable_decode_device']=device
        frames=0
        try:
            while not self.stop_event.is_set():
                try:image,extra=parser._read_output(process.stdout)
                except (OSError,ValueError) as error:
                    if device is not None:raise _HardwareDecodeFailure('NVDEC_READ_'+type(error).__name__,frames) from error
                    raise
                if image is None:break
                if frames<start_frame:
                    frames+=1;continue
                at=job['start_unix_ms']+round(frames*1000/self.fps)
                if not any(at>=route['since_unix_ms'] for route in job['routes']):
                    frames+=1;continue
                meta=_base_metadata(self.source_id,self.camera_id,'segment:'+video.stem+':'+str(frames),job['stream_kind'],at)
                meta.update(photo_window_managed=True,decode_keyframes_only=False,
                    capture_origin='DURABLE_MOTION_VIDEO',video_segment_start_unix_ms=job['start_unix_ms'],
                    video_segment_duration_ms=job['duration_ms'],server_receive_uncertainty_ms=max(2000,math.ceil(job['duration_ms']/1000)*1000),
                    detection_native_tiles=job['stream_kind']=='main',
                    motion_scene_active=job.get('active',True),
                    _durable_routes=job['routes'])
                body=extra['_encoded_image'];received=time.monotonic()-(time.time()-at/1000)
                while not self.stop_event.is_set():
                    try:self.callback(body,meta,received);break
                    except Exception:
                        with self.lock:self.stats['inbox_errors']+=1;self.stats['last_error']='DURABLE_INGRESS_BACKPRESSURE_SEGMENT_RETAINED'
                        self.stop_event.wait(.25)
                if self.stop_event.is_set():return
                frames+=1
                with self.lock:
                    self.stats['persisted_candidates']+=1;self.stats['decoded_frames']+=1
                    if device is not None:self.stats['hardware_durable_frames']=self.stats.get('hardware_durable_frames',0)+1
            try:code=process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                if device is not None:raise _HardwareDecodeFailure('NVDEC_EXIT_TIMEOUT',frames) from error
                raise
            if code or not frames or frames<start_frame:
                if device is not None:raise _HardwareDecodeFailure('NVDEC_EXIT_'+str(code)+'_FRAMES_'+str(frames),frames)
                raise ValueError('segment decode incomplete; original retained')
            self._retire(path,job,'DURABLE_FRAME_INGRESS_COMMITTED',frames)
            with self.lock:self.stats['decoded_segments']+=1
            return True
        except _SegmentDecodeFailure:
            raise
        except Exception as error:
            raise _SegmentDecodeFailure('SEGMENT_'+type(error).__name__,frames) from error
        finally:
            self._terminate(process)
            with self.lock:self.decoder=None;self.stats['durable_decode_device']=None
    def _manage(self):
        while not self.stop_event.is_set():
            try:
                self._recover_commits();self._discover()
                # File names carry the immutable recording server epoch. Sort
                # paths first, and load only candidates needed for this turn.
                # Parsing thousands of old JSON jobs delayed fresh discovery.
                jobs=list(self.folder.glob('*.job.json'))
                with self.lock:self.stats.update(pending_video_segments=len(jobs),manager_stage='SELECTING',manager_at_unix_ms=int(time.time()*1000))
                for path in sorted(jobs,key=lambda p:int(p.name.removesuffix('.job.json').rsplit('-',1)[1]),reverse=self.prefer_recent):
                    job=json.loads(path.read_text())
                    if job.get('camera_id')!=self.camera_id or job.get('source_id')!=self.source_id:
                        raise ValueError('persisted video belongs to another source')
                    if self.stop_event.is_set():break
                    if job.get('retry_after_unix_ms',0)>int(time.time()*1000):continue
                    end=job['start_unix_ms']+job['duration_ms']
                    if int(time.time()*1000)<end+4000:continue
                    # A fresh closed segment gets the first turn, then the
                    # oldest pending segment. Both retain their original IDs,
                    # server timestamps and committed routes.
                    recent_turn=self.prefer_recent;self.prefer_recent=not self.prefer_recent
                    if 'active' not in job:
                        job['active'],job['motion_decision']=self._active(job['start_unix_ms'],end);_write(path,job)
                    if not job['active']:
                        last_check=self.last_forced_check if recent_turn else self.last_forced_old_check
                        if job['start_unix_ms']-last_check>=int(self.settings.get('idle_face_check_seconds',60))*1000:
                            job['sample_limit']=1;job['motion_decision']='IDLE_MAX_GAP_FACE_CHECK'
                            if recent_turn:self.last_forced_check=job['start_unix_ms']
                            else:self.last_forced_old_check=job['start_unix_ms']
                        else:
                            self._retire(path,job,'MOTION_SCREENED_IDLE')
                            with self.lock:self.stats['idle_segments']+=1
                            break
                    with self.lock:self.stats['manager_stage']='WAITING_DECODE_SLOT'
                    if not _SLOTS.acquire(self.stop_event,self._discover):break
                    with self.lock:self.stats['manager_stage']='DECODING'
                    try:self._decode(path,job)
                    except Exception as error:
                        job['retry_after_unix_ms']=int(time.time()*1000)+5000;job['last_error']='DECODE_OR_DURABLE_COMMIT_RETRY_ORIGINAL_RETAINED';_write(path,job)
                        location=traceback.extract_tb(error.__traceback__)[-1]
                        with self.lock:self.stats['capture_errors']+=1;self.stats.update(last_error=job['last_error'],last_error_type=type(error).__name__,last_error_function=location.name,last_error_line=location.lineno)
                    finally:_SLOTS.release()
                    break  # One closed segment per fair scheduling turn.
                with self.lock:self.stats['manager_stage']='RECOVERING_ORPHAN'
                self._recover_unlisted()
            except Exception as error:
                location=traceback.extract_tb(error.__traceback__)[-1]
                with self.lock:self.stats['capture_errors']+=1;self.stats.update(last_error='PENDING_VIDEO_RETAINED_FOR_RETRY',last_error_type=type(error).__name__,last_error_function=location.name,last_error_line=location.lineno)
                self.stop_event.wait(1)
            self.stop_event.wait(.1)
    def latest(self,after_frame_id=None,max_age_s=2):
        from face_engine.monitoring_service import decode_image
        with self.lock:
            frame=self.live_frame
            if (frame is None or frame.metadata['frame_id'] in (after_frame_id,self.live_consumed_id)
                    or time.monotonic()-frame.received_monotonic>max_age_s):return None
            self.live_consumed_id=frame.metadata['frame_id']
        return Frame(decode_image(frame.encoded_image),frame.metadata,frame.received_monotonic,frame.encoded_image)
    def request_main(self,*args,**kwargs):return None  # Native main frames are already retained.
    def metrics(self):
        with self.lock:
            result=dict(self.stats,continuous=True,motion_video=True,main_photo_enabled=True)
            if self.motion_reader:
                current=getattr(self.motion_reader,'reader',self.motion_reader)
                result['cuda_decode_device']=getattr(current,'decode_cuda_device',None)
                result['motion_hardware_fallbacks']=current.metrics().get('hardware_decode_fallbacks',0) if current else 0
            result['live_enabled']=self.live_enabled
            result['main_only']=self.main_only
            result['live_preferred_stream']=self.live_preferred_stream
            result['cuda_readers_per_device_per_process']=self.cuda_reader_capacity
            result['live_frame_id']=self.live_frame.metadata['frame_id'] if self.live_frame else None
            result['live_stream_kind']=self.live_frame.metadata.get('stream_kind') if self.live_frame else None
            if self.last_frame_at is not None:result['frame_age_seconds']=round(time.monotonic()-self.last_frame_at,3)
            return result
    def stop(self):
        self.stop_event.set();self.rotate.set()
        with self.lock:recorder=self.recorder;decoder=self.decoder;reader=self.motion_reader
        self._terminate(recorder);self._terminate(decoder)
        if reader:reader.stop()
        for thread in self.threads:
            thread.join(10)
            if thread.is_alive():raise TimeoutError('motion-video input did not stop; pending segments retained')
