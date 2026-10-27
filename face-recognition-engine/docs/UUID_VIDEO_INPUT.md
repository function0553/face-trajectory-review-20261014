# UUID video input on server 252

The existing backend API, OpenAPI, UUID/name fields and authentication remain
unchanged. Activity bindings, dates, enabled state and imported people determine
normal operation. No person names, walking routes or camera pin lists are used.

## Continuous video

`uuid_sources.continuous: true` keeps every eligible bound online camera acquired
continuously. `max_readers: 512` is an explicit safety ceiling; crossing it fails
visibly instead of silently rotating cameras. Small installations prefer main
RTSP. Above `continuous_main_capacity` (16 on 252), continuous substream decoding
preserves all-camera video coverage; inadequate face quality triggers a bounded
main-stream video acquisition, also persisted for inference. An unavailable
substream falls back to main. Simultaneously decoding all 373 main streams exceeded
the isolated 20 GiB test limit on 252, so that mode is not qualified.
`transport: hybrid` resolves private camera
addresses from the existing local registry and uses UUID media relay for NAT
cameras. Device port is not confused with RTSP port. Credentials stay out of argv,
logs and health responses. Only exact channel UUIDs and indices are used.

The primary source is RTSP video. Independent ISAPI alertStream subscriptions on
reachable cameras provide auxiliary native face captures where supported and
already enabled. A picture polling endpoint is not the primary input.

Video candidates are sampled uniformly at up to 2 fps. A generic motion gate
retains moving-scene candidates, a short post-motion interval and periodic idle
checks. Admitted JPEG candidates enter the durable P1 SQLite inbox before face
recognition; eight CPU inference workers claim each pending capture once. Pending
captures survive restart. Native face captures use the weighted urgent queue as
auxiliary confirmations; this does not change the person's business category.
Processing delay is separate from camera capture time.
No finite camera/recognizer system can promise zero missed faces: field of view,
face visibility, sampling, connectivity, quality and resource limits still apply.
The previous eight-camera rotation is not used for continuous operation.

Health includes per-camera frames, last-frame age, persisted candidates, inbox
refusals and acquisition errors, plus oldest pending capture age. An offline UUID,
failed decoder, full inbox or unresolved clock is not successful coverage.
Capacity and delay require verification on the real camera population.

## Camera time

Native face events retain their actual capture-time field. Ordinary video retains
its same-frame time evidence where available; an OSD fallback reads date and time
from the exact decoded image, never from the receiver clock. A camera clock query
is calibration, not a substitute for a frame's capture time. The existing
`/v1/clocks/profiles` endpoint stores measured offset/uncertainty, with evidence
from the camera time API and NTP-synchronized reference UTC. Profiles are attached
at acquisition so delayed recognition does not retimestamp older pictures.
Illegible or ambiguous OSD and unqualified clocks remain pending, not confirmed
identity/trajectory results. No quality or identity thresholds are relaxed.

## Collector stream correlation repair

The original collector callback cached preview sessions by global integer ID.
The repair keys by device UUID, session, channel and main/sub kind, then validates
the SDK callback device identity before binding a stream UUID. Unknown or
ambiguous callbacks are rejected. Only this internal callback class is replaced;
public controller methods, DTOs and other JAR entries remain byte-identical.
Ten collision/identity regression cases passed. Concurrent rechecks of the two
previously mixed UUIDs returned the correct 2043 and 1043 scenes after deployment.
This evidence is not a claim that every possible camera has passed a walk test.
