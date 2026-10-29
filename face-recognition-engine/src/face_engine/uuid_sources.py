"""Resolve backend UUIDs through the local media registry, without name joins."""
from bisect import bisect_left
import ipaddress
import json
import math
import os
from pathlib import Path
import hashlib
import shutil
import uuid as uuid_module
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit, quote
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UUIDSourceUnavailable(RuntimeError):
    pass


class UUIDSources:
    def __init__(self, settings, *, clock=time.monotonic):
        self.clock = clock
        self.registry_url = settings['registry_url']
        address = urlsplit(self.registry_url)
        if (address.scheme != 'http' or address.username or address.password or address.query
                or address.fragment or not address.hostname
                or not (address.hostname == 'localhost' or ipaddress.ip_address(address.hostname).is_loopback)):
            raise ValueError('UUID registry must be a local HTTP endpoint without credentials')
        self.rtsp_base = settings['rtsp_base_url'].rstrip('/')
        address = urlsplit(self.rtsp_base)
        if address.scheme != 'rtsp' or not address.hostname or address.username or address.password or address.path not in ('', '/') or address.query or address.fragment:
            raise ValueError('UUID RTSP base must be a configured RTSP authority')
        self.app = settings['live_app']
        if not isinstance(self.app, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', self.app):
            raise ValueError('invalid UUID live app')
        self.ffmpeg = settings['ffmpeg_path']
        self.transport = settings.get('transport', 'relay')
        self.rtsp_port = settings.get('direct_rtsp_port', 554)
        if self.transport not in ('relay', 'direct_rtsp', 'hybrid') or type(self.rtsp_port) is not int or not 1 <= self.rtsp_port <= 65535:
            raise ValueError('invalid UUID video transport')
        self.limit = settings.get('max_readers', 8)
        self.continuous = settings.get('continuous', False)
        self.continuous_main_capacity = settings.get('continuous_main_capacity', 16)
        if type(self.continuous_main_capacity) is not int or not 0 <= self.continuous_main_capacity <= 1024:
            raise ValueError('continuous_main_capacity must be 0..1024')
        self.preferred_stream = settings.get('preferred_stream', 'main')
        if self.preferred_stream not in ('main', 'sub'):
            raise ValueError('preferred_stream must be main or sub')
        self.keyframes_only = settings.get('continuous_decode_keyframes_only', False)
        self.motion_admission = dict(settings.get('motion_admission', {}))
        self.motion_video = dict(settings.get('motion_video', {}))
        if self.motion_video.get('enabled'):
            if not isinstance(self.motion_video.get('directory'),str) or not Path(self.motion_video['directory']).is_absolute():
                raise ValueError('motion-video directory must be absolute')
        self.main_photo_enabled = settings.get('main_photo_enabled', True)
        if type(self.main_photo_enabled) is not bool:
            raise ValueError('main_photo_enabled must be boolean')
        if self.motion_admission:
            from face_engine.analysis_admission import MotionAdmission
            MotionAdmission(**self.motion_admission)
        if type(self.keyframes_only) is not bool:
            raise ValueError('continuous_decode_keyframes_only must be boolean')
        self.main_archive = settings.get('main_archive_directory')
        if self.main_archive is not None and (not isinstance(self.main_archive, str) or not Path(self.main_archive).is_absolute()):
            raise ValueError('main_archive_directory must be absolute')
        if type(self.continuous) is not bool:
            raise ValueError('UUID continuous must be boolean')
        self.rotate = settings.get('rotation_seconds', 60)
        self.ttl = settings.get('registry_refresh_seconds', 10)
        self.registry_grace = settings.get('registry_grace_seconds', 0)
        if type(self.registry_grace) not in (int,float) or not math.isfinite(self.registry_grace) or not 0 <= self.registry_grace <= 300:
            raise ValueError('registry grace must be 0..300 seconds')
        self.last_registry_at = None
        self.registry_degraded = False
        if type(self.limit) is not int or not 1 <= self.limit <= 1024:
            raise ValueError('UUID max_readers must be 1..1024')
        if 'pinned_camera_ids' in settings or 'pinned_until_unix_ms' in settings:
            raise ValueError('UUID camera pinning is unsupported; use normal activity bindings')
        for value, low, high in [(self.rotate, 20, 300), (self.ttl, 1, 60)]:
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('invalid UUID registry/rotation interval')
        self.next_refresh = 0
        self.next_rotation = 0
        self.offset = 0
        self.known = set()
        self.no_video = set()
        self._direct = {}
        self._connections = {}
        self.available = False
        self.population = ()
        self.status = dict(status='NOT_QUERIED')
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def resolve(self, requested):
        if not requested:
            self._direct.clear()
            self._connections.clear()
            self.next_refresh = 0
            self.status = dict(status='IDLE', registered=0, selected=0, deferred=0)
            return {}
        now = self.clock()
        if now >= self.next_refresh:
            try:
                with self.opener.open(Request(self.registry_url, headers={'Accept': 'application/json'}), timeout=3) as response:
                    if response.status != 200:
                        raise ValueError('registry HTTP status')
                    raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError('registry body limit')
                devices = json.loads(raw)
                if isinstance(devices, dict):
                    devices = devices.get('devices', devices.get('data'))
                if not isinstance(devices, list):
                    raise ValueError('registry device list required')
                known, no_video, direct, connections = set(), set(), {}, {}
                for device in devices:
                    if not isinstance(device, dict) or device.get('online') is not True:
                        continue
                    channels = device.get('channels', [])
                    if isinstance(channels, dict):
                        channels = channels.values()
                    if not isinstance(channels, (list, type({}.values()))):
                        continue
                    for channel in channels:
                        if not isinstance(channel, dict) or channel.get('online') is not True or channel.get('enabled') is False:
                            continue
                        uuid = channel.get('uuid')
                        if isinstance(uuid, str) and re.fullmatch(r'[A-Za-z0-9_]{1,1024}', uuid):
                            if device.get('channelCount') == 0:
                                no_video.add(uuid)
                            else:
                                known.add(uuid)
                                if self.transport in ('direct_rtsp', 'hybrid') and uuid in requested:
                                    # The registry's channel index is authoritative.
                                    # Device.port is the SDK port, not an RTSP port.
                                    index = channel.get('index')
                                    try:
                                        ip = ipaddress.ip_address(device.get('ip', ''))
                                    except ValueError:
                                        continue
                                    if (type(index) is not int or not 1 <= index <= 655
                                            or ip.is_loopback or ip.is_unspecified or ip.is_multicast):
                                        continue
                                    username, password = device.get('username'), device.get('password')
                                    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
                                        continue
                                    host = '[' + str(ip) + ']' if ip.version == 6 else str(ip)
                                    authority = quote(username, safe='') + ':' + quote(password, safe='') + '@' + host + ':' + str(self.rtsp_port)
                                    base = 'rtsp://' + authority + '/Streaming/Channels/'
                                    direct[uuid] = (base + str(index*100+2), base + str(index*100+1))
                                    connections[uuid] = dict(ip=str(ip), index=index, username=username, password=password)
                self.known = known
                self.no_video = no_video
                self._direct = direct
                self._connections = connections
                self.available = True
                self.last_registry_at = now
                self.registry_degraded = False
                self.next_refresh = now + self.ttl
            except Exception:
                if self.available and self.last_registry_at is not None and self.registry_grace>0 and now-self.last_registry_at<self.registry_grace:
                    # Keep an already identified live stream through a short
                    # registry outage. Activity revocation still comes from P2.
                    self.registry_degraded = True
                    self.next_refresh = now+min(self.ttl,3)
                else:
                    self.known.clear()
                    self.no_video.clear()
                    self._direct.clear()
                    self._connections.clear()
                    self.available = False
                    self.next_refresh = now + min(self.ttl, 3)
                    self.status = dict(status='REGISTRY_UNAVAILABLE')
                    raise UUIDSourceUnavailable('UUID registry unavailable') from None
        if not self.available:
            raise UUIDSourceUnavailable('UUID registry unavailable')
        eligible = set(requested) & self.known
        unavailable = eligible - set(self._direct) if self.transport == 'direct_rtsp' else set()
        eligible -= unavailable
        population = tuple(sorted(eligible))
        slots = self.limit
        if population != self.population:
            # Online/offline churn must not repeatedly reset the cursor to
            # camera zero and starve every later UUID in a large installation.
            anchor = self.population[self.offset] if self.population else None
            self.offset = bisect_left(population, anchor) % len(population) if population and anchor else 0
            if not self.population or not population:
                self.next_rotation = now + self.rotate
            self.population = population
        if self.continuous and len(population) > self.limit:
            raise UUIDSourceUnavailable('Continuous camera count exceeds explicit capacity; rotation is disabled')
        if self.continuous:
            self.offset = 0
            slots = len(population)
        elif slots and len(population) > slots and now >= self.next_rotation:
            self.offset = (self.offset + slots) % len(population)
            self.next_rotation = now + self.rotate
        selected = (population + population)[self.offset:self.offset + min(slots, len(population))]
        self.status = dict(status='REGISTRY_DEGRADED' if self.registry_degraded else 'READY', registered=len(eligible), selected=len(selected),
                           registry_cache_age_seconds=max(0,now-self.last_registry_at) if self.last_registry_at is not None else None,
                           deferred=max(0, len(eligible) - len(selected)),
                           unresolved=len(set(requested) - self.known), max_readers=self.limit,
                           no_video_channels=len(set(requested) & self.no_video),
                           transport=self.transport, unavailable_direct_sources=len(unavailable),
                           continuous=self.continuous,
                           preferred_stream=self.preferred_stream,
                           rotation_seconds=self.rotate)
        # Deployed CCIC CommonUtil parses -0 as main and -3 as sub. UUIDs are
        # exact registry keys; the app and relay authority are operator config.
        use_direct = {uuid for uuid in selected if uuid in self._connections and
                      (self.transport == 'direct_rtsp' or (self.transport == 'hybrid' and ipaddress.ip_address(self._connections[uuid]['ip']).is_private))}
        return {uuid: dict(camera_id=uuid, source_id=uuid, backend='ccic_uuid',
                           sub_url=self._direct[uuid][0] if uuid in use_direct else self.rtsp_base + '/' + self.app + '/' + uuid + '-3',
                           main_url=self._direct[uuid][1] if uuid in use_direct else self.rtsp_base + '/' + self.app + '/' + uuid + '-0',
                           capture_connection=self._connections.get(uuid), continuous=self.continuous,
                           preferred_stream=self.status['preferred_stream'],
                           ffmpeg_path=self.ffmpeg, sample_fps=2 if self.continuous else 1,
                           **(dict(decode_keyframes_only=self.keyframes_only, main_archive_directory=self.main_archive,
                                   motion_admission=dict(self.motion_admission),
                                   main_photo_enabled=self.main_photo_enabled,
                                   motion_video=dict(self.motion_video)) if self.continuous else {}))
                for uuid in selected}


class FailoverCamera:
    """Prefer main video, with bounded fallback to sub and reconnection."""
    def __init__(self, sub_url, main_url, source_id, camera_id, *, ffmpeg='ffmpeg',
                 sample_fps=1, failover_seconds=22, preferred_stream='main',
                 decode_keyframes_only=False, main_archive_directory=None):
        self.reader_type = PrivateRTSPReader
        self.urls = {'sub': sub_url, 'main': main_url}
        self.source_id, self.camera_id = source_id, camera_id
        self.ffmpeg, self.sample_fps = ffmpeg, sample_fps
        self.failover_seconds = failover_seconds
        self.decode_keyframes_only = decode_keyframes_only
        self.main_archive_directory = main_archive_directory
        if preferred_stream not in ('main', 'sub'):
            raise ValueError('preferred_stream must be main or sub')
        self.kind = 'sub' if preferred_stream=='sub' and sub_url else ('main' if main_url else 'sub')
        self.preferred_stream=preferred_stream
        self.next_main_probe=time.monotonic()+60
        self.probe_reader=None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.reader = None
        self.thread = None
        self.retired_frames = 0
        self.switches = 0
        self.last_frame_id = None
        self.last_frame_at = time.monotonic()
        self.stop_error = None

    def _new(self, kind=None):
        kind=kind or self.kind
        return self.reader_type(self.urls[kind], self.source_id, self.camera_id, kind,
                                ffmpeg=self.ffmpeg, sample_fps=self.sample_fps,
                                decode_keyframes_only=self.decode_keyframes_only and kind=='main',
                                main_archive_directory=self.main_archive_directory if kind=='main' else None,
                                first_frame_timeout_s=20, stall_timeout_s=20)

    def start(self):
        with self.lock:
            if self.reader is not None:
                return self
            self.reader = self._new().start()
            self.last_frame_at = time.monotonic()
            self.thread = threading.Thread(target=self._monitor, name='uuid-stream-failover', daemon=True)
            self.thread.start()
        return self

    def _monitor(self):
        while not self.stop_event.wait(.25):
            # Recover the preferred stream while the fallback keeps running.
            # This also returns a low-cost sub reader after a temporary outage.
            preferred=self.preferred_stream
            if self.kind!=preferred and self.urls.get(preferred) and time.monotonic()>=self.next_main_probe:
                self.next_main_probe=time.monotonic()+60
                probe=self._new(preferred)
                with self.lock:self.probe_reader=probe
                promoted=False
                try:
                    probe.start()
                    if probe.wait_latest(20) is not None and not self.stop_event.is_set():
                        with self.lock:
                            old=self.reader;self.reader=probe;self.kind=preferred
                            self.last_frame_at=time.monotonic();self.last_frame_id=None;self.switches+=1
                            if old:self.retired_frames+=old.metrics()['frames']
                        promoted=True
                        if old:old.stop()
                except (OSError,ValueError,TimeoutError):
                    self.stop_error='PREFERRED_'+preferred.upper()+'_PROBE_FAILED'
                finally:
                    if not promoted:probe.stop()
                    with self.lock:self.probe_reader=None
            with self.lock:
                if self.reader is None:
                    continue
                if self.reader.metrics().get('state') == 'WAITING_CONNECTION_SLOT':
                    self.last_frame_at = time.monotonic()
                    continue
                frame = self.reader.latest(max_age_s=2)
                if frame is not None and frame.metadata['frame_id'] != self.last_frame_id:
                    self.last_frame_id = frame.metadata['frame_id']
                    self.last_frame_at = time.monotonic()
                other = 'main' if self.kind == 'sub' else 'sub'
                if time.monotonic() - self.last_frame_at < self.failover_seconds or not self.urls.get(other):
                    continue
                frames = self.reader.metrics()['frames']
                try:
                    self.reader.stop()
                except TimeoutError:
                    self.stop_error = 'READER_STOP_TIMEOUT'
                    continue
                self.retired_frames += frames
                self.stop_error = None
                if self.stop_event.is_set():
                    return
                self.kind = other
                self.reader = self._new().start()
                self.last_frame_id = None
                self.last_frame_at = time.monotonic()
                self.switches += 1

    def latest(self, after_frame_id=None, max_age_s=2):
        with self.lock:
            return None if self.reader is None else self.reader.latest(after_frame_id, max_age_s)

    def request_main(self, reason='LOW_FACE_QUALITY', after_frame_id=None):
        with self.lock:
            if self.kind == 'main':
                return None if self.reader is None else self.reader.latest(after_frame_id, 2)
        if not self.urls.get('main') or self.stop_event.is_set():
            return None
        reader = self.reader_type(self.urls['main'], self.source_id, self.camera_id, 'main',
                                   ffmpeg=self.ffmpeg, sample_fps=1, one_shot=True,
                                   first_frame_timeout_s=20, stall_timeout_s=20)
        try:
            reader.start()
            frame = reader.wait_latest(20)
            if frame is not None:
                frame.metadata['fallback_reason'] = reason
            return frame
        finally:
            reader.stop()

    def metrics(self):
        with self.lock:
            value = self.reader.metrics() if self.reader is not None else dict(frames=0, state='NEW')
            frame = None if self.reader is None else self.reader.latest(max_age_s=5)
            if frame is not None:
                value.update(width=int(frame.image_bgr.shape[1]), height=int(frame.image_bgr.shape[0]),
                             frame_age_seconds=round(max(0, time.monotonic()-frame.received_monotonic), 3),
                             time_source=frame.metadata.get('time_source', 'UNKNOWN'))
            if self.stop_error:
                value['last_error'] = self.stop_error
            return dict(value, frames=value['frames'] + self.retired_frames, stream_kind=self.kind,
                        failovers=self.switches)

    def stop(self):
        self.stop_event.set()
        with self.lock:probe=self.probe_reader
        if probe is not None:probe.stop()
        if self.thread is not None:
            self.thread.join(timeout=12)
            if self.thread.is_alive():
                raise TimeoutError('UUID reader did not stop')
        with self.lock:
            if self.reader is not None:
                self.reader.stop()


from face_engine.acquisition import LatestFrameReader, _CREATE_FLAGS, _rtsp_timeout_option

_RTSP_CONNECT_SLOTS = threading.BoundedSemaphore(16)


class PrivateRTSPReader(LatestFrameReader):
    """Pass the RTSP URL over stdin, keeping credentials out of process argv."""
    def __init__(self, *args, **kwargs):
        self.decode_keyframes_only=kwargs.pop('decode_keyframes_only',False)
        self.decode_cuda_device=kwargs.pop('decode_cuda_device',None)
        self.cpu_ffmpeg=kwargs.pop('cpu_ffmpeg',None)
        self.concat_input_options=kwargs.pop('concat_input_options',True)
        self.motion_preview=kwargs.pop('motion_preview',False)
        self.motion_decode_scale=kwargs.pop('motion_decode_scale',1)
        if type(self.motion_decode_scale) is not int or self.motion_decode_scale not in (1,2,4,8):
            raise ValueError('motion_decode_scale must be 1, 2, 4 or 8')
        if self.decode_cuda_device is not None and (type(self.decode_cuda_device) is not int or self.decode_cuda_device<0):
            raise ValueError('decode_cuda_device must be a nonnegative integer')
        archive=kwargs.pop('main_archive_directory',None)
        if type(self.decode_keyframes_only) is not bool:raise ValueError('decode_keyframes_only must be boolean')
        super().__init__(*args, **kwargs)
        self.archive_directory=None
        self.archive_next_check=0
        self.archive_session=None
        if archive:
            root=Path(archive)
            if not root.is_absolute():raise ValueError('main archive root must be absolute')
            self.archive_directory=root/hashlib.sha256(self.camera_id.encode()).hexdigest()
            self.archive_directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            manifest=dict(schema='main-video-archive-v1',camera_id=self.camera_id,source_id=self.source_id,
                          stream_kind='main',container_timing_basis='RECEIVE_CLOCK_NOT_CAPTURE',
                          identity_time_basis='CAMERA_OSD_SAME_IMAGE')
            path=self.archive_directory/'camera.json'
            try:
                with path.open('x',encoding='utf-8') as stream:json.dump(manifest,stream)
                path.chmod(0o600)
            except FileExistsError:
                if json.loads(path.read_text())!=manifest:raise ValueError('main archive camera identity conflict')
        self._jpeg_buffer=bytearray()
        self._connect_lock = threading.Lock()
        self._connect_held = False
        self._preferred_cuda_device=self.decode_cuda_device
        self._hardware_ffmpeg=self.ffmpeg
        self._hardware_concat_options=self.concat_input_options
        self._hardware_retry_at=0.0
        self._hardware_error=None
        self._hardware_stderr_thread=None

    def _drain_hardware_errors(self, process):
        """Drain stderr without retaining or exposing private RTSP diagnostics."""
        tail=b''
        try:
            while True:
                block=getattr(process.stderr,'read1',process.stderr.read)(4096)
                if not block:return
                text=(tail+block).lower();tail=text[-256:]
                category=None
                if any(x in text for x in (b'out of memory',b'cuda_error_out_of_memory')):
                    category='CUDA_OUT_OF_MEMORY'
                elif any(x in text for x in (b'cannot load libcuda',b'cannot load libnvcuvid',
                                             b'cuda_error_no_device',b'failed to initialise cuda',
                                             b'failed setup for format cuda',b'no device available for decoder')):
                    category='CUDA_INITIALIZATION_FAILED'
                elif any(x in text for x in (b'unsupported codec',b'cuda_error_not_supported')):
                    category='CUDA_DECODE_UNSUPPORTED'
                elif any(x in text for x in (b'invalid output format for hwframe download',
                                             b'failed to download frame',b'function not implemented')):
                    category='CUDA_FILTER_FAILED'
                elif b'option' in text and any(x in text for x in (b'not found',b'not allowed',b'unknown keyword')):
                    category='HARDWARE_INPUT_OPTION_UNSUPPORTED'
                if category:
                    with self._lock:
                        self._hardware_error=category
                        self._metrics['hardware_last_error']=category
        except (OSError,ValueError):
            pass
        finally:
            process.stderr.close()

    def _hardware_eof(self):
        if self._hardware_stderr_thread:self._hardware_stderr_thread.join(.2)
        with self._lock:
            error=self._hardware_error
            reason=error or ('FRAME_TIMEOUT' if self._metrics.get('last_error')=='FRAME_TIMEOUT'
                            else 'END_OF_STREAM_OR_TRANSPORT')
            self._metrics['hardware_last_error']=reason
            self._metrics['hardware_decode_reconnects']=self._metrics.get('hardware_decode_reconnects',0)+1
        # A network interruption is not proof that NVDEC failed. Reconnect the
        # same GPU decoder; only explicit hardware/input failures use CPU briefly.
        if error and self.cpu_ffmpeg:
            self.decode_cuda_device=None;self.ffmpeg=self.cpu_ffmpeg
            self.concat_input_options=True
            self._hardware_retry_at=time.monotonic()+60+int(hashlib.sha256(self.camera_id.encode()).hexdigest()[:4],16)%16
            with self._lock:
                self._metrics['hardware_decode_fallbacks']=self._metrics.get('hardware_decode_fallbacks',0)+1

    def _release_connect(self):
        with self._connect_lock:
            if self._connect_held:
                self._connect_held = False
                _RTSP_CONNECT_SLOTS.release()

    def read_encoded_output(self, stream):
        """Read the original JPEG without decoding pixels that ingress discards."""
        try:
            while True:
                if (self._preferred_cuda_device is not None and self.decode_cuda_device is None
                        and self._hardware_retry_at and time.monotonic()>=self._hardware_retry_at):
                    # End the temporary CPU session so the normal reconnect loop
                    # can retry NVDEC, even when that CPU stream keeps producing.
                    return None,{}
                if self.archive_directory and time.monotonic()>=self.archive_next_check:
                    self.archive_next_check=time.monotonic()+30
                    self._prune_archive()
                    if shutil.disk_usage(self.archive_directory).free<10*1024**3:
                        raise ValueError('MAIN_VIDEO_ARCHIVE_CAPACITY')
                end=self._jpeg_buffer.find(b'\xff\xd9')
                if end>=0:
                    body=bytes(self._jpeg_buffer[:end+2]);del self._jpeg_buffer[:end+2]
                    if len(body)>12*1024*1024:raise ValueError('decoder JPEG byte limit exceeded')
                    if not body.startswith(b'\xff\xd8'):raise ValueError('invalid decoder JPEG')
                    position=2;dimensions=None
                    while position+4<=len(body):
                        if body[position]!=255:raise ValueError('invalid decoder JPEG marker')
                        while position<len(body) and body[position]==255:position+=1
                        marker=body[position];position+=1
                        if marker in (216,217):continue
                        length=int.from_bytes(body[position:position+2],'big')
                        if length<2 or position+length>len(body):raise ValueError('invalid decoder JPEG segment')
                        if marker in (192,193,194,195,197,198,199,201,202,203,205,206,207):
                            if length<8:raise ValueError('invalid decoder JPEG dimensions')
                            height=int.from_bytes(body[position+3:position+5],'big');width=int.from_bytes(body[position+5:position+7],'big')
                            dimensions=(height,width);break
                        if marker==218:break
                        position+=length
                    if not dimensions or min(dimensions)<1 or dimensions[0]*dimensions[1]>self.max_pixels:
                        raise ValueError('oversized decoder JPEG')
                    evidence={'_encoded_image':body,'encoded_image_dimensions':[dimensions[1],dimensions[0]]}
                    if self.archive_directory:
                        evidence['main_video_archive_directory']=str(self.archive_directory)
                        evidence['archive_timing_basis']='RECEIVE_CLOCK_NOT_CAPTURE'
                    evidence['decode_keyframes_only']=self.decode_keyframes_only
                    return body,evidence
                if len(self._jpeg_buffer)>12*1024*1024:raise ValueError('decoder JPEG byte limit exceeded')
                block=getattr(stream,'read1',stream.read)(65536)
                if not block:
                    if self.decode_cuda_device is not None and not self._stop.is_set():self._hardware_eof()
                    if self._jpeg_buffer:raise EOFError('truncated decoder JPEG')
                    return None,{}
                self._jpeg_buffer.extend(block)
        finally:
            self._release_connect()

    def _read_output(self, stream):
        body,evidence=self.read_encoded_output(stream)
        if body is None:return None,evidence
        import cv2,numpy as np
        scale=self.motion_decode_scale
        flags={1:cv2.IMREAD_COLOR,2:cv2.IMREAD_REDUCED_COLOR_2,
               4:cv2.IMREAD_REDUCED_COLOR_4,8:cv2.IMREAD_REDUCED_COLOR_8}
        image=cv2.imdecode(np.frombuffer(body,np.uint8),flags[scale])
        width,height=evidence['encoded_image_dimensions']
        expected=((height+scale-1)//scale,(width+scale-1)//scale)
        if image is None or image.shape[:2]!=expected:raise ValueError('invalid decoded JPEG image')
        if scale!=1:evidence['motion_thumbnail_scale']=scale
        return image,evidence

    def _prune_archive(self):
        # Unacknowledged recording is evidence, not a rolling cache.
        return


    def stop(self, *args, **kwargs):
        self._release_connect()
        try:return super().stop(*args, **kwargs)
        finally:
            if self._hardware_stderr_thread:self._hardware_stderr_thread.join(1)

    def _command(self):
        if (self._preferred_cuda_device is not None and self.decode_cuda_device is None
                and self._hardware_retry_at and time.monotonic()>=self._hardware_retry_at):
            self.decode_cuda_device=self._preferred_cuda_device;self.ffmpeg=self._hardware_ffmpeg
            self.concat_input_options=self._hardware_concat_options;self._hardware_retry_at=0.0
            with self._lock:self._metrics['hardware_decode_retries']=self._metrics.get('hardware_decode_retries',0)+1
        command = super()._command()
        index = command.index('-i')
        tail=command[index+2:]
        if self.decode_keyframes_only and '-vf' in tail:
            vf=tail.index('-vf');del tail[vf:vf+2]
            # Repeated source PTS must not terminate the JPEG encoder. Image
            # ordinals are transport timing only; identity uses same-image OSD.
            tail[0:0]=['-vf','setpts=N/TB','-fps_mode','passthrough']
        codec=tail.index('-c:v')
        tail[codec+1]='mjpeg';tail[codec+2:codec+2]=['-q:v','2']
        hardware=[]
        if self.decode_cuda_device is not None:
            hardware=['-hwaccel','cuda','-hwaccel_device',str(self.decode_cuda_device),'-hwaccel_output_format','cuda']
            vf=tail.index('-vf');filters=tail[vf+1].split(',')
            filters[1:1]=['hwdownload','format=nv12'];tail[vf+1]=','.join(filters)
        if self.motion_preview:
            vf=tail.index('-vf');tail[vf+1]+=',scale=320:180'
        archive=[]
        if self.archive_directory:
            self.archive_next_check=0
            session=uuid_module.uuid4().hex
            self.archive_session=session
            archive=['-map','0:v:0','-an','-sn','-dn','-c:v','copy','-f','segment',
                     '-segment_time','30','-reset_timestamps','1','-segment_format','matroska',
                     str(self.archive_directory/(session+'-%06d.mkv'))]
        return [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-threads', '1', '-filter_threads', '1', '-filter_complex_threads', '1',
                *hardware,
                *(['-skip_frame','nokey'] if self.decode_keyframes_only else []),
                *(['-use_wallclock_as_timestamps','1'] if self.archive_directory else []),
                '-protocol_whitelist', 'pipe,rtsp,rtsps,tcp,tls,udp,rtp',
                '-f', 'concat', '-safe', '0', '-i', 'pipe:0'] + archive + tail

    def _spawn(self, command):
        self._jpeg_buffer=bytearray()
        # ffconcat quoting, independent of shell parsing. Registry credentials
        # are percent-encoded before URL construction. No playlist is persisted.
        url = self.url.replace('\\', '\\\\').replace("'", "'\\''")
        if '\n' in url or '\r' in url or not url.lower().startswith(('rtsp://', 'rtsps://')):
            raise ValueError('invalid private RTSP source')
        playlist = "ffconcat version 1.0\nfile '" + url + "'\n"
        if self.concat_input_options:
            playlist += 'option rtsp_transport tcp\noption probesize 262144\noption analyzeduration 1000000\n'
        option = _rtsp_timeout_option(self.ffmpeg)
        if option and self.concat_input_options:
            playlist += 'option ' + option.lstrip('-') + ' ' + str(int(self.stall_timeout*1_000_000)) + '\n'
        with self._lock:
            self._metrics['state'] = 'WAITING_CONNECTION_SLOT'
        while not _RTSP_CONNECT_SLOTS.acquire(timeout=.1):
            if self._stop.is_set():
                raise OSError('RTSP connection cancelled')
        with self._connect_lock:
            self._connect_held = True
        if self._stop.is_set():
            self._release_connect()
            raise OSError('RTSP connection cancelled')
        process=None
        try:
            if self._hardware_stderr_thread:self._hardware_stderr_thread.join(1)
            self._hardware_error=None
            hardware=self.decode_cuda_device is not None
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE if hardware else subprocess.DEVNULL,
                                       creationflags=_CREATE_FLAGS)
            if hardware:
                self._hardware_stderr_thread=threading.Thread(target=self._drain_hardware_errors,
                    args=(process,),name='private-hardware-errors',daemon=True)
                self._hardware_stderr_thread.start()
        except BaseException:
            self._release_connect()
            if process is not None:
                if process.poll() is None:process.kill()
                process.wait(timeout=3)
                for stream in (process.stdin,process.stdout,process.stderr):
                    if stream:stream.close()
            if self._hardware_stderr_thread and self._hardware_stderr_thread.ident is None:
                self._hardware_stderr_thread=None
            raise
        try:
            process.stdin.write(playlist.encode('utf-8'))
            process.stdin.close()
        except BaseException:
            self._release_connect()
            process.kill()
            process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()
            raise
        return process
