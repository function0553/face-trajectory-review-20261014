# Durable per-camera best shots before first delivery

This release keeps backend URLs, observation/event fields, matching thresholds
and P2's first-valid-observation five-second deduplication unchanged.

## Observation windows and lifecycle

Managed continuous video captures remain in the existing durable inbox for up
to six seconds of original server receipt time. The earliest pending capture
for that activity/source/route is processed first, even when the fair scheduler
picked a newer seed. Each window has at most 24 capture records and 64 MiB
of encoded originals. A capture may contain an existing three-frame main burst.
Long passages continue in bounded windows; this is not an unlimited wait until
a person leaves. Private `scheduling.photo_selection_window_seconds` supports
0 (previous behavior) through 12 seconds; default 6.

The wait uses deferred camera scopes, not sleeping inference workers. Camera
acquisition continues independently. No new unbounded image cache is created.
Only one worker reserves an activity/source at a time. Restart leaves pending
originals intact. All selected deliveries and all window capture results commit
in one transaction. Failure never switches to a forced low-res identity guess.

## Per-face selection and feature work

All input frames still receive detection and quality checks. A small aligned
appearance comparison can select one original before expensive embedding work
only when scope, camera, stream, dimensions, short time gaps, high box overlap
and near-identical appearance agree unambiguously. The anchor expires after
two seconds. New appearance, a new face, ambiguity, another camera/activity,
main/sub transitions and refresh expiry require independent embeddings.

No embedding is copied onto another photo, and no registered identity is
assigned by the appearance tracker. Winners carry their own crop, feature and
timestamp. Final short-track selection uses independent feature agreement and
normalized boxes, preserving every ambiguous independent observation.

## Main evidence takes precedence

A usable main face in the same committed scope, within 2.5 seconds of receipt
time and at least 0.5 normalized box overlap, requires that corresponding low
track to defer to main evidence. Clear main faces, or substantially larger main
faces with comparable sharpness, qualify for this preference. This requirement
also propagates along the associated low-resolution track. It does NOT require
feature similarity between the main and low hypotheses: disagreement is exactly
the case in which the low-resolution identity guess must not win.

Every main face still passes normal quality and P2 gallery matching. A main
score below its identity threshold cannot borrow the low-resolution candidate's
person ID. A different main identity does not produce the low candidate's event.
Absent, unusable, unrelated, stale or another-camera main observations are not
negative evidence against an unrelated low-res face. Low-only fallback remains
subject to the existing minimum score and person-margin checks.

This uses original server receipt times as requested; it is not proof of
simultaneous camera exposure. Crossings or uncertain correspondence may reduce
recall. Low thresholds can still cause false matches; no zero-error claim is
made. Image quality is not identity confidence.

## Verification

Tests cover contradictory main features, low-only fallback, multiple people,
track refresh, crossings, camera/activity isolation, restart, durable ordering,
transaction rollback and concurrent worker reservations. Real retained-frame
evaluations and live acquisition checks are recorded separately from synthetic
tests. No replay is described as a new real passage or formal backend receipt.
