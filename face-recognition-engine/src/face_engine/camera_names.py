"""Read camera channel names from ISAPI, independently of inference and tables.

Only explicit device connections are queried. No device-wide name or another
channel's name is used as a fallback. Credentials and raw errors stay private.
"""
from concurrent.futures import ThreadPoolExecutor
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'device redirects disabled', headers, fp)


def channel_name(xml, stream_id):
    if len(xml) > 65536 or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
        raise ValueError('invalid device XML')
    root = ET.fromstring(xml)
    local = lambda tag: tag.rsplit('}', 1)[-1]
    if local(root.tag) != 'StreamingChannel':
        raise ValueError('one StreamingChannel required')
    values = {}
    for child in root:
        key = local(child.tag)
        if key in ('id', 'channelName'):
            if key in values or len(child):
                raise ValueError('ambiguous channel response')
            values[key] = (child.text or '').strip()
    if values.get('id') != str(stream_id):
        raise ValueError('device returned another channel')
    name = values.get('channelName', '')
    if not name or len(name.encode('utf-8')) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError('device channel name is unavailable')
    return name


class ISAPINameReader:
    def __init__(self, url, username, password, refresh_seconds=5, timeout_seconds=2):
        parsed = urllib.parse.urlsplit(url)
        match = re.fullmatch(r'/ISAPI/Streaming/channels/([1-9][0-9]*)', parsed.path, re.I)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or not match):
            raise ValueError('device name URL must identify one ISAPI streaming channel, without credentials')
        self.stream_id = int(match[1])
        if self.stream_id < 101 or self.stream_id % 100 == 0:
            raise ValueError('explicit ISAPI stream channel required')
        for value, minimum, maximum in ((refresh_seconds, 1, 300), (timeout_seconds, .1, 5)):
            if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError('device name polling interval or timeout outside bounds')
        self.refresh_seconds, self.timeout = refresh_seconds, timeout_seconds
        self.url = url
        credentials = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        credentials.add_password(None, url, username, password)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPDigestAuthHandler(credentials))

    def read(self):
        request = urllib.request.Request(self.url, headers={'Accept': 'application/xml', 'Accept-Encoding': 'identity'})
        with self.opener.open(request, timeout=self.timeout) as response:
            if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise ValueError('device name response not supported')
            chunks, size, deadline = [], 0, time.monotonic() + self.timeout
            while size <= 65536:
                if time.monotonic() > deadline:
                    raise TimeoutError('device name read timed out')
                chunk = response.read1(min(4096, 65537-size))
                if not chunk:
                    break
                size += len(chunk)
                chunks.append(chunk)
            return channel_name(b''.join(chunks), self.stream_id)

    @classmethod
    def from_camera(cls, camera):
        config, hik = camera.get('device_name'), camera.get('hikvision', {})
        if config is None:
            if not hik.get('enabled'):
                return None
            # The existing alert subscription already binds this exact physical
            # channel. Its first stream uses channel * 100 + 1 in ISAPI.
            channel = str(hik.get('expected_channel_id', ''))
            if not re.fullmatch(r'[1-9][0-9]*', channel):
                raise ValueError('device name needs the configured physical channel')
            alert = urllib.parse.urlsplit(os.environ.get(hik.get('alert_url_env', ''), ''))
            url = urllib.parse.urlunsplit((alert.scheme, alert.netloc,
                '/ISAPI/Streaming/channels/' + str(int(channel)*100+1), '', ''))
            config = hik
        else:
            if not isinstance(config, dict) or config.get('protocol', 'hikvision_isapi') != 'hikvision_isapi':
                raise ValueError('device_name currently supports hikvision_isapi')
            url = os.environ.get(config.get('url_env', ''), '')
        username = os.environ.get(config.get('username_env', ''), '')
        password = os.environ.get(config.get('password_env', ''), '')
        if not url or not username or not password:
            raise ValueError('device name connection environment is incomplete')
        reader = cls(url, username, password, config.get('refresh_seconds', 5), config.get('timeout_seconds', 2))
        if hik.get('enabled') and reader.stream_id // 100 != int(hik['expected_channel_id']):
            raise ValueError('name query and alert channel do not match')
        # Check recognizable RTSP routes too; main/sub streams of the same
        # physical channel may legitimately use different stream suffixes.
        for field in ('sub_url_env', 'main_url_env'):
            stream = urllib.parse.urlsplit(os.environ.get(camera.get(field, ''), ''))
            match = re.fullmatch(r'/Streaming/Channels/([1-9][0-9]*)/?', stream.path, re.I)
            if match and int(match[1]) // 100 != reader.stream_id // 100:
                raise ValueError('name query and video channel do not match')
        return reader


class CameraNames:
    """Bounded asynchronous refresh; snapshots never perform network I/O."""
    def __init__(self):
        self.entries, self.lock = {}, threading.Lock()
        self.stopping, self.thread = threading.Event(), None

    def add(self, source, camera, reader):
        if self.thread is not None:
            raise RuntimeError('configure name sources before starting')
        self.entries[(source, camera)] = dict(reader=reader, name=None, checked=None,
            due=0, status='PENDING' if reader else 'UNCONFIGURED')

    def refresh(self, key):
        reader = self.entries[key]['reader']
        started = time.monotonic()
        try:
            name, status = reader.read(), 'OK'
        except Exception as error:
            name = None
            status = 'HTTP_' + str(error.code) if isinstance(error, urllib.error.HTTPError) else type(error).__name__
        with self.lock:
            self.entries[key].update(name=name, status=status, checked=started,
                due=time.monotonic()+reader.refresh_seconds)

    def snapshot(self, source, camera):
        with self.lock:
            entry = self.entries.get((source, camera))
            if not entry or entry['checked'] is None or entry['reader'] is None:
                return None
            if time.monotonic()-entry['checked'] > entry['reader'].refresh_seconds + 2*entry['reader'].timeout:
                return None
            return entry['name']

    def status(self):
        with self.lock:
            return [dict(source_id=source, camera_id=camera, status=entry['status'],
                         age_seconds=None if entry['checked'] is None else time.monotonic()-entry['checked'])
                    for (source, camera), entry in self.entries.items()]

    def start(self):
        if self.thread is not None:
            raise RuntimeError('name refresh already started')
        def run():
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix='camera-name') as pool:
                pending = {}
                while not self.stopping.is_set():
                    pending = {key: future for key, future in pending.items() if not future.done()}
                    with self.lock:
                        due = sorted((entry['due'], key) for key, entry in self.entries.items()
                                     if entry['reader'] and key not in pending and entry['due'] <= time.monotonic())
                    for _, key in due[:4-len(pending)]:
                        pending[key] = pool.submit(self.refresh, key)
                    self.stopping.wait(.05)
        self.thread = threading.Thread(target=run, name='camera-names', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                raise RuntimeError('camera name refresh did not stop')
