"""Acquisition-only process groups; no face models, matching or public API."""
import argparse
import ctypes
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import signal
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid

from face_engine.trajectory_bridge import encoded,scoped_event_key


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    try:
        with temporary.open('x',encoding='utf-8') as stream:
            if os.name!='nt':os.chmod(temporary,0o600)
            # Control/status snapshots are reconstructible. Never serialize
            # the whole control plane behind a disk flush for every snapshot.
            stream.write(encoded(value));stream.flush()
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


class BatchProducer:
    """Group commit candidate images; return only after their original bytes sync."""
    def __init__(self,root,*,max_mb,min_free_mb):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.max_bytes=max_mb*1024*1024;self.min_free=min_free_mb*1024*1024
        self.condition=threading.Condition();self.pending=[];self.queued_bytes=0
        self.bytes=0;self.next_size=0;self.error=None;self.stop=False
        self.quota_scans=0
        self.thread=threading.Thread(target=self._run,name='acquisition-group-commit',daemon=True);self.thread.start()
    def push(self,body,metadata,category='NORMAL'):
        if not isinstance(body,bytes) or not 0<len(body)<=12*1024*1024:raise ValueError('invalid candidate size')
        header=dict(key=scoped_event_key('capture',metadata,'frame_id'),metadata=metadata,category=category,
                    image_sha256=hashlib.sha256(body).hexdigest())
        raw=encoded(header).encode()
        if len(raw)>256*1024:raise ValueError('invalid candidate metadata size')
        event=threading.Event();item=[header,raw,body,event,None];size=len(raw)+len(body)+8
        with self.condition:
            if self.stop or self.queued_bytes+size>64*1024*1024:raise RuntimeError('CAPACITY: acquisition group buffer full')
            self.pending.append(item);self.queued_bytes+=size;self.condition.notify()
        event.wait()
        if item[4] is not None:raise RuntimeError(item[4])
        return header['key']
    def _run(self):
        while True:
            with self.condition:
                while not self.pending and not self.stop:self.condition.wait(.5)
                if self.stop and not self.pending:return
                # Collect concurrent camera writers into one filesystem flush.
                deadline=time.monotonic()+.25
                while not self.stop and len(self.pending)<128 and time.monotonic()<deadline:
                    self.condition.wait(max(.001,deadline-time.monotonic()))
                items=[];size=8
                while self.pending and len(items)<128:
                    item=self.pending[0];amount=len(item[1])+len(item[2])+8
                    if items and size+amount>64*1024*1024:break
                    items.append(self.pending.pop(0));size+=amount;self.queued_bytes-=amount
            temporary=self.root/(uuid.uuid4().hex+'.batch.partial');final=temporary.with_suffix('')
            error=None
            try:
                if self.next_size==0 or (self.bytes+size>self.max_bytes and time.monotonic()>=self.next_size):
                    used=0
                    with os.scandir(self.root) as entries:
                        for entry in entries:
                            if entry.name.endswith('.batch') and entry.is_file(follow_symlinks=False):
                                try:used+=entry.stat(follow_symlinks=False).st_size
                                except FileNotFoundError:pass
                            elif entry.is_dir(follow_symlinks=False):
                                for path in Path(entry.path).glob('*.cap'):
                                    try:used+=path.stat().st_size
                                    except FileNotFoundError:pass
                    self.bytes=used;self.quota_scans+=1
                    self.next_size=time.monotonic()+5
                if self.bytes+size>self.max_bytes:
                    raise RuntimeError('CAPACITY_DURABLE_GROUP_BYTES')
                if shutil.disk_usage(self.root).free<self.min_free+size:
                    raise RuntimeError('CAPACITY_DISK_RESERVE')
                with temporary.open('xb') as stream:
                    if os.name!='nt':os.chmod(temporary,0o600)
                    stream.write(b'FACEBT01')
                    for _,raw,body,_,_ in items:stream.write(struct.pack('>II',len(raw),len(body))+raw+body)
                    stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,final)
                from face_engine.capture_ingress import CaptureIngress
                CaptureIngress._sync_directory(self.root);self.bytes+=size;self.error=None
            except BaseException as exc:
                error=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith('CAPACITY_') else 'DURABLE_BATCH_WRITE_'+type(exc).__name__
                self.error=error
                if temporary.exists():temporary.unlink()
            finally:
                for item in items:item[4]=error;item[3].set()
    def status(self):
        with self.condition:return dict(bytes=self.bytes,queued=len(self.pending),queued_bytes=self.queued_bytes,
            max_bytes=self.max_bytes,last_error=self.error,quota_scans=self.quota_scans)
    def close(self):
        with self.condition:self.stop=True;self.condition.notify()
        self.thread.join(30)
        if self.thread.is_alive():raise TimeoutError('acquisition group commit did not stop')


