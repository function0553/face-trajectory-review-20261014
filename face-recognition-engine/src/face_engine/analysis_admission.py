"""Camera-local motion and bounded shared inference budgets.

Adapted design: swadhinbiswas/AegisVision, commit 7ad92210bcfb491dc2090ef7fc73ab27cab8c03d
(CC0-1.0), camera_manager.py and analysis_scheduler.py. Identity matching stays
outside admission. This implementation uses the existing durable/fair queues;
it never submits futures into a second, unbounded executor.
"""
from collections import Counter
import math
import threading
import time

import cv2
import numpy as np


class MotionAdmission:
    """Each camera owns its references; gradual movement can accumulate.

    The first frame and a finite idle deadline are always admitted. The last
    admitted image is a second reference so slow movement is not forgotten on
    every frame. Failed persistence resets admission for a prompt retry.
    """
    def __init__(self, idle_seconds=60, thumbnail_size=(96, 54), cumulative=True):
        if (type(idle_seconds) not in (int, float) or not math.isfinite(idle_seconds)
                or not 1 <= idle_seconds <= 60):
            raise ValueError('motion idle_seconds must be 1..60')
        if (not isinstance(thumbnail_size, (list, tuple)) or len(thumbnail_size) != 2
                or any(type(v) is not int or not 16 <= v <= 320 for v in thumbnail_size)):
            raise ValueError('motion thumbnail_size must contain two dimensions in 16..320')
        if type(cumulative) is not bool:
            raise ValueError('motion cumulative must be boolean')
        self.idle_seconds, self.thumbnail_size, self.cumulative = idle_seconds, tuple(thumbnail_size), cumulative
        self.previous = self.reference = None
        self.active_until = 0
        self.last_saved = -float('inf')
        self.last_seen = -float('inf')
        self.lock = threading.Lock()
        self.counters = Counter()

    def reset(self):
        with self.lock:
            self.previous = self.reference = None
            self.active_until = 0
            self.last_saved = self.last_seen = -float('inf')
            self.counters['resets'] += 1

    def capture_failed(self):
        self.reset()
        with self.lock:
            self.counters['persistence_retry_resets'] += 1

    def admit(self, image, now):
        if not math.isfinite(now):
            raise ValueError('finite motion timestamp required')
        small = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), self.thumbnail_size)
        with self.lock:
            self.counters['examined'] += 1
            first = self.previous is None or now < self.last_seen
            changed = False
            accumulated = False
            if not first:
                changed = np.count_nonzero(cv2.absdiff(small, self.previous) > 12) >= 16
                if self.cumulative and not changed and self.reference is not None:
                    accumulated = np.count_nonzero(cv2.absdiff(small, self.reference) > 12) >= 16
            self.previous = small
            self.last_seen = now
            if changed or accumulated:
                self.active_until = now + 2
            deadline = now - self.last_saved >= self.idle_seconds
            admitted = first or changed or accumulated or now < self.active_until or deadline
            if admitted:
                self.last_saved = now
                self.reference = small.copy()
                reason = 'first' if first else ('motion' if changed else (
                    'cumulative_motion' if accumulated else ('idle_deadline' if deadline else 'motion_tail')))
                self.counters[reason] += 1
                self.counters['admitted'] += 1
            else:
                self.counters['idle_skipped'] += 1
            return admitted

    def stats(self):
        with self.lock:
            return dict(self.counters, idle_seconds=self.idle_seconds,
                        thumbnail_size=list(self.thumbnail_size), cumulative=self.cumulative)


class AnalysisBudget:
    """Thread-safe token bucket shared by infer workers, without dropping input.

    A zero rate preserves the existing unrestricted inference speed. No forced
    camera check bypasses a configured budget; durable inputs wait in the fair
    inbox instead of accumulating extra futures or shutting down readers.
    """
    def __init__(self, rate=0, burst=4, *, clock=time.monotonic):
        if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 100000:
            raise ValueError('analysis_fps_limit must be finite and nonnegative')
        if type(burst) is not int or not 1 <= burst <= 64:
            raise ValueError('analysis_burst must be 1..64')
        self.rate, self.capacity, self.clock = rate, burst, clock
        self.tokens, self.updated = float(burst), clock()
        self.lock = threading.Lock()
        self.admitted = self.deferred = 0

    def delay(self):
        if not self.rate:
            return 0
        with self.lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + max(0, now-self.updated)*self.rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                self.admitted += 1
                return 0
            self.deferred += 1
            return (1-self.tokens)/self.rate

    def wait(self, stop_event):
        while not stop_event.is_set():
            delay = self.delay()
            if delay == 0:
                return True
            stop_event.wait(min(delay, .25))
        return False

    def stats(self):
        with self.lock:
            return dict(enabled=bool(self.rate), fps_limit=self.rate, burst=self.capacity,
                        admissions=self.admitted, deferred_checks=self.deferred)
