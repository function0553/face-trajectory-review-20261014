"""Camera acquisition -> quality-qualified embeddings -> durable P2 monitoring.

python -m face_engine.monitoring_service --config examples/monitoring.example.json
The P2 service owns identity matching, three personnel classes and trajectories.
This process owns model inference, fair frame scheduling and acquisition retry.
"""
import argparse
import base64
from collections import Counter, OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import struct
import threading
import time
import urllib.parse
from contextlib import contextmanager
from functools import wraps

import numpy as np

from face_engine.config import load_config
from face_engine.monitoring_delivery import DeliveryStore, delivery_key
from face_engine.scheduling import AdmissionPlanner, CameraPriority, FairLatestQueue, FrameJob, CATEGORIES
from face_engine.trajectory_bridge import (TrajectoryExtractor, encoded, activity_context,
    verify_activity_receipt, ActivityContextError, EventConflict,
    MaintenancePaused, ConsumerLeaseBusy, RetentionExpired, maintenance_request)


def runtime_operation(fn):
    """Fence a complete input/inference/send operation before maintenance."""
    @wraps(fn)
    def invoke(self, *args, **kwargs):
        with self.operation():
            return fn(self, *args, **kwargs)
    return invoke


def decode_image(raw, max_pixels=16_000_000):
    """Read dimensions before decoding untrusted JPEG/PNG callback bodies."""
    if not raw or len(raw) > 12*1024*1024:
        raise ValueError("image size outside allowed range")
    width = height = 0
    if raw.startswith(b'\x89PNG\r\n\x1a\n') and len(raw) >= 24:
        width, height = struct.unpack('>II', raw[16:24])
    elif raw.startswith(b'\xff\xd8'):
        pos = 2
        while pos < len(raw):
            if raw[pos] != 255:
                raise ValueError("invalid JPEG marker")
            while pos < len(raw) and raw[pos] == 255:
                pos += 1
            if pos >= len(raw):
                break
            marker = raw[pos]
            pos += 1
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if marker == 0xDA or pos+2 > len(raw):
                break
            size = int.from_bytes(raw[pos:pos+2], 'big')
            if size < 2 or pos+size > len(raw):
                raise ValueError("invalid JPEG segment")
            if marker in (0xC0, 0xC1, 0xC2):
                if size < 7:
                    raise ValueError("invalid JPEG dimensions")
                height, width = struct.unpack('>HH', raw[pos+3:pos+7])
                break
            pos += size
    if width < 1 or height < 1 or width*height > max_pixels:
        raise ValueError("supported JPEG/PNG dimensions required within pixel limit")
    import cv2
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    expected_shapes = {(height, width)}
    if raw.startswith(b'\xff\xd8'):
        # OpenCV honors JPEG EXIF orientation, including mirrored rotations.
        # Orientations 5-8 swap dimensions but preserve the prechecked pixel bound.
        expected_shapes.add((width, height))
    if image is None or image.shape[:2] not in expected_shapes:
        raise ValueError("image could not be decoded consistently")
    return image


