"""Explicit server reception order without a camera-OSD dependency.

Uses the original per-frame received_at_unix_ms, never the inference time.
The existing normalized UTC event representation retains server-reception
provenance; it does not assert optical camera capture time or bounded latency.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.request


class ServerReceivedClock:
    def __init__(self, settings, endpoint, authorization):
        self.endpoint, self.authorization = endpoint, authorization
        self.evidence = Path(settings['evidence_dir'])
        self.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.Lock()
        self.registered = set()
        self.ntp_checked_at = 0
        self.node = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def for_capture(self, camera, connection=None):
        # Acquisition must not wait for clock profile registration or OCR.
        return None

    def record(self, image, metadata):
        received = metadata.get('received_at_unix_ms')
        if type(received) is not int or received <= 0:
            raise ValueError('original server frame reception time required')
        now = int(time.time()*1000)
        if received > now+60000:
            raise ValueError('server reception time is in the future')
        day = received//86400000
        first, last = day*86400000, (day+1)*86400000
        # Include a margin around day boundaries for clock quantization.
        version = 'server-receive-v1:'+self.node+':'+str(day)
        camera = metadata['camera_id']
        key = camera, version
        with self.lock:
            if time.monotonic()-self.ntp_checked_at > 60 or not self.ntp_checked_at:
                answer = subprocess.run(['timedatectl','show','--property=NTPSynchronized','--value'],
                    capture_output=True, text=True, timeout=3)
                if answer.returncode or answer.stdout.strip() != 'yes':
                    raise ValueError('server UTC clock is not NTP synchronized')
                self.ntp_checked_at = time.monotonic()
            if key not in self.registered:
                profile = dict(camera_id=camera, profile_version=version, verified=True,
                    capture_semantics='SERVER_RECEIVED_AT', source_evidence_id=version,
                    fixed_utc_offset_minutes=0, calibrations=[dict(calibration_id=version,
                        source_evidence_id=version, valid_from_utc_ms=first-60000,
                        valid_until_utc_ms=last+60000, offset_ms=0,
                        uncertainty_ms=1000, time_resolution_ms=1)])
                request = urllib.request.Request(self.endpoint+'/v1/clocks/profiles',
                    data=json.dumps(dict(profile=profile)).encode(),
                    headers={'Authorization':self.authorization,'Content-Type':'application/json'})
                with self.opener.open(request,timeout=10) as response:reply=json.load(response)
                if reply.get('ok') is not True:
                    raise ValueError('P2 refused server reception clock profile')
                self.registered.add(key)
                path=self.evidence/(hashlib.sha256((camera+version).encode()).hexdigest()+'.server-clock.json')
                path.write_text(json.dumps(dict(profile=profile,time_basis='SERVER_RECEIVED_AT',
                    camera_capture_time_verified=False,network_delay_bounded=False)),encoding='utf-8')
                path.chmod(0o600)
        evidence=dict(camera_id=camera,source_id=metadata['source_id'],
            frame_id=metadata['frame_id'],received_at_unix_ms=received,time_basis='SERVER_RECEIVED_AT')
        eid='server-frame-receive:'+hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()
        return dict(camera_id=camera,source_id=metadata['source_id'],profile_version=version,
            raw_time=datetime.fromtimestamp(received/1000,timezone.utc).isoformat(timespec='milliseconds'),
            source_kind='SERVER_FRAME_RECEIVED',source_evidence_id=eid,text_resolution_ms=1)

    def close(self):
        self.registered.clear()
