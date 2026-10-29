"""Continuous per-camera acquisition with durable motion candidates.

Every online bound source stays open. Motion admission reduces idle inference
cost; it never turns cameras off or depends on an enrolled person's identity.
"""
import hashlib
import threading
import time
import uuid

import cv2
import numpy as np

cv2.setNumThreads(1)

from face_engine.acquisition import _base_metadata
from face_engine.uuid_sources import FailoverCamera
from face_engine.analysis_admission import MotionAdmission
from face_engine.main_photo import pack_burst

# Each acquisition process has bounded, non-queued supplemental connections.
_HD_SLOTS = threading.BoundedSemaphore(2)


class ContinuousCamera:
    def __init__(self, sub_url, main_url, source_id, camera_id, *, connection=None,
                 on_capture, camera_clock=None, ffmpeg='ffmpeg', sample_fps=2, preferred_stream='main',
                 decode_keyframes_only=False, main_archive_directory=None, motion_admission=None,
                 main_photo_enabled=True):
        self.source_id, self.camera_id = source_id, camera_id
        self.connection = connection
        self.sub_url, self.main_url = sub_url, main_url
        self.callback, self.clock = on_capture, camera_clock
        self.ffmpeg, self.fps = ffmpeg, sample_fps
        self.preferred_stream = preferred_stream
        self.decode_keyframes_only = decode_keyframes_only
        self.main_archive_directory = main_archive_directory
        if type(main_photo_enabled) is not bool:
            raise ValueError('main_photo_enabled must be boolean')
        self.main_photo_enabled = main_photo_enabled
        self.photo_frames = []
        self.photo_started = None
        self.hd_thread = None
        self.hd_reader = None
        self.next_hd = 0
        self.stop_event = threading.Event()
        self.thread = None
        self.video = None
        self.lock = threading.RLock()
        self.motion = MotionAdmission(**(motion_admission or {}))
        self.motion_stream_kind = None
        self.state = dict(state='NEW', frames=0, persisted_candidates=0, idle_frames=0,
                          capture_errors=0, inbox_errors=0, last_error=None, stream_kind='main',
                          hd_requests=0, hd_bursts=0, hd_frames=0, hd_errors=0,
                          hd_busy=0, main_photo_bursts=0)
        self.last_frame_at = None
        self.session_id = uuid.uuid4().hex

    def start(self):
        with self.lock:
            if self.thread is not None:
                return self
            self.thread = threading.Thread(target=self._run, name='continuous-capture', daemon=True)
            self.thread.start()
        return self

    def _set(self, **values):
        with self.lock:
            self.state.update(values)

    def _accept(self, body, image, metadata, now):
        self._flush_photo(now)
        stream_kind = metadata.get('stream_kind')
        if stream_kind != self.motion_stream_kind:
            self.motion.reset()
            self.motion_stream_kind = stream_kind
        with self.lock:
            self.state['frames'] += 1
            self.last_frame_at = now
            self.state.update(state='STREAMING', width=image.shape[1], height=image.shape[0], last_error=None)
        if not self.motion.admit(image, now):
            with self.lock:
                self.state['idle_frames'] += 1
            return
        if self.main_photo_enabled and stream_kind == 'main':
            if metadata.get('decode_keyframes_only'):
                # The broad main-video pass may inspect I-frames only. Open a
                # short full-decode burst during motion to catch better poses
                # between keyframes without full-decoding every idle camera.
                self._start_hd(metadata, now)
            if self.photo_frames and sum(len(b) for b, _ in self.photo_frames) + len(body) > 8 * 1024 * 1024:
                self._flush_photo(now, force=True)
            if not self.photo_frames:
                self.photo_started = now
            self.photo_frames.append((body, dict(metadata)))
            self._flush_photo(now, force=len(self.photo_frames) >= 3)
            return
        if self.main_photo_enabled and stream_kind == 'sub':
            # Trigger before the slow inference queue. The sub stream keeps
            # delivering while a separate bounded reader gathers main frames.
            metadata = dict(metadata, hd_upgrade_managed=True)
            self._start_hd(metadata, now)
        self._persist(body, metadata, now)

    def _persist(self, body, metadata, now):
        if self.main_photo_enabled:
            metadata = dict(metadata, photo_window_managed=True)
        while not self.stop_event.is_set():
            try:
                self.callback(body, metadata, now)
                with self.lock:
                    self.state['persisted_candidates'] += 1
                return
            except Exception:
                with self.lock:
                    self.state['inbox_errors'] += 1
                    self.state['state'] = 'WAITING_DURABLE_STORAGE'
                    self.state['last_error'] = 'DURABLE_INBOX_BACKPRESSURE_RETAINING_FRAME'
                self.stop_event.wait(.25)

    def _emit_photo(self, frames, received):
        if not frames:
            return
        if len(frames) == 1:
            body, meta = frames[0]
            self._persist(body, meta, received)
            return
        try:
            body, meta = pack_burst(frames)
        except ValueError:
            # Oversized or irregular input is still retained as independent
            # original frames; never drop people to fit a photo burst.
            for body, meta in frames:
                self._persist(body, meta, received)
            return
        self._persist(body, meta, received)
        with self.lock:
            self.state['main_photo_bursts'] += 1

    def _flush_photo(self, now, force=False):
        if self.photo_frames and (force or now - self.photo_started >= 1.5):
            frames, received = self.photo_frames, self.photo_started
            self.photo_frames, self.photo_started = [], None
            self._emit_photo(frames, received)

    def _start_hd(self, trigger, now):
        if not self.main_url or self.stop_event.is_set() or now < self.next_hd:
            return
        with self.lock:
            if self.hd_thread is not None and self.hd_thread.is_alive():
                return
            if not _HD_SLOTS.acquire(blocking=False):
                self.state['hd_busy'] += 1
                self.next_hd = now + 1
                return
            self.next_hd = now + 15
            self.state['hd_requests'] += 1
            self.hd_thread = threading.Thread(target=self._fetch_hd,
                args=(dict(trigger),), name='main-photo-burst', daemon=True)
            self.hd_thread.start()

    def _fetch_hd(self, trigger):
        from face_engine.uuid_sources import PrivateRTSPReader
        frames, first_received, reader = [], None, None
        try:
            reader = PrivateRTSPReader(self.main_url, self.source_id, self.camera_id, 'main',
                ffmpeg=self.ffmpeg, sample_fps=3, first_frame_timeout_s=10, stall_timeout_s=3)
            with self.lock:
                self.hd_reader = reader
            if self.stop_event.is_set():
                return
            reader.start()
            deadline, last_id = time.monotonic() + 10, None
            while not self.stop_event.is_set() and time.monotonic() < deadline and len(frames) < 3:
                frame = reader.wait_latest(.25, after_frame_id=last_id)
                if frame is None:
                    continue
                meta = dict(frame.metadata)
                last_id = meta['frame_id']
                if frame.received_monotonic < time.monotonic() - 2:
                    continue
                if first_received is None:
                    first_received = frame.received_monotonic
                    deadline = min(deadline, first_received + 1.5)
                if frame.received_monotonic - first_received > 1.5:
                    break
                profile = self.clock.for_capture(self.camera_id, self.connection) if self.clock else None
                if profile:
                    meta['camera_clock_profile'] = profile
                meta.update(hd_upgrade_managed=True, capture_origin='MOTION_TRIGGERED_MAIN_VIDEO',
                    trigger_frame_id=trigger['frame_id'],
                    trigger_received_at_unix_ms=trigger.get('received_at_unix_ms'))
                body = frame.encoded_image
                if body is None:
                    ok, encoded = cv2.imencode('.jpg', frame.image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
                    if not ok:
                        raise ValueError('main photo encoding failed')
                    body = encoded.tobytes()
                frames.append((body, meta))
            if frames and not self.stop_event.is_set():
                self._emit_photo(frames, first_received)
                with self.lock:
                    self.state['hd_bursts'] += 1
                    self.state['hd_frames'] += len(frames)
            elif not self.stop_event.is_set():
                raise TimeoutError('main photo had no decoded frame')
        except Exception:
            with self.lock:
                self.state['hd_errors'] += 1
        finally:
            try:
                if reader:
                    reader.stop()
            finally:
                with self.lock:
                    self.hd_reader = None
                _HD_SLOTS.release()

    def _run(self):
        kind, failures, sequence, last_id = 'main', 0, 0, None
        try:
            # Stagger connection establishment uniformly; no route has priority.
            self.stop_event.wait(int(hashlib.sha256(self.camera_id.encode()).hexdigest()[:4],16)/65536)
            self._set(state='CONNECTING')
            if self.clock:
                self.clock.for_capture(self.camera_id, self.connection)
            while not self.stop_event.is_set():
                begin = time.monotonic()
                profile = self.clock.for_capture(self.camera_id, self.connection) if self.clock else None
                try:
                    if self.video is None:
                        self.video = FailoverCamera(self.sub_url, self.main_url, self.source_id,
                            self.camera_id, ffmpeg=self.ffmpeg, sample_fps=self.fps,
                            preferred_stream=self.preferred_stream,
                            decode_keyframes_only=self.decode_keyframes_only,
                            main_archive_directory=self.main_archive_directory).start()
                    frame = self.video.latest(after_frame_id=last_id, max_age_s=3)
                    if frame is None:
                        self._flush_photo(time.monotonic())
                        self.stop_event.wait(.1)
                        continue
                    image, metadata = frame.image_bgr, dict(frame.metadata)
                    last_id = metadata['frame_id']
                    body=frame.encoded_image
                    if body is None:
                        ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 95])
                        if not ok:raise ValueError('video candidate encode failed')
                        body=encoded.tobytes()
                    self._set(source_transport='UUID_RTSP_VIDEO', stream_kind=metadata['stream_kind'],
                              main_video_archive_enabled=bool(metadata.get('main_video_archive_directory')),
                              decode_keyframes_only=metadata.get('decode_keyframes_only',False))
                    sequence += 1
                    if profile:
                        metadata['camera_clock_profile'] = profile
                    now = frame.received_monotonic
                    self._set(camera_clock_profile_ready=bool(profile))
                    self._accept(body, image, metadata, now)
                except (OSError, ValueError):
                    failures += 1
                    kind = 'sub' if kind=='main' else 'main'
                    with self.lock:
                        self.state['capture_errors'] += 1
                        self.state['last_error'] = 'CAMERA_ACQUISITION_FAILED'
                        self.state['state'] = 'BACKOFF'
                self.stop_event.wait(max(.01, 1/self.fps-(time.monotonic()-begin)))
        finally:
            self._flush_photo(time.monotonic(), force=True)
            if self.video:
                self.video.stop()
            self._set(state='STOPPED')

    def latest(self, *args, **kwargs):
        # Candidates go directly into the durable inbox, not a replaceable
        # latest-frame inference slot that can erase a brief walk-by.
        return None

    def request_main(self, *args, **kwargs):
        if self.video is None or self.stop_event.is_set():
            return None
        profile = self.clock.for_capture(self.camera_id,self.connection) if self.clock else None
        frame = self.video.request_main(*args, **kwargs)
        if frame is not None and profile:
            frame.metadata['camera_clock_profile'] = profile
        return frame

    def metrics(self):
        with self.lock:
            result = dict(self.state, continuous=True, durable_candidates=True)
            result['motion_admission'] = self.motion.stats()
            result['main_photo_enabled'] = self.main_photo_enabled
            result['main_photo_buffered_frames'] = len(self.photo_frames)
            result['hd_active'] = self.hd_thread is not None and self.hd_thread.is_alive()
            if self.last_frame_at is not None:
                result['frame_age_seconds'] = round(time.monotonic()-self.last_frame_at, 3)
            return result

    def stop(self):
        self.stop_event.set()
        with self.lock:
            hd_reader = self.hd_reader
        if hd_reader:
            hd_reader.stop()
        if self.hd_thread:
            self.hd_thread.join(timeout=15)
            if self.hd_thread.is_alive():
                raise TimeoutError('main photo reader stop timeout')
        if self.video:
            self.video.stop()
        if self.thread:
            self.thread.join(timeout=35)
            if self.thread.is_alive():
                raise TimeoutError('continuous camera stop timeout')