def read_runtime_config(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if value.get('schema_version') != 1:
        raise ValueError("runtime schema_version must be 1")
    for key in ('model_config', 'state_file', 'policy', 'graph'):
        if key not in value:
            raise ValueError(f"runtime {key} required")
    for key in ('model_config', 'state_file', 'cameras_file'):
        if value.get(key):
            candidate = Path(value[key])
            value[key] = str(candidate if candidate.is_absolute() else path.parent/candidate)
    if value['policy'].get('max_clock_uncertainty_ms', 60000) > 60000:
        raise ValueError("camera error allowance cannot exceed 60000 ms")
    value['policy'].setdefault('max_clock_uncertainty_ms', 60000)
    value.setdefault('trajectory_options', {}).setdefault('max_clock_uncertainty_ms', 60000)
    value.setdefault('endpoint', 'http://127.0.0.1:8081')
    if urllib.parse.urlsplit(value['endpoint']).scheme not in ('http', 'https'):
        raise ValueError("HTTP(S) P2 endpoint required")
    return value


class MonitoringRuntime:
    def __init__(self, config, extractor, store):
        self.config, self.extractor, self.store = config, extractor, store
        scheduling = config.get('scheduling', {})
        self.queue = FairLatestQueue(weights=tuple(scheduling.get('weights', [6, 3, 1])),
            max_cameras=scheduling.get('max_queued_cameras', 128),
            max_bytes=scheduling.get('max_queue_mb', 512)*1024*1024,
            max_age_seconds=scheduling.get('max_frame_age_seconds', 2))
        self.admission = AdmissionPlanner(weights=tuple(scheduling.get('weights', [6, 3, 1])))
        self.priority = CameraPriority(scheduling.get('priority_ttl_seconds', 10))
        frequencies = scheduling.get('fps', [6, 2, 1])
        if (not isinstance(frequencies, (list, tuple)) or len(frequencies) != 3 or
                any(type(x) not in (int, float) or not np.isfinite(x) or x <= 0 for x in frequencies)):
            raise ValueError("all categories need a positive sampling frequency")
        self.fps = dict(zip(CATEGORIES, frequencies))
        self.stop_event = threading.Event()
        self.cameras, self.hikvision, self.camera_contexts = {}, [], {}
        self.stats_lock = threading.Lock()
        self.counters = Counter()
        self.latencies = deque(maxlen=1000)
        self.pending_latency = {}
        self.completed_latency = deque(maxlen=1000)
        self.live_last_processed = OrderedDict()
        self.live_intervals = {category: deque(maxlen=3000) for category in CATEGORIES}
        self.last_sample, self.last_frame, self.last_main_probe = {}, {}, {}
        self.poll_cursor = 0
        self.main_pending = set()
        self.main_lock = threading.Lock()
        self.main_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='main-frame')
        self.threads = []
        self.consumer_lease = None
        self.maintenance_condition = threading.Condition(threading.RLock())
        self.operation_local = threading.local()
        self.active_operations = 0
        self.maintenance_paused = False
        self.maintenance_state = 'NEW'
        self.maintenance_thread = None

    @contextmanager
    def operation(self):
        depth = getattr(self.operation_local, 'depth', 0)
        if depth == 0:
            with self.maintenance_condition:
                if self.maintenance_paused or maintenance_request(self.store.path).exists() or self.stop_event.is_set():
                    raise MaintenancePaused('P1 maintenance or shutdown; retry the same event')
                self.active_operations += 1
        self.operation_local.depth = depth + 1
        try:
            yield
        finally:
            self.operation_local.depth = depth
            if depth == 0:
                with self.maintenance_condition:
                    self.active_operations -= 1
                    self.maintenance_condition.notify_all()

    def count(self, key, amount=1):
        with self.stats_lock:
            self.counters[key] += amount

    @runtime_operation
    def submit_frame(self, frame):
        meta = dict(frame.metadata)
        context = activity_context(meta)
        key = encoded([context['activity_id'], meta['source_id'], meta['camera_id'], meta.get('stream_kind', 'sub')])
        category = self.priority.category(meta['camera_id'], activity_id=context['activity_id'])
        return self.queue.put(FrameJob(key, frame.image_bgr, meta, category, frame.received_monotonic))

    @runtime_operation
    def submit_capture(self, image_bytes, metadata):
        decode_image(image_bytes)
        return self.store.capture(image_bytes, metadata, self.priority.category(metadata['camera_id'], activity_id=metadata.get('activity_id')))

    def _configured_metadata(self, frame):
        """Attach exactly one explicit, versioned configured input route."""
        meta = dict(frame.metadata)
        context = self.camera_contexts.get(meta.get('camera_id'))
        if context is None or context['source_id'] != meta.get('source_id'):
            raise ActivityContextError('acquisition has no configured activity route')
        route = activity_context(context)
        for field, value in route.items():
            if field in meta and meta[field] != value:
                raise ActivityContextError('acquisition activity route conflict')
        meta.update(route)
        return meta

    @runtime_operation
    def _hikvision_frame(self, frame):
        import cv2
        ok, body = cv2.imencode('.png', frame.image_bgr)
        if not ok:
            raise ValueError("capture encoding failed")
        metadata = self._configured_metadata(frame)
        self.store.capture(body.tobytes(), metadata, self.priority.category(metadata['camera_id'], activity_id=metadata['activity_id']))

    def _request_main(self, metadata):
        if self.stop_event.is_set():
            return
        camera_id = metadata['camera_id']
        context = self.camera_contexts.get(camera_id)
        if context is None or context['source_id'] != metadata['source_id'] or activity_context(context) != activity_context(metadata):
            self.count('main_request_unbound_activity')
            return
        camera = self.cameras.get(camera_id)
        if camera is None:
            return
        with self.main_lock:
            if self.stop_event.is_set():
                return
            if camera_id in self.main_pending or len(self.main_pending) >= 2:
                self.count('main_request_busy')
                return
            self.main_pending.add(camera_id)

        def fetch():
            try:
                with self.operation():
                    # The native acquisition and its image are part of quiescence.
                    frame = camera.request_main(reason='insufficient_face_quality')
                    if frame is not None:
                        frame = replace(frame, metadata=self._configured_metadata(frame))
                        self.submit_frame(frame)
                        self.count('main_frames_received')
            except Exception:
                self.count('main_request_errors')
            finally:
                with self.main_lock:
                    self.main_pending.discard(camera_id)
        def clear_pending(_future=None):
            with self.main_lock:
                self.main_pending.discard(camera_id)
        try:
            future = self.main_pool.submit(fetch)
            future.add_done_callback(clear_pending)
        except RuntimeError:
            clear_pending()
            if not self.stop_event.is_set():
                raise

    def _deliveries(self, observations):
        result = []
        for observation in observations:
            context = activity_context(observation)
            # P2 normalizes raw camera time and ignores legacy qualification claims.
            payload = dict(observation=observation,
                           time_record=observation.get('time_record'),
                           policy=self.config['policy'], graph=self.config['graph'],
                           options=self.config['trajectory_options'])
            payload.update(context)
            route = '/v1/monitoring/observe'
            key = delivery_key(route, payload)
            result.append((key, route, payload, self.priority.category(observation['camera_id'], activity_id=context['activity_id'])))
        return result

    @runtime_operation
    def process_frame(self, image, metadata, *, capture_key=None, received_monotonic=None, scheduling_category=None):
        start = time.monotonic()
        context = activity_context(metadata)
        scheduling_category = scheduling_category or self.priority.category(metadata['camera_id'], activity_id=context['activity_id'])
        result = self.extractor.extract_detailed([(image, metadata)])[0]
        deliveries = self._deliveries(result['observations'])
        with self.stats_lock:
            for observation in result['observations']:
                if len(self.pending_latency) < self.store.max_pending + self.store.max_faces_per_capture:
                    self.pending_latency[(context['activity_id'], observation['source_id'], observation['observation_id'])] = received_monotonic if received_monotonic is not None else start
        # Store quality/decoding outcomes, not a second copy of biometric vectors.
        report = {k: v for k, v in result.items() if k != 'observations'}
        report['observation_ids'] = [item['observation_id'] for item in result['observations']]
        if capture_key:
            self.store.finish_capture(capture_key, report, deliveries)
        else:
            for item in deliveries:
                self.store.enqueue(*item)
        if result['needs_high_resolution'] and metadata.get('stream_kind') != 'main':
            self._request_main(metadata)
        elif result.get('detected_faces') == 0 and metadata.get('stream_kind') == 'sub':
            # No detection in a low-resolution stream is not proof that no face
            # exists. Probe sparingly; every replacement is independent evidence.
            interval = self.config.get('scheduling', {}).get('empty_main_probe_seconds', 30)
            camera_id = encoded([context['activity_id'], metadata['source_id'], metadata['camera_id']])
            if interval > 0 and start-self.last_main_probe.get(camera_id, 0) >= interval:
                self.last_main_probe[camera_id] = start
                self._request_main(metadata)
        elapsed = time.monotonic()-start
        with self.stats_lock:
            self.counters['processed_frames'] += 1
            self.counters['qualified_faces'] += len(result['observations'])
            self.counters['quality_rejected_faces'] += len(result['rejected_faces'])
            self.latencies.append(dict(inference_seconds=elapsed,
                acquisition_to_enqueued_seconds=time.monotonic()-received_monotonic if received_monotonic is not None else None))
        if metadata.get('stream_kind') in ('sub', 'main'):
            self._record_live_completion(encoded([context['activity_id'], metadata['source_id'], metadata['camera_id']]), scheduling_category, time.monotonic())
        return report

    def _record_live_completion(self, camera_id, category, completed):
        with self.stats_lock:
            previous = self.live_last_processed.pop(camera_id, None)
            if previous is not None:
                self.live_intervals[category].append(completed-previous)
            self.live_last_processed[camera_id] = completed
            if len(self.live_last_processed) > 4096:
                self.live_last_processed.popitem(last=False)
                self.counters['camera_coverage_metrics_evicted'] += 1

    def _receipt(self, request, reply):
        verify_activity_receipt(request, reply, require_route='observation' in request or 'direction' in request)
        observation = request.get('observation', {})
        monitor = reply.get('monitoring') or {}
        if isinstance(monitor, dict):
            # The activity is part of the hint key; an EXIT can affect only it.
            self.priority.observe(observation.get('camera_id'), monitor.get('target_id'),
                monitor.get('category'), monitor.get('continue_monitoring', monitor.get('status') == 'ACTIVE'), revision=monitor,
                activity_id=request['activity_id'])
        with self.stats_lock:
            started = self.pending_latency.pop((request['activity_id'], observation.get('source_id'), observation.get('observation_id')), None)
            if started is not None:
                self.completed_latency.append(time.monotonic()-started)
        self.count('p2_' + str(reply.get('status', 'ACKNOWLEDGED')))

    def _send_loop(self):
        authorization = os.environ.get(self.config.get('p2_authorization_env', 'FACE_TRAJECTORY_AUTHORIZATION'))
        while not self.stop_event.is_set():
            try:
                with self.operation():
                    sent = self.store.send_one(self.config['endpoint'], timeout=self.config.get('p2_timeout_seconds', 3),
                        authorization=authorization, on_receipt=self._receipt)
            except MaintenancePaused:
                sent = False
            except Exception:
                self.count('delivery_internal_errors')
                sent = False
            if not sent:
                self.stop_event.wait(.05)

    @runtime_operation
    def _infer_cycle(self, capture_turn):
        capture = self.store.next_capture() if capture_turn else None
        job = None if capture else self.queue.pop()
        if job is None and capture is None:
            capture = self.store.next_capture()
        capture_turn = not capture_turn
        if job is None and capture is None:
            self.stop_event.wait(.01)
            return capture_turn
        try:
            if capture:
                key, body, metadata = capture
                self.process_frame(decode_image(body), metadata, capture_key=key)
            else:
                self.process_frame(job.image, job.metadata, received_monotonic=job.received_monotonic, scheduling_category=job.category)
        except (ValueError, KeyError) as error:
            self.count('input_errors')
            if capture:
                self.store.finish_capture(capture[0], {'status': 'REJECTED', 'reason': type(error).__name__}, [])
        except MaintenancePaused:
            # A request arriving during an admitted capture leaves it pending
            # for retry. Release its image-bearing exception stack before the
            # operation fence drains and hands the consumer lease to C++.
            return capture_turn
        except Exception:
            self.count('inference_or_capacity_errors')
            # Durable captures remain pending on transient inference/storage failure.
            self.stop_event.wait(.25)
        return capture_turn

    def _infer_loop(self):
        capture_turn = True
        while not self.stop_event.is_set():
            try:
                capture_turn = self._infer_cycle(capture_turn)
            except MaintenancePaused:
                self.stop_event.wait(.05)

    @runtime_operation
    def _poll_cycle(self):
        now = time.monotonic()
        candidates = []
        items = list(self.cameras.items())
        if items:
            start = self.poll_cursor % len(items)
            items = items[start:] + items[:start]
            self.poll_cursor += 1
        for camera_id, camera in items:
            context = self.camera_contexts[camera_id]
            category = self.priority.category(camera_id, activity_id=context['activity_id'])
            if now-self.last_sample.get(camera_id, 0) < 1/self.fps[category]:
                continue
            frame = camera.latest(after_frame_id=self.last_frame.get(camera_id),
                max_age_s=self.queue.max_age_seconds)
            if frame is not None:
                meta = self._configured_metadata(frame)
                key = encoded([meta['activity_id'], meta['source_id'], meta['camera_id'], meta.get('stream_kind', 'sub')])
                candidates.append(FrameJob(key, frame.image_bgr, meta, category, frame.received_monotonic))
        for job in self.admission.admit(self.queue, candidates):
            camera_id = job.metadata['camera_id']
            self.last_sample[camera_id] = now
            self.last_frame[camera_id] = job.metadata['frame_id']
        self.stop_event.wait(.02)

    def _poll_loop(self):
        while not self.stop_event.is_set():
            try:
                self._poll_cycle()
            except MaintenancePaused:
                self.stop_event.wait(.05)

    def configure_cameras(self):
        if not self.config.get('cameras_file'):
            return
        from face_engine.acquisition import CameraAcquisition, HikvisionEventReader
        config = json.loads(Path(self.config['cameras_file']).read_text(encoding='utf-8-sig'))
        if config.get('schema_version') != 1:
            raise ValueError("camera schema_version must be 1")
        if self.cameras or self.hikvision:
            raise RuntimeError('cameras are already configured')
        seen_camera_ids, subscriptions = set(), {}
        for item in config['cameras']:
            camera_id, source_id = item['camera_id'], item['source_id']
            context = activity_context(item.get('activity_context'))
            if not isinstance(camera_id, str) or not camera_id or not isinstance(source_id, str) or not source_id:
                raise ValueError('nonempty camera_id and source_id required')
            if camera_id in seen_camera_ids:
                raise ValueError('duplicate camera_id')
            seen_camera_ids.add(camera_id)
            self.camera_contexts[camera_id] = dict(context, source_id=source_id)
            def environment(field, required=False):
                name = item.get(field)
                result = os.environ.get(name, '') if name else ''
                if required and not result:
                    raise ValueError(f"missing camera environment variable for {field}")
                return result or None
            if item.get('sub_url_env'):
                camera = CameraAcquisition(environment('sub_url_env', True), environment('main_url_env'),
                    source_id=source_id, camera_id=camera_id,
                    sample_fps=item.get('sample_fps', 10), ffmpeg=environment('ffmpeg_path_env') or 'ffmpeg',
                    clock_profile_version=item.get('clock_profile_version'),
                    backend=item.get('backend', 'ffmpeg'), gst_python=environment('gst_python_env'))
                self.cameras[camera_id] = camera
            hik = item.get('hikvision', {})
            if hik.get('enabled'):
                values = {name: os.environ.get(hik.get(name+'_env', ''), '') for name in ('alert_url', 'username', 'password')}
                if not all(values.values()):
                    raise ValueError('Hikvision connection environment is incomplete')
                if not hik.get('expected_channel_id'):
                    raise ValueError('Hikvision expected_channel_id is required to bind NVR channel to camera')
                # Several NVR channels share one alertStream. Subscribe once per
                # actual URL/credentials, then bind each event's explicit channel.
                subscription_key = (values['alert_url'], values['username'], values['password'])
                bindings = subscriptions.setdefault(subscription_key, {})
                channel = hik['expected_channel_id']
                if channel in bindings:
                    raise ValueError('conflicting Hikvision channel binding')
                bindings[channel] = dict(source_id=source_id, camera_id=camera_id,
                    clock_profile_version=item.get('clock_profile_version'))
        for (url, username, password), bindings in subscriptions.items():
            self.hikvision.append(HikvisionEventReader(url, username=username, password=password,
                on_frame=self._hikvision_frame, channel_bindings=bindings))

    def start(self):
        if self.consumer_lease is not None or self.stop_event.is_set():
            raise RuntimeError('runtime is already started or stopped')
        lease = self.store.consumer_lease(wait=True)
        lease.__enter__()
        self.consumer_lease = lease
        self.maintenance_state = 'RUNNING'
        try:
            for camera in self.cameras.values():
                camera.start()
            for reader in self.hikvision:
                reader.start()
            for name, fn in [('infer', self._infer_loop), ('deliver', self._send_loop), ('sample', self._poll_loop)]:
                thread = threading.Thread(target=fn, name='monitoring-'+name, daemon=True)
                self.threads.append(thread)
                thread.start()
            self.maintenance_thread = threading.Thread(target=self._maintenance_loop,
                name='monitoring-maintenance', daemon=True)
            self.maintenance_thread.start()
        except BaseException:
            # start() is also a public library API: failed partial startup must
            # stop prior readers and release the lease without requiring CLI finally.
            # If cleanup itself fails, stop() retains the lease and state stays open.
            self.stop()
            raise

    def _purge_hot_state(self):
        with self.queue.lock:
            for queue in self.queue.queues.values():
                queue.clear()
            self.queue.bytes = 0
        with self.priority.lock:
            self.priority.hints.clear()
            self.priority.revisions.clear()
        self.admission.retain_keys(())
        with self.stats_lock:
            self.pending_latency.clear()
            self.live_last_processed.clear()
            self.latencies.clear()
            self.completed_latency.clear()
            for values in self.live_intervals.values():
                values.clear()
        self.last_frame.clear()
        self.last_sample.clear()
        self.last_main_probe.clear()
        with self.main_lock:
            self.main_pending.clear()

    def _pause_for_maintenance(self):
        with self.maintenance_condition:
            self.maintenance_paused = True
            self.maintenance_state = 'QUIESCING'
            while self.active_operations and not self.stop_event.is_set():
                self.maintenance_condition.wait(.05)
            if self.stop_event.is_set():
                return
        self._drop_acquisition()
        self._purge_hot_state()
        self.store.close()
        if self.consumer_lease is not None:
            self.consumer_lease.__exit__(None, None, None)
            self.consumer_lease = None
        self.maintenance_state = 'PAUSED'
        self.count('maintenance_pauses')

    def _drop_acquisition(self):
        # Return from the shutdown helper before releasing the lease: even the
        # final loop variable may own a native reader's cached image. Existing
        # acquisition instances cannot restart; resume constructs fresh ones.
        for camera in self.cameras.values():
            camera.stop()
        for reader in self.hikvision:
            reader.stop()
        self.main_pool.shutdown(wait=True, cancel_futures=True)
        self.cameras.clear()
        self.hikvision.clear()
        self.camera_contexts.clear()

    def _resume_after_maintenance(self):
        lease = self.store.consumer_lease()
        try:
            lease.__enter__()
        except (MaintenancePaused, ConsumerLeaseBusy):
            # Marker removal and the C++ owner's OS lease release are separate
            # operations. Stay paused and retry on the next maintenance tick;
            # expected handoff contention is not a failed SQLite/native resume.
            self.count('maintenance_resume_waits')
            return
        self.consumer_lease = lease
        try:
            self.store.reopen()
            self.main_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='main-frame')
            self.configure_cameras()
            for camera in self.cameras.values():
                camera.start()
            for reader in self.hikvision:
                reader.start()
            with self.maintenance_condition:
                self.maintenance_paused = False
                self.maintenance_state = 'RUNNING'
                self.maintenance_condition.notify_all()
            self.count('maintenance_resumes')
        except BaseException:
            # Hold the lease after any partial native restart; releasing it with
            # a live reader would permit cleanup while its image is in memory.
            self.maintenance_state = 'RESUME_FAILED'
            raise

    def _maintenance_loop(self):
        request = maintenance_request(self.store.path)
        while not self.stop_event.is_set():
            try:
                if ((request.exists() and self.maintenance_state != 'PAUSED') or
                        self.maintenance_state in ('PAUSE_FAILED', 'RESUME_FAILED')):
                    self._pause_for_maintenance()
                elif not request.exists() and self.maintenance_state == 'PAUSED':
                    self._resume_after_maintenance()
            except Exception:
                self.count('maintenance_errors')
                if self.maintenance_state == 'QUIESCING':
                    self.maintenance_state = 'PAUSE_FAILED'
                # Errors retain the lease or PAUSED state, and are observable.
            self.stop_event.wait(.05)

    def status(self):
        if maintenance_request(self.store.path).exists() or self.maintenance_paused:
            with self.maintenance_condition:
                return dict(ok=True, maintenance=dict(state=self.maintenance_state,
                    active_operations=self.active_operations, requested=maintenance_request(self.store.path).exists()),
                    delivery={'status': 'MAINTENANCE_PAUSED'})
        with self.stats_lock:
            samples = list(self.latencies)
            counters = dict(self.counters)
            completed = list(self.completed_latency)
            live_last = dict(self.live_last_processed)
            live_intervals = {category: list(values) for category, values in self.live_intervals.items()}
        def summary(key):
            values = [row[key] for row in samples if row[key] is not None]
            return {'samples': len(values), 'p50_seconds': float(np.percentile(values, 50)),
                    'p95_seconds': float(np.percentile(values, 95)), 'max_seconds': max(values)} if values else {'samples': 0}
        now = time.monotonic()
        camera_scopes = {encoded([context['activity_id'], context['source_id'], camera]):
            dict(activity_id=context['activity_id'], camera_id=camera, source_id=context['source_id'])
            for camera, context in self.camera_contexts.items() if camera in self.cameras}
        never = sorted(set(camera_scopes)-set(live_last))
        ages = sorted(((camera, max(0, now-live_last[camera])) for camera in camera_scopes if camera in live_last), key=lambda item: item[1], reverse=True)
        coverage = dict(basis='interval between completed live-frame inspections; separate from frame age and result latency',
            configured_live_cameras=len(self.cameras), never_processed_count=len(never), never_processed_sample=never[:10],
            longest_since_last_processed=[dict(camera_scopes[camera], seconds=age) for camera, age in ages[:10]],
            processing_interval_by_current_scheduling_class={category: dict(samples=len(values),
                p95_seconds=float(np.percentile(values, 95)) if values else None,
                max_seconds=max(values) if values else None) for category, values in live_intervals.items()})
        with self.maintenance_condition:
            maintenance = dict(state=self.maintenance_state, active_operations=self.active_operations,
                requested=maintenance_request(self.store.path).exists())
        try:
            with self.operation():
                delivery_status = self.store.status()
        except MaintenancePaused:
            delivery_status = {'status': 'MAINTENANCE_PAUSED'}
        return dict(ok=True, counters=counters, queue=self.queue.stats(), admission=self.admission.stats(), delivery=delivery_status, maintenance=maintenance, live_camera_coverage=coverage,
            latency={key: summary(key) for key in ('inference_seconds', 'acquisition_to_enqueued_seconds')},
            backend={'detector': getattr(self.extractor.detector, 'backend', 'UNKNOWN'),
                     'recognizer': getattr(self.extractor.recognizer, 'backend', 'UNKNOWN')},
            p2_ack_latency={'samples': len(completed), 'p95_seconds': float(np.percentile(completed, 95)) if completed else None,
                            'basis': 'decode receipt for live frames; inference start for durable captures; not camera transport latency'},
            threads={thread.name: thread.is_alive() for thread in self.threads},
            clocks='camera evidence required; reception time does not qualify',
            camera_count=len(self.cameras), capture_source_count=len(self.hikvision))

    def stop(self):
        self.stop_event.set()
        if self.maintenance_thread is not None:
            self.maintenance_thread.join(timeout=20)
            if self.maintenance_thread.is_alive():
                raise RuntimeError('maintenance quiescence did not stop; keep state database open')
        self._drop_acquisition()
        for thread in self.threads:
            if thread.ident is not None:
                thread.join(timeout=10)
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError('monitoring thread did not stop; keep state database open')
        deadline = time.monotonic() + 20
        with self.maintenance_condition:
            while self.active_operations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('input operation did not stop; retain consumer lease')
                self.maintenance_condition.wait(min(.05, remaining))
        # C++ maintenance is permitted only after this consumer lease is released.
        # Drop all pending frames, old response hints and latency/camera keys first;
        # stopping a P1 process for maintenance is independent of P2 monitoring.
        self._purge_hot_state()
        self.store.close()
        if self.consumer_lease is not None:
            self.consumer_lease.__exit__(None, None, None)
            self.consumer_lease = None


