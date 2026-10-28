# Original main-stream photos and short-burst selection

The backend entry, v4.1 fields, original-image Base64 and identity thresholds
remain unchanged. This change does not sharpen or generate facial details.

## Acquisition

Continuous main input groups up to three original frames across at most 1.5
seconds. A short tail flushes after 1.5 seconds, even if no new frame arrives.
The grouped durable record stays within the existing 12 MiB ingress bound;
oversized or irregular groups fall back to independent original captures.

When a camera falls back to sub-stream, an admitted motion candidate triggers a
separate main video connection immediately in the acquisition process, before
inference backlog. Sub capture continues. At most two supplementary readers run
per acquisition process, without a queued request pool; a busy camera retries
on later motion. Per-camera requests have a 15-second cooldown. Each reader has
a 10-second first-frame budget and collects up to three fresh main frames within
1.5 seconds. This cannot guarantee capturing a person who has already left.

The supplemental frames carry their own source UUID, frame ID and original
server receipt time. The trigger ID is diagnostic only. Main/sub arrival times
are not treated as verified simultaneous exposure; no sub-stream face box is
scaled onto main video, and no later photo is attached to an earlier event.

If the broad main-video pass uses I-frame-only decoding, motion also triggers
the same bounded full-decode burst to inspect the intervening frames. Continuous
main-stream preference and sub fallback remain unchanged. Full native-frame-rate
decode of all idle cameras is not enabled by this change.

The private setting `uuid_sources.main_photo_enabled` defaults to `true` and
can disable both grouping and proactive supplemental main capture. Existing
non-continuous input and native camera snapshots keep their original paths.

## Inference and selection

Every original main frame receives normal face detection, quality checking and
feature extraction. Faces are associated only within this short burst using
spatial overlap (IoU >= 0.15), feature cosine >= 0.55 and an unambiguous
one-to-one association. This association cutoff is not a gallery recognition
threshold. Different people are selected separately. Ambiguous or disjoint
observations remain independent; there is no global largest-face selection.

Native face size, sharpness, detection confidence and pose rank each short
track. Originals with native face size >=48px, sharpness >=40, yaw proxy <=0.6
and clipping <=8% rank ahead of tiny/blurred alternatives. This is a preference,
not an identity threshold or a hard rejection: an all-low-quality burst retains
its best actual observation to preserve the existing coverage policy. The
selection window is at most 1.5 seconds; it is not a wait-until-near guarantee.
One whole original observation wins, including its own embedding,
unaltered face crop and timestamp. No template is updated by this selector.
The existing P2 gallery still performs identity assignment. All selected
deliveries and the capture result commit in one transaction; retry is
idempotent. Input hashes and selected original frame IDs remain in the report.
P2 retains its contractual first-valid-observation 5-second deduplication rule;
a later HD observation does not rewrite an already committed earlier photo.

## Acceptance boundary

Tests cover corruption, cross-camera rejection, time windows, multiple people,
ambiguity, atomic delivery, consistent original photo/time and non-blocking sub
capture. Retained footage evaluation is separate from these implementation
tests. Neither a healthy service nor historical replay proves new field recall
or formal backend push receipt. If no valid enabled activity is routed,
acquisition remains idle. Very small or blurred main-stream originals remain
limited by the camera's actual image detail.
