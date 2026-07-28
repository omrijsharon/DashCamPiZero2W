# Milestone 7 splitmux dynamic-audio refusal probe

This hash-closed harness records why dynamic microphone detach/restore must not
yet be enabled in production. It is deliberately non-mutating: it does not
construct a media pipeline, open the camera or encoder, request or release a
pad, create the proposed quarantine directory, write recording media, touch the
catalog, or change services, networking, storage, or AP state. Its only write
is one new atomically published JSON evidence file outside `/srv/dashcam`.

The exact target public API exposes `audio_%u` request pads and post-switch
split actions/notifications, but it does not expose the complete transaction
barrier needed to mutate an async-finalizing splitmux graph safely. In
particular, `release_request_pad` has no drain-completion contract,
fragment-opened is already after the switch, and no public callback proves both
old-context closure and new-mux readiness before track mutation.

A safe future experiment would require one strict application-thread
transaction:

1. Block the final audio source with a blocking/IDLE probe.
2. Send branch EOS, observe the drained EOS at the final audio pad, and drop it
   before it reaches splitmux.
3. Block encoded-video input.
4. Request an exact keyframe split and prove old fragment closure plus new mux
   readiness without relying on a post-switch notification.
5. Mutate the exact request pad outside split switching.
6. On restore, validate the first AAC access unit against current pipeline
   running time before unblocking video.

The public API cannot presently prove step 4 without private internals or an
unacceptable video stall. Therefore this harness always returns nonzero and
sets `passed=false`, `outcome=refused`. It never produces diagnostic MP4s; the
media/hash/decode/skew matrix is deferred until the barrier itself is
evidence-backed. This is a refusal result, not a test pass; it is not Milestone 7 completion.

The probe still binds its evidence to the exact installed release, production
configuration, stable microphone match, the combined parent graph and
separately owned replaceable audio-ingress graph hash, target
GStreamer version, splitmux pad templates, actions, properties, and bounded
`gst-inspect-1.0 splitmuxsink` hash. It also checks read-only that
`dashcamd.service` is inactive/dead with MainPID zero and that the caller's
proposed new quarantine directory is safe and absent. It never creates that
directory.

Review and verify the copied directory:

```sh
sha256sum -c SHA256SUMS
sha256sum SHA256SUMS
```

Run with the exact installed release interpreter:

```sh
PYTHON=/opt/dashcam/releases/<release>/venv/bin/python
HARNESS=/path/to/milestone7-hotplug/run.py
MANIFEST_SHA256=<sha256-of-SHA256SUMS>

sudo -u dashcam "$PYTHON" "$HARNESS" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  probe-refusal \
  --output-directory /srv/dashcam/quarantine/m7-hotplug-20260727a \
  --output /var/tmp/dashcam-m7-hotplug-refusal.json
```

The expected process exit code is `1`; inspect the exclusive JSON evidence and
retain its SHA-256 with the reviewed manifest.
