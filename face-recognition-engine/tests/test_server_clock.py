import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from face_engine.server_clock import ServerReceivedClock


class ServerClock(unittest.TestCase):
    def test_original_reception_time_survives_queue_delay_and_has_explicit_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            clock=ServerReceivedClock(dict(evidence_dir=directory),'http://127.0.0.1:1','Bearer unit-only')
            received=int(time.time()*1000)-120000
            meta=dict(camera_id='C',source_id='C',frame_id='original',received_at_unix_ms=received)
            synced=type('Synced',(),dict(returncode=0,stdout='yes\n'))()
            with patch('face_engine.server_clock.subprocess.run',return_value=synced),patch.object(clock.opener,'open',return_value=io.BytesIO(b'{"ok":true}')) as posted:
                first=clock.record(None,meta);second=clock.record(None,meta)
            self.assertEqual(first,second)
            self.assertEqual(first['source_kind'],'SERVER_FRAME_RECEIVED')
            self.assertEqual(posted.call_count,1)
            profile=json.loads(posted.call_args.args[0].data)['profile']
            self.assertEqual(profile['capture_semantics'],'SERVER_RECEIVED_AT')
            evidence=json.loads(next(Path(directory).glob('*.server-clock.json')).read_text())
            self.assertFalse(evidence['camera_capture_time_verified'])
            from datetime import datetime
            self.assertEqual(round(datetime.fromisoformat(first['raw_time']).timestamp()*1000),received)

    def test_missing_reception_time_never_uses_processing_time(self):
        with tempfile.TemporaryDirectory() as directory:
            clock=ServerReceivedClock(dict(evidence_dir=directory),'http://127.0.0.1:1','Bearer unit-only')
            with self.assertRaisesRegex(ValueError,'original server'):
                clock.record(None,dict(camera_id='C',source_id='C',frame_id='original'))


if __name__=='__main__':unittest.main()
