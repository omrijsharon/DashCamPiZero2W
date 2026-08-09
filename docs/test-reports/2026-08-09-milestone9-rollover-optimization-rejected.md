# Milestone 9 rollover optimization evidence (2026-08-09)

## Scope and accepted state

This report records bounded diagnostics and two pre-matrix optimization
screens on the verified Pi at `192.168.68.112` (board serial
`00000000db28ffe4`). It does not replace the prespecified Section C1 paired
ten-clip matrix. Every run used the physical IMX219, connected USB microphone,
hardware H.264, verified exFAT volume, and production GPS path. GPS lock quality
varied, so cross-run differences are diagnostic rather than formal paired
equivalence.

The Pi was finally rolled back through the reviewed bundle to accepted release
`0.1.0.dev0-5f95dd806342ac9e`. `dashcamd` and AP fallback are inactive,
`NRestarts=0`, and `throttled=0x0`. After that identity was reverified, the two
non-current generated candidate release directories were removed; their
hash-closed bundles and local privacy-safe evidence remain.

## Attribution

An exact-5f95 probe attributed 1,065 active overlay callbacks. Mean total probe
time was 3.177125 ms/frame: 2.136417 ms extraction, 0.661929 ms rendering, and
0.378779 ms other probe work. A second extraction probe attributed mean costs
of 0.334820 ms to caps, 0.674494 ms to memory validation, 0.270419 ms to video
metadata, 0.492714 ms to frame construction, and 0.200802 ms to FD identity.
The privacy-safe result hashes are
`8d3f1fbbb57403a5c1f95640d03e54db87eea18fa81c4a2a84b8a1254d2564a4`
and `0ffb731cba84e184423227dc44d8e5d6483ef2afd72408e5484681027bbe404b`.

A Pi-local anonymous-mmap benchmark rejected one `process_vm_writev` scatter
as a renderer-row-copy replacement: its median was about 216,759 ns versus
97,990 ns for the current bounded slice writes, only 0.452x as fast.

Three accepted-release traces localized the recording loss to rollover. The
first complete 60-second clips had zero drops; their successors acquired
timestamp-gap drops during the post-boundary CPU burst. One trace observed
three exact one-frame 66.67 ms gaps at camera PTS 61.95 to 62.35 seconds. A later
trace observed seven missing frames at PTS 62.76 to 63.46 seconds. Method timing
showed GPS-window snapshotting at only about 0.2 ms and ordinary finalization
at about 0.95 seconds wall/163 ms worker CPU. A 500 ms per-thread trace showed
a Python durable worker rising to 61 to 76% CPU for about 1.5 seconds while the
ordinary media threads remained near their normal load. This identifies
GPS-sidecar UTC/name reconciliation and repeated large canonical JSON work as
the dominant extra rollover burst; it does not attribute all `task0` work
exclusively to the overlay.

## Serialized-CAPS candidate

Git `3ff2a03` retained full per-buffer buffer/memory/video-meta/FD validation
but cached only serialized stream CAPS. Its release was
`0.1.0.dev0-6701a87526859e59`; manifest, `SHA256SUMS`, and wheel hashes were
`57d25e3cf12e76463228e5d97807708845b7f3d667377675764a6d0829cea54f`,
`23b5f4e28b91fd0c02b5469906a4ed5e8d1928114be8e27b33bbabab89a56776`,
and `351451c5f80c1f93712bf4d2816d681b445252795899d23e692f76c3f547c104`.
Exact apply and idempotent reapply changed no packages and started no service.

Its one allowed screen measured mean CPU 92.3874%, p95 107.9664%, maximum
157.9809%, and 2,101 encoded frames. It had two
`encoder-input-pts-gap` drops, zero restarts/renderer failures/throttling, and
`task0` at 42.2577%. It therefore failed the absolute promotion screen of p95
at most 98% with zero drops. Candidate two was not run.

An immediate open-sky control after rollback to 5f95 measured mean 95.1073%,
p95 113.9837%, maximum 143.9802%, and 14 timestamp-gap drops. The candidate
was directionally better than this control, but neither result passes. The
control result/samples hashes are
`5a33a235c5408078e421c39de03f462216e29baa5771f545ebe407fb9830aa8f`
and `67176238a7561a72106f0afe5a8d0f933607fde7596c42b334b0e49e300c0925`.

## Canonical-sidecar memoization candidate

Git `80a7832` added logical-state-neutral memoization of deterministic canonical
bytes on the frozen, transitively immutable `ClipSidecar`. Every persisted
byte, canonical parse, hash, readback, catalog, and durability check remains in
place. The full local suite passed 1,995 tests with 10 unchanged host-specific
skips.

Release `0.1.0.dev0-909a35988e8b708e` had manifest, `SHA256SUMS`, and wheel
hashes `e79c4ff1074660d18a3288144a8e84695537a6be5ed85a36dfe114adc7bb75b6`,
`b31df21f5bd5959c1836b48c834262f55d885c9d60b7d2915bf5a1afa99df567`,
and `c9b7a5d970f83187e911406e4b3143035f6973f03b010740827415539d1b2608`.
Exact apply and idempotent reapply changed no packages and started no service.

Its sole screen improved again but still failed: mean CPU 92.0138%, p95
102.9872%, maximum 132.9827%, 2,101 encoded frames, and one exact one-frame
timestamp gap at PTS 61.48 seconds. `task0` was 42.4326%; restarts, renderer
failures, sync failures, and throttling were zero. GPS was stale for much of
the sample window and valid near its end, another reason not to treat the
cross-run deltas as a formal paired result. Result, samples, and exact gap
hashes are `81028c56f3ccb8ee1fa07fda7301cdad998b5deb466b80bb41d11b08002579ea`,
`5fac1b3ebde806f4b82f56cab212eea9307c9f1aa126a522b399c74fe6618e0c`,
and `c33784892f049f9798845dd24d1a3c6a3f18931c718504eaf578f3df07391202`.
Candidate two and the formal matrix were not run.

Rollback plan/apply hashes are
`e45f759f4191f86587002b1190f71f7af1be18a6f25ba2adb67d9b8d01073990`
and `ef55201f2ea86b3812ceab8930ed93bf400bd85fda562b1f5057e9bc68fadcf8`.
The ignored privacy-safe archives are
`artifacts/pi-m9-20260809/dashcam-m9-caps-evidence-3ff2a03.tgz` and
`artifacts/pi-m9-20260809/dashcam-m9-sidecar-evidence-80a7832.tgz`; the latter
hashes to `7febe6ac2c1922b71506df2b5e8c6f4ed2b7579a3285f7263846976af943ede1`.

## Decision

Both candidates remain rejected evidence in Git, while the installed Pi stays
on accepted 5f95. The next optimization must remove or substantially reduce
the redundant provisional-to-canonical reconciliation transaction when a
trusted anchor already exists, while preserving late-lock recovery, stable
UUIDs, collision refusal, and durable intent semantics. A bounded raw queue may
later improve scheduling resilience, but it cannot by itself close the CPU
gate. Milestone 9 resource acceptance and its exit gate remain open.
