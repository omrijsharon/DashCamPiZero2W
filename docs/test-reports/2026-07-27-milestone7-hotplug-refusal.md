# Milestone 7 dynamic-audio hotplug refusal — 2026-07-27

## Result

This is an expected, fail-closed **refusal**, not a passing hotplug test and
not Milestone 7 completion.  The direct plan to release and re-request the
live `splitmuxsink` `audio_%u` request pad is unsafe on the exact installed
stack, so the probe made no media or pipeline mutation.

The copied evidence is ignored by Git but retained locally at
`artifacts/pi-m7-20260727/dashcam-m7-hotplug-refusal-20260727.json`.

| Field | Value |
| --- | --- |
| Installed release | `0.1.0.dev0-011a148e085da278` |
| Hash-closed probe manifest SHA-256 | `1374c5d664749ed685e59309f7bb8f3284525174af4264a02752695ea140275c` |
| Evidence SHA-256 | `d71c329f032472de8b26001ef4ccae3673dfe714e5d59bab4cbac2093ad51236` |
| GStreamer | `1.26.2` |
| Production graph SHA-256 | `ee1140c2d513e0fdb8d11a46fffc0dab3985504a6c53def67edd2c5024027100` |
| `gst-inspect-1.0 splitmuxsink` SHA-256 | `463bf0a138fc0ecac7afbb40af07d98e5e26e7d1f38c0022af55cee8a7ad8bab` |
| Microphone match | `08bb:2902`, `USB_PnP_Sound_Device`, `platform-3f980000.usb-usb-0:1:1.0`, `hw:1,0,0` |

`dashcamd.service` was inactive/dead with main PID zero.  The probe did not
construct a pipeline or open the camera/encoder.  It performed zero request-pad,
service, network, or exFAT operations; it created no quarantine directory and
no media.  The result therefore cannot establish unplug survival, video-only
fallback, or restoration behavior.

## Why direct dynamic pad mutation is refused

The public API does expose `audio_%u`, request/release-pad operations, and
post-switch actions/notifications.  It does **not** provide the atomic
pre-switch transaction needed for an asynchronous-finalizing muxer:

- no public request-pad drain-completion contract after branch EOS;
- no public barrier proving old fragment closure and new mux readiness before
  track mutation;
- `fragment-opened` is already a post-switch notification; and
- asynchronous finalization leaves an unresolved old-context/new-context race.

Without that proof, a live release/re-request could lose queued AAC, attach an
audio track to the wrong fragment context, or require an unbounded video stall.
The direct implementation is rejected rather than enabled experimentally in
the production recorder.

## Provisional next architecture

The next target is an exact-Pi experiment using **immutable, complete recording
generations** and a two-slot IDR handoff.  A generation owns its complete mux
topology for its lifetime: it is never dynamically stripped or reattached.
At a proven IDR handoff, a prepared replacement generation may take over while
the existing generation finishes.  The camera and hardware encoder must remain
continuous across the handoff.

This remains provisional until the exact-Pi experiment proves same camera,
encoder, and clock identities; monotonic video counters; no bus warnings/errors;
decodable A/V, video-only, and restored-A/V clips; IDR starts; continuity; and
sub-100 ms restored A/V skew.  No remaining Milestone 7 task is checked by this
refusal.
