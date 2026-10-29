# Whole-venue camera scope

The production photo-selection and recognition paths have no five-camera allow
list and no test-person-specific behavior. Five cameras in the retained walk
replay are historical validation samples, not the deployment scope.

P2 receives camera UUIDs and activity routes from backend control requests. P1
resolves every eligible bound UUID through the media directory. Continuous mode
opens all resolvable bound sources; camera rotation is disabled. The same main
preference, sub fallback, photo window, main-evidence precedence, quality checks
and identity thresholds apply to each managed continuous source.

The GPU and 252 operator configurations use:

```json
{
  "uuid_sources": {
    "continuous": true,
    "max_readers": 1024,
    "continuous_main_capacity": 1024,
    "preferred_stream": "main"
  }
}
```

`max_readers` is an explicit connection admission ceiling, not a proven decode,
recognition throughput or simultaneous full-frame-rate capacity guarantee.
Without a valid enabled activity, acquisition remains idle; configuration scope
does not imply current running readers. Directory resolution does not prove
successful decoding, identity recognition or backend event delivery.

Whole-venue acceptance must reconcile backend bound UUIDs, directory entries,
resolved input connections, fresh decoded frames, face/identity decisions and
event receipts. Missing UUIDs, disabled/offline channels and non-video devices
are reported separately. Camera names never substitute for exact UUID joins.

## Mixed Hikvision and GB28181 sources

The native Hikvision directory is not a complete inventory of the platform's
GB28181 inputs. A bound UUID absent from that directory is not sufficient
evidence that the camera is absent from the platform.

For the installed media platform, verified GB28181 entries use its `gblive`
application, with the exact camera UUID followed by `-0` for main and `-3` for
sub. These are platform-specific conventions, verified against the installed
stream enum and UUID parser; they must not be guessed for another platform.

The existing private `cameras_file` can supply these physical inputs alongside
the UUID resolver. Each entry uses `backend: ccic_uuid`, `continuous: true`, an
exact backend `camera_id`, matching `source_id`, and explicit relay `main_url`
and `sub_url`. The existing main preference, motion admission and main-photo
settings also apply. The catalog contains no activity routes. Backend activity
bindings still decide when a source runs and where observations belong.

Operator procedure:

1. Export only camera UUID, parent UUID, access protocol and status from the
   authoritative platform inventory. Match by exact IDs, never display names.
2. Verify the UUID's parent relation and protocol before constructing a URL.
   Passive registration IDs and installation-location text are not camera IPs.
3. Back up the private camera catalog, preserve other protocol entries, and
   publish the merged file atomically. Keep private URLs and inventory out of
   the source repository.
4. Read back both GPU and 252 catalogs. The existing binding refresh reads the
   catalog without changing public interfaces or restarting collection.
5. Verify fresh main frames, then sub fallback where main fails. Separately
   verify continuous acquisition and durable ingress. Decoder success alone
   is not identity recognition or a backend point receipt.

On 2026-10-29, 83 exact GB28181 source entries were added to both private
catalogs. A bounded live-input probe decoded 357 of 390 native Hikvision sources
and 57 of 83 GB28181 sources, all through main streams. The snapshot therefore
verified live decoding for 414 of 723 bound cameras; 59 known sources did not
decode within the probe limits and 250 bindings had no resolved source in that
snapshot. These are point-in-time observations, not a promise of sustained
concurrent throughput. Two GB28181 live sources also passed through the actual
acquisition worker and isolated durable ingress; temporary verification images
were removed. Neither check generated person observations or backend events.

The 33 failed Hikvision sources were registered passive ISUP inputs. Local relay
requests returned stream-not-found, preview handles remained unlinked, and the
stream receiver had no established links from their remote address at the
network snapshot. Registration and a successful preview request are therefore
not video-receipt proof. Do not replace those UUIDs with guessed direct camera
ports or relax physical device/channel binding to obtain a picture.

Historical recording replay points must remain labeled as replay. Live
acceptance requires a currently eligible backend activity, its enrolled
personnel, fresh decoded camera frames, automatic identity/point decisions and
verified backend acknowledgements. If the backend activity is disabled or its
window has expired, idle readers are expected; operators must not silently
extend backend-owned activities merely to make the status appear active.
