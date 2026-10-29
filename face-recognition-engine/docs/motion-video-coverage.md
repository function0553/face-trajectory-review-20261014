# Continuous motion screening and durable native video

The backend endpoints, activity and person lifecycle, feature space, recognition
thresholds, and result payloads are unchanged. This mode applies to every eligible
UUID source, including configured GB sources; it contains no person or camera
allowlist. A configured source is not proof that an offline device supplies video.

## Processing path

1. Keep the main stream open and copy encoded video into closed, uniquely named
   segments. Prefer main and fall back to sub after connection failures, retrying
   main later. Original bytes are retained without an overwriting ring.
2. Continuously decode a low resolution feed at 2 fps for camera-local background
   subtraction. Movement and a short tail select native video for face analysis.
   Missing or uncertain motion observations conservatively select analysis.
3. Decode moving segments at 2 fps, including non-keyframes. Per-process fair
   decode slots bound concurrency without discarding queued segments. Idle clips
   receive lightweight motion screening and periodic face checks.
4. Commit every selected native JPEG to durable ingress before retiring its raw
   segment. Retry the same frame on durable storage refusal. Preserve unfinished
   segments and immutable original activity/camera routes across restarts.
5. Detect and recognize with the existing model. If full-frame detection misses a
   face, bounded crops from that same native image retain small-face detail.
   No generated facial detail or coordinates borrowed from another stream are used.

Recording timestamps remain original server recording times, not delayed inference
times. A separate clock profile records conservative uncertainty for segment timing.

## Retention and capacity

Pending ingress, delivery images and raw video are not expired to recover space.
C++ retention also protects PENDING captures. A segment is retired after its selected
frames are durably committed, or after idle motion screening and required checks.
Completed records remain subject to their ordinary lifecycle.

When the backend revokes a camera binding or pauses its activity, queued originals
move to `HELD_ROUTE`. Their pixels and original identities remain unchanged, outside
the active inference capacity. Newly imported frames of that revoked route are held
as well. Re-enabling the same committed route returns held work to the active queue
as capacity permits. Registry connectivity alone does not revoke a route. This
prevents old revoked work from repeatedly consuming every inference turn.

Private deployment budgets cap ingress and video spool independently. Reaching a
budget or disk reserve produces explicit storage backpressure; it cannot guarantee
unlimited recording. Monitor backlog age, bytes, failed sources, motion frames,
committed native frames and processed inference separately. A recording camera is
not proof of a recognized identity or successful backend persistence.

## Verification boundary

The release has targeted Python/C++ checks and real live video-to-durable-ingress
verification. Those establish input and retention behavior, not whole-venue identity
recall. Real walking, multiple people, distant/back-facing subjects and backend
event persistence require field acceptance. 2 fps analysis can still miss brief
face visibility; retained moving video permits later reprocessing.
