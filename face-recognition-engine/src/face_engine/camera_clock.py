"""Camera clock calibration and timestamps read from the exact captured image.

Neither HTTP server Date nor image receipt time is a capture timestamp. Clock
profiles describe measured camera clock error; OSD OCR supplies each frame time.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sqlite3
from contextlib import closing
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from face_engine.uuid_sources import NoRedirect


def osd_datetime(text):
    text = unicodedata.normalize('NFKC', text).replace(' ', '')
    dates = re.findall(r'(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})(?:日)?', text)
    times = re.findall(r'(?<!\d)([0-2]\d):([0-5]\d):([0-5]\d)(?!\d)', text)
    if len(set(dates)) != 1 or len(set(times)) != 1:
        raise ValueError('one complete unambiguous camera date and time required')
    values = tuple(map(int, dates[0] + times[0]))
    return datetime(*values)  # Reject impossible dates/hours; no digit repairs.


class OSDClockReader:
    def __init__(self, python_path=None):
        if python_path:
            path = Path(python_path).resolve()
            if not path.is_dir():
                raise ValueError('OCR runtime directory missing')
            sys.path.insert(0, str(path))
        from rapidocr_onnxruntime import RapidOCR
        self.engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1, use_cls=False)
        self.lock = threading.Lock()
        self.regions = {}

    def read(self, image, camera):
        import cv2
        import numpy as np
        shape = image.shape[:2]
        key = camera, shape
        with self.lock:
            regions = self.regions.get(key)
            if regions:
                pieces, scores = [], []
                for x1, y1, x2, y2 in regions:
                    result, _ = self.engine(image[y1:y2, x1:x2], use_det=False, use_cls=False)
                    if not result:
                        break
                    pieces.extend(str(row[0]) for row in result)
                    scores.extend(float(row[1]) for row in result)
                if scores and min(scores) >= .90:
                    try:
                        raw = ' '.join(pieces)
                        return osd_datetime(raw), dict(text=raw, confidence=min(scores), regions=regions)
                    except ValueError:
                        pass
                self.regions.pop(key, None)
            result, _ = self.engine(image, use_cls=False)
            candidates = []
            for box, text, score in result or []:
                norm = unicodedata.normalize('NFKC', text)
                if not (re.search(r'20\d{2}[年/.-]', norm) or re.search(r'\d\d:', norm)):
                    continue
                points = np.asarray(box)
                x1, y1 = np.maximum(0, points.min(axis=0).astype(int)-4)
                x2, y2 = np.minimum([shape[1], shape[0]], points.max(axis=0).astype(int)+5)
                region = tuple(map(int, (x1, y1, x2, y2)))
                # Re-read a native-size clock crop if the full-image pass lost
                # punctuation. It is never repaired with receiver time.
                if float(score) < .90 or norm.count(':') == 1:
                    crop = image[y1:y2, x1:x2]
                    crop = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                    rec, _ = self.engine(crop, use_det=False, use_cls=False)
                    if rec:
                        text, score = rec[0]
                if float(score) >= .90:
                    candidates.append((region, str(text), float(score)))
            raw = ' '.join(row[1] for row in candidates)
            try:
                value = osd_datetime(raw)
            except ValueError:
                # Small OSD characters can disappear in the full-frame text
                # detector. Retry unchanged edge strips at a larger OCR scale;
                # require agreement between colour and grayscale reads. Neither
                # receipt time nor expected digits participate in this retry.
                return self._read_edge_strips(image, key)
            if not candidates:
                raise ValueError('camera OSD not legible')
            self.regions[key] = [row[0] for row in candidates]
            return value, dict(text=raw, confidence=min(row[2] for row in candidates),
                               regions=self.regions[key])

    def _read_edge_strips(self, image, key):
        import cv2
        import numpy as np
        height, width = image.shape[:2]
        strip_height = min(height, max(96, height // 4))
        for top in dict.fromkeys((0, height - strip_height)):
            crop = image[top:top + strip_height]
            readings = []
            variants = (crop, cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))
            for variant in variants:
                scaled = cv2.resize(variant, None, fx=2, fy=2,
                                    interpolation=cv2.INTER_CUBIC)
                result, _ = self.engine(scaled, use_cls=False)
                pieces, scores, regions = [], [], []
                for box, text, score in result or []:
                    norm = unicodedata.normalize('NFKC', str(text))
                    if float(score) < .90 or not (
                            re.search(r'20\d{2}[年/.-]', norm) or
                            re.search(r'\d\d:', norm)):
                        continue
                    points = np.asarray(box, dtype=float) / 2
                    points[:, 1] += top
                    x1, y1 = np.maximum(0, np.floor(points.min(axis=0)).astype(int) - 4)
                    x2, y2 = np.minimum([width, height],
                                       np.ceil(points.max(axis=0)).astype(int) + 5)
                    pieces.append(str(text)); scores.append(float(score))
                    regions.append(tuple(map(int, (x1, y1, x2, y2))))
                raw = ' '.join(pieces)
                try:
                    value = osd_datetime(raw)
                except ValueError:
                    continue
                readings.append((value, raw, scores, regions))
            if len(readings) == 2:
                if readings[0][0] != readings[1][0]:
                    raise ValueError('camera OSD retry readings disagree')
                value, raw, scores, regions = readings[0]
                self.regions[key] = regions
                return value, dict(text=raw,
                    confidence=min(score for row in readings for score in row[2]),
                    regions=regions, preprocessing='same-image-edge-strip-2x',
                    consensus_passes=len(readings))
        raise ValueError('one complete unambiguous camera date and time required')


class CameraClock:
    def __init__(self, settings, endpoint, authorization):
        self.registry = settings['registry_url'].rstrip('/').removesuffix('/api/devices')
        self.endpoint = endpoint.rstrip('/')
        self.authorization = authorization
        self.evidence = Path(settings['evidence_dir'])
        self.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ocr = OSDClockReader(settings.get('ocr_python_path'))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.lock = threading.RLock()
        self.probe_slots = threading.BoundedSemaphore(8)
        self.profiles = {}
        self.retry_at = {}
        self.state_file = settings.get('state_file')
        self.ntp_checked_at = 0
        self.ntp_synced = False
        self.refresh_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='camera-clock')
        self.refreshing = set()

    def for_capture(self, camera, connection=None):
        with self.lock:
            cached = self.profiles.get(camera)
            now=time.monotonic()
            due=now>=self.retry_at.get(camera,0) and (not cached or now>=cached['refresh_at'])
            if due and camera not in self.refreshing:
                self.refreshing.add(camera)
                def refresh():
                    try:
                        self.prepare(camera,connection)
                    finally:
                        with self.lock:self.refreshing.discard(camera)
                self.refresh_pool.submit(refresh)
            if cached and now<cached['expires_at']:
                return dict(cached['metadata'])
        # Missing clock evidence holds identity delivery; never hold up video
        # acquisition while a camera clock API or reference endpoint is slow.
        return None

    def close(self):
        self.refresh_pool.shutdown(wait=True, cancel_futures=True)

    def _json(self, url, body=None, auth=False):
        headers = {'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body).encode()
        if auth:
            headers['Authorization'] = self.authorization
        with self.opener.open(urllib.request.Request(url, data=body, headers=headers), timeout=8) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('clock response too large')
        return json.loads(raw)

    def _reference(self):
        now = time.monotonic()
        with self.lock:
            check_ntp = now - self.ntp_checked_at > 60
        if check_ntp:
            # A slow D-Bus query must not hold the cache lock used by every
            # acquisition thread. Duplicate background probes are harmless.
            p = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'],
                               capture_output=True, text=True, timeout=4)
            with self.lock:
                self.ntp_synced = p.returncode == 0 and p.stdout.strip() == 'yes'
                self.ntp_checked_at = now
        with self.lock:
            if not self.ntp_synced:
                raise ValueError('host NTP synchronization unavailable')
        health = self._json(self.endpoint + '/health')
        clock = health.get('clock', {})
        stamp = clock.get('stamp', {})
        if not clock.get('trusted') or stamp.get('verified') is not True:
            raise ValueError('verified reference UTC unavailable')
        if abs(stamp['now_unix_ms']-time.time_ns()//1_000_000) > 2000:
            raise ValueError('host UTC differs from trusted reference')
        return stamp

    def prepare(self, camera, connection=None):
        """Measure now, before capture, so delayed recognition keeps its profile."""
        now = time.monotonic()
        with self.lock:
            cached = self.profiles.get(camera)
            if cached and now < cached['refresh_at']:
                return dict(cached['metadata'])
            if now < self.retry_at.get(camera, 0):
                return dict(cached['metadata']) if cached and now < cached['expires_at'] else None
            self.retry_at[camera] = now + 15
        with self.probe_slots:
            try:
                stamp = self._reference()
                before = time.time_ns()//1_000_000
                response = None
                if connection:
                    import ipaddress
                    import requests
                    if ipaddress.ip_address(connection['ip']).is_private:
                        session = requests.Session()
                        session.trust_env = False
                        try:
                            with session.get('http://'+connection['ip']+'/ISAPI/System/time',
                                    auth=requests.auth.HTTPDigestAuth(connection['username'], connection['password']),
                                    timeout=(2, 3), allow_redirects=False) as reply:
                                if reply.status_code == 200 and len(reply.content) <= 65536:
                                    response = reply.content.decode('utf-8')
                        except requests.RequestException:
                            pass
                        finally:
                            session.close()
                if response is None:
                    response = self._json(self.registry + '/api/device/getTime?' + urllib.parse.urlencode({'channelUuid': camera}))
                after = time.time_ns()//1_000_000
                data = response.get('data', response) if isinstance(response, dict) else response
                if isinstance(data, str):
                    root = ET.fromstring(data)
                    data = {node.tag.split('}')[-1]: node.text for node in root.iter()}
                if not isinstance(data, dict) or not data.get('localTime'):
                    raise ValueError('camera clock API did not return localTime')
                camera_time = datetime.fromisoformat(data['localTime'].replace('Z', '+00:00'))
                if camera_time.tzinfo is None or str(data.get('timeMode', '')).upper() != 'NTP':
                    raise ValueError('camera NTP mode and explicit timezone required')
                utc_offset = int(camera_time.utcoffset().total_seconds()/60)
                offset = int(camera_time.timestamp()*1000) - (before+after)//2
                uncertainty = math.ceil((after-before)/2) + 1000 + int(stamp['uncertainty_ms']) + 100
                if abs(offset)+uncertainty+999 > 60000:
                    raise ValueError('camera clock error exceeds one minute')
                evidence = dict(camera_id=camera, camera_clock=data, before_utc_ms=before,
                                after_utc_ms=after, reference_stamp=stamp, host_ntp_synced=True,
                                measured_offset_ms=offset, uncertainty_ms=uncertainty)
                eid = 'camera-clock-' + hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
                profile = dict(camera_id=camera, profile_version=eid, verified=True,
                    capture_semantics='DEVICE_CAPTURE', source_evidence_id=eid,
                    fixed_utc_offset_minutes=utc_offset, calibrations=[dict(calibration_id=eid,
                    source_evidence_id=eid, valid_from_utc_ms=before-10000,
                    valid_until_utc_ms=after+300000, offset_ms=offset,
                    uncertainty_ms=uncertainty, time_resolution_ms=1000)])
                (self.evidence/(eid+'.json')).write_text(json.dumps(evidence, ensure_ascii=False), encoding='utf-8')
                reply = self._json(self.endpoint+'/v1/clocks/profiles', {'profile': profile}, auth=True)
                if reply.get('ok') is not True:
                    raise ValueError('P2 refused camera clock profile')
                metadata = dict(profile_version=eid, utc_offset_minutes=utc_offset,
                                valid_from_utc_ms=before-10000, valid_until_utc_ms=after+300000)
                with self.lock:
                    self.profiles[camera] = dict(metadata=metadata, refresh_at=time.monotonic()+120,
                                                 expires_at=time.monotonic()+240)
                return metadata
            except Exception:
                with self.lock:
                    cached = self.profiles.get(camera)
                    return dict(cached['metadata']) if cached and time.monotonic()<cached['expires_at'] else None

    def record(self, image, metadata):
        profile = metadata.get('camera_clock_profile')
        raw, ocr = self.ocr.read(image, metadata['camera_id'])
        profile_covers_image=False
        if isinstance(profile,dict):
            at=int(raw.replace(tzinfo=timezone(timedelta(minutes=profile['utc_offset_minutes']))).timestamp()*1000)
            profile_covers_image=profile['valid_from_utc_ms']-60000<=at<profile['valid_until_utc_ms']+60000
        if not profile_covers_image and getattr(self,'state_file',None):
            # Recover only a persisted measured calibration for this exact
            # camera and the OSD on this exact frame. A newer receiver clock
            # must never retimestamp a delayed observation.
            uri=Path(self.state_file).resolve().as_uri()+'?mode=ro'
            with closing(sqlite3.connect(uri,uri=True,timeout=2)) as db:
                rows=db.execute("SELECT value FROM state WHERE ns='clock_profile' AND json_extract(value,'$.camera_id')=?",(metadata['camera_id'],)).fetchall()
            candidates=[]
            for (text,) in rows:
                saved=json.loads(text)
                if saved.get('verified') is not True:continue
                offset=saved['fixed_utc_offset_minutes']
                camera_ms=int(raw.replace(tzinfo=timezone(timedelta(minutes=offset))).timestamp()*1000)
                for calibration in saved.get('calibrations',[]):
                    utc_ms=camera_ms-calibration['offset_ms']
                    if calibration['valid_from_utc_ms']<=utc_ms<calibration['valid_until_utc_ms']:
                        candidates.append((calibration['valid_from_utc_ms'],saved['profile_version'],dict(
                            profile_version=saved['profile_version'],utc_offset_minutes=offset,
                            valid_from_utc_ms=calibration['valid_from_utc_ms'],valid_until_utc_ms=calibration['valid_until_utc_ms'])))
            if candidates:profile=min(candidates,key=lambda row:row[:2])[2]
        if not isinstance(profile, dict):
            raise ValueError('capture has no measured camera clock profile')
        instant = raw.replace(tzinfo=timezone(timedelta(minutes=profile['utc_offset_minutes'])))
        milliseconds = int(instant.timestamp()*1000)
        # A mismatched/old OSD image does not borrow a newer calibration.
        if not profile['valid_from_utc_ms']-60000 <= milliseconds < profile['valid_until_utc_ms']+60000:
            raise ValueError('image OSD outside its capture clock profile')
        evidence = dict(camera_id=metadata['camera_id'], frame_id=metadata['frame_id'],
                        image_sha256=hashlib.sha256(image.tobytes()).hexdigest(), ocr=ocr,
                        raw_time=instant.isoformat(timespec='seconds'))
        eid = 'osd-' + hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        (self.evidence/(eid+'.json')).write_text(json.dumps(evidence, ensure_ascii=False), encoding='utf-8')
        return dict(raw_time=evidence['raw_time'], camera_id=metadata['camera_id'],
                    source_id=metadata['source_id'], profile_version=profile['profile_version'],
                    source_kind='CAMERA_OSD_SAME_IMAGE_OCR', source_evidence_id=eid,
                    text_resolution_ms=1000)
