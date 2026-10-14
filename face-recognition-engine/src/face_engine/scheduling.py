"""Bounded latest-frame scheduling; identity confidence is never a priority input.

Weights allocate service opportunities among busy classes. Cameras within each
class rotate FIFO, and replacing a stale frame does not reset its queue position.
"""
from collections import OrderedDict, Counter
from dataclasses import dataclass
import heapq
import threading
import time


CATEGORIES = ("PRIORITY", "NORMAL", "UNREGISTERED")


@dataclass
class FrameJob:
    key: str
    image: object
    metadata: dict
    category: str
    received_monotonic: float


class FairLatestQueue:
    def __init__(self, weights=(6, 3, 1), *, max_cameras=128,
                 max_bytes=512 * 1024 * 1024, max_age_seconds=2.0, clock=time.monotonic):
        if len(weights) != 3 or any(type(w) is not int or w < 1 for w in weights):
            raise ValueError("all three classes require positive integer weights")
        if max_cameras < 1 or max_bytes < 1 or max_age_seconds <= 0:
            raise ValueError("queue limits must be positive")
        self.queues = {category: OrderedDict() for category in CATEGORIES}
        # Interleave rounds to avoid a large contiguous priority burst.
        self.cycle = [category for i in range(max(weights))
                      for category, weight in zip(CATEGORIES, weights) if i < weight]
        self.cursor = 0
        self.max_cameras, self.max_bytes = max_cameras, max_bytes
        self.max_age_seconds, self.clock = max_age_seconds, clock
        self.bytes = 0
        self.counters = Counter()
        self.lock = threading.Lock()

    def put(self, job):
        return self.offer(job) in ("accepted", "replaced")

    def offer(self, job, *, require_pending=False):
        """Return an atomic admission result; refreshes cannot create a slot.

        A consumer can pop between pending_keys() and the producer's refresh.
        require_pending makes that race explicit instead of bypassing admission
        fairness with an accidental new enqueue.
        """
        if job.category not in CATEGORIES:
            raise ValueError("unknown scheduling category")
        size = int(job.image.nbytes)
        with self.lock:
            if size > self.max_bytes:
                self.counters["oversized"] += 1
                return "oversized"
            old_queue = next((q for q in self.queues.values() if job.key in q), None)
            old = old_queue.get(job.key) if old_queue is not None else None
            if require_pending and old is None:
                return "not_pending"
            old_size = int(old.image.nbytes) if old else 0
            count = sum(map(len, self.queues.values()))
            if (old is None and count >= self.max_cameras) or self.bytes - old_size + size > self.max_bytes:
                # Do not evict another waiting camera: under load it would starve.
                self.counters["capacity_rejected"] += 1
                return "capacity_rejected"
            if old:
                if old.metadata.get("frame_id") == job.metadata.get("frame_id"):
                    self.counters["duplicate"] += 1
                    return "duplicate"
                self.counters["replaced"] += 1
                if old.category != job.category:
                    del old_queue[job.key]
            self.queues[job.category][job.key] = job
            self.bytes += size - old_size
            self.counters["accepted"] += 1
            return "replaced" if old else "accepted"

    def pending_keys(self):
        """One bounded snapshot for planning; offer() rechecks under the lock."""
        with self.lock:
            return frozenset(key for queue in self.queues.values() for key in queue)

    def pop(self):
        with self.lock:
            now = self.clock()
            for _ in self.cycle:
                category = self.cycle[self.cursor]
                self.cursor = (self.cursor + 1) % len(self.cycle)
                queue = self.queues[category]
                while queue:
                    _, job = queue.popitem(last=False)
                    self.bytes -= int(job.image.nbytes)
                    if now - job.received_monotonic > self.max_age_seconds:
                        self.counters["expired"] += 1
                        continue
                    self.counters["served_" + category] += 1
                    return job
            return None

    def stats(self):
        with self.lock:
            return {**self.counters, "pending": {c: len(q) for c, q in self.queues.items()},
                    "pending_bytes": self.bytes, "weights": {c: self.cycle.count(c) for c in CATEGORIES}}


