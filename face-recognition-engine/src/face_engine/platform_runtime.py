"""Consume v4 bindings from the algorithm's own SQLite, never the backend DB.

The camera catalog contains physical input connections keyed by backend UUID.
Activity routing comes exclusively from committed platform control requests.
"""
from contextlib import closing
import copy
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from face_engine.monitoring_service import MonitoringRuntime, runtime_operation
from face_engine.scheduling import FrameJob
from face_engine.trajectory_bridge import ActivityContextError, MaintenancePaused, activity_context, encoded


def binding_snapshot(path, now_ms):
    """A consistent, read-only snapshot of algorithm-owned activity records."""
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        db.execute('PRAGMA query_only=ON')
        rows = db.execute("SELECT value FROM state WHERE ns='activity'").fetchall()
    routes = {}
    for (raw,) in rows:
        value = json.loads(raw)
        config = value.get('configuration', {})
        if (config.get('platform_protocol') != 4 or value.get('enabled') is not True
                or config.get('activity_state') == '2'
                or not config['start_unix_ms'] <= now_ms < config['end_unix_ms']):
            continue
        context = activity_context(dict(value, route_rule_id='PLATFORM-V4'))
        for binding in value.get('bindings', []):
            if binding.get('kind') != 'CAMERA':
                continue
            camera = binding['device_id']
            if binding['source_id'] != camera or binding['route_rule_id'] != 'PLATFORM-V4':
                raise ValueError('invalid algorithm v4 binding')
            routes.setdefault(camera, []).append(dict(context, camera_name=binding['camera_name']))
    for contexts in routes.values():
        contexts.sort(key=lambda item: item['activity_id'])
    return routes


