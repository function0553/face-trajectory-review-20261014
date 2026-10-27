# Small camera OSD text recovery

The full-frame text detector can miss a legible timestamp in a 704 x 576 camera
frame, preventing an otherwise eligible face from entering the camera timeline.
If the original pass cannot parse one complete timestamp, the clock reader now
tries the original image's top and bottom edge strips at twice the OCR scale.
Colour and grayscale passes must independently produce the same complete date
and time, with every contributing text confidence at least 0.90. Disagreement
or incomplete text keeps the observation blocked.

The retry does not use a camera-specific crop, a person's identity, expected
digits, receipt time, or processing time. Evidence still hashes the original
decoded image and includes the OCR preprocessing and consensus count. Existing
measured clock profiles and qualification remain required. Cached clock regions
refer to coordinates in the original image.

Validation used real retained camera frames: the previously missed sub-stream
timestamp was recovered with two agreeing passes; repeated reads agreed; an
existing main-stream and sub-stream pair retained their original timestamps.
This verifies timestamp recovery, not face recognition accuracy or full route
coverage.

Human-confirmed identity corrections use the existing attribution API and an
independent evidence reference. They remain distinguishable from automatic
recognition and are not evidence of an unattended field-test pass. A stored
camera timeline does not establish receipt by an external backend.