def make_http_server(runtime, host='127.0.0.1', port=8092, token=None):
    if host not in ('localhost',) and not ipaddress.ip_address(host).is_loopback and not token:
        raise ValueError('non-loopback callback listener requires a configured bearer token')

    def input_operation(fn):
        @wraps(fn)
        def invoke(handler):
            if fn.__name__ == 'do_GET' and handler.path == '/health':
                return fn(handler)
            try:
                with runtime.operation():
                    return fn(handler)
            except MaintenancePaused:
                handler.close_connection = True
                handler.respond(503, dict(ok=False, error='maintenance paused; retry the same logical event'))
        return invoke

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, value):
            body = encoded(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            import hmac
            if token and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+token):
                self.respond(401, dict(ok=False, error='authentication required'))
                return False
            return True

        @input_operation
        def do_GET(self):
            if not self.authorized():
                return
            if self.path == '/health':
                self.respond(200, runtime.status())
            elif urllib.parse.urlsplit(self.path).path == '/v1/captures/status':
                try:
                    params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,
                        keep_blank_values=True, strict_parsing=True, max_num_fields=8)
                    if set(params) != {'capture_id', 'activity_id'} or any(len(params[k]) != 1 or not params[k][0] for k in params):
                        raise ValueError('exactly one nonempty capture_id and activity_id required')
                    with runtime.operation():
                        result = runtime.store.capture_status(params['capture_id'][0])
                    if result is None:
                        self.respond(404, dict(ok=False, error='capture not found'))
                    elif result.get('activity_id') != params['activity_id'][0]:
                        self.respond(403, dict(ok=False, error='capture activity context mismatch'))
                    else:
                        self.respond(200, dict(ok=True, status='FOUND', **result))
                except (ValueError, TypeError):
                    self.respond(400, dict(ok=False, error='exactly one nonempty capture_id and activity_id required'))
                except Exception:
                    self.respond(503, dict(ok=False, error='capture status unavailable'))
            else:
                self.respond(404, dict(ok=False, error='route not found'))

        @input_operation
        def do_POST(self):
            if not self.authorized():
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 17*1024*1024:
                    self.respond(413, dict(ok=False, error='body exceeds limit'))
                    return
                self.connection.settimeout(10)
                value = json.loads(self.rfile.read(size))
                if self.path == '/v1/captures':
                    raw = base64.b64decode(value['image_base64'], validate=True)
                    key = runtime.submit_capture(raw, value['metadata'])
                    state = runtime.store.capture_status(key)
                    self.respond(202, dict(ok=True, status='QUARANTINED' if state['state'] == 'QUARANTINED' else 'DURABLY_QUEUED', capture_id=key,
                        **{k: state[k] for k in ('activity_id', 'activity_num', 'config_version', 'binding_version', 'monitoring_version', 'route_rule_id')}))
                elif self.path == '/v1/gate-events':
                    route = '/v1/monitoring/gate-event'
                    key = delivery_key(route, value)
                    runtime.store.enqueue(key, route, value, 'PRIORITY')
                    state = runtime.store.status(key)
                    self.respond(202, dict(ok=True, status='QUARANTINED' if state['status'] == 'QUARANTINED' else 'DURABLY_QUEUED', delivery_id=key,
                        **{k: value.get(k) for k in ('activity_id', 'activity_num', 'config_version', 'binding_version', 'monitoring_version', 'route_rule_id')}))
                else:
                    self.respond(404, dict(ok=False, error='route not found'))
            except EventConflict:
                self.respond(409, dict(ok=False, error='scoped event content conflict'))
            except RetentionExpired:
                self.respond(410, dict(ok=False, status='EXPIRED', error='expired event cannot be recreated'))
            except (ValueError, KeyError, TypeError):
                self.respond(400, dict(ok=False, error='invalid capture or gate event'))
            except Exception:
                self.respond(503, dict(ok=False, error='durable input unavailable; retry same event'))
    return ThreadingHTTPServer((host, port), Handler)


