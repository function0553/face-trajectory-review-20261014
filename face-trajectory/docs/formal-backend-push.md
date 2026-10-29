# Formal backend result delivery

The algorithm POSTs each existing person-track event to the backend's existing
`/algorithm/person-track/events` receiver. The backend owns delivery to its
frontend. There is no reverse business callback from the backend to the
algorithm. The ordinary response to this POST is used to decide whether to
acknowledge the durable queue item: HTTP 200, integer `code=200`, and the matching
`event_id`. Failed or uncertain requests retain the original item for retry.

Activity synchronization, the monitoring switch and person enrollment are
backend-to-algorithm control requests. A healthy control entry does not prove
that algorithm-to-backend result delivery has been configured. Monitoring
continues to be controlled by the backend's existing switch endpoint.

Configure the daemon with its existing `-push-url` option and server-side
`FACE_TRAJECTORY_PUSH_AUTHORIZATION`. Do not place credentials in frontend
configuration, API bodies, source control or diagnostic output. The receiver
URL must be reachable from the actual GPU host, not another server's loopback.

Before attaching a formal receiver to a retained test runtime, inspect its
pending queues. The optional private environment variable
`FACE_TRAJECTORY_PUSH_EXCLUDED_ACTIVITIES` accepts a JSON array of exact activity
IDs. It excludes those activities from push selection while retaining their
pending records without sending or acknowledging them. It is startup operator
configuration, applied alongside the sender's existing per-activity retry
exclusions. By default it is empty, preserving previous behavior. It does not
change activity state, personnel, identity thresholds, HTTP paths, schemas or
receipt rules. Future isolated tests must also be excluded or run in a separate
runtime before generating test events.

On 2026-10-29, the retained GPU walk daemon had queued result generation enabled
but no formal receiver or callback credential. Formal delivery was configured
using the backend's existing receiver and credential, with historical replay
and walk activities excluded. No historical queued event was manually sent or
acknowledged. Capture runs independently of the result daemon and can retain
input while the result daemon is replaced.

Receiver authentication and rejection of malformed JSON are configuration
checks. Sender queue-isolation tests are regression checks. Neither is evidence
that a real person has generated a point accepted by the backend. Verify that
last step with real camera identity events and their matching persisted backend
receipts; do not fabricate a location event to claim field acceptance.
