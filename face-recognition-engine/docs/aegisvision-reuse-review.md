# AegisVision reuse review

Reviewed upstream commit: `7ad92210bcfb491dc2090ef7fc73ab27cab8c03d`, CC0-1.0.
All paths below are under upstream `backend/app/`. Scope is the existing
backend-controlled camera/face/trajectory service, without replacing its API.

| Upstream module | Applicable part | Integration decision |
| --- | --- | --- |
| `services/camera_manager.py` | Motion gating, finite max-gap, activity-aware sampling | Adapted in the preceding admission release; keep each camera isolated. |
| `services/analysis_scheduler.py` | Shared analysis token budget | Adapted; wait in the existing fair durable queues, with no bypass or extra executor backlog. |
| `services/camera_ops_service.py` | Offline grace, cooldown, incident recovery | Adapted into read-only feed diagnostics. Fresh frame/status evidence is required; health is separate from identity recognition. |
| `services/camera_manager.py` | Bounded clip buffer, clip retention | Existing four-segment video ring and global cache targets cover this need. No second full-frame RAM ring is added for hundreds of cameras. |
| `services/face_engine.py` | Avoid repeated feature work for stable observations | Evaluate camera/session-scoped feature computation reuse. Do not reuse a previous person's identity label or copy the shared global track dictionary. |
| `services/face_backend.py` | Detector/recognizer adapters and singleton model lifetime | Existing SCRFD/ArcFace interfaces already provide this. The upstream small embedding model and legacy 128-D fallback are not interchangeable with the registered gallery. |
| `services/camera_registry.py` | Persistent per-camera state and lifecycle reconciliation | Keep backend UUID bindings as the authority. Reuse reconciliation ideas rather than adding a second editable camera inventory. |
| `services/ingestion_service.py` | Multiple-photo enrollment and idempotent imports | Existing platform import remains authoritative; directory scanning must not invent person IDs or bypass backend registration. |
| `services/alert_dispatcher.py`, `services/alert_service.py` | Event separation and retention patterns | Keep the existing immutable outbox and matching-event-ID backend acknowledgement. No unsolicited notification channel is introduced. |
| `services/spatial_memory.py` | Registered asset ROI background comparison | This is asset displacement monitoring, not cross-camera person tracking. Outside the present trajectory scope. |
| `services/health_sentinel.py`, `services/productivity_engine.py` | Fatigue/posture/productivity rules | Outside the present trajectory scope; these are not camera transport health checks. |
| Routes/admin/UI/copilot | Management and presentation | Existing backend owns these business flows; do not add parallel management APIs. |

## Feed diagnostic output

The existing authenticated P1 health endpoint gains `fleet_diagnostics`.
`LIVE` requires a recent decoded frame and a non-stale acquisition snapshot;
nonzero cumulative frame counts or a `STREAMING` label alone cannot prove it.
Missing bound UUIDs stay separate from decoder failures. No eligible activity
reports `IDLE_NO_ELIGIBLE_ACTIVITY_ROUTE`, not a camera outage.

Recent unavailable/recovered transitions are bounded to 64 entries with
cooldown. This projection neither sends alerts nor restarts cameras, changes
activity configuration, stores pictures or emits person-location events.
Existing backend requests, recognition thresholds and Base64 event payloads
remain unchanged. Actual field identity/route acceptance still requires real
camera evidence and backend delivery receipts.
