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
from face_engine.trajectory_bridge import ActivityContextError, activity_context, encoded


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
        self.next_binding_poll = 0
        self.uuid_sources = None
        if config.get('uuid_sources'):
            from face_engine.uuid_sources import UUIDSources
            self.uuid_sources = UUIDSources(config['uuid_sources'])
        self.capture_clock = None
        if config.get('camera_clock'):
            from face_engine.camera_clock import CameraClock
            self.capture_clock = CameraClock(config['camera_clock'], config['camera_clock'].get('endpoint', config['endpoint']),
                os.environ.get(config.get('p2_authorization_env', 'FACE_TRAJECTORY_AUTHORIZATION'), ''))

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

    def _refresh_bindings(self, *, start_readers):
        from face_engine.acquisition import CameraAcquisition, HikvisionEventReader
        if self.cleanup_pending:
            if not self._stop_inputs():
                self.next_binding_poll = time.monotonic()+self.binding_interval
                return
            self.cleanup_pending = False
        try:
            routes = binding_snapshot(self.binding_state, int(time.time()*1000))
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
                requested = set(routes) - set(entries)
                resolved = self.uuid_sources.resolve(requested)
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
                    signatures[camera] = (encoded([sub, main, args, dynamic]), sub, main, args, dynamic)
                if dynamic and item.get('continuous'):
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
                            from face_engine.continuous_capture import ContinuousCamera
                            args = {k:v for k,v in args.items() if k!='continuous'}
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
            self.binding_status = dict(status='MISSING_VIDEO_SOURCE' if missing else ('BOUND' if routes else 'IDLE'),
                bound_camera_count=len(routes), active_input_count=len(live),
                activity_route_count=sum(map(len, live.values())), missing_camera_ids=sorted(missing))
            if self.uuid_sources is not None:
                self.binding_status['uuid_sources'] = copy.deepcopy(self.uuid_sources.status)
            self.count('platform_binding_refreshes')
            # Changing activities must not accumulate unbounded scheduler keys.
            keys = {encoded([route['activity_id'], route['source_id'], camera, kind])
                    for camera, contexts in live.items() for route in contexts for kind in ('sub', 'main')}
            self.last_sample = {key: value for key, value in self.last_sample.items() if key in keys}
            self.last_frame = {key: value for key, value in self.last_frame.items() if key in keys}
            self.admission.retain_keys(keys)
        except Exception:
            self.binding_status = dict(status='BINDING_SOURCE_UNAVAILABLE', missing_camera_ids=[])
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
            self.store.capture(body, meta,
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
                self.store.capture(body.tobytes(), meta,
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

    @runtime_operation
    def process_frame(self, image, metadata, **kwargs):
        with self.platform_lock:
            if not self._route_current(metadata):
                raise ActivityContextError('platform camera route was revoked; frame rejected')
        return super().process_frame(image, metadata, **kwargs)

    def _request_main(self, metadata):
        camera_id = metadata['camera_id']
        with self.platform_lock:
            if not self._route_current(metadata):
                return
            camera = self.cameras.get(camera_id)
        if camera is None or self.stop_event.is_set():
            return
        with self.main_lock:
            if camera_id in self.main_pending or len(self.main_pending) >= 2:
                return
            self.main_pending.add(camera_id)
        def fetch():
            try:
                with self.operation():
                    frame = camera.request_main(reason='insufficient_face_quality')
                    if frame is not None:
                        if getattr(camera, 'callback', None) is not None:
                            import cv2
                            ok, body = cv2.imencode('.jpg', frame.image_bgr, [cv2.IMWRITE_JPEG_QUALITY,95])
                            if not ok:
                                raise ValueError('main frame encoding failed')
                            self._continuous_capture(body.tobytes(),frame.metadata,frame.received_monotonic)
                        else:
                            for routed in self._routed_frames(frame):
                                self.submit_frame(routed)
            except Exception:
                self.count('main_request_errors')
        try:
            future = self.main_pool.submit(fetch)
            def done(_):
                with self.main_lock:
                    self.main_pending.discard(camera_id)
            future.add_done_callback(done)
        except RuntimeError:
            with self.main_lock:
                self.main_pending.discard(camera_id)

    @runtime_operation
    def _poll_cycle(self):
        if time.monotonic() >= self.next_binding_poll:
            self.refresh_bindings()
        candidates, now = [], time.monotonic()
        with self.platform_lock:
            for camera_id, camera in self.cameras.items():
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
            self.last_sample[job.key] = now
            self.last_frame[job.key] = job.metadata['frame_id']
        self.stop_event.wait(.02)

    def _drop_acquisition(self):
        # Stop can join a callback. Never hold platform_lock across reader.stop().
        with self.binding_refresh_lock:
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
        return result
