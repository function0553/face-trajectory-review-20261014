# Bounded multi-camera runtime

The retained pre-optimization version is tagged separately. Endpoint paths,
backend request fields, camera UUID bindings and identity thresholds do not
change in this optimization.

## Reception-time mode

Set the private `camera_clock.time_basis` to `SERVER_RECEIVED_AT` to use each
frame's original `received_at_unix_ms`. This avoids making camera OSD OCR a
dependency of live identity recognition. It never uses inference completion
time or the current replay time to replace an older frame's reception time.
Existing frame time records remain immutable.

An NTP-synchronized server registers an explicit `SERVER_RECEIVED_AT` clock
profile. The existing internal clock-profile schema gains this additional
semantics value; ordinary backend calls require no new fields. Raw observations
identify reception time as `RECEIVED_AT`. The existing normalized UTC event
representation retains `time_basis=SERVER_RECEIVED_AT`,
`camera_capture_time_verified=false`, and `network_delay_bounded=false` in its
provenance. UTC normalization does not turn reception time into optical camera
capture time. Cross-camera network buffering can change physical arrival order.
This mode therefore supplies server reception order, not a measured physical
travel-time guarantee. Camera-time mode and historical records remain supported.

## Disposable cache policy

The private configuration may enable:

```json
{
  "cache_retention": {
    "enabled": true,
    "max_age_seconds": 1800,
    "ingress_max_mb": 16384,
    "main_archive_max_mb": 8192,
    "sweep_seconds": 10
  }
}
```

Ingress retention applies both a global budget and a fair acquisition-group
share. Claimed input files are protected. Evictions are recorded as
**unprocessed or partially imported**, including the receiver-file time window;
they are not recorded as successful recognition. Old pending SQLite images are
marked `QUARANTINED` with `CACHE_RETENTION_UNPROCESSED`; immutable IDs and hashes
remain so retries cannot recreate the same discarded input. Recognition
receipts, pending deliveries, registered people and template galleries are not
removed by this cache policy.

Main video uses four rolling 30-second segments per reader. Old sessions expire;
the global archive sweep additionally targets the configured budget while
protecting open segments. Open fragments and the sweep interval can temporarily
exceed that target. A full per-camera archive quota no longer stops the decoder.
Point-event face captures and deliberately retained replay evidence are separate
from this disposable cache.

Quota reconciliation is throttled instead of rescanning a full acquisition
directory on every refused frame. A full inference inbox waits for room before
reading and hashing another large batch. These controls bound storage and reduce
retry work; if input persistently exceeds inference throughput, older
unprocessed cache can still be evicted. Eviction counts and coverage gaps must
remain visible, and must not be described as lossless full-camera coverage.

## Validation scope

The pre-optimization version is retained as Git tag
`retained-20261027-auto5-ee418db`. An isolated replay using the original single
enrollment template processed 141 previously quality-qualified original frames
from seven cameras, including nonmatches and earlier transport/clock errors.
The reception-time version produced 118 automatic matched observations across
five cameras with zero replay errors and zero manual corrections. A separate
database/API audit verified the unchanged gallery and explicit server-reception
provenance on all 141 intake records. These are model matches at the configured
thresholds, not independently established ground-truth identities. This subset
replay does not establish full-fleet lossless coverage or a formal backend push
receipt.