class AdmissionPlanner:
    """Fair admission for cameras still outside a bounded latest-frame queue.

    One producer supplies this poll's eligible frames. Pending frames refresh
    without spending admission turns. Successful new admissions rotate classes
    and then cameras by their oldest successful admission; rejection never makes
    a camera younger. Processing quotas remain owned by FairLatestQueue.
    """
    def __init__(self, weights=(6, 3, 1)):
        if len(weights) != 3 or any(type(w) is not int or w < 1 for w in weights):
            raise ValueError("all three classes require positive integer weights")
        self.cycle = [category for i in range(max(weights))
                      for category, weight in zip(CATEGORIES, weights) if i < weight]
        self.cursor = 0
        self.sequence = 0
        self.last_admitted = {}
        self.counters = Counter()
        self.lock = threading.Lock()

    def admit(self, queue, candidates):
        """Return accepted FrameJobs; callers advance sampling clocks only for these.

        A key occurs at most once per round. If a refresh loses its pending slot
        to the consumer, it joins the normal admission competition in this round.
        Oversized jobs cannot block the rest. A frame that fits an empty queue
        reserves the next turn if capacity is temporarily full: otherwise small
        frames could keep filling freed bytes and starve a larger waiting frame.
        """
        candidates = {job.key: job for job in candidates}
        if any(job.category not in CATEGORIES for job in candidates.values()):
            raise ValueError("unknown scheduling category")
        with self.lock:
            pending = queue.pending_keys()
            accepted = []
            waiting = {category: [] for category in CATEGORIES}
            for key, job in candidates.items():
                if key in pending:
                    outcome = queue.offer(job, require_pending=True)
                    if outcome == "replaced":
                        accepted.append(job)
                        self.counters["refreshed"] += 1
                        continue
                    if outcome != "not_pending":
                        self.counters["refresh_" + outcome] += 1
                        continue
                waiting[job.category].append((self.last_admitted.get(key, 0), key, job))
            for items in waiting.values():
                heapq.heapify(items)
            while any(waiting.values()):
                for offset in range(len(self.cycle)):
                    slot = (self.cursor + offset) % len(self.cycle)
                    category = self.cycle[slot]
                    if not waiting[category]:
                        continue
                    _, key, job = heapq.heappop(waiting[category])
                    outcome = queue.offer(job)
                    if outcome in ("accepted", "replaced"):
                        accepted.append(job)
                        if outcome == "accepted":
                            self.sequence += 1
                            self.last_admitted[key] = self.sequence
                            self.cursor = (slot + 1) % len(self.cycle)
                            self.counters["admitted_" + category] += 1
                        else:
                            self.counters["refreshed"] += 1
                    else:
                        self.counters["admission_" + outcome] += 1
                        if outcome == "capacity_rejected":
                            self.counters["deferred_capacity"] += 1 + sum(map(len, waiting.values()))
                            return accepted
                    break
            return accepted

    def retain_keys(self, active_keys):
        """Discard scheduling bookkeeping when configured sources are removed."""
        with self.lock:
            active = set(active_keys)
            self.last_admitted = {key: value for key, value in self.last_admitted.items()
                                  if key in active}

    def stats(self):
        with self.lock:
            return {**self.counters, "tracked_keys": len(self.last_admitted),
                    "weights": {category: self.cycle.count(category) for category in CATEGORIES}}


class CameraPriority:
    """Short-lived scheduling hints from confirmed active identities, never identity reuse."""
    def __init__(self, ttl_seconds=10, clock=time.monotonic):
        if ttl_seconds <= 0:
            raise ValueError("positive priority TTL required")
        self.ttl, self.clock = ttl_seconds, clock
        self.hints = {}
        self.revisions = {}
        self.lock = threading.Lock()

    def observe(self, camera_id, target_id, category, active, revision=None, *, activity_id=None):
        if not isinstance(activity_id, str) or not activity_id or category not in CATEGORIES or not target_id or (active and not camera_id):
            return
        with self.lock:
            version = tuple((revision or {}).get(k, 0) for k in ('person_revision', 'association_revision', 'gate_revision'))
            target_key = (activity_id, target_id)
            previous = self.revisions.get(target_key, (0, 0, 0))
            if any(a < b for a, b in zip(version, previous)):
                return
            self.revisions[target_key] = version
            # Exit invalidates this person's boosts on every camera.
            if not active:
                for key in list(self.hints):
                    if key[0] == activity_id and key[2] == target_id:
                        del self.hints[key]
            else:
                self.hints[(activity_id, camera_id, target_id)] = (category, self.clock() + self.ttl)

    def category(self, camera_id, *, activity_id=None):
        with self.lock:
            now = self.clock()
            self.hints = {k: v for k, v in self.hints.items() if v[1] > now}
            found = {v[0] for k, v in self.hints.items() if k[0] == activity_id and k[1] == camera_id}
            return next((category for category in CATEGORIES if category in found), "NORMAL")
