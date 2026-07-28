# Milestone 7 owner-assisted physical microphone-loss harness

This exact-Pi, hash-closed capability experiment asks the owner to physically
unplug the matched USB microphone while a real A/V recording is active. It is
not production code and does not prove reconnection or repeated hot-plug.
`safe_to_integrate_production` remains `false`.

The harness is transitively hash-closed over the accepted immutable-generation
harness. The sibling `milestone7-generation-handoff` directory must be staged
unchanged; its pinned manifest SHA-256 is
`ba780c442491ee0f278daaadd2df11d48c3f5ac7adce802d3634a536e07c1013`.
Before importing any sibling code, this harness independently reads the
sibling manifest and exact `README.md`/`run.py` members through bounded
`O_NOFOLLOW` regular-file descriptors, verifies all hashes, and executes the
already-verified `run.py` bytes.

Before `PLAYING`, it creates a complete A/V generation and a complete
video-only generation, including all splitmux and tee request pads. One parent
retains the exact IMX219, NV12, `v4l2h264enc`, H.264 parser, clock, base time,
and registered ALSA source object. After at least two A/V fragments it prints:

```text
OWNER_ACTION_REQUIRED: unplug the exact USB microphone now
```

The owner has at most `--loss-timeout-seconds` (30–60, default 60) to unplug
the microphone. The armed control loop polls the bounded repository
stable-identity discovery every 500 ms while independently draining the
GStreamer bus. It starts with the exact initially matched identity and
endpoint, then requires two consecutive `NOT_FOUND` observations separated by
at least 500 ms. One transient `NOT_FOUND` followed by the same exact match
does not trigger. `AMBIGUOUS`, `REFUSED`, or a changed matched identity or
endpoint refuses. Polling is capped at 128 ordered observations and never
runs in a media callback, so it cannot directly backpressure frame movement.
This confirmed stable-identity disappearance—not a GStreamer error—is the
physical-loss trigger.

At most four ordered `GstMessageError` records from the pre-registered exact
`alsasrc` object are optional corroboration. Zero errors is valid. Exact-source
errors may arrive before or after handoff through the bounded loss session and
parent shutdown; source path, domain/code/message, debug text, order, and
monotonic timing are retained. A foreign or fifth error still refuses. After
bus quiet, the discovery trigger window closes before topology changes, while
the corroboration window remains open until the parent has reached `NULL` and
its bus is quiet. A 32-fragment pre-loss cap and 40-file total validation cap
keep owner latency, media, hash, probe, and decode work consistent and
bounded. At the next encoded IDR, the harness switches to the already-complete
video-only generation without waiting for another audio buffer. The common
encoded-video blocker uses a bounded control handshake and removes itself from
its streaming callback; the control side must then prove that the tee sink is
neither blocked nor blocking. External probe removal is not treated as proof.
It gates and unlinks the old generation, and requires the exact old active
fragment to close. It then requires successor sticky events, first-IDR data,
one-frame normalized video continuity, at least 30 further frames inside the
existing three-second bound, and at least three video-only fragments.

The initial dynamic-bin `sync_state_with_parent()` call and its timing are
saved. After the successor admits its first IDR, the harness revalidates that
the parent is terminal `PLAYING`, the successor is unlocked, its exact tee
link remains present, and its valve is open. It snapshots the generation bin,
splitmuxsink, valve, queue, active `mp4mux`/`filesink` children, and queue
levels. An already terminal `PLAYING` graph needs no correction. A normal
`PAUSED`/pending-`PLAYING` ASYNC transition receives only a 200 ms grace
period. If and only if the complete successor then has the exact terminal
`PAUSED`/`VOID_PENDING` shape observed on the target, the harness performs one
explicit `successor.bin.set_state(PLAYING)` call. It records the full
`StateChangeReturn` and requires the bin, splitmuxsink, valve, queue, muxer,
and filesink all to reach terminal `PLAYING` within one second before the
existing 30-frame gate. No second correction is allowed. Parent drift, lower
or mismatched child state, unexpected pending state, a refused or unresolved
ASYNC return, or a queue reaching any configured bound refuses.

The failed exact ALSA source can make the parent pipeline's non-mutating state
query return `FAILURE` even while its actual state remains
`PLAYING`/`VOID_PENDING`. That parent-only query shape is recorded as
`parent_state_query_known_degraded_after_audio_source_failure` and is accepted
only after the exact stable-identity loss pair and recognized exact ALSA
disconnect errors, with the registered parent objects unchanged, the original
clock/base time present, and no unexpected bus error, warning, clock loss, or
QoS. It never relaxes the generation or splitmux-child state returns. Camera
raw, shared encoded-video, and successor counters must already be nonzero and
all advance inside the same convergence deadline; parent identity/state and
every successor child are revalidated after that progress. Parent lower state,
non-VOID pending state, non-exact loss, or stalled counters refuses.

