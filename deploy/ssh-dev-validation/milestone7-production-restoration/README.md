# Milestone 7 production audio-restoration qualification

This hash-closed exact-Pi harness has two commands through the real production
recorder. `qualify` retains the accepted two **logical sysfs** microphone
loss/recovery cycles. `qualify-physical` performs one bounded owner-assisted
physical unplug/replug cycle. Both use ordinary production construction with
the qualified loss-isolation and restoration defaults and supply no feature
override. They run while `dashcamd.service` is inactive/dead and never start,
stop, restart, or otherwise mutate that service.

The logical command's one privileged operation is a bounded `authorized=0`
then `authorized=1` write to the exact matched microphone's USB sysfs node.
The physical command performs zero authorization writes. The node is resolved
from the matched ALSA control node's bounded udev ancestry and rechecked
against the full configured stable identity.  Reauthorization is attempted in
`finally` before recorder cleanup, even after an ambiguous qualification
failure.  A naturally changed ALSA card index is accepted only if the same
stable identity rematches; this harness never selects by card index.

Each cycle must prove A/V -> video-only -> restored A/V through safe IDR
handoffs, continuous camera/encoder identities, truthful MP4+JSON media,
hardware H.264 decode, A/V stream-edge skew below 100 ms, unchanged drops,
zero pipeline restarts, unthrottled hardware, bounded three-slot topology,
and clean teardown.  Every new clip must be IDR-first.  The expected ordered
audio truth contains `true,false,true,false,true`.

Loss containment is immediate: the production proof binds the exact force-key
request to its downstream event and NAL-5 arrival, retains the final AAC edge,
and requires `0 <= forced-IDR edge skew < 100,000,000 ns`.  A request/downstream
seqnum mismatch is retained as evidence rather than silently normalized; only
the equality flag may be false.  The two logical cycles must expose distinct,
increasing force-key request counts.  The target capability basis is recorded
in `docs/test-reports/2026-07-28-milestone7-force-key-live.md`.

The public topology proof must explicitly say its request-pad counts were
measured and peer ownership was proven, with 4 video-tee (3 recorder slots
plus 1 permanent continuity sink), 1 audio-tee, 3 output-video, and 1
output-audio request pads.  The state-consistent route
proof must show only the active tee pads linked, every standby tee pad
unlinked, exact splitmux ownership, and no foreign peers.  Every phase requires
an explicitly `stable` published topology observation; a last-known snapshot
labeled as an in-progress or faulted handoff is never acceptance evidence.
The bounded `restoring_at_boundary` coordinator state and the driver's
`handoff_in_progress` topology are both waited through; neither can satisfy
restored-A/V acceptance.
one current audio ingress with its exact 10 descendants, zero stale
descendants, and the exact monotonic replacement count; the failed ingress
remains the one owned current ingress until bounded restoration replaces it.
Missing or false measurement flags are a refusal; inferred counts are not
accepted.

The logical command is not physical-unplug evidence. The physical command
prints and flushes `OWNER_ACTION_REQUIRED: UNPLUG_MICROPHONE` only after the
initial production A/V proof. It never writes a USB `authorized` attribute and
accepts unplug only when the exact old USB authorization path disappears. It
then requires the ordinary production loss handoff plus at least six seconds
of continuing video before printing
`OWNER_ACTION_REQUIRED: RECONNECT_MICROPHONE`. Reconnect is accepted only when
repository discovery rematches the complete stable identity and the newly
resolved USB authorization path exists with value `1`. Each owner-action wait
is bounded to 30–1,800 seconds (default 1,800) so an owner action coordinated
through a remote chat session cannot race a short media-control timeout.

The physical command requires one truthful A/V -> video-only -> restored-A/V
media witness, IDR-first hardware decode, stream-edge skew below 100 ms,
unchanged drops, zero pipeline/service restarts, unthrottled hardware, and
clean teardown. It proves neither absent-at-boot behavior nor rejection of a
different device. A naturally reassigned ALSA card index is recorded if it
actually occurs but is not claimed otherwise.

Run only on the exact installed release after its bundle has been verified:

```sh
MANIFEST_SHA256=$(sha256sum SHA256SUMS | cut -d' ' -f1)
PYTHON=/opt/dashcam/releases/<release>/venv/bin/python
sudo "$PYTHON" run.py --expected-manifest-sha256 "$MANIFEST_SHA256" qualify \
  --output /var/lib/dashcam/m7-production-restoration-result.json
```

For the single owner-assisted physical cycle:

```sh
sudo "$PYTHON" run.py --expected-manifest-sha256 "$MANIFEST_SHA256" \
  qualify-physical \
  --owner-action-timeout-seconds 1800 \
  --output /var/lib/dashcam/m7-production-physical-result.json
```

Do not unplug before the first exact marker or reconnect before the second.
Owner messages are synchronization prompts only; the result depends on
observed USB topology and recorder evidence, never on a human confirmation.

The result is an exclusive, bounded rootfs JSON file.  It is refused if it is
inside `/srv/dashcam`, already exists, has a missing/symlink/foreign-filesystem
parent, is unsafe, or any required proof is missing.  The parent must already
exist directly on the root filesystem; the harness never creates an arbitrary
evidence directory.  It records root free bytes and before/after throttle
facts, retaining the exact `throttled=0x0` gate.

The logical qualification requires the exact public slot progression
`av(1) -> video_a(2) -> av(1) -> video_b(3) -> av(1)` with activation IDs
`1..5`. The physical qualification requires one exact
`av(1) -> video_a(2) -> av(1)` progression. If the runtime exposes a public
cleanup snapshot it is captured;
otherwise successful `runtime.stop()` is the recorded cleanup proof.
`passed=true` is evidence only for the exact Pi/image/release.

The harness binds the runtime's public lifecycle observer before `start()` and
persists at most 16 recovery events, including kind, restart count, recovery
attempt, and safe exact detail.  Printable detail that looks like an absolute
path, URL, or secret assignment is not copied into evidence; its SHA-256 and
length bind the omitted public value without leaking it.  Overflow fails the
observer closed and is recorded.  The lifecycle journal is initialized before
qualification work and remains present in failure results, so a recovery cause
observed before a later refusal is not lost.
