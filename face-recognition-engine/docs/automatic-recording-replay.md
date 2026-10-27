# Automatic recognition from retained camera frames

`tools/replay_recorded_frames.py` reads original JPEG frame archives produced by
the camera collectors. It runs the normal detector, quality checks, recognizer,
camera OSD reader, clock qualification and `/v1/monitoring/observe` intake.
The camera timeline is queried through the existing API after replay.

Create a dedicated replay activity using the existing activity, binding and
person registration APIs. Copy the original enrollment templates and camera
UUID bindings. Keep the replay activity separate from any activity containing
human identity corrections. A non-platform replay activity does not add live
camera collection routes. Its output mode may be `UE_CAMERA_EVENTS` for the
existing camera timeline contract.

The tool requires an empty observation history and original enrollment templates
only. It does not select a person for an observation, call an attribution
correction endpoint, substitute receiver time, or change source/camera/frame IDs.
Observation IDs use the normal activity-scoped derivation for the dedicated
replay activity. Each report retains the original source activity, frame ID,
image hash, camera OSD time, score and actual P2 decision. Every quality-qualified
face is submitted; only `ACCEPTED` plus `MATCHED` counts toward automatic results.
Template learning is frozen during the replay to keep validation frames out of
the reference gallery. A repeated input frame must have identical original bytes.
Transient HTTP 502/503/504 and connection failures retry the same serialized
request up to three attempts. Observation IDs, clock evidence and policy contents
are retained; validation or conflict responses are never retried as new events.

```powershell
$env:FACE_TRAJECTORY_AUTHORIZATION = 'Bearer <server-side credential>'
python tools/replay_recorded_frames.py `
  --model-config <model-config> `
  --monitoring-config <private-monitoring-config> `
  --activity-id <dedicated-replay-activity> `
  --pack <original-frames.pack> `
  --report-dir <new-private-report-directory>
```

Provide credentials through the configured server environment, never through
command-line arguments or committed files. `--pack` may be repeated. Archives
can contain `FACEBT01` framing or the preserved length/header/JPEG records. Each
archive is read to a fixed prefix; an incomplete final record is excluded. This
does not restore periods that were never recorded or recover deleted images.

## Recall-oriented operating point

Merge `config/stream-recall-overlay.json` into the private monitoring configuration
to select main-stream 0.35 and sub-stream 0.15 similarity thresholds. These are
experimental recall-oriented thresholds, not calibrated identity probabilities.
They apply uniformly to all cameras and registered people, without route or
person exceptions. Lower thresholds increase the risk of incorrect identities;
an automatic multi-camera replay verifies the software chain, not a field-wide
accuracy guarantee. Scores below the threshold remain unconfirmed.

Use a new policy ID when changing policy contents. The existing stream-policy
selector includes stream kind and threshold in its policy ID, preserving old
immutable observation receipts. Preserve the prior configuration for rollback.

The replay result explicitly lists errors, camera coverage and whether at least
two distinct cameras occurred in one person's automatically matched timeline.
An algorithm timeline does not establish that a production backend received or
persisted a push event. No public endpoint or request/response field is added.
