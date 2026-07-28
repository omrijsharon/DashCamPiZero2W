# Milestone 7 production audio-loss qualification

This hash-closed exact-Pi harness qualifies the real production recorder's
immutable audio-loss path through its ordinary production construction. It
calls `build_production_runtime` with only the required config and identity
paths and supplies no audio-loss feature override. It does not duplicate the
handoff implementation and does not alter production source or systemd unit
enablement. The result records that these ordinary defaults were exercised.

The harness requires the exact microphone to be connected and must run as
root because its one privileged operation is a bounded write to the exact
USB device's sysfs `authorized` attribute. It resolves that attribute from the
matched ALSA control node's bounded udev ancestry and rechecks USB VID, PID,
product, stable physical identity, and initial `authorized=1` before writing.
It never scans by USB enumeration order.

The production runtime first performs its normal storage preflight and records
real A/V media on verified `/srv/dashcam`. After measured A/V progress, the
harness writes `authorized=0`. The real backend must corroborate two stable
`NOT_FOUND` observations, hand off at an IDR to its immutable video-only
generation, retain the same camera/encoder session, report truthful
`UNAVAILABLE/microphone_loss_isolated`, continue encoded-frame progress, and
keep its pipeline restart count at zero.

In `finally`, the harness writes `authorized=1` back to the same exact sysfs
attribute before recorder cleanup, even when qualification fails. It then
requires the microphone's complete stable identity to rematch; a changed ALSA
index is allowed. It stops the real runtime cleanly and validates every new
production MP4+JSON pair: ordered A/V followed by video-only sidecars, exact
stream sets, actual H.264 NAL type 5 at every start, and independent Raspberry
Pi hardware-H.264 decode. Service state, restart count, runtime snapshots,
storage truth, identities, authorization transactions, hashes, and throttling
are retained in one exclusive rootfs JSON result.

Run only while `dashcamd.service` is exactly inactive/dead:

```sh
PYTHON=/opt/dashcam/releases/<release>/venv/bin/python
MANIFEST_SHA256=$(sha256sum SHA256SUMS | cut -d' ' -f1)

sudo "$PYTHON" run.py \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  qualify \
  --output /var/lib/dashcam/m7-production-loss-result.json
```

The run fails closed on foreign sysfs ancestry, identity drift, ambiguous or
refused discovery, failure to restore authorization, any production restart,
storage/media mismatch, incomplete cleanup, timeout, or throttling. A pass is
qualification evidence for this exact Pi/image. The accepted 2026-07-27 run
qualified the one-way loss handoff through ordinary production defaults;
microphone restoration remains disabled pending its separate repeated-cycle
qualification.
