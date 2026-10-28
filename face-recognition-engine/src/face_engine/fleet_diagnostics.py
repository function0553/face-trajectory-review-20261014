"""Bounded camera feed incident snapshots, independent of face identities.

Adapted design: AegisVision camera_ops_service.py at
7ad92210bcfb491dc2090ef7fc73ab27cab8c03d (CC0-1.0). This is a read-only health
projection: it sends no alerts, reboots no devices and changes no activity.
"""
from collections import Counter, deque
import math
import threading
import time


class FleetDiagnostics:
    def __init__(self, *, grace_seconds=20, metrics_max_age_seconds=3,
                 cooldown_seconds=60, max_events=64, clock=time.monotonic):
        for value in (grace_seconds, metrics_max_age_seconds, cooldown_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('positive finite feed diagnostic interval required')
        if type(max_events) is not int or not 1 <= max_events <= 1024:
            raise ValueError('max_events must be 1..1024')
        self.grace = grace_seconds
        self.metrics_max_age = metrics_max_age_seconds
        self.cooldown = cooldown_seconds
        self.clock = clock
        self.records = {}
        self.events = deque(maxlen=max_events)
        self.lock = threading.Lock()

    @staticmethod
    def _number(value):
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None

    def _classify(self, metrics, elapsed):
        frame_age = self._number(metrics.get('frame_age_seconds'))
        metrics_age = self._number(metrics.get('metrics_age_seconds'))
        if metrics_age is not None and metrics_age > self.metrics_max_age:
            return 'STALE_ACQUISITION_STATUS'
        if frame_age is not None and frame_age > self.grace:
            return 'FRAME_STALLED'
        frames = self._number(metrics.get('frames'))
        if frames and frame_age is not None:
            return 'LIVE'
        if elapsed < self.grace:
            return 'STARTING'
        if not frames:
            return 'NO_DECODED_FRAME'
        return 'FRAME_FRESHNESS_UNKNOWN'

    def snapshot(self, inputs, binding_status):
        """Input metrics never include connection URLs or biometric data."""
        now = self.clock()
        with self.lock:
            self.records = {key: row for key, row in self.records.items() if key in inputs}
            counts = Counter()
            issues = []
            for camera, metrics in inputs.items():
                record = self.records.setdefault(camera, dict(since=now, offline=False, last_event=-float('inf')))
                state = self._classify(metrics, max(0, now-record['since']))
                counts[state] += 1
                offline = state not in ('LIVE', 'STARTING')
                # Only fresh frame evidence resolves an incident, not a restart
                # label or an old nonzero cumulative decoded-frame counter.
                if state == 'LIVE' and record['offline']:
                    self.events.append(dict(camera_id=camera, event='FEED_RECOVERED',
                                            observed_monotonic=now))
                    record.update(offline=False, last_event=now)
                elif offline and (not record['offline'] or now-record['last_event'] >= self.cooldown):
                    self.events.append(dict(camera_id=camera, event='FEED_UNAVAILABLE',
                                            reason=state, observed_monotonic=now))
                    record.update(offline=True, last_event=now)
                if offline and len(issues) < 32:
                    issues.append(dict(camera_id=camera, reason=state,
                        frame_age_seconds=self._number(metrics.get('frame_age_seconds')),
                        metrics_age_seconds=self._number(metrics.get('metrics_age_seconds'))))
            recent = [dict(row) for row in self.events]
            unresolved = len(binding_status.get('missing_camera_ids', []))
            if not inputs:
                state = 'IDLE_NO_ELIGIBLE_ACTIVITY_ROUTE' if binding_status.get('status') == 'IDLE' else 'NO_ACTIVE_READERS'
            elif counts['LIVE'] == len(inputs) and not unresolved:
                state = 'LIVE'
            else:
                state = 'STARTING_OR_PARTIAL_COVERAGE'
            return dict(state=state, registered_readers=len(inputs), states=dict(counts),
                unresolved_bound_camera_count=unresolved, issue_sample=issues,
                recent_feed_events=recent, observed_monotonic=now,
                basis='fresh frame and acquisition-worker status; not face recognition or backend delivery',
                external_alerts_sent=False)
