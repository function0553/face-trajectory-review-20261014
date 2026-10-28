# AegisVision admission adaptation

Reference: [swadhinbiswas/AegisVision](https://github.com/swadhinbiswas/AegisVision/tree/7ad92210bcfb491dc2090ef7fc73ab27cab8c03d),
commit `7ad92210bcfb491dc2090ef7fc73ab27cab8c03d`.
Its [LICENSE](https://github.com/swadhinbiswas/AegisVision/blob/7ad92210bcfb491dc2090ef7fc73ab27cab8c03d/LICENSE)
is CC0-1.0. We adapted the camera-local motion/max-gap design from
`backend/app/services/camera_manager.py` and token-bucket analysis budgeting from
`backend/app/services/analysis_scheduler.py`. No external service or additional
face model is installed by this adaptation.

## Integrated behavior

* Every continuous reader owns its own motion references and counters. Comparing
  both the previous frame and the last admitted frame catches gradual movement
  that stays below the change threshold between adjacent frames.
* The first frame, a stream switch, and a finite idle deadline admit a frame.
  A failed durable write resets admission so the next unchanged frame retries.
* Moving scenes keep the existing capture frequency and two-second motion tail.
  Idle scenes admit at the configured maximum interval. All bound streams stay
  open; this does not rotate cameras or use a person's identity to prioritize one.
* A single analysis token bucket is shared by inference workers. When a configured
  limit is reached they wait before claiming a frame from existing fair queues.
  Mandatory camera checks cannot bypass that limit and no extra executor queue
  accumulates images. The default rate is zero, preserving unrestricted speed.

Private runtime configuration, with no change to backend requests:

```json
{
  "uuid_sources": {
    "motion_admission": {
      "idle_seconds": 15,
      "thumbnail_size": [160, 90],
      "cumulative": true
    }
  },
  "scheduling": {
    "analysis_fps_limit": 0,
    "analysis_burst": 4
  }
}
```

The bounded rolling video cache from the previous release remains in place.
Camera metrics expose `motion_admission`; runtime health exposes `analysis_budget`.
Counters distinguish idle skips, cumulative motion and maximum-gap admissions.
Admission is not recognition success or a backend receipt. A maximum admission
interval is not an inference latency guarantee under overload, and low-motion
walk-through coverage still needs real footage/field verification.

The upstream global face-tracking state, identity embedding fallback and
unbounded executor submission are not imported. The existing ArcFace feature
space, enrollment, matching thresholds, original Base64 capture and public
endpoints remain unchanged.
