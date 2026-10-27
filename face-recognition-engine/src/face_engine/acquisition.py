"""Bounded camera acquisition; receipt time is never capture-time evidence.

RTSP decoding uses an isolated FFmpeg process so a blocked decoder can be
terminated. Hikvision input supports documented ISAPI faceCapture JSON MIME
parts and conservatively preserves XML events. No device configuration writes,
vendor recognition results, or remote image-URL fetching are performed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.parser import BytesHeaderParser
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np


@dataclass(frozen=True)
class Frame:
    image_bgr: np.ndarray
    metadata: dict
    received_monotonic: float
    encoded_image: bytes | None = None


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


def _identifier(value, name):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{name} must be a nonempty string of at most 512 characters")
    return value


def _base_metadata(source_id, camera_id, frame_id, kind, received_ms):
    return dict(source_id=source_id, camera_id=camera_id, frame_id=frame_id,
                stream_kind=kind, time_source="UNKNOWN", clock_domain="UNKNOWN",
                clock_uncertainty_ms=-1, received_at_unix_ms=received_ms)


_CREATE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@lru_cache(maxsize=8)
def _rtsp_timeout_option(ffmpeg):
    """Do not confuse FFmpeg 4.x listen timeout with its socket stimeout."""
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-h", "demuxer=rtsp"],
                                capture_output=True, text=True, timeout=10,
                                creationflags=_CREATE_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (result.stdout + result.stderr).splitlines()
    if any(line.strip().startswith("-stimeout ") for line in lines):
        return "-stimeout"
    if any(line.strip().startswith("-timeout ") and "socket" in line.lower()
           and "listen" not in line.lower() for line in lines):
        return "-timeout"
    return None


def _ppm_frame(stream, max_pixels):
    """Read lossless PPM emitted by FFmpeg, checking size before allocating."""
    def line():
        data = stream.readline(257)
        if len(data) > 256:
            raise ValueError("oversized PPM header")
        return data
    magic = line()
    if not magic:
        return None
    if magic != b"P6\n":
        raise ValueError("unexpected FFmpeg frame format")
    dimensions = line().split()
    if len(dimensions) != 2 or not all(v.isdigit() for v in dimensions):
        raise ValueError("invalid PPM dimensions")
    width, height = map(int, dimensions)
    if width < 1 or height < 1 or width * height > max_pixels or line() != b"255\n":
        raise ValueError("invalid or oversized PPM frame")
    remaining = width * height * 3
    data = bytearray()
    while remaining:
        block = stream.read(min(remaining, 1024 * 1024))
        if not block:
            raise EOFError("incomplete decoded frame")
        data.extend(block)
        remaining -= len(block)
    return np.frombuffer(data, np.uint8).reshape(height, width, 3)[:, :, ::-1].copy()


class LatestFrameReader:
    """One-slot, lossless, native-size decoder with reconnect and hard watchdog.

    `time_provider(metadata)` may return a raw `time_record` only when it is
    correlated to THIS frame by a verified acquisition adapter. It must not
    substitute local receive time. The generic RTSP adapter has no such mapping.
    `latest()` transfers a read-only image plus copied metadata to the consumer.
    """
    def __init__(self, url, source_id, camera_id, stream_kind="sub", sample_fps=10,
                 ffmpeg="ffmpeg", first_frame_timeout_s=12, stall_timeout_s=5,
                 reconnect_min_s=1, reconnect_max_s=30, max_pixels=32_000_000,
                 time_provider: Callable | None = None, clock_profile_version=None,
                 realtime=False, loop_file=False, one_shot=False):
        self.url = _identifier(url, "url")
        self.source_id = _identifier(source_id, "source_id")
        self.camera_id = _identifier(camera_id, "camera_id")
        if stream_kind not in ("sub", "main"):
            raise ValueError("stream_kind must be sub or main")
        self.stream_kind, self.ffmpeg = stream_kind, str(ffmpeg)
        self.sample_fps = _positive(sample_fps, "sample_fps")
        if self.sample_fps > 60:
            raise ValueError("sample_fps must be at most 60")
        self.first_timeout = _positive(first_frame_timeout_s, "first_frame_timeout_s")
        self.stall_timeout = _positive(stall_timeout_s, "stall_timeout_s")
        self.backoff_min = _positive(reconnect_min_s, "reconnect_min_s")
        self.backoff_max = _positive(reconnect_max_s, "reconnect_max_s")
        if self.backoff_max < self.backoff_min or type(max_pixels) is not int or not 1 <= max_pixels <= 64_000_000:
            raise ValueError("invalid reconnect or pixel bound")
        self.max_pixels, self.time_provider = max_pixels, time_provider
        self.clock_profile_version = clock_profile_version
        self.realtime, self.loop_file, self.one_shot = realtime, loop_file, one_shot
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._frame = None
        self._process = None
        self._deadline = 0.0
        self._thread = None
        self._watchdog = None
        self._metrics = dict(frames=0, reconnects=0, watchdog_kills=0, state="NEW", last_error=None)

    def start(self):
        with self._lock:
            if self._thread is not None:
                if self._thread.is_alive():
                    return self
                raise RuntimeError("reader cannot be restarted after stop; construct a new reader")
            self._thread = threading.Thread(target=self._run, name=f"capture-{self.camera_id}", daemon=True)
            self._watchdog = threading.Thread(target=self._watch, name="capture-watchdog", daemon=True)
            self._thread.start()
            self._watchdog.start()
        return self

    def latest(self, after_frame_id=None, max_age_s=2.0):
        _positive(max_age_s, "max_age_s")
        with self._lock:
            frame = self._frame
            if frame is None or frame.metadata["frame_id"] == after_frame_id or time.monotonic() - frame.received_monotonic > max_age_s:
                return None
            return Frame(frame.image_bgr, copy.deepcopy(frame.metadata), frame.received_monotonic,frame.encoded_image)

    def wait_latest(self, timeout_s, after_frame_id=None):
        deadline = time.monotonic() + _positive(timeout_s, "timeout_s")
        while True:
            with self._condition:
                frame = self._frame
                if frame is not None and frame.metadata["frame_id"] != after_frame_id:
                    return Frame(frame.image_bgr, copy.deepcopy(frame.metadata), frame.received_monotonic,frame.encoded_image)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return None
                self._condition.wait(min(remaining, 0.1))

    def metrics(self):
        with self._lock:
            return dict(self._metrics)

    def stop(self, timeout_s=3):
        self._stop.set()
        with self._condition:
            process = self._process
            self._condition.notify_all()
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        for thread in (self._thread, self._watchdog):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout_s)
                if thread.is_alive():
                    raise TimeoutError("capture reader did not stop within bound")

    def _command(self):
        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.url.lower().startswith(("rtsp://", "rtsps://")):
            command += ["-rtsp_transport", "tcp"]
            option = _rtsp_timeout_option(self.ffmpeg)
            if option:
                command += [option, str(int(self.stall_timeout * 1_000_000))]
        else:
            if self.realtime:
                command += ["-re"]
            if self.loop_file:
                command += ["-stream_loop", "-1"]
        command += ["-threads", "1", "-i", self.url, "-map", "0:v:0", "-an", "-sn", "-dn"]
        if self.one_shot:
            command += ["-frames:v", "1"]
        else:
            command += ["-vf", f"fps={self.sample_fps:g}"]
        return command + ["-threads", "1", "-c:v", "ppm", "-f", "image2pipe", "pipe:1"]

    def _watch(self):
        while not self._stop.wait(0.1):
            with self._lock:
                process = self._process
                expired = process is not None and time.monotonic() >= self._deadline and process.poll() is None
                if expired:
                    self._metrics["watchdog_kills"] += 1
                    self._metrics["last_error"] = "FRAME_TIMEOUT"
                    self._deadline = float("inf")
            if expired:
                try:
                    process.kill()
                except OSError:
                    pass

    def _spawn(self, command):
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, creationflags=_CREATE_FLAGS)

    def _read_output(self, stream):
        return _ppm_frame(stream, self.max_pixels), {}

    def _run(self):
        backoff = self.backoff_min
        try:
            while not self._stop.is_set():
                process = None
                session_id, sequence = uuid.uuid4().hex, 0
                started = time.monotonic()
                try:
                    command = self._command()
                    if self._stop.is_set():
                        break
                    process = self._spawn(command)
                    with self._lock:
                        self._process, self._deadline = process, time.monotonic() + self.first_timeout
                        self._metrics["state"] = "CONNECTING"
                        # Drop prior-session frames while reconnecting.
                        self._frame = None
                    while not self._stop.is_set():
                        image, additional = self._read_output(process.stdout)
                        encoded_image=additional.pop('_encoded_image',None)
                        if image is None:
                            with self._lock:
                                if self._metrics["last_error"] is None:
                                    self._metrics["last_error"] = "DECODER_END_OF_STREAM"
                            break
                        received, received_ms = time.monotonic(), time.time_ns() // 1_000_000
                        sequence += 1
                        meta = _base_metadata(self.source_id, self.camera_id,
                            f"{self.stream_kind}:{session_id}:{sequence}", self.stream_kind, received_ms)
                        meta["capture_session_id"] = session_id
                        if self.clock_profile_version:
                            meta["clock_profile_version"] = self.clock_profile_version
                        meta.update(additional)
                        if self.time_provider:
                            record = self.time_provider(copy.deepcopy(meta))
                            if record is not None:
                                if not isinstance(record, dict) or not isinstance(record.get("raw_time"), str):
                                    raise ValueError("time_provider must return raw time_record or None")
                                meta["time_record"] = copy.deepcopy(record)
                        image.setflags(write=False)
                        with self._condition:
                            self._frame = Frame(image, meta, received,encoded_image)
                            self._deadline = received + self.stall_timeout
                            self._metrics.update(state="STREAMING", last_error=None)
                            self._metrics["frames"] += 1
                            self._condition.notify_all()
                        if self.one_shot:
                            return
                except (OSError, ValueError, EOFError, subprocess.SubprocessError) as error:
                    with self._lock:
                        # Do not expose URLs, passwords, or subprocess command lines.
                        self._metrics["last_error"] = type(error).__name__
                finally:
                    if process is not None:
                        if process.poll() is None:
                            try:
                                process.kill()
                            except OSError:
                                pass
                        process.wait(timeout=3)
                        if process.stdout:
                            process.stdout.close()
                    with self._lock:
                        self._process = None
                if self.one_shot:
                    return
                with self._lock:
                    self._frame = None
                    self._metrics["state"] = "BACKOFF"
                    self._metrics["reconnects"] += 1
                if sequence and time.monotonic() - started >= 10:
                    backoff = self.backoff_min
                if self._stop.wait(backoff):
                    break
                backoff = min(self.backoff_max, backoff * 2)
        finally:
            with self._condition:
                self._metrics["state"] = "STOPPED"
                if not self.one_shot:
                    self._frame = None
                self._condition.notify_all()


class CameraAcquisition:
    """Continuous substream and bounded on-demand main-stream replacement.

    A main frame has a NEW frame ID and NEW time evidence. `fallback_of_frame_id`
    only links the quality request; it conveys neither identity nor simultaneity.
    """
    def __init__(self, sub_url, main_url, source_id, camera_id, main_min_interval_s=5,
                 main_timeout_s=8, backend="ffmpeg", gst_python=None, **reader_options):
        if backend not in ("ffmpeg", "gstreamer"):
            raise ValueError("backend must be ffmpeg or gstreamer")
        self.reader_class = GStreamerReferenceReader if backend == "gstreamer" else LatestFrameReader
        if backend == "gstreamer":
            reader_options["gst_python"] = gst_python or sys.executable
        self.sub = self.reader_class(sub_url, source_id, camera_id, "sub", **reader_options)
        self.main_url, self.source_id, self.camera_id = main_url, source_id, camera_id
        self.options = dict(reader_options)
        self.interval = _positive(main_min_interval_s, "main_min_interval_s")
        self.timeout = _positive(main_timeout_s, "main_timeout_s")
        self._main_lock = threading.Lock()
        self._last_attempt = float("-inf")
        self._stop = threading.Event()
        self._active_main = None
        self._state_lock = threading.Lock()

    def start(self):
        self.sub.start()
        return self

    def latest(self, after_frame_id=None, max_age_s=2):
        return self.sub.latest(after_frame_id, max_age_s)

    def request_main(self, reason="LOW_FACE_QUALITY", after_frame_id=None):
        if not self.main_url or self._stop.is_set() or not self._main_lock.acquire(blocking=False):
            return None
        reader = None
        try:
            if time.monotonic() - self._last_attempt < self.interval or self._stop.is_set():
                return None
            self._last_attempt = time.monotonic()
            options = dict(self.options)
            options.update(one_shot=True, loop_file=False, first_frame_timeout_s=self.timeout)
            reader = self.reader_class(self.main_url, self.source_id, self.camera_id, "main", **options)
            with self._state_lock:
                if self._stop.is_set():
                    return None
                self._active_main = reader
                reader.start()
            frame = reader.wait_latest(self.timeout)
            if frame is not None and not self._stop.is_set():
                frame.metadata["fallback_reason"] = str(reason)[:128]
                if after_frame_id:
                    frame.metadata["fallback_of_frame_id"] = str(after_frame_id)[:512]
                return frame
            return None
        finally:
            if reader is not None:
                reader.stop()
            with self._state_lock:
                self._active_main = None
            self._main_lock.release()

    def stop(self):
        self._stop.set()
        with self._state_lock:
            main = self._active_main
        if main is not None:
            main.stop()
        self.sub.stop()


def reference_time_record(reference_caps, timestamp_ns, camera_id, profile_version=None,
                          reference_count=1):
    """Convert ONE same-buffer NTP reference; never map PTS/receive time to UTC.

    Gst timestamp/x-ntp is integer nanoseconds since 1900-01-01, not the
    32.32-bit wire format and not a Unix timestamp. Milliseconds are truncated
    explicitly; the original nanoseconds remain in provenance. The profile
    must independently validate the sender clock and capture semantics.
    """
    if type(reference_count) is not int or reference_count != 1 or not isinstance(reference_caps, str):
        return None
    if reference_caps.split(",", 1)[0].strip() != "timestamp/x-ntp":
        return None
    if type(timestamp_ns) is not int or not 0 <= timestamp_ns < 2**64 - 1:
        return None
    milliseconds = timestamp_ns // 1_000_000
    try:
        instant = datetime(1900, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=milliseconds)
    except (OverflowError, ValueError):
        return None
    # No repair with the receiver's current time if NTP era/wrap is ambiguous.
    record = dict(raw_time=instant.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        camera_id=camera_id, source_kind="GSTREAMER_SAME_BUFFER_NTP_REFERENCE",
        reference_caps=reference_caps[:2048], reference_timestamp_ns=timestamp_ns,
        text_resolution_ms=1, discarded_submillisecond_ns=timestamp_ns % 1_000_000)
    if profile_version:
        record["profile_version"] = profile_version
    return record


def _mapped_bgr(data, width, height, stride, offset, max_pixels):
    """Copy a padded Gst BGR plane without treating padding as pixels."""
    if any(type(value) is not int for value in (width, height, stride, offset)):
        raise ValueError("integer image layout required")
    if width < 1 or height < 1 or width * height > max_pixels or stride < width * 3 or offset < 0:
        raise ValueError("invalid or oversized GStreamer image layout")
    if offset + (height - 1) * stride + width * 3 > len(data):
        raise ValueError("GStreamer plane exceeds mapped buffer")
    return np.ndarray((height, width, 3), dtype=np.uint8, buffer=data,
                      offset=offset, strides=(stride, 3, 1)).copy()


def _gst_local_status():
    try:
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstVideo  # noqa: F401
        Gst.init(None)
        version = tuple(Gst.version())
        required = ("rtspsrc", "decodebin", "videoconvert", "appsink")
        missing = [name for name in required if Gst.ElementFactory.find(name) is None]
        return dict(available=version[:2] >= (1, 22) and not missing, version=list(version),
                    missing_elements=missing, reason=None if version[:2] >= (1, 22) else "GSTREAMER_1_22_REQUIRED")
    except Exception as error:
        return dict(available=False, reason=type(error).__name__)


@lru_cache(maxsize=4)
def gstreamer_backend_status(gst_python=None):
    """Read-only local prerequisite probe; does not open a camera connection."""
    try:
        result = subprocess.run([gst_python or sys.executable, "-u", str(Path(__file__).resolve()), "--gst-probe"],
            capture_output=True, text=True, timeout=10, creationflags=_CREATE_FLAGS)
        if result.returncode or len(result.stdout) > 8192:
            return dict(available=False, reason="GSTREAMER_PROBE_FAILED")
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else dict(available=False, reason="INVALID_PROBE_RESULT")
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return dict(available=False, reason=type(error).__name__)


class GStreamerReferenceReader(LatestFrameReader):
    """Optional isolated GStreamer 1.22+ reader preserving same-buffer NTP meta.

    Unlike generic FFmpeg image piping, this adapter can retain camera RTP/RTCP
    reference timestamps. A timestamp is still unqualified until P2 verifies
    the camera's profile. Missing/multiple/non-NTP references remain UNKNOWN.
    """
    def __init__(self, *args, gst_python=None, gst_latency_ms=100, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.url.lower().startswith(("rtsp://", "rtsps://")):
            raise ValueError("GStreamer reference reader requires an RTSP(S) URL")
        if type(gst_latency_ms) is not int or not 0 <= gst_latency_ms <= 5000:
            raise ValueError("gst_latency_ms must be in 0..5000")
        if self.time_provider is not None:
            raise ValueError("GStreamer same-buffer time cannot be overridden by time_provider")
        self.gst_python, self.gst_latency_ms = gst_python or sys.executable, gst_latency_ms
        status = gstreamer_backend_status(self.gst_python)
        if status.get("available") is not True:
            raise RuntimeError("GStreamer backend unavailable: " + str(status.get("reason") or status.get("missing_elements")))

    def _command(self):
        # Credentials travel over stdin, not as process command-line arguments.
        return [self.gst_python, "-u", str(Path(__file__).resolve()), "--gst-worker"]

    def _spawn(self, command):
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, creationflags=_CREATE_FLAGS)
        config = dict(url=self.url, camera_id=self.camera_id, clock_profile_version=self.clock_profile_version,
            sample_fps=self.sample_fps, latency_ms=self.gst_latency_ms, max_pixels=self.max_pixels,
            socket_timeout_us=int(self.stall_timeout * 1_000_000), one_shot=self.one_shot,
            reference_wait_s=min(5.0, self.first_timeout * 0.75) if self.one_shot else 0)
        try:
            process.stdin.write(json.dumps(config, ensure_ascii=False).encode("utf-8") + b"\n")
            process.stdin.close()
        except Exception:
            process.kill()
            process.wait(timeout=3)
            raise
        return process

    def _read_output(self, stream):
        line = stream.readline(8193)
        if not line:
            return None, {}
        if len(line) > 8192 or not line.startswith(b"GSTMETA "):
            raise ValueError("invalid GStreamer frame metadata transport")
        metadata = json.loads(line[8:])
        if not isinstance(metadata, dict) or set(metadata) - {"time_record", "reference_time_status", "acquisition_backend"}:
            raise ValueError("invalid GStreamer frame metadata fields")
        image = _ppm_frame(stream, self.max_pixels)
        if image is None:
            raise EOFError("GStreamer metadata without its image")
        return image, metadata


def _gst_worker(config):
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gst, GstVideo
    Gst.init(None)
    if tuple(Gst.version())[:2] < (1, 22):
        raise RuntimeError("GStreamer 1.22+ required")
    # URL is set separately so quotes in credentials cannot inject a pipeline.
    pipeline = Gst.parse_launch(
        "rtspsrc name=camera protocols=tcp add-reference-timestamp-meta=true "
        "drop-on-latency=true ! application/x-rtp,media=video ! decodebin ! "
        "videoconvert ! video/x-raw,format=BGR ! appsink name=frames "
        "sync=false max-buffers=1 drop=true emit-signals=false")
    source, sink = pipeline.get_by_name("camera"), pipeline.get_by_name("frames")
    source.set_property("location", config["url"])
    source.set_property("latency", config["latency_ms"])
    source.set_property("tcp-timeout", config["socket_timeout_us"])
    bus = pipeline.get_bus()
    reference_caps = Gst.Caps.from_string("timestamp/x-ntp")
    reference_type = Gst.reference_timestamp_meta_api_get_type()
    next_sample = 0.0
    reference_deadline = time.monotonic() + config["reference_wait_s"]
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer PLAYING failed")
        while True:
            message = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is not None:
                # Diagnostics can include credentialled URLs; do not print them.
                raise RuntimeError("GStreamer stream error or EOS")
            sample = sink.emit("try-pull-sample", 100 * Gst.MSECOND)
            if sample is None:
                continue
            now = time.monotonic()
            if now < next_sample:
                continue
            next_sample = now + 1 / config["sample_fps"]
            buffer = sample.get_buffer()
            info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
            if info is None or info.finfo.name != "BGR":
                raise ValueError("GStreamer did not negotiate BGR")
            layout = GstVideo.buffer_get_video_meta(buffer)
            if layout is not None and (layout.width != info.width or layout.height != info.height or layout.n_planes != 1):
                raise ValueError("unsupported Gst video plane layout")
            stride, offset = ((int(layout.stride[0]), int(layout.offset[0])) if layout is not None
                              else (int(info.stride[0]), int(info.offset[0])))
            count = buffer.get_n_meta(reference_type)
            reference = buffer.get_reference_timestamp_meta(reference_caps) if count == 1 else None
            record = None
            if reference is not None:
                record = reference_time_record(reference.reference.to_string(), int(reference.timestamp),
                    config["camera_id"], config.get("clock_profile_version"), reference_count=count)
            if config["one_shot"] and record is None and now < reference_deadline:
                # An initial RTCP sender report may arrive after the first frame.
                # Briefly wait within the request deadline, then preserve UNKNOWN.
                continue
            metadata = dict(acquisition_backend="gstreamer", reference_time_status=(
                "RAW_NTP_REFERENCE" if record else "AMBIGUOUS_REFERENCE" if count > 1 else "NO_VALID_NTP_REFERENCE"))
            if record:
                metadata["time_record"] = record
            mapped, mapping = buffer.map(Gst.MapFlags.READ)
            if not mapped:
                raise ValueError("GStreamer buffer mapping failed")
            try:
                image = _mapped_bgr(mapping.data, int(info.width), int(info.height), stride, offset, config["max_pixels"])
            finally:
                buffer.unmap(mapping)
            # Metadata and image come from the SAME sample and one framed pipe
            # message; no independent timestamp queue or PTS-nearest guessing.
            sys.stdout.buffer.write(b"GSTMETA " + json.dumps(metadata, separators=(",", ":")).encode() + b"\n")
            sys.stdout.buffer.write(f"P6\n{info.width} {info.height}\n255\n".encode())
            sys.stdout.buffer.write(image[:, :, ::-1].tobytes())
            sys.stdout.buffer.flush()
            if config["one_shot"]:
                return
    finally:
        pipeline.set_state(Gst.State.NULL)


class MultipartError(ValueError):
    pass


@dataclass(frozen=True)
class MimePart:
    headers: dict
    body: bytes


class BoundedMultipartParser:
    """Streaming MIME parser with byte bounds and no cross-connection state."""
    def __init__(self, content_type, max_part_bytes=8 * 1024 * 1024, max_header_bytes=8192):
        message = Message()
        message["content-type"] = content_type
        boundary = message.get_boundary()
        if message.get_content_type() not in ("multipart/mixed", "multipart/form-data") or not boundary:
            raise MultipartError("ISAPI multipart content type with boundary required")
        try:
            raw = boundary.encode("ascii")
        except UnicodeEncodeError as error:
            raise MultipartError("non-ASCII boundary") from error
        if not 1 <= len(raw) <= 200 or any(byte < 32 or byte > 126 for byte in raw):
            raise MultipartError("invalid multipart boundary")
        if type(max_part_bytes) is not int or not 1 <= max_part_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid part byte bound")
        self.marker = b"--" + raw
        self.max_part_bytes, self.max_header_bytes = max_part_bytes, max_header_bytes
        self.buffer = bytearray()
        self.state, self.headers, self.length = "boundary", {}, None
        self.closed = False

    def feed(self, data):
        if not isinstance(data, bytes) or len(data) > 64 * 1024:
            raise MultipartError("feed requires a bytes chunk of at most 64 KiB")
        if self.closed:
            if data.strip():
                raise MultipartError("data after closing boundary")
            return []
        self.buffer.extend(data)
        parts = []
        while True:
            if self.state == "boundary":
                if self.buffer.startswith(b"\r\n"):
                    del self.buffer[:2]
                if len(self.buffer) < len(self.marker) + 2:
                    break
                if not self.buffer.startswith(self.marker):
                    raise MultipartError("invalid multipart boundary or part length")
                suffix = bytes(self.buffer[len(self.marker):len(self.marker) + 2])
                if suffix == b"--":
                    del self.buffer[:len(self.marker) + 2]
                    if self.buffer.strip():
                        raise MultipartError("unexpected multipart epilogue")
                    self.buffer.clear()
                    self.closed = True
                    break
                if suffix != b"\r\n":
                    raise MultipartError("invalid multipart boundary suffix")
                del self.buffer[:len(self.marker) + 2]
                self.state = "headers"
            elif self.state == "headers":
                end = self.buffer.find(b"\r\n\r\n")
                if end < 0:
                    if len(self.buffer) > self.max_header_bytes:
                        raise MultipartError("multipart headers too large")
                    break
                if end > self.max_header_bytes:
                    raise MultipartError("multipart headers too large")
                raw_headers = bytes(self.buffer[:end])
                if b"\x00" in raw_headers:
                    raise MultipartError("invalid multipart headers")
                message = BytesHeaderParser().parsebytes(raw_headers + b"\r\n\r\n")
                if message.defects:
                    raise MultipartError("malformed multipart headers")
                self.headers = {}
                for key, value in message.items():
                    key = key.lower()
                    if key in self.headers:
                        raise MultipartError("duplicate multipart header")
                    self.headers[key] = value
                raw_length = self.headers.get("content-length")
                if raw_length is not None and (not raw_length.isdigit() or len(raw_length) > 10):
                    raise MultipartError("invalid part content-length")
                self.length = int(raw_length) if raw_length is not None else None
                if self.length is not None and self.length > self.max_part_bytes:
                    raise MultipartError("multipart part too large")
                del self.buffer[:end + 4]
                self.state = "body"
            else:
                if self.length is None:
                    end = self.buffer.find(b"\r\n" + self.marker)
                    if end < 0:
                        if len(self.buffer) > self.max_part_bytes + len(self.marker) + 2:
                            raise MultipartError("multipart part too large")
                        break
                else:
                    end = self.length
                    if len(self.buffer) < end + 2:
                        break
                    if self.buffer[end:end + 2] != b"\r\n":
                        raise MultipartError("content-length does not match part body")
                if end > self.max_part_bytes:
                    raise MultipartError("multipart part too large")
                parts.append(MimePart(self.headers, bytes(self.buffer[:end])))
                del self.buffer[:end + 2]
                self.state = "boundary"
        if len(self.buffer) > self.max_part_bytes + self.max_header_bytes + len(self.marker) + 4:
            raise MultipartError("multipart buffer bound exceeded")
        return parts

    def finish(self):
        if not self.closed:
            raise MultipartError("truncated multipart message")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MultipartError("duplicate JSON key")
        result[key] = value
    return result


def _tree_bound(value, depth=0):
    if depth > 24:
        raise MultipartError("event metadata nesting exceeds bound")
    if isinstance(value, dict):
        for item in value.values():
            _tree_bound(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _tree_bound(item, depth + 1)


def decode_jpeg(data, max_pixels=32_000_000):
    """Check JPEG SOF dimensions before OpenCV allocates decoded pixels."""
    if not isinstance(data, bytes) or not 4 <= len(data) <= 8 * 1024 * 1024 or data[:2] != b"\xff\xd8":
        raise MultipartError("bounded JPEG image required")
    cursor, dimensions = 2, None
    while cursor < len(data):
        if data[cursor] != 0xFF:
            raise MultipartError("invalid JPEG marker")
        while cursor < len(data) and data[cursor] == 0xFF:
            cursor += 1
        if cursor >= len(data):
            break
        marker = data[cursor]
        cursor += 1
        if marker in (0xD9, 0xDA):
            break
        if marker in (0x01, *range(0xD0, 0xD9)):
            continue
        if cursor + 2 > len(data):
            raise MultipartError("truncated JPEG")
        size = int.from_bytes(data[cursor:cursor + 2], "big")
        if size < 2 or cursor + size > len(data):
            raise MultipartError("invalid JPEG segment length")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if size < 8:
                raise MultipartError("invalid JPEG SOF")
            height = int.from_bytes(data[cursor + 3:cursor + 5], "big")
            width = int.from_bytes(data[cursor + 5:cursor + 7], "big")
            dimensions = (height, width)
            if not height or not width or height * width > max_pixels:
                raise MultipartError("decoded JPEG exceeds pixel bound")
            break
        cursor += size
    if dimensions is None:
        raise MultipartError("JPEG dimensions unavailable")
    import cv2
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if image is None or image.shape[:2] != dimensions:
        raise MultipartError("JPEG decoding failed")
    image.setflags(write=False)
    return image


def _channel_identifier(value):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        number = int(value)
    else:
        raise ValueError("positive integer ISAPI channel ID required")
    if not 1 <= number <= 2147483647:
        raise ValueError("ISAPI channel ID outside allowed range")
    return str(number)


def _validated_channel_bindings(bindings):
    """Copy explicit NVR channel identities; never infer a camera from event order."""
    if not isinstance(bindings, dict) or not 1 <= len(bindings) <= 4096:
        raise ValueError("channel_bindings must contain 1 to 4096 explicit channels")
    result, cameras = {}, set()
    for channel, binding in bindings.items():
        channel = _channel_identifier(channel)
        if channel in result:
            raise ValueError("duplicate normalized ISAPI channel binding")
        if not isinstance(binding, dict) or set(binding) - {"source_id", "camera_id", "clock_profile_version"}:
            raise ValueError("invalid ISAPI channel binding")
        source = _identifier(binding.get("source_id"), "source_id")
        camera = _identifier(binding.get("camera_id"), "camera_id")
        if camera in cameras:
            raise ValueError("camera_id cannot bind multiple ISAPI channels")
        cameras.add(camera)
        profile = binding.get("clock_profile_version")
        if profile is not None:
            profile = _identifier(profile, "clock_profile_version")
        result[channel] = dict(source_id=source, camera_id=camera, clock_profile_version=profile)
    return result


class HikvisionMultipartDecoder:
    """Parse serialized ISAPI metadata then content-ID-associated JPEG parts.

    Non-face events and unreferenced images never become face observations.
    Device faceId/traceUuid/person IDs are not recognition truth. Background
    pictures remain whole images; each detected face must be compared separately.
    A connection reset MUST construct a fresh decoder.
    """
    def __init__(self, content_type, source_id=None, camera_id=None, clock_profile_version=None,
                 max_event_bytes=16 * 1024 * 1024, max_images_per_event=32,
                 expected_channel_id=None, channel_bindings=None):
        self.parser = BoundedMultipartParser(content_type)
        if channel_bindings is not None:
            if any(value is not None for value in (source_id, camera_id, clock_profile_version, expected_channel_id)):
                raise ValueError("channel_bindings cannot be combined with a single camera binding")
            self.channel_bindings = _validated_channel_bindings(channel_bindings)
            self.source_id = self.camera_id = None
        else:
            self.channel_bindings = None
            self.source_id = _identifier(source_id, "source_id")
            self.camera_id = _identifier(camera_id, "camera_id")
        self.clock_profile_version = clock_profile_version
        self.max_event_bytes, self.max_images = max_event_bytes, max_images_per_event
        self.expected_channel_id = None if expected_channel_id is None else _channel_identifier(expected_channel_id)
        self.references, self.event, self.seen = {}, None, set()
        self.event_binding = None
        self.event_bytes = 0
        self.diagnostics = dict(metadata_events=0, images=0, unbound_images=0,
                                ignored_events=0, incomplete_events=0)

    def feed(self, data):
        result = []
        for part in self.parser.feed(data):
            kind = part.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if kind in ("application/json", "application/xml", "text/xml"):
                self._metadata(part.body, kind)
            elif kind in ("image/jpeg", "image/jpg"):
                frame = self._image(part)
                if frame is not None:
                    result.append(frame)
            else:
                # Unknown parts do not acquire meaning from neighbouring events.
                self.event_bytes += len(part.body)
                if self.event_bytes > self.max_event_bytes:
                    raise MultipartError("event byte bound exceeded")
        return result

    def finish(self):
        self.parser.finish()
        if set(self.references) - self.seen:
            self.diagnostics["incomplete_events"] += 1

    def _metadata(self, body, kind):
        if len(body) > 256 * 1024:
            raise MultipartError("event metadata exceeds 256 KiB")
        if set(self.references) - self.seen:
            self.diagnostics["incomplete_events"] += 1
        self.references, self.event, self.seen = {}, None, set()
        self.event_binding = None
        self.event_bytes = len(body)
        self.diagnostics["metadata_events"] += 1
        try:
            if kind == "application/json":
                obj = json.loads(body.decode("utf-8-sig"), object_pairs_hook=_json_object,
                                 parse_constant=lambda _: (_ for _ in ()).throw(MultipartError("non-finite JSON")))
                _tree_bound(obj)
                if not isinstance(obj, dict):
                    raise MultipartError("event JSON object required")
                if isinstance(obj.get("EventNotificationAlert"), dict):
                    obj = obj["EventNotificationAlert"]
                stable_event = {key: value for key, value in obj.items()
                                if key not in ("activePostCount", "isDataRetransmission")}
                stable_bytes = json.dumps(stable_event, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")
                event_type, state = obj.get("eventType"), obj.get("eventState")
                channel, event_time = obj.get("channelID"), obj.get("dateTime")
                if event_type not in ("faceCapture", "faceSnap") or state not in ("active", None):
                    self.diagnostics["ignored_events"] += 1
                    return
                captures = obj.get("faceCapture", [])
                if not isinstance(captures, list):
                    raise MultipartError("faceCapture must be an array")
                for capture in captures:
                    if not isinstance(capture, dict):
                        raise MultipartError("faceCapture entry must be object")
                    attrs = capture.get("targetAttrs", {})
                    if not isinstance(attrs, dict):
                        raise MultipartError("targetAttrs must be object")
                    raw_time = attrs.get("faceTime") or capture.get("faceTime")
                    uid = capture.get("uid")
                    faces = capture.get("faces", [])
                    if not isinstance(faces, list):
                        raise MultipartError("faces must be array")
                    for face in faces:
                        if not isinstance(face, dict):
                            raise MultipartError("face entry must be object")
                        self._reference(face.get("contentID"), "face", raw_time, uid)
                    self._reference(attrs.get("contentID"), "background", raw_time, uid)
            else:
                upper = body.upper()
                if b"\x00" in body or b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
                    raise MultipartError("XML declarations/entities are not accepted")
                root = ET.fromstring(body)
                def local(tag):
                    return tag.rsplit("}", 1)[-1]
                def scalar(parent, name):
                    matches = [node.text for node in parent if local(node.tag) == name]
                    if len(matches) > 1:
                        raise MultipartError("duplicate XML event field")
                    return matches[0] if matches else None
                if local(root.tag) != "EventNotificationAlert":
                    raise MultipartError("unsupported XML event root")
                stable_root = copy.deepcopy(root)
                for child in list(stable_root):
                    if local(child.tag) in ("activePostCount", "isDataRetransmission"):
                        stable_root.remove(child)
                stable_bytes = ET.tostring(stable_root, encoding="utf-8")
                nodes = [(root, 0)]
                count = 0
                while nodes:
                    node, depth = nodes.pop()
                    count += 1
                    if depth > 24 or count > 10000:
                        raise MultipartError("XML event nesting/element bound exceeded")
                    nodes.extend((child, depth + 1) for child in node)
                event_type, state = scalar(root, "eventType"), scalar(root, "eventState")
                channel, event_time = scalar(root, "channelID"), scalar(root, "dateTime")
                if event_type not in ("faceCapture", "faceSnap") or state not in ("active", None):
                    self.diagnostics["ignored_events"] += 1
                    return
                # XML schemas differ by firmware. Retain only explicit contentID
                # references; no assumption that alarm dateTime is capture time.
                for node in root.iter():
                    if local(node.tag) == "contentID":
                        self._reference(node.text, "unclassified", None, None)
        except (UnicodeError, json.JSONDecodeError, ET.ParseError, RecursionError) as error:
            raise MultipartError("invalid event metadata") from error
        try:
            channel_key = _channel_identifier(channel)
        except ValueError:
            channel_key = None
        if self.channel_bindings is not None:
            binding = self.channel_bindings.get(channel_key)
        elif self.expected_channel_id is None or channel_key == self.expected_channel_id:
            binding = dict(source_id=self.source_id, camera_id=self.camera_id,
                           clock_profile_version=self.clock_profile_version)
        else:
            binding = None
        if binding is None:
            self.references.clear()
            self.diagnostics["ignored_events"] += 1
            return
        self.event_binding = binding
        self.event = dict(event_type=event_type, vendor_channel_id=channel,
                          vendor_event_time=event_time,
                          metadata_sha256=hashlib.sha256(stable_bytes).hexdigest())

    def _reference(self, content_id, role, raw_time, uid):
        if content_id is None or content_id == "":
            return
        _identifier(content_id, "contentID")
        if content_id in self.references:
            # A repeated ID is ambiguous even if the vendor calls it one person.
            raise MultipartError("ambiguous duplicate contentID reference")
        if len(self.references) >= self.max_images:
            raise MultipartError("event image count exceeds bound")
        self.references[content_id] = dict(image_role=role,
            raw_capture_time=raw_time if isinstance(raw_time, str) and len(raw_time) <= 128 else None,
            vendor_event_uid=uid if isinstance(uid, str) and len(uid) <= 512 else None)

    def _image(self, part):
        self.event_bytes += len(part.body)
        if self.event_bytes > self.max_event_bytes:
            raise MultipartError("event byte bound exceeded")
        content_id = part.headers.get("content-id", "").strip().strip("<>")
        if not content_id:
            disposition = Message()
            disposition["content-disposition"] = part.headers.get("content-disposition", "")
            # Only exact reference matching; never guess from filename or order.
            content_id = disposition.get_param("name", header="content-disposition")
        if self.event is None or content_id not in self.references:
            self.diagnostics["unbound_images"] += 1
            return None
        if content_id in self.seen:
            raise MultipartError("duplicate image contentID within one event")
        self.seen.add(content_id)
        reference = self.references[content_id]
        image = decode_jpeg(part.body)
        received, received_ms = time.monotonic(), time.time_ns() // 1_000_000
        image_hash = hashlib.sha256(part.body).hexdigest()
        source_id, camera_id = self.event_binding["source_id"], self.event_binding["camera_id"]
        profile = self.event_binding["clock_profile_version"]
        # UID scopes image evidence; it is never copied to subject_id.
        event_key = reference["vendor_event_uid"] or self.event["metadata_sha256"]
        identity = json.dumps([source_id, camera_id, event_key, content_id, image_hash],
                              ensure_ascii=False, separators=(",", ":"))
        frame_id = "hikvision:" + hashlib.sha256(identity.encode()).hexdigest()
        meta = _base_metadata(source_id, camera_id, frame_id, "hikvision", received_ms)
        meta.update(self.event, **reference, vendor_content_id=content_id, image_sha256=image_hash)
        if profile:
            meta["clock_profile_version"] = profile
        if reference["raw_capture_time"]:
            meta["time_record"] = dict(raw_time=reference["raw_capture_time"], camera_id=camera_id,
                                       capture_semantics="DEVICE_CAPTURE")
            if profile:
                meta["time_record"]["profile_version"] = profile
        self.diagnostics["images"] += 1
        return Frame(image, meta, received)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "ISAPI redirects are disabled", headers, fp)


class HikvisionEventReader:
    """Read-only Digest-authenticated ISAPI alertStream with fresh reconnect state.

    `on_frame` must enqueue quickly into a bounded consumer queue; it must not
    run inference on this reader thread. Errors are exposed as types/status
    codes only. Raw URLs, passwords and vendor personal metadata are not logged.
    """
    def __init__(self, url, source_id=None, camera_id=None, on_frame=None, username=None, password=None,
                 read_timeout_s=5, reconnect_min_s=1, reconnect_max_s=30,
                 clock_profile_version=None, expected_channel_id=None, channel_bindings=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("ISAPI URL must be HTTP(S); pass credentials separately")
        if not callable(on_frame):
            raise ValueError("on_frame callback required")
        if channel_bindings is not None:
            if any(value is not None for value in (source_id, camera_id, clock_profile_version, expected_channel_id)):
                raise ValueError("channel_bindings cannot be combined with a single camera binding")
            self.channel_bindings = _validated_channel_bindings(channel_bindings)
        else:
            _identifier(source_id, "source_id")
            _identifier(camera_id, "camera_id")
            self.channel_bindings = None
            if expected_channel_id is not None:
                expected_channel_id = _channel_identifier(expected_channel_id)
        self.url, self.source_id, self.camera_id, self.on_frame = url, source_id, camera_id, on_frame
        self.timeout = _positive(read_timeout_s, "read_timeout_s")
        if self.timeout > 30:
            raise ValueError("read_timeout_s must be at most 30")
        self.minimum = _positive(reconnect_min_s, "reconnect_min_s")
        self.maximum = _positive(reconnect_max_s, "reconnect_max_s")
        if self.maximum < self.minimum:
            raise ValueError("invalid reconnect bound")
        credentials = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        if username is not None:
            credentials.add_password(None, url, username, password or "")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                                   urllib.request.HTTPDigestAuthHandler(credentials))
        self.clock_profile_version, self.expected_channel_id = clock_profile_version, expected_channel_id
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._metrics = dict(state="NEW", reconnects=0, frames=0, last_error=None)

    def start(self):
        if self._thread is not None:
            if self._thread.is_alive():
                return self
            raise RuntimeError("reader cannot be restarted; construct a new reader")
        self._thread = threading.Thread(target=self._run, name="hikvision-events", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(self.timeout + 1)
            if self._thread.is_alive():
                raise TimeoutError("ISAPI reader callback/connect has not stopped")

    def metrics(self):
        with self._lock:
            return dict(self._metrics)

    def _run(self):
        delay = self.minimum
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                request = urllib.request.Request(self.url, method="GET",
                    headers={"Accept": "multipart/mixed, multipart/form-data", "Accept-Encoding": "identity"})
                with self.opener.open(request, timeout=self.timeout) as response:
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise MultipartError("compressed ISAPI event streams unsupported")
                    decoder = HikvisionMultipartDecoder(response.headers.get("Content-Type", ""),
                        self.source_id, self.camera_id, self.clock_profile_version,
                        expected_channel_id=self.expected_channel_id, channel_bindings=self.channel_bindings)
                    with self._lock:
                        self._metrics.update(state="STREAMING", last_error=None)
                    while not self._stop.is_set():
                        block = response.read1(32 * 1024)
                        if not block:
                            break
                        for frame in decoder.feed(block):
                            if self._stop.is_set():
                                break
                            self.on_frame(frame)
                            with self._lock:
                                self._metrics["frames"] += 1
            except Exception as error:
                with self._lock:
                    self._metrics["last_error"] = (f"HTTP_{error.code}" if isinstance(error, urllib.error.HTTPError)
                                                   else type(error).__name__)
                if isinstance(error, urllib.error.HTTPError) and error.code in (401, 403):
                    delay = self.maximum
            with self._lock:
                self._metrics["state"] = "BACKOFF"
                self._metrics["reconnects"] += 1
            if time.monotonic() - started >= 10:
                delay = self.minimum
            if self._stop.wait(delay):
                break
            delay = min(self.maximum, delay * 2)
        with self._lock:
            self._metrics["state"] = "STOPPED"


if __name__ == "__main__":
    if sys.argv[1:] == ["--gst-probe"]:
        print(json.dumps(_gst_local_status()))
    elif sys.argv[1:] == ["--gst-worker"]:
        try:
            configuration = sys.stdin.buffer.readline(8193)
            if len(configuration) > 8192:
                raise ValueError("oversized worker config")
            _gst_worker(json.loads(configuration))
        except Exception:
            # Do not leak camera URLs or authentication through child diagnostics.
            raise SystemExit(2)
    else:
        raise SystemExit("Library module; use monitoring service or --gst-probe")
