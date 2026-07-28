# Milestone 7 production force-key A/B diagnostic

This is a hash-closed, exact-Pi diagnostic artifact. It constructs the real
production three-slot immutable-generation graph and audio ingress without
starting `dashcamd`. It sends request A while the original A/V route is intact,
deauthorizes only the exactly matched USB microphone, waits for the exact
terminal `audio_source` ALSA error, deliberately does **not** invoke production
loss isolation, and sends request B while the same route remains intact.

The result captures construction/start/request thread IDs; force-key events at
both directions of encoder and parser sink/source pads; NAL type 5 and
non-delta counts; flow at the encoder, parser, tee, continuity branch, and all
three generation slots; element/pad/peer state; clock/base-time identity; the
published fixed topology; and foreign request counts.

This artifact is diagnostic evidence only. Either B outcome is reportable; A
must succeed as the control. It does not prove physical unplug, restoration,
or that a production change is safe to integrate. The harness always attempts
to reauthorize the exact USB device, set the pipeline to NULL, delete only its
bounded uniquely named pending MP4 files, and prove the service stayed
inactive.

The v2 phase runs after B on one dedicated driver worker. It prewarms and links
the video-only successor only behind its closed valve while leaving the
original A/V route active. With exact low request count 1 it first invokes the
unchanged production `_arm_forced_idr_gate` for a bounded 500 ms expected
refusal. Passive probes must prove the response reaches `encoder.src` and
`parser.sink`, never `parser.src`, while NAL5 continues. It then repeats count
1 through a diagnostic encoder-edge observer, briefly holds the exact NAL5 at
`video_tee.sink`, releases it, unlinks the never-opened successor, and returns
that slot to standby. The phase refuses any active-route unlink, fragment
opening, loss isolation, audio retirement, or surviving probe/worker.

Run only from the installed release virtual environment, as root, with
`dashcamd.service` inactive and the exact microphone connected:

```sh
MANIFEST_SHA256="$(sha256sum SHA256SUMS | cut -d' ' -f1)"
sudo /opt/dashcam/current/venv/bin/python run.py \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  run-diagnostic \
  --output /tmp/m7-production-force-ab-result-1.json
```

The output must be a fresh direct file on rootfs, never on `/srv/dashcam`.
