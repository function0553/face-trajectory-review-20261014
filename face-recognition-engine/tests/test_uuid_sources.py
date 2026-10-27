"""UUID registry resolution, bounded rotation, fail-closed and stream failover."""
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from face_engine.uuid_sources import UUIDSources, UUIDSourceUnavailable, FailoverCamera, PrivateRTSPReader


class Response(io.BytesIO):
    status = 200


class UUIDResolution(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.settings = dict(registry_url='http://127.0.0.1:7600/api/devices',
                             rtsp_base_url='rtsp://127.0.0.1:554', live_app='hiklive',
                             ffmpeg_path='/private/bin/ffmpeg', max_readers=2,
                             rotation_seconds=30, registry_refresh_seconds=10)
        self.resolver = UUIDSources(self.settings, clock=lambda: self.now)
        self.devices = [dict(online=True, password='NEVER_OUTPUT', channels=[
            dict(uuid='CAM_A', channelName='same name', online=True, enabled=True),
            dict(uuid='CAM_B', channelName='same name', online=True, enabled=True),
            dict(uuid='CAM_C', channelName='different', online=True),
            dict(uuid='OFFLINE', online=False), dict(uuid='DISABLED', online=True, enabled=False)])]
        self.mock = patch.object(self.resolver.opener, 'open', side_effect=lambda *a, **k: Response(json.dumps(self.devices).encode()))
        self.open = self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_exact_uuid_and_real_main_sub_format(self):
        result = self.resolver.resolve({'CAM_A': 'backend name', 'UNKNOWN': 'same name'})
        self.assertEqual(set(result), {'CAM_A'})
        self.assertEqual(result['CAM_A']['sub_url'], 'rtsp://127.0.0.1:554/hiklive/CAM_A-3')
        self.assertEqual(result['CAM_A']['main_url'], 'rtsp://127.0.0.1:554/hiklive/CAM_A-0')
        self.assertEqual(self.resolver.status['unresolved'], 1)
        self.assertNotIn('NEVER_OUTPUT', repr(self.resolver.__dict__))

    def test_offline_disabled_and_wrong_uuid_are_not_guessed(self):
        self.assertEqual(self.resolver.resolve(['OFFLINE', 'DISABLED', 'same name']), {})

    def test_zero_video_channel_capability_is_not_treated_as_usable_video(self):
        self.devices.append(dict(online=True, channelCount=0, channels=[dict(uuid='NO_VIDEO', online=True)]))
        self.assertEqual(set(self.resolver.resolve(['NO_VIDEO', 'CAM_A'])), {'CAM_A'})
        self.assertEqual(self.resolver.status['no_video_channels'], 1)
        self.assertEqual(self.resolver.no_video, {'NO_VIDEO'})

    def test_no_active_binding_does_not_open_registry_or_streams(self):
        self.assertEqual(self.resolver.resolve([]), {})
        self.open.assert_not_called()

    def test_registry_cache_and_bounded_fair_rotation(self):
        first = self.resolver.resolve(['CAM_A', 'CAM_B', 'CAM_C'])
        self.assertEqual(set(first), {'CAM_A', 'CAM_B'})
        self.now += 2
        self.resolver.resolve(['CAM_A', 'CAM_B', 'CAM_C'])
        self.assertEqual(self.open.call_count, 1)
        self.now += 30
        second = self.resolver.resolve(['CAM_A', 'CAM_B', 'CAM_C'])
        self.assertEqual(len(second), 2)
        self.assertEqual(set(first) | set(second), {'CAM_A', 'CAM_B', 'CAM_C'})
        self.assertEqual(self.resolver.status['deferred'], 1)

    def test_registry_failure_revokes_cache_and_stays_unavailable_until_retry(self):
        self.resolver.resolve(['CAM_A'])
        self.now += 11
        self.open.side_effect = OSError('sensitive error')
        with self.assertRaisesRegex(UUIDSourceUnavailable, '^UUID registry unavailable$'):
            self.resolver.resolve(['CAM_A'])
        self.assertEqual(self.resolver.known, set())
        with self.assertRaises(UUIDSourceUnavailable):
            self.resolver.resolve(['CAM_A'])
        self.assertEqual(self.resolver.status['status'], 'REGISTRY_UNAVAILABLE')

    def test_registry_membership_churn_does_not_reset_rotation_deadline(self):
        self.assertEqual(set(self.resolver.resolve(['CAM_0', 'CAM_A', 'CAM_B', 'CAM_C'])), {'CAM_A', 'CAM_B'})
        self.now += 11
        self.devices[0]['channels'].append(dict(uuid='CAM_0', online=True))
        self.assertEqual(set(self.resolver.resolve(['CAM_0', 'CAM_A', 'CAM_B', 'CAM_C'])), {'CAM_A', 'CAM_B'})
        self.now = 131
        self.assertEqual(set(self.resolver.resolve(['CAM_0', 'CAM_A', 'CAM_B', 'CAM_C'])), {'CAM_C', 'CAM_0'})

    def test_registry_must_be_local_and_no_uri_injection(self):
        for change in [dict(registry_url='http://192.0.2.1/api/devices'), dict(live_app='../escape'),
                       dict(rtsp_base_url='rtsp://user:password@127.0.0.1'),
                       dict(pinned_camera_ids=['CAM_A']),
                       dict(pinned_camera_ids=['CAM_A', 'CAM_A'], pinned_until_unix_ms=200000)]:
            with self.assertRaises(ValueError):
                UUIDSources(dict(self.settings, **change))

    def direct_resolver(self):
        self.resolver = UUIDSources(dict(self.settings, transport='direct_rtsp'), clock=lambda: self.now)
        self.mock.stop()
        new_mock = patch.object(self.resolver.opener, 'open', side_effect=lambda *a, **k: Response(json.dumps(self.devices).encode()))
        self.open = new_mock.start()
        self.addCleanup(new_mock.stop)
        return self.resolver

    def test_direct_resolution_uses_exact_registry_channel_and_encodes_credentials(self):
        self.devices[0].update(ip='192.0.2.12', username='u@:', password="p' &/$")
        self.devices[0]['channels'][0]['index'] = 2
        resolver = self.direct_resolver()
        result = resolver.resolve(['CAM_A'])['CAM_A']
        self.assertEqual(result['main_url'], "rtsp://u%40%3A:p%27%20%26%2F%24@192.0.2.12:554/Streaming/Channels/201")
        self.assertTrue(result['sub_url'].endswith('/202'))
        self.assertNotIn('password', repr(resolver.status))

    def test_direct_sources_without_authoritative_index_fail_closed(self):
        self.devices[0].update(ip='192.0.2.12', username='u', password='p')
        resolver = self.direct_resolver()
        self.assertEqual(resolver.resolve(['CAM_A']), {})
        self.assertEqual(resolver.status['unavailable_direct_sources'], 1)
        self.assertNotIn('hiklive', repr(resolver._direct))

    def test_direct_cache_revoked_after_registry_failure_or_no_active_binding(self):
        self.devices[0].update(ip='192.0.2.12', username='u', password='p')
        self.devices[0]['channels'][0]['index'] = 1
        resolver = self.direct_resolver()
        self.assertTrue(resolver.resolve(['CAM_A']))
        self.assertTrue(resolver._direct)
        resolver.resolve([])
        self.assertFalse(resolver._direct)
        self.assertTrue(resolver.resolve(['CAM_A']))
        self.now += 11
        self.open.side_effect = OSError()
        with self.assertRaises(UUIDSourceUnavailable):
            resolver.resolve(['CAM_A'])
        self.assertFalse(resolver._direct)


class Fallback(unittest.TestCase):
    def test_main_failure_switches_to_sub_and_preserves_actual_stream_kind(self):
        import numpy as np
        from face_engine.acquisition import Frame
        made = []
        class Reader:
            def __init__(self, url, source, camera, kind, **kwargs):
                self.kind, self.stopped = kind, False
                made.append(self)
            def start(self): return self
            def latest(self, *args, **kwargs):
                if self.kind == 'sub':
                    return Frame(np.zeros((4, 5, 3), np.uint8), dict(frame_id=str(time.monotonic()), stream_kind='sub'), time.monotonic())
            def stop(self): self.stopped = True
            def metrics(self): return dict(frames=int(self.kind == 'sub'), state='STREAMING' if self.kind == 'sub' else 'BACKOFF')
        camera = FailoverCamera('rtsp://test/sub', 'rtsp://test/main', 'S', 'C', failover_seconds=.01)
        camera.reader_type = Reader
        camera.start()
        try:
            deadline = time.monotonic() + 2
            while camera.metrics()['stream_kind'] != 'sub' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(camera.latest().metadata['stream_kind'], 'sub')
            self.assertTrue(made[0].stopped)
            self.assertEqual(camera.metrics()['failovers'], 1)
            self.assertNotIn('rtsp://', repr(camera.metrics()))
        finally:
            camera.stop()

        self.assertTrue(all(reader.stopped for reader in made))

    def test_healthy_main_is_preferred_and_not_replaced_by_available_sub(self):
        import numpy as np
        from face_engine.acquisition import Frame
        made = []
        class Reader:
            def __init__(self, url, source, camera, kind, **kwargs):
                self.kind, self.stopped = kind, False
                made.append(self)
            def start(self): return self
            def latest(self, *args, **kwargs):
                return Frame(np.zeros((4, 5, 3), np.uint8),
                             dict(frame_id=str(time.monotonic()), stream_kind=self.kind), time.monotonic())
            def metrics(self): return dict(frames=1, state='STREAMING')
            def stop(self): self.stopped = True
        camera = FailoverCamera('rtsp://test/sub', 'rtsp://test/main', 'S', 'C', failover_seconds=.3)
        camera.reader_type = Reader
        try:
            camera.start()
            time.sleep(.7)
            self.assertEqual(camera.latest().metadata['stream_kind'], 'main')
            self.assertEqual([reader.kind for reader in made], ['main'])
            self.assertEqual(camera.metrics()['failovers'], 0)
        finally:
            camera.stop()


class PrivateInput(unittest.TestCase):
    def test_rtsp_secret_is_passed_only_through_stdin(self):
        url = 'rtsp://private-user:private-secret@192.0.2.12/Streaming/Channels/101'
        reader = PrivateRTSPReader(url, 'S', 'C', one_shot=True)
        class Pipe(io.BytesIO):
            def close(self): self.saved = self.getvalue(); super().close()
        class Process:
            def __init__(self): self.stdin, self.stdout = Pipe(), io.BytesIO()
        process = Process()
        with patch('face_engine.uuid_sources._rtsp_timeout_option', return_value='-timeout'), patch('face_engine.acquisition._rtsp_timeout_option', return_value='-timeout'), patch('face_engine.uuid_sources.subprocess.Popen', return_value=process) as spawn:
            command = reader._command()
            reader._spawn(command)
        self.assertNotIn('private-secret', repr(command))
        self.assertNotIn('private-user', repr(command))
        self.assertIn(url.encode(), process.stdin.saved)
        self.assertIn(b'option timeout ', process.stdin.saved)
        self.assertEqual(spawn.call_args.kwargs['stderr'], -3)
        self.assertIn('-frames:v', command)
        reader._release_connect()

    def test_playlist_injection_is_rejected_before_process_spawn(self):
        reader = PrivateRTSPReader('rtsp://192.0.2.12/\nfile /etc/passwd', 'S', 'C')
        with patch('face_engine.uuid_sources.subprocess.Popen') as spawn:
            with self.assertRaises(ValueError): reader._spawn([])
        spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