The exact splitmux `audio_0` sink-pad probe is a synchronized first-EOS
arbiter. The first correctly ordered EOS may pass; every second, unexpected,
or mismatched EOS is recorded, dropped before splitmux, and latches failure.
If the arbiter is already terminal `NATURAL` before topology work, the harness
installs no audio IDLE blocker and never tries to send another audio event. If
the arbiter is still `OPEN`, the retired audio valve source receives an IDLE
blocking probe before gate/unlink and the harness sends one uniquely
identified `CUSTOM_DOWNSTREAM` serialization barrier through the retired
audio queue sink. A natural EOS ordered ahead of that barrier wins the natural
branch; an OPEN arbiter consumes the barrier before splitmux and permits
manual reservation. A required IDLE block is removed only after the terminal
decision, exact old unlink, exact old fragment closure, and both old EOS
observations. The exact output arbiter remains permanently installed and
drops/latches any late or duplicate EOS. The original two-second absolute
deadline covers the IDLE block, barrier dispatch/observation, queue proof, and
any reserved manual EOS plus the following topology/dispatch work; timeout or
identity drift refuses before audio EOS injection. If a failure occurs before
normal old-fragment closure, bounded cleanup removes any branch-local IDLE
block before requesting parent `NULL`; the permanent arbiter remains installed
through that failure path, and parent-NULL cleanup retries removal if needed.

If one natural upstream EOS reached the arbiter before the barrier, that path
is selected with no audio EOS dispatch. It is accepted only when the event
crossed the exact pad while the original generation was active, externally
linked, valve-open, and not retired; it followed every armed, recognized exact
registered-`alsasrc` disconnect error; and the sequence-identified consecutive
discovery pair established stable `NOT_FOUND`. Scheduler timing is recorded as
EOS before the first sample, between the pair, or after the stable pair but
before handoff/dispatch. The full discovery suffix after the first exact error
must contain only `NOT_FOUND` with no endpoint, and no rematch may follow EOS.
If both stable samples precede EOS, one final bounded read-only exact-device
check immediately before topology mutation must again return `NOT_FOUND` with
no endpoint. Its evidence is mandatory; `MATCHED`, `AMBIGUOUS`, `REFUSED`,
malformed, or failed final discovery refuses with no audio dispatch. No second
EOS may exist. If the barrier reaches an OPEN arbiter, the harness atomically
reserves one new EOS sequence number and sends that exact event only through
the retired audio queue sink. A true send return requires its exact arbiter
observation. A false return is delivery only when that same reserved sequence
number was observed exactly once; false without observation refuses. There is
no direct-to-`audio_0` fallback.

Two post-gate, post-barrier queue snapshots at least 50 ms apart must report
zero buffers, bytes, and time while the audio counter remains stable. Exact
fragment closure, exactly one forwarded audio EOS, zero duplicate refusals,
and unchanged post-closure pad identity are mandatory. An EOS before arming or
the recognized error, a foreign error, inactive/foreign pad identity, rematch,
duplicate EOS, nonempty/unstable queue, barrier/order failure, or closure
failure refuses without another audio attempt. No live request pad is released
or re-requested; serialization, timing, sequence-number, and identity evidence
is retained.
The failed `GstBaseSrc` child is not independently state-mutated inside the
live parent: unlinking the retired A/V generation isolates its
allow-not-linked audio tee, and the child remains under the sole parent state
transition until bounded parent `NULL`.
During that same bounded loss session, at most four failed
pipeline latency-recalculation attempts may be retained as an ordered
disconnect-side transient. Each records its source name/path, monotonic time,
and time relative to the armed owner window. These refusals never trigger the
handoff. A latency refusal before arming, after the loss session closes, or
beyond the fixed count refuses the run and remains present in failure
evidence.
After verified parent `NULL`, one bounded repository device-discovery attempt
must report `NOT_FOUND` and expose no capture endpoint while the owner leaves
the microphone disconnected. Its attempt count and status are saved as an
independent final absence check.

