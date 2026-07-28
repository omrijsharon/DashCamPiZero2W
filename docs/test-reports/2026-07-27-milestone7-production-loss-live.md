# Milestone 7 production microphone-loss isolation — exact Pi

Date: 2026-07-27  
Reference boot ID: `601693e3-fa96-427e-906b-1621463a15cd`  
Installed release: `0.1.0.dev0-2439b9fc544ffffc`

## Scope and accepted result

This is the accepted normal-default qualification of the production one-way
microphone-loss path. It used `build_production_runtime` with ordinary
production defaults and no audio-loss override. The authoritative result is the
ignored local evidence
`artifacts/pi-m7-20260727/m7-production-loss-result-9.json`, SHA-256
`8c113bc4bda8fcb1d5017fa63ad510b75ba7984ef8f88c0dc8810cb5485c2ffb`.

The loss was deliberately induced by changing the already matched exact USB
device's sysfs `authorized` value from `1` to `0`, then restored from `0` to
`1` during harness cleanup. This is controlled software deauthorization, not
an owner-performed physical unplug. It proves the production logical-loss
isolation slice only; it does not satisfy the physical-unplug, absent-at-boot,
wrong-device, repeated-cycle, or audio-restoration gates.

The normal-default result has `passed=true` and passed with:

- ordered new clips with `audio_available` `[true, false]`;
- an IDR first video packet and independent Raspberry Pi hardware-H.264 decode
  for both clips;
- no encoder-input-PTS-gap drop increase (`9` before and `9` after);
- zero pipeline restarts, while retaining the camera and hardware encoder
  session;
- truthful runtime audio state `UNAVAILABLE` and reason
  `microphone_loss_isolated` after the handoff;
- exact microphone identity rematched after reauthorization;
- `dashcamd.service` inactive/dead before and after, with no service mutation;
- `throttled=0x0` before and after; and
- final root free space of `2,784,268,288` bytes.

The prior explicit-override qualification was an implementation iteration;
result 9 above is the authoritative normal-default acceptance evidence.

## Closed deployment and repeatability

The hash-closed production deployment was release
`0.1.0.dev0-2439b9fc544ffffc`.

- Bundle `SHA256SUMS` SHA-256:
  `ca356807da03743f2e8cd679affe091a573244d529e5573a1e69e59fe9002d27`
- Install-plan `manifest_sha256`:
  `1217e7e71198b35bad2ac991c90ac44b32d6fa035280b8592cacd44eea810101`
- Initial authoritative plan SHA-256:
  `64a06ee6c432b78b468b74156ced9a33c782b5c2f1393da78fd84859e2676ee1`
- Idempotent repeat-plan SHA-256:
  `1f4867c865935ebec0bbe68dcbb4653c3f6cab56a613fb77c4c0e6d27b762c07`
- Production-loss harness manifest SHA-256:
  `388437adae8b1f117fe05badb51c40ee36ea1bddb8a067de93d43e26df61114d`

The repeat apply made no package changes and left root free space unchanged.
The qualifying harness ran while the installed, enabled unit was inactive/dead;
it did not start or mutate the systemd service.

## Production behavior qualified

The production default constructs immutable complete A/V and video-only
recording generations around one parent camera/hardware-encoder session. On the
recognized microphone loss, it corroborates two stable `NOT_FOUND` discovery
observations for the exact configured identity, then makes an IDR-held routing
handoff to the prebuilt video-only generation. It does not remove or re-request
a live `splitmuxsink` audio pad.

The audio EOS path is serialized by a first-EOS-wins closure arbiter. Worker
dispatch, accounting, queues, successor routing, and cleanup are bounded. The
old generation is retained through parent `NULL`; cleanup is retryable if a
bounded `NULL` attempt cannot complete immediately. Fragment closures remain
chronologically finalized, and the successor's sticky/IDR state is checked
before it receives the handoff. These properties prevent the optional audio
branch from requiring a camera or media-pipeline restart in the accepted loss
path.

The observed runtime state retains the original matched identity as diagnostic
context but clears effective audio caps, reports
`UNAVAILABLE/microphone_loss_isolated`, and marks
`loss_isolated_without_video_restart=true`. The first clip's sidecar truthfully
reported audio; the successor clip's sidecar truthfully reported video-only.

## Boundaries and remaining work

The earlier exact-Pi direct dynamic `splitmuxsink` audio-pad mutation probe
remains refused: its public API does not supply a safe drain/closure/new-mux
barrier across asynchronous finalization. The production implementation avoids
that unsafe route through immutable generations rather than claiming the
refused operation works.

Production one-way loss isolation is enabled. Audio restoration/reconnect is
intentionally disabled: reauthorizing the device in this harness restored its
identity only during cleanup and did not return the active runtime to A/V.
Absent-at-boot video-only behavior, owner-assisted physical unplug, repeated
disconnect/reconnect, rejection of a different device, and restoration at a
safe boundary each remain separate unaccepted tasks. Milestone 7 and its exit
gate remain open.