def enroll_manifest(path, extractor, store):
    path = Path(path).resolve()
    entries = json.loads(path.read_text(encoding='utf-8-sig'))['persons']
    queued = []
    for person in entries:
        context = activity_context(person, require_route=False)
        if any(not isinstance(person.get(k), str) or not person[k] for k in ('source_id', 'event_id')):
            raise ValueError('enrollment source_id and per-person sync event_id required')
        if person['category'] not in ('PRIORITY', 'NORMAL'):
            raise ValueError('enrolled persons require PRIORITY or NORMAL')
        templates = []
        if not person['photos']:
            raise ValueError('person requires at least one photograph')
        for photo_path in person['photos']:
            local = Path(photo_path)
            if not local.is_absolute():
                local = path.parent/local
            raw = local.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            meta = dict(context, source_id=person['source_id'], camera_id='enrollment', frame_id=digest,
                time_source='UNKNOWN', purpose='GALLERY_ENROLLMENT')
            detail = extractor.extract_detailed([(decode_image(raw), meta)])[0]
            if detail['detected_faces'] != 1 or len(detail['observations']) != 1:
                raise ValueError(f"enrollment requires exactly one quality-qualified face: {local.name}")
            observation = detail['observations'][0]
            template_id = 'enrolled:'+hashlib.sha256(encoded([context['activity_id'], person['person_id'], person['revision'], digest]).encode()).hexdigest()
            templates.append(dict(template_id=template_id, embedding=observation['embedding'], feature_space=extractor.feature_space))
        payload = {key: person[key] for key in ('person_id', 'category', 'revision', 'expected_revision', 'evidence_id')}
        for field in ('person_name', 'profile_photo_url'):
            if field in person:
                payload[field] = person[field]
        payload.update(context, source_id=person['source_id'], event_id=person['event_id'])
        payload['templates'] = templates
        key = delivery_key('/v1/monitoring/persons', payload)
        store.enqueue(key, '/v1/monitoring/persons', payload)
        queued.append(key)
    return queued


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--validate', action='store_true', help='validate config without models/network')
    parser.add_argument('--capacity-report', action='store_true', help='report unmeasured/configured capacity without models or network')
    parser.add_argument('--enroll', help='ID/category/photos manifest; durably enqueue enrollment')
    parser.add_argument('--flush-only', action='store_true')
    parser.add_argument('--run-seconds', type=float, default=0)
    args = parser.parse_args()
    config = read_runtime_config(args.config)
    if args.capacity_report:
        from face_engine.capacity import capacity_report
        cameras = json.loads(Path(config['cameras_file']).read_text(encoding='utf-8-sig')) if config.get('cameras_file') else {'cameras': []}
        print(encoded(capacity_report(config, cameras)))
        return
    if args.validate:
        load_config(config['model_config'])
        print(encoded(dict(ok=True, status='CONFIG_VALIDATED_ONLY')))
        return
    store = DeliveryStore(config['state_file'], weights=tuple(config.get('scheduling', {}).get('weights', [6, 3, 1])), **config.get('storage', {}))
    runtime = server = None
    try:
        if args.flush_only:
            authorization = os.environ.get(config.get('p2_authorization_env', 'FACE_TRAJECTORY_AUTHORIZATION'))
            store.close()
            def flush_once():
                with store.consumer_lease(wait=True):
                    store.reopen()
                    try:
                        return store.send_one(config['endpoint'], authorization=authorization)
                    finally:
                        store.close()
            while flush_once():
                pass
            with store.consumer_lease(wait=True):
                store.reopen()
                print(encoded(store.status()))
            return
        model_config = load_config(config['model_config'])
        extractor = TrajectoryExtractor.from_config(model_config)
        # Treat explicit cuda:<id> exactly like cuda for the fail-fast
        # production guard; otherwise a failed CUDA initialization could be
        # accepted silently when a worker is pinned to the second GPU.
        if str(model_config['device']).startswith('cuda') and any(
                getattr(model, 'backend', None) != 'CUDA' for model in (extractor.detector, extractor.recognizer)):
            raise RuntimeError('requested CUDA models did not initialize on CUDA; use device: cpu explicitly after measuring the intended load')
        if args.enroll:
            print(encoded(dict(queued=enroll_manifest(args.enroll, extractor, store))))
            return
        runtime = MonitoringRuntime(config, extractor, store)
        runtime.configure_cameras()
        http = config.get('http', {})
        server = make_http_server(runtime, http.get('host', '127.0.0.1'), http.get('port', 8092),
            os.environ.get(http.get('token_env', 'FACE_MONITORING_TOKEN')))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime.start()
        deadline = time.monotonic()+args.run_seconds if args.run_seconds else None
        while deadline is None or time.monotonic() < deadline:
            runtime.stop_event.wait(.25)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if runtime:
            runtime.stop()
        store.close()


if __name__ == '__main__':
    main()