Finalization injects EOS only into the live video-only branch, so it does not
depend on the dead audio source producing EOS. Request pads are released only
after the whole parent is verified `NULL`. The final valve remains closed and
linked while its exact branch closure completes. The one-shot IDR probe proves
its callback-owned removal before a tracked, bounded parent-`NULL` worker
starts; success is impossible while an EOS or `NULL` worker remains alive. If
the parent-`NULL` worker itself
cannot finish within 17 seconds, the harness fails and the outer SSH/process
timeout is the final containment—no in-process thread cancellation is claimed.
The ordinary pre-shutdown bus-quiet drain runs before the common tee is
blocked, so it cannot fill the upstream record queue while recording is
stopped. While the next IDR is held, the harness installs an exact counter on
the common tee input before installing the terminal blocker, plus counters on
the stable parent encoded-video boundary and final video valve sink. It proves
that the parent-minus-routed baseline is exactly the held IDR, then closes the
valve. Through parent `NULL`, routed generation counters remain frozen. The
preinstalled tee probe retains only a 64-buffer ring plus monotonic total and
eviction counts. When the blocker reports the held IDR, the harness snapshots
its exact last-record index and sequence. The tee total must not change before
release, and the retained, consecutive terminal suffix must start at that
sequence. Pre-block ring eviction is valid and never consumes the nine-record
terminal suffix bound; only a true terminal-suffix overflow latches an error.
The
common-tee and closed-valve PTS sequences must match exactly and begin with the
held IDR, proving that all buffers which entered the tee were dropped by the
closed valve. The stable parent sequence may contain only one additional,
immediate PTS successor which never entered the tee. That parent-only frame is
accepted only when its upstream observation occurred after the tracked parent
`NULL` request and after exact final-fragment closure, but before `NULL`
completed. Therefore it cannot reach the valve, muxer, or file.
Python GI buffer probes do not expose the downstream source-pad flow return,
so this harness does not claim that the final upstream return was `FLUSHING`.
Instead it requires direct evidence that the parent-only PTS never entered the
common tee. If that stronger boundary attribution is absent, the run refuses.

This tail is not accepted by widening a fixed frame constant. The evidence
records block, release, pad-unblocked, parent-`NULL` request/completion, and
all three bounded buffer sequences. The `NULL` request must begin within one
30-fps frame period after the block callback is released. The IDR-held
fragment-closure interval remains separately governed by the existing
three-second blocker-release deadline; it is not counted as terminal media
flow. From the release request, both the closed-valve wall-time window and the
`NULL`-request control window are bounded to seven periods. The complete
tail's exact PTS span is independently bounded to seven periods, and its count
may not exceed the ceiling-derived PTS budget. PTS order and category-prefix
identity must be exact, with no non-monotonic or large gap. The exact closed
MP4's device, inode, size, and SHA-256 are also captured after the post-closure
bus drain and must remain identical after parent `NULL`. Baseline/index drift,
tee movement before release, more than one
parent-only frame, any parent-only frame observed before `NULL`, a tee/valve
mismatch, media mutation, stalled shutdown, counter drift, or unattributed
frame refuses with complete evidence retained.
Parent pipeline EOS remains forbidden throughout startup, active recording,
loss detection, handoff, successor proof, and ordinary fragment recording.
The final shutdown establishes a narrow phase record only after the post-loss
successor/state/fragment proofs pass. A parent EOS is optional and may be
accepted exactly once only after the harness's `final-video-only` EOS dispatch
was accepted and its exact active fragment was observed closed, while the
final valve is closed, the exact generation remains linked, and all post-loss
proofs still match. Source object/path, dispatch sequence/timing, closure
timing, and phase are retained. EOS before that closure, after the terminal
bus drain, from another source, without the accepted dispatch, or a second EOS
still latches `unexpected_pipeline_eos`. Every clip must have the exact
generation stream set, start with a real H.264 IDR, and decode with the Pi
hardware decoder. Unexpected errors, warnings, QoS, clock loss, restart,
throttle, foreign paths, stale output, timeouts, or cleanup failures refuse.

Success and failure evidence include bounded video-path snapshots at IDR
release, first successor data, continuous-flow proof or stall, and fragment
wait completion or stall. Each snapshot records raw-camera, parent-encoded,
old-generation, and successor counters together with pipeline/bin state,
tee-pad blocking/link identity, valve state, retained audio-IDLE count, opened
and closed fragment names, and bounded live file sizes. This distinguishes a
camera/encoder stop, a retained common-pad block, a routing/state error, and a
splitmux-only stall without extending any wait.

Stage this directory and the pinned sibling under one common parent, then run
with the exact installed release:

```sh
sha256sum -c SHA256SUMS
MANIFEST_SHA256=$(sha256sum SHA256SUMS | cut -d' ' -f1)
PYTHON=/opt/dashcam/releases/<release>/venv/bin/python

sudo -u dashcam "$PYTHON" run.py \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  run-experiment \
  --output-directory /srv/dashcam/quarantine/m7-physical-loss-20260727a \
  --output /var/lib/dashcam/m7-physical-loss-20260727a.json \
  --loss-timeout-seconds 60
```

The media target must be one fresh direct child of
`/srv/dashcam/quarantine`. The JSON target must be the matching fresh direct
child `/var/lib/dashcam/<same-run-name>.json`; the existing resolved
`/var/lib/dashcam` directory is required and the harness never creates a
result parent directory.
The harness never touches pending, clips, sidecars, catalogs, services, or
network state. `--help` only parses/imports hash-verified code and does not open
camera, microphone, or storage hardware.
