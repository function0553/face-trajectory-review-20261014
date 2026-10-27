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


class MotionAdmission:
    def __init__(self, idle_seconds=60):
        self.previous = None
        self.active_until = 0
        self.last_saved = -float('inf')
        self.idle_seconds = idle_seconds

    def admit(self, image, now):
        small = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (96, 54))
        if self.previous is None:
            self.previous = small
            self.last_saved = now
            return True
        changed = np.count_nonzero(cv2.absdiff(small, self.previous)>12) >= 16
        self.previous = small
        if changed:
            self.active_until = now+2
        admitted = changed or now < self.active_until or now-self.last_saved >= self.idle_seconds
        if admitted:
            self.last_saved = now
        return admitted


class ContinuousCamera:
    def __init__(self, sub_url, main_url, source_id, camera_id, *, connection=None,
                 on_capture, camera_clock=None, ffmpeg='ffmpeg', sample_fps=2, preferred_stream='main'):
        self.source_id, self.camera_id = source_id, camera_id
        self.connection = connection
        self.sub_url, self.main_url = sub_url, main_url
        self.callback, self.clock = on_capture, camera_clock
        self.ffmpeg, self.fps = ffmpeg, sample_fps
        self.preferred_stream = preferred_stream
        self.stop_event = threading.Event()
        self.thread = None
        self.video = None
        self.lock = threading.RLock()
        self.motion = MotionAdmission()
        self.state = dict(state='NEW', frames=0, persisted_candidates=0, idle_frames=0,
                          capture_errors=0, inbox_errors=0, last_error=None, stream_kind='main')
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
        with self.lock:
            self.state['frames'] += 1
            self.last_frame_at = now
            self.state.update(state='STREAMING', width=image.shape[1], height=image.shape[0], last_error=None)
        if not self.motion.admit(image, now):
            with self.lock:
                self.state['idle_frames'] += 1
            return
        try:
            self.callback(body, metadata, now)
            with self.lock:
                self.state['persisted_candidates'] += 1
        except Exception:
            with self.lock:
                self.state['inbox_errors'] += 1
                self.state['last_error'] = 'DURABLE_INBOX_REFUSED_CAPTURE'

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
                            preferred_stream=self.preferred_stream).start()
                    frame = self.video.latest(after_frame_id=last_id, max_age_s=3)
                    if frame is None:
                        self.stop_event.wait(.1)
                        continue
                    image, metadata = frame.image_bgr, dict(frame.metadata)
                    last_id = metadata['frame_id']
                    body=frame.encoded_image
                    if body is None:
                        ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 95])
                        if not ok:raise ValueError('video candidate encode failed')
                        body=encoded.tobytes()
                    self._set(source_transport='UUID_RTSP_VIDEO', stream_kind=metadata['stream_kind'])
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
            if self.last_frame_at is not None:
                result['frame_age_seconds'] = round(time.monotonic()-self.last_frame_at, 3)
            return result

    def stop(self):
        self.stop_event.set()
        if self.video:
            self.video.stop()
        if self.thread:
            self.thread.join(timeout=35)
            if self.thread.is_alive():
                raise TimeoutError('continuous camera stop timeout')