class CaptureProcesses:
    def __init__(self, root, ingress, *, count=16):
        if type(count) is not int or not 2<=count<=64:raise ValueError('acquisition_processes must be 2..64')
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.ingress=ingress;self.count=count;self.lock=threading.RLock()
        self.entries={};self.processes={};self.configs={};self.last={};self.cached_metrics={};self.next_metrics=0
        self.roots=[ingress.root/('process-'+str(i)) for i in range(count)]
        ingress.external_roots=self.roots
    def group(self,camera):return int(hashlib.sha256(camera.encode()).hexdigest()[:8],16)%self.count
    def configure(self, rows):
        with self.lock:
            self.entries=rows
            auxiliary={index:dict(cameras={},subscriptions={}) for index in range(self.count)}
            for camera,entry in rows.items():
                connection=entry['connection'].get('connection')
                if not connection or not ipaddress.ip_address(connection['ip']).is_private:continue
                device=encoded([connection['ip'],connection['username'],connection['password']])
                group=auxiliary[self.group(device)]
                group['cameras'][camera]=entry
                subscription=group['subscriptions'].setdefault(device,dict(
                    url='http://'+connection['ip']+'/ISAPI/Event/notification/alertStream',
                    username=connection['username'],password=connection['password'],channels={}))
                channel=str(connection['index'])
                value=dict(source_id=entry['connection']['source_id'],camera_id=camera,clock_profile_version=None)
                if channel in subscription['channels'] and subscription['channels'][channel]!=value:
                    raise ValueError('ambiguous native acquisition channel')
                subscription['channels'][channel]=value
            for index in range(self.count):
                cameras={c:v for c,v in rows.items() if self.group(c)==index}
                cfg=dict(schema_version=1,parent_pid=os.getpid(),cameras=cameras,
                    auxiliary_cameras=auxiliary[index]['cameras'],
                    auxiliary_subscriptions=list(auxiliary[index]['subscriptions'].values()),
                    ingress_directory=str(self.roots[index]),
                    max_mb=max(1,self.ingress.max_bytes//(1024*1024*self.count)),
                    min_free_mb=self.ingress.min_free//(1024*1024))
                text=encoded(cfg);path=self.root/('process-'+str(index)+'.json')
                if self.last.get(index)!=text:atomic_json(path,cfg);self.last[index]=text
                process=self.processes.get(index)
                if process is not None and process.poll() is not None:
                    process.wait();self.processes.pop(index)
                if (cameras or cfg['auxiliary_subscriptions']) and index not in self.processes:
                    if sys.platform!='linux':raise ValueError('acquisition process groups require Linux')
                    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                        OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
                    self.processes[index]=subprocess.Popen([sys.executable,'-m','face_engine.capture_process',
                        '--config',str(path)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,env=env,start_new_session=sys.platform=='linux')
    def remove(self,camera):
        with self.lock:
            if camera not in self.entries:return
            rows=dict(self.entries);rows.pop(camera)
            self.configure(rows)
    def metrics(self,camera):
        now=time.monotonic()
        with self.lock:
            if now>=self.next_metrics:
                result={}
                for index,process in self.processes.items():
                    path=self.root/('process-'+str(index)+'.status.json')
                    try:
                        data=json.loads(path.read_text());age=max(0,time.time()-data['at_unix_ms']/1000)
                        if data['pid']!=process.pid:continue
                        for key,value in data.get('cameras',{}).items():
                            if self.group(key)!=index:continue
                            result[key]=dict(value,acquisition_process=process.pid,metrics_age_seconds=round(age,3))
                            if 'frame_age_seconds' in value:result[key]['frame_age_seconds']=value['frame_age_seconds']+age
                    except (OSError,ValueError,KeyError):pass
                self.cached_metrics=result;self.next_metrics=now+1
            return dict(self.cached_metrics.get(camera,dict(state='CONNECTING',frames=0,continuous=True)))
    def close(self):
        with self.lock:
            processes=list(self.processes.values());self.entries={}
            for p in processes:
                if p.poll() is None:p.terminate()
            deadline=time.monotonic()+40
            for p in processes:
                try:p.wait(timeout=max(.1,deadline-time.monotonic()))
                except subprocess.TimeoutExpired:
                    if sys.platform=='linux' and os.getpgid(p.pid)==p.pid:os.killpg(p.pid,signal.SIGKILL)
                    else:p.kill()
                    p.wait(timeout=5)
            self.processes.clear();self.last.clear();self.cached_metrics.clear()


class ProcessCamera:
    def __init__(self, camera, manager, *, main_url=None, sub_url=None, ffmpeg='ffmpeg',
                 source_id=None, clock=None, connection=None):
        self.camera_id=camera;self.manager=manager;self.main_url=main_url;self.sub_url=sub_url
        self.ffmpeg=ffmpeg;self.source_id=source_id or camera;self.clock=clock;self.connection=connection
        self.callback=True  # Parent persists one-shot main frames through its durable ingress.
    def start(self):return self
    def latest(self,*args,**kwargs):return None
    def metrics(self):return self.manager.metrics(self.camera_id)
    def stop(self):self.manager.remove(self.camera_id)
    def request_main(self,reason='insufficient_face_quality',**kwargs):
        from face_engine.uuid_sources import PrivateRTSPReader
        if not self.main_url:return None
        profile=self.clock.for_capture(self.camera_id,self.connection) if self.clock else None
        reader=PrivateRTSPReader(self.main_url,self.source_id,self.camera_id,'main',ffmpeg=self.ffmpeg,
            sample_fps=1,one_shot=True,first_frame_timeout_s=20,stall_timeout_s=20)
        try:
            reader.start();frame=reader.wait_latest(20)
            if frame is not None:
                frame.metadata['fallback_reason']=reason
                if profile:frame.metadata['camera_clock_profile']=profile
            return frame
        finally:reader.stop()


def run_worker(config_path):
    from face_engine.capture_ingress import CaptureIngress
    from face_engine.continuous_capture import ContinuousCamera
    path=Path(config_path);cfg=json.loads(path.read_text());parent=cfg['parent_pid']
    if sys.platform=='linux':
        # End all acquisition in this process when its owning P1 exits, even
        # after an unclean parent crash. Never leave an orphan capturing images.
        ctypes.CDLL(None).prctl(1,signal.SIGTERM)
        if os.getppid()!=parent:return
    stop=threading.Event()
    def terminate(*_):
        # Let copy-only recorders finalize their current segment before exit.
        # The parent force-kills this owned process group after its stop limit.
        stop.set()
    signal.signal(signal.SIGTERM,terminate)
    ingress=BatchProducer(cfg['ingress_directory'],max_mb=cfg['max_mb'],min_free_mb=cfg['min_free_mb'])
    readers={};signatures={};current={};routing={};native={};config_lock=threading.Lock()
    class ClockCache:
        def for_capture(self,camera,connection=None):
            with config_lock:return routing.get(camera,{}).get('clock_profile')
    def callback(camera,body,metadata,received,urgent=False):
        with config_lock:entry=routing.get(camera,{})
        metadata=dict(metadata);recorded_routes=metadata.pop('_durable_routes',None)
        if recorded_routes is not None:
            if metadata.get('capture_origin')!='DURABLE_MOTION_VIDEO' or metadata.get('camera_id')!=camera:
                raise ValueError('invalid recorded acquisition route')
            accepted=0
            for route in recorded_routes:
                context=route['context']
                if context.get('source_id')!=metadata.get('source_id'):raise ValueError('recorded source conflict')
                if metadata['received_at_unix_ms']<route['since_unix_ms']:continue
                category='PRIORITY' if metadata.get('motion_scene_active') else (
                    'PRIORITY' if route.get('category')=='PRIORITY' else 'BACKGROUND')
                ingress.push(body,dict(metadata,**context),category);accepted+=1
            if not accepted:raise ValueError('recorded frame predates its committed activity route')
            return
        accepted=0
        for route in entry.get('routes',[]):
            if received<route['since_monotonic']:continue
            meta=dict(metadata,**route['context'])
            ingress.push(body,meta,'PRIORITY' if urgent else route.get('category','NORMAL'))
            accepted+=1
        if not accepted:raise ValueError('no committed acquisition route')
    def native_frame(frame):
        import cv2
        meta=dict(frame.metadata);camera=meta['camera_id']
        profile=ClockCache().for_capture(camera)
        if profile:
            meta['camera_clock_profile']=profile
            if isinstance(meta.get('time_record'),dict):
                meta['time_record']=dict(meta['time_record'],profile_version=profile['profile_version'])
        meta['capture_origin']='AUXILIARY_NATIVE_FACE_CAPTURE'
        ok,body=cv2.imencode('.png',frame.image_bgr)
        if not ok:raise ValueError('native candidate encoding failed')
        callback(camera,body.tobytes(),meta,frame.received_monotonic,urgent=True)
    try:
        while not stop.is_set():
            if os.getppid()!=parent:break
            try:
                cfg=json.loads(path.read_text())
                if cfg.get('schema_version')!=1 or cfg['parent_pid']!=parent:raise ValueError('invalid acquisition owner')
                with config_lock:
                    current=cfg['cameras'];routing=dict(cfg.get('auxiliary_cameras',{}),**current)
                desired={hashlib.sha256(encoded(value).encode()).hexdigest():value for value in cfg.get('auxiliary_subscriptions',[])}
                for key in list(native):
                    if key not in desired:native.pop(key).stop()
                for key,value in desired.items():
                    if key not in native:
                        from face_engine.acquisition import HikvisionEventReader
                        native[key]=HikvisionEventReader(value['url'],username=value['username'],password=value['password'],
                            on_frame=native_frame,channel_bindings=value['channels']).start()
                for camera in list(readers):
                    entry=current.get(camera);sig=encoded(entry['connection']) if entry else None
                    if signatures[camera]!=sig:
                        readers.pop(camera).stop();signatures.pop(camera)
                for camera,entry in current.items():
                    if camera in readers:
                        if hasattr(readers[camera],'update_routes'):readers[camera].update_routes(entry['routes'])
                        continue
                    args=dict(entry['connection']);sub=args.pop('sub_url');main=args.pop('main_url')
                    motion_video=args.pop('motion_video',{})
                    factory=ContinuousCamera;extra={}
                    if motion_video.get('enabled'):
                        from face_engine.motion_video import MotionVideoCamera
                        factory=MotionVideoCamera;extra=dict(motion_video=motion_video,activity_routes=entry['routes'])
                    reader=factory(sub,main,on_capture=lambda body,meta,received,camera=camera:
                        callback(camera,body,meta,received),camera_clock=ClockCache(),**extra,**args)
                    readers[camera]=reader;signatures[camera]=encoded(entry['connection']);reader.start()
                atomic_json(path.with_suffix('.status.json'),dict(pid=os.getpid(),at_unix_ms=int(time.time()*1000),
                    cameras={camera:reader.metrics() for camera,reader in readers.items()},
                    auxiliary=[reader.metrics() for reader in native.values()],ingress=ingress.status()))
            except (OSError,ValueError,KeyError):
                # A missing/bad control file revokes acquisition, never extends
                # the lifetime of a stale activity or substitutes a UUID.
                with config_lock:current={};routing={}
                for reader in list(readers.values()):reader.stop()
                readers.clear();signatures.clear()
                for reader in native.values():reader.stop()
                native.clear()
            stop.wait(.5)
    finally:
        with config_lock:current={};routing={}
        for reader in readers.values():reader.stop()
        for reader in native.values():reader.stop()
        ingress.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    run_worker(parser.parse_args().config)
