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