class PlatformMonitoringRuntime(MonitoringRuntime):
    """One physical reader per UUID, with explicit isolated activity routes."""
    def __init__(self, config, extractor, store):
        super().__init__(config, extractor, store)
        settings = config['platform_bindings']
        self.binding_state = settings['state_file']
        self.binding_interval = settings.get('poll_seconds', 2)
        if (type(self.binding_interval) not in (int, float)
                or not math.isfinite(self.binding_interval) or not .1 <= self.binding_interval <= 30):
            raise ValueError('platform binding poll_seconds must be 0.1..30')
        if Path(self.binding_state).resolve() == Path(store.path).resolve():
            raise ValueError('P1 delivery state must differ from P2 algorithm state')
        self.platform_lock = threading.RLock()
        self.binding_refresh_lock = threading.RLock()
        self.platform_routes = {}
        self.route_since = {}
        self.reader_signatures = {}
        self.hik_signatures = {}
        self.cleanup_pending = False
        self.binding_status = dict(status='NOT_REFRESHED', missing_camera_ids=[])
        from face_engine.fleet_diagnostics import FleetDiagnostics
        self.feed_diagnostics = FleetDiagnostics()
        self.next_binding_poll = 0
        self.uuid_sources = None
        self.uuid_cached_entries = {}
        self.live_camera_rollout = None
        if config.get('uuid_sources'):
            from face_engine.uuid_sources import UUIDSources
            self.uuid_sources = UUIDSources(config['uuid_sources'])
        self.capture_clock = None
        if config.get('camera_clock'):
            from face_engine.camera_clock import CameraClock
            clock_type = CameraClock
            if config['camera_clock'].get('time_basis') == 'SERVER_RECEIVED_AT':
                from face_engine.server_clock import ServerReceivedClock
                clock_type = ServerReceivedClock
            self.capture_clock = clock_type(dict(config['camera_clock'],state_file=self.binding_state), config['camera_clock'].get('endpoint', config['endpoint']),
                os.environ.get(config.get('p2_authorization_env', 'FACE_TRAJECTORY_AUTHORIZATION'), ''))
        self.capture_processes = None
        process_count = config.get('uuid_sources',{}).get('acquisition_processes',1)
        if process_count != 1:
            if not config.get('uuid_sources',{}).get('continuous') or self.capture_ingress is None:
                raise ValueError('acquisition processes require continuous durable capture')
            from face_engine.capture_process import CaptureProcesses
            acquisition_root=config['uuid_sources'].get('acquisition_directory',str(store.path)+'.acquisition')
            self.capture_processes=CaptureProcesses(acquisition_root,self.capture_ingress,count=process_count)

    def configure_cameras(self):
        # The maintenance owner configures readers while ingress is paused.
        # It already holds the exclusive consumer lease; do not enter operation().
        with self.binding_refresh_lock:
            if not self.stop_event.is_set():
                self._refresh_bindings(start_readers=False)

    @runtime_operation
    def refresh_bindings(self, *, start_readers=True):
        with self.binding_refresh_lock:
            if not self.stop_event.is_set():
                self._refresh_bindings(start_readers=start_readers)

    def start(self):
        super().start()
        thread=threading.Thread(target=self._binding_refresh_loop,name='monitoring-bindings',daemon=True)
        self.threads.append(thread)
        try:thread.start()
        except BaseException:
            self.stop()
            raise

    def _binding_refresh_loop(self):
        # Exactly one owner performs runtime discovery. Network delays cannot
        # suspend polling of readers whose activity routes are already committed.
        while not self.stop_event.is_set():
            if time.monotonic()>=self.next_binding_poll:
                try:self.refresh_bindings()
                except MaintenancePaused:pass
            self.stop_event.wait(.1)

    def _motion_video_settings(self):
        settings=dict(self.config.get('uuid_sources',{}).get('motion_video',{}))
        filename=settings.pop('live_camera_ids_file',None)
        error=None
        if filename is not None:
            try:
                if not isinstance(filename,str) or not Path(filename).is_absolute():
                    raise ValueError('live_camera_ids_file must be an absolute path')
                selected=json.loads(Path(filename).read_text(encoding='utf-8-sig'))
                if not isinstance(selected,list) or any(not isinstance(v,str) or not v for v in selected):
                    raise ValueError('live camera rollout must be a JSON string array')
                settings['live_camera_ids']=list(selected)
            except (OSError,ValueError,TypeError) as exc:
                # Keep the last applied selection. A missing first rollout must
                # not inherit a wildcard from an old cached connection.
                error=type(exc).__name__
                self.count('platform_binding_rollout_errors')
                settings['live_camera_ids']=list(self.live_camera_rollout or [])
        return settings,error

    def _refresh_bindings(self, *, start_readers):
        from face_engine.acquisition import CameraAcquisition, HikvisionEventReader
        if self.cleanup_pending:
            if not self._stop_inputs():
                self.next_binding_poll = time.monotonic()+self.binding_interval
                return
            self.cleanup_pending = False
        try:
            motion_settings,rollout_error=self._motion_video_settings()
            routes = binding_snapshot(self.binding_state, int(time.time()*1000))
            # Revoke paused/removed/revised routes before any remote source lookup.
            # Newly added routes still wait for their own resolved reader below.
            with self.platform_lock:
                retained={camera:[context for context in contexts if any(
                    all(context.get(key)==value for key,value in route.items()) for route in routes.get(camera,()))]
                    for camera,contexts in self.platform_routes.items()}
                self.platform_routes={camera:contexts for camera,contexts in retained.items() if contexts}
                retained_keys={encoded([camera,context]) for camera,contexts in self.platform_routes.items()
                    for context in contexts}
                self.route_since={key:value for key,value in self.route_since.items() if key in retained_keys}
            # Delayed images keep their original route. Revoked routes wait on
            # disk outside the active inference budget, without deleting bytes.
            uri=Path(self.binding_state).resolve().as_uri()+'?mode=ro'
            with closing(sqlite3.connect(uri,uri=True,timeout=1)) as route_db:
                committed=[json.loads(v) for (v,) in route_db.execute("SELECT value FROM state WHERE ns='activity'")]
            allowed=[]
            for activity in committed:
                conf=activity.get('configuration',{})
                if activity.get('enabled') is not True or conf.get('platform_protocol')!=4 or conf.get('activity_state')=='2':continue
                for binding in activity.get('bindings',[]):
                    camera=binding.get('device_id')
                    if binding.get('kind')=='CAMERA' and binding.get('source_id')==camera:
                        allowed.append((activity['activity_id'],activity['activity_num'],camera,camera,binding.get('route_rule_id')))
            if self.uuid_sources is not None and self.config.get('uuid_sources',{}).get('continuous'):
                self.store.set_capture_routes(allowed)
            catalog = json.loads(Path(self.config['cameras_file']).read_text(encoding='utf-8-sig'))
            if catalog.get('schema_version') != 1:
                raise ValueError('camera catalog schema_version must be 1')
            entries = {}
            for item in catalog['cameras']:
                camera = item['camera_id']
                if not isinstance(camera, str) or not camera or camera in entries:
                    raise ValueError('camera catalog requires unique backend UUIDs')
                if 'activity_context' in item:
                    raise ValueError('dynamic catalog must not contain static activity routes')
                entries[camera] = item
            deferred = set()
            if self.uuid_sources is not None:
                from face_engine.uuid_sources import UUIDSourceUnavailable
                requested = set(routes) - set(entries)
                try:
                    resolved = self.uuid_sources.resolve(requested)
                    self.uuid_cached_entries = copy.deepcopy(resolved)
                except UUIDSourceUnavailable:
                    # Registry reachability does not revoke a committed P2
                    # binding or invalidate an already identified connection.
                    # Never invent a new UUID or reuse another camera's input.
                    resolved = {camera: item for camera, item in self.uuid_cached_entries.items()
                                if camera in requested}
                    self.uuid_sources.status = dict(status='REGISTRY_UNAVAILABLE_CACHED',
                        selected=len(resolved), unresolved=len(requested-set(resolved)))
                    if requested and not resolved:
                        raise
                entries.update(resolved)
                deferred = requested & self.uuid_sources.known - set(resolved)
            live, signatures, subscriptions = {}, {}, {}
            missing = []
            for camera in routes:
                if camera not in entries:
                    if camera in deferred:
                        continue
                    missing.append(camera)
                    continue
                item = entries[camera]
                source = item.get('source_id', camera)
                if not isinstance(source, str) or not source:
                    raise ValueError('physical source_id required')
                def env(field):
                    name = item.get(field)
                    return os.environ.get(name, '') if name else ''
                dynamic = item.get('backend') == 'ccic_uuid'
                sub, main = ((item.get('sub_url', ''), item.get('main_url', '')) if dynamic
                             else (env('sub_url_env'), env('main_url_env')))
                if item.get('sub_url_env') and not sub:
                    raise ValueError('camera video environment unavailable')
                hik = item.get('hikvision', {})
                if not sub and not hik.get('enabled'):
                    missing.append(camera)
                    continue
                live[camera] = [dict(route, source_id=source) for route in routes[camera]]
                if sub:
                    args = dict(source_id=source, camera_id=camera,
                        sample_fps=item.get('sample_fps', 10), ffmpeg=item.get('ffmpeg_path') or env('ffmpeg_path_env') or 'ffmpeg',
                        clock_profile_version=item.get('clock_profile_version'),
                        backend=item.get('backend', 'ffmpeg'), gst_python=env('gst_python_env') or None)
                    if dynamic:
                        args = {key: args[key] for key in ['source_id', 'camera_id', 'sample_fps', 'ffmpeg']}
                        if item.get('continuous'):
                            args['connection'] = item.get('capture_connection')
                            args['continuous'] = True
                            args['preferred_stream'] = item.get('preferred_stream', 'main')
                            args['decode_keyframes_only'] = item.get('decode_keyframes_only',False)
                            args['main_archive_directory'] = item.get('main_archive_directory')
                            args['motion_admission'] = item.get('motion_admission', {})
                            args['main_photo_enabled'] = item.get('main_photo_enabled', True)
                            # Source-cache entries describe a connection, not the
                            # current fleet rollout. Runtime settings must also
                            # reach cameras restored from the registry cache.
                            args['motion_video'] = dict(item.get('motion_video', {}))
                            args['motion_video'].update(motion_settings)
                            selected=args['motion_video'].get('live_camera_ids',[])
                            if not isinstance(selected,list) or any(not isinstance(v,str) or not v for v in selected):
                                raise ValueError('live_camera_ids must be a camera UUID list')
                            # Only this camera's enabled bit belongs in its
                            # connection signature, not the changing fleet list.
                            args['motion_video']['live_camera_ids']=[camera] if '*' in selected or camera in selected else []
                            args['motion_video'].pop('live_camera_ids_file',None)
                    signatures[camera] = (encoded([sub, main, args, dynamic]), sub, main, args, dynamic)
                if dynamic and item.get('continuous') and not self.capture_processes:
                    connection = item.get('capture_connection')
                    if connection:
                        import ipaddress
                        if ipaddress.ip_address(connection['ip']).is_private:
                            values = ('http://'+connection['ip']+'/ISAPI/Event/notification/alertStream',
                                      connection['username'], connection['password'])
                            subscriptions.setdefault(values, {})[str(connection['index'])] = dict(
                                source_id=source, camera_id=camera, clock_profile_version=None)
                if hik.get('enabled'):
                    values = tuple(os.environ.get(hik.get(key+'_env', ''), '')
                                   for key in ('alert_url', 'username', 'password'))
                    channel = str(hik.get('expected_channel_id', ''))
                    if not all(values) or not channel.isdigit() or int(channel) < 1:
                        raise ValueError('Hikvision catalog connection/channel unavailable')
                    channels = subscriptions.setdefault(values, {})
                    if channel in channels:
                        raise ValueError('ambiguous physical Hikvision channel')
                    channels[channel] = dict(source_id=source, camera_id=camera,
                        clock_profile_version=item.get('clock_profile_version'))
            # One NVR stream already carries its configured channel directory.
            # Activity switches change routing only, never the physical stream's
            # channel set. Unbound channels are discarded by _routed_frames().
            for camera, item in entries.items():
                hik = item.get('hikvision', {})
                if not hik.get('enabled'):
                    continue
                values = tuple(os.environ.get(hik.get(key+'_env', ''), '')
                               for key in ('alert_url', 'username', 'password'))
                if values not in subscriptions:
                    continue
                channel = str(hik.get('expected_channel_id', ''))
                source = item.get('source_id', camera)
                if not channel.isdigit() or int(channel) < 1 or not isinstance(source, str) or not source:
                    raise ValueError('invalid physical Hikvision catalog channel')
                binding = dict(source_id=source, camera_id=camera, clock_profile_version=item.get('clock_profile_version'))
                previous = subscriptions[values].get(channel)
                if previous is not None and previous != binding:
                    raise ValueError('ambiguous physical Hikvision catalog channel')
                subscriptions[values][channel] = binding
            # Revoke routes before any reader is stopped or replaced.
            current_time = time.monotonic()
            since = {}
            for camera, contexts in live.items():
                for context in contexts:
                    key = encoded([camera, context])
                    since[key] = self.route_since.get(key, current_time)
            with self.platform_lock:
                self.platform_routes, self.route_since = live, since
            for camera in list(self.cameras):
                if camera not in signatures or self.reader_signatures[camera] != signatures[camera][0]:
                    self.cameras[camera].stop()
                    self.cameras.pop(camera)
                    self.reader_signatures.pop(camera, None)
            for camera, (signature, sub, main, args, dynamic) in signatures.items():
                if camera not in self.cameras:
                    if dynamic:
                        if args.get('continuous'):
                            args = {k:v for k,v in args.items() if k!='continuous'}
                            if self.capture_processes:
                                from face_engine.capture_process import ProcessCamera
                                reader=ProcessCamera(camera,self.capture_processes,sub_url=sub,main_url=main or None,
                                    ffmpeg=args['ffmpeg'],source_id=args['source_id'],clock=self.capture_clock,
                                    connection=args.get('connection'))
                            else:
                                if args.pop('motion_video', {}).get('enabled'):
                                    raise ValueError('motion-video capture requires acquisition_processes')
                                from face_engine.continuous_capture import ContinuousCamera
                                reader = ContinuousCamera(sub, main or None, **args,
                                    on_capture=self._continuous_capture, camera_clock=self.capture_clock)
                        else:
                            from face_engine.uuid_sources import FailoverCamera
                            reader = FailoverCamera(sub, main or None, **args)
                    else:
                        reader = CameraAcquisition(sub, main or None, **args)
                    self.cameras[camera] = reader
                    self.reader_signatures[camera] = signature
                    if start_readers:
                        reader.start()
            desired = {hashlib.sha256(encoded([values, channels]).encode()).hexdigest(): (values, channels)
                       for values, channels in subscriptions.items()}
            for key in list(self.hik_signatures):
                if key not in desired:
                    self.hik_signatures[key].stop()
                    self.hik_signatures.pop(key)
            for key, (values, channels) in desired.items():
                if key not in self.hik_signatures:
                    reader = HikvisionEventReader(values[0], username=values[1], password=values[2],
                        on_frame=self._hikvision_frame, channel_bindings=channels)
                    self.hik_signatures[key] = reader
                    if start_readers:
                        reader.start()
            self.hikvision = list(self.hik_signatures.values())
            if self.capture_processes:
                process_rows={}
                for camera,(_,sub,main,args,dynamic) in signatures.items():
                    if not (dynamic and args.get('continuous')):continue
                    connection={k:v for k,v in args.items() if k!='continuous'}
                    connection.update(sub_url=sub,main_url=main or None)
                    profile=self.capture_clock.for_capture(camera,args.get('connection')) if self.capture_clock else None
                    process_rows[camera]=dict(connection=connection,clock_profile=profile,routes=[dict(
                        context=context,since_monotonic=self.route_since[encoded([camera,context])],
                        category=self.priority.category(camera,activity_id=context['activity_id'])) for context in live[camera]])
                self.capture_processes.configure(process_rows)
            self.binding_status = dict(status='MISSING_VIDEO_SOURCE' if missing else ('BOUND' if routes else 'IDLE'),
                bound_camera_count=len(routes), active_input_count=len(live),
                activity_route_count=sum(map(len, live.values())), missing_camera_ids=sorted(missing))
            if rollout_error:
                self.binding_status['live_rollout']=dict(status='READ_FAILED_SELECTION_RETAINED',error_type=rollout_error)
            elif self.config.get('uuid_sources',{}).get('motion_video',{}).get('live_camera_ids_file') is not None:
                self.live_camera_rollout=list(motion_settings['live_camera_ids'])
            if self.uuid_sources is not None:
                self.binding_status['uuid_sources'] = copy.deepcopy(self.uuid_sources.status)
            self.count('platform_binding_refreshes')
            # Changing activities must not accumulate unbounded scheduler keys.
            keys = {encoded([route['activity_id'], route['source_id'], camera, kind])
                    for camera, contexts in live.items() for route in contexts for kind in ('sub', 'main')}
            with self.platform_lock:
                self.last_sample = {key: value for key, value in self.last_sample.items() if key in keys}
                self.last_frame = {key: value for key, value in self.last_frame.items() if key in keys}
            self.admission.retain_keys(keys)
        except Exception as exc:
            self.binding_status = dict(status='BINDING_SOURCE_UNAVAILABLE', missing_camera_ids=[],
                error_type=type(exc).__name__, error_errno=getattr(exc, 'errno', None))
            with self.platform_lock:
                self.platform_routes.clear()
                self.route_since.clear()
            # No stale subscription may continue after a failed snapshot.
            # Keep any reader that did not stop: quiescence must retry it before
            # releasing a lease, and polling must never reuse a half-stopped input.
            self.cleanup_pending = not self._stop_inputs()
            self.count('platform_binding_errors')
        self.next_binding_poll = time.monotonic()+self.binding_interval

    def _stop_inputs(self):
        rows=[(inputs,signatures,key,reader) for inputs,signatures in
              ((self.cameras,self.reader_signatures),(self.hik_signatures,None))
              for key,reader in list(inputs.items())]
        with ThreadPoolExecutor(max_workers=min(64,max(1,len(rows))),thread_name_prefix='binding-stop') as pool:
            pending=[(inputs,signatures,key,pool.submit(reader.stop)) for inputs,signatures,key,reader in rows]
            for inputs,signatures,key,future in pending:
                try:
                    future.result()
                except Exception:
                    self.count('platform_reader_stop_errors')
                else:
                    inputs.pop(key)
                    if signatures is not None:
                        signatures.pop(key, None)
        self.hikvision = list(self.hik_signatures.values())
        return not self.cameras and not self.hik_signatures

    def _routed_frames(self, frame):
        with self.platform_lock:
            result = []
            for context in self.platform_routes.get(frame.metadata.get('camera_id'), []):
                if context['source_id'] != frame.metadata.get('source_id'):
                    continue
                key = encoded([frame.metadata['camera_id'], context])
                if frame.received_monotonic < self.route_since[key]:
                    continue  # Cached images cannot predate a newly installed route.
                meta = copy.deepcopy(frame.metadata)
                meta.update(context)
                result.append(replace(frame, metadata=meta))
            return result

    @runtime_operation
    def _continuous_capture(self, body, metadata, received_monotonic):
        from face_engine.acquisition import Frame
        frame = Frame(None, metadata, received_monotonic)
        routed_frames = self._routed_frames(frame)
        if not routed_frames:
            raise ActivityContextError('continuous frame has no current activity route')
        for routed in routed_frames:
            meta = routed.metadata
            self.persist_camera_capture(body, meta,
                self.priority.category(meta['camera_id'], activity_id=meta['activity_id']))
        self.count('continuous_candidates_persisted')

    @runtime_operation
    def _hikvision_frame(self, frame):
        import cv2
        frames = self._routed_frames(frame)
        if not frames:
            return
        ok, body = cv2.imencode('.png', frame.image_bgr)
        if not ok:
            raise ValueError('capture encoding failed')
        refused = None
        for routed in frames:
            meta = routed.metadata
            if self.capture_clock:
                connection = self.uuid_sources._connections.get(meta['camera_id']) if self.uuid_sources else None
                profile = self.capture_clock.for_capture(meta['camera_id'], connection)
                if profile:
                    meta['camera_clock_profile'] = profile
                    if isinstance(meta.get('time_record'), dict):
                        meta['time_record']['profile_version'] = profile['profile_version']
            try:
                # This is queue urgency, independent of the person's business
                # category. Native face pictures can confirm a video route
                # promptly while weighted scheduling still services video.
                auxiliary = self.config.get('uuid_sources', {}).get('continuous') is True
                if auxiliary:
                    meta['capture_origin'] = 'AUXILIARY_NATIVE_FACE_CAPTURE'
                self.persist_camera_capture(body.tobytes(), meta,
                    'PRIORITY' if auxiliary else self.priority.category(meta['camera_id'], activity_id=meta['activity_id']))
            except RuntimeError as error:
                # One activity out of capacity must not cost the camera's other activities.
                if not str(error).startswith('CAPACITY:'):
                    raise
                refused = refused or error
        if refused:
            raise refused

    def _route_current(self, metadata):
        return any(all(metadata.get(key) == value for key, value in context.items())
                   for context in self.platform_routes.get(metadata.get('camera_id'), []))

    def _committed_route_current(self, metadata):
        # A transient registry failure or a disconnected reader does not revoke
        # an already durable frame. Only committed activity state can do that.
        try:
            uri=Path(self.binding_state).resolve().as_uri()+'?mode=ro'
            with closing(sqlite3.connect(uri,uri=True,timeout=1)) as db:
                row=db.execute("SELECT value FROM state WHERE ns='activity' AND key=?",(metadata.get('activity_id'),)).fetchone()
            activity=json.loads(row[0]) if row else None
        except (OSError,ValueError,sqlite3.Error) as error:
            raise RuntimeError('committed binding unavailable; keep capture for retry') from error
        if not activity or activity.get('enabled') is not True:return False
        configuration=activity.get('configuration',{})
        if configuration.get('platform_protocol')!=4 or configuration.get('activity_state')=='2':return False
        # V4 P2 owns revision and capture-window checks. A queued frame keeps
        # its original context across end-time/name updates; it must not be
        # destroyed merely because recognition happens later than acquisition.
        if any(metadata.get(k)!=activity.get(k) for k in ('activity_id','activity_num')):return False
        camera=metadata.get('camera_id')
        return metadata.get('source_id')==camera and any(
            b.get('kind')=='CAMERA' and b.get('device_id')==camera and b.get('source_id')==camera
            and b.get('route_rule_id')==metadata.get('route_rule_id')
            for b in activity.get('bindings',[]))

    @runtime_operation
    def process_frame(self, image, metadata, **kwargs):
        if not self._committed_route_current(metadata):
            raise RuntimeError('platform camera route is paused or revoked; retain original capture')
        return super().process_frame(image, metadata, **kwargs)

    @runtime_operation
    def process_burst(self, body, metadata, **kwargs):
        if not self._committed_route_current(metadata):
            raise RuntimeError('platform camera route is paused or revoked; retain original capture')
        return super().process_burst(body, metadata, **kwargs)

    @runtime_operation
    def process_photo_window(self, captures):
        if not all(self._committed_route_current(meta) for _, _, meta in captures):
            raise RuntimeError('platform camera route is paused or revoked; retain original captures')
        return super().process_photo_window(captures)

    def _request_main(self, metadata, fallback_frame=None):
        camera_id = metadata.get('camera_id')
        live=metadata.get('capture_origin')=='LIVE_MOTION_VIDEO'
        if live and fallback_frame is not None:
            try:
                if (fallback_frame.metadata.get('camera_id')!=camera_id
                        or fallback_frame.metadata.get('source_id')!=metadata.get('source_id')
                        or fallback_frame.metadata.get('stream_kind')!='sub'
                        or fallback_frame.metadata.get('frame_id')!=metadata.get('frame_id')
                        or activity_context(fallback_frame.metadata)!=activity_context(metadata)):
                    return False
            except (ValueError,KeyError,TypeError):return False
        with self.platform_lock:
            if not self._route_current(metadata):
                return False
            camera = self.cameras.get(camera_id)
        if camera is None or self.stop_event.is_set():
            return False
        with self.main_lock:
            if camera_id in self.main_pending or len(self.main_pending) >= self.main_frame_workers:
                return False
            self.main_pending.add(camera_id)
        def fetch():
            upgrade_failed=False
            try:
                with self.operation():
                    try:frame = camera.request_main(reason='insufficient_face_quality')
                    except Exception:
                        upgrade_failed=True
                        raise
                    if frame is None:upgrade_failed=True
                    if frame is not None:
                        if live:
                            if (frame.metadata.get('camera_id')!=camera_id
                                    or frame.metadata.get('source_id')!=metadata.get('source_id')
                                    or frame.metadata.get('stream_kind')!='main'):
                                raise ValueError('live main frame source differs from trigger')
                            # A new main frame owns its own receipt time and pixels.
                            # Do not send it back through the historical disk queue.
                            frame=replace(frame,metadata=dict(frame.metadata,capture_origin='LIVE_MOTION_VIDEO',
                                hd_upgrade_managed=True,hd_trigger_frame_id=metadata.get('frame_id'),
                                detection_native_tiles=True))
                            for routed in self._routed_frames(frame):
                                if activity_context(routed.metadata)==activity_context(metadata):
                                    if self.submit_frame(routed):self.count('live_main_frames_received')
                        elif getattr(camera, 'callback', None) is not None:
                            body=frame.encoded_image
                            if body is None:
                                import cv2
                                ok, encoded = cv2.imencode('.jpg', frame.image_bgr, [cv2.IMWRITE_JPEG_QUALITY,95])
                                if not ok:raise ValueError('main frame encoding failed')
                                body=encoded.tobytes()
                            self._continuous_capture(body,frame.metadata,frame.received_monotonic)
                        else:
                            for routed in self._routed_frames(frame):
                                self.submit_frame(routed)
            except Exception:
                self.count('main_request_errors')
            finally:
                # A usable main image must never be replaced by a sub guess.
                # Only acquisition failure may return the original low-res input.
                if live and upgrade_failed and fallback_frame is not None:
                    try:
                        with self.operation():
                            fallback=replace(fallback_frame,metadata=dict(fallback_frame.metadata,
                                hd_upgrade_managed=True,main_upgrade_failed=True))
                            for routed in self._routed_frames(fallback):
                                if activity_context(routed.metadata)==activity_context(metadata):
                                    if self.submit_frame(routed):self.count('live_main_fallback_frames')
                    except Exception:self.count('live_main_fallback_errors')
        try:
            future = self.main_pool.submit(fetch)
            def done(_):
                with self.main_lock:
                    self.main_pending.discard(camera_id)
            future.add_done_callback(done)
            return True
        except RuntimeError:
            with self.main_lock:
                self.main_pending.discard(camera_id)
            return False

    @runtime_operation
    def _poll_cycle(self):
        candidates, now = [], time.monotonic()
        with self.platform_lock:
            for camera_id, camera in list(self.cameras.items()):
                frame = camera.latest(max_age_s=self.queue.max_age_seconds)
                if frame is None:
                    continue
                for routed in self._routed_frames(frame):
                    meta = routed.metadata
                    key = encoded([meta['activity_id'], meta['source_id'], camera_id, meta.get('stream_kind', 'sub')])
                    category = self.priority.category(camera_id, activity_id=meta['activity_id'])
                    if (now-self.last_sample.get(key, 0) < 1/self.fps[category]
                            or self.last_frame.get(key) == meta['frame_id']):
                        continue
                    candidates.append(FrameJob(key, routed.image_bgr, meta, category, routed.received_monotonic))
        for job in self.admission.admit(self.queue, candidates):
            with self.platform_lock:
                self.last_sample[job.key] = now
                self.last_frame[job.key] = job.metadata['frame_id']
        self.stop_event.wait(.02)

    def _drop_acquisition(self):
        # Stop can join a callback. Never hold platform_lock across reader.stop().
        with self.binding_refresh_lock:
            if self.capture_processes:self.capture_processes.close()
            with self.platform_lock:
                self.platform_routes.clear()
                self.route_since.clear()
            super()._drop_acquisition()
            self.reader_signatures.clear()
            self.hik_signatures.clear()
            self.cleanup_pending = False
            self.binding_status = dict(status='PAUSED' if self.maintenance_paused else 'STOPPED', missing_camera_ids=[])

    def _camera_scopes(self):
        with self.platform_lock:
            return {encoded([context['activity_id'], context['source_id'], camera]):
                dict(activity_id=context['activity_id'], camera_id=camera, source_id=context['source_id'])
                for camera, contexts in self.platform_routes.items() for context in contexts if camera in self.cameras}

    def stop(self):
        super().stop()
        if self.capture_clock:
            self.capture_clock.close()

    def status(self):
        result = super().status()
        with self.platform_lock:
            result['platform_bindings'] = copy.deepcopy(self.binding_status)
            result['video_inputs'] = {camera: reader.metrics() for camera, reader in list(self.cameras.items())
                                      if hasattr(reader, 'metrics')}
            result['fleet_diagnostics'] = self.feed_diagnostics.snapshot(result['video_inputs'], self.binding_status)
        return result
