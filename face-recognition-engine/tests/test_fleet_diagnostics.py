import unittest
from face_engine.fleet_diagnostics import FleetDiagnostics


class FeedIncidentProjection(unittest.TestCase):
    def test_startup_offline_cooldown_and_real_frame_recovery(self):
        now = [0.0]
        monitor = FleetDiagnostics(clock=lambda: now[0])
        inputs = {'C': dict(state='CONNECTING', frames=0, metrics_age_seconds=0)}
        self.assertEqual(monitor.snapshot(inputs, {})['states'], {'STARTING': 1})
        now[0] = 21
        self.assertEqual(monitor.snapshot(inputs, {})['states'], {'NO_DECODED_FRAME': 1})
        now[0] = 22
        self.assertEqual(len(monitor.snapshot(inputs, {})['recent_feed_events']), 1)
        inputs['C']['state'] = 'STREAMING'  # State label alone is not evidence.
        self.assertEqual(monitor.snapshot(inputs, {})['states'], {'NO_DECODED_FRAME': 1})
        inputs['C'].update(frames=1, frame_age_seconds=.1)
        self.assertEqual(monitor.snapshot(inputs, {})['recent_feed_events'][-1]['event'], 'FEED_RECOVERED')
        self.assertEqual(monitor.snapshot(inputs, {})['states'], {'LIVE': 1})

    def test_old_worker_snapshot_cannot_claim_a_live_camera(self):
        monitor = FleetDiagnostics()
        result = monitor.snapshot({'C': dict(frames=100, frame_age_seconds=0, metrics_age_seconds=30)}, {})
        self.assertEqual(result['states'], {'STALE_ACQUISITION_STATUS': 1})
        self.assertFalse(result['external_alerts_sent'])

    def test_counters_do_not_replace_freshness_and_inactive_routes_do_not_alert(self):
        now = [0.0]
        monitor = FleetDiagnostics(clock=lambda: now[0])
        inputs = {'C': dict(frames=100)}
        monitor.snapshot(inputs, {})
        now[0] = 21
        self.assertEqual(monitor.snapshot(inputs, {})['states'], {'FRAME_FRESHNESS_UNKNOWN': 1})
        result = monitor.snapshot({}, {'status': 'IDLE'})
        self.assertEqual(result['state'], 'IDLE_NO_ELIGIBLE_ACTIVITY_ROUTE')
        self.assertEqual(result['registered_readers'], 0)
        self.assertEqual(len(monitor.records), 0)

    def test_bounded_history_and_directory_gaps_remain_explicit(self):
        monitor = FleetDiagnostics(max_events=2)
        result = monitor.snapshot({str(i): dict(frames=5, frame_age_seconds=25) for i in range(5)},
                                  {'missing_camera_ids': ['unresolved']})
        self.assertEqual(len(result['recent_feed_events']), 2)
        self.assertEqual(result['unresolved_bound_camera_count'], 1)
        self.assertEqual(result['states'], {'FRAME_STALLED': 5})


if __name__ == '__main__':
    unittest.main()
