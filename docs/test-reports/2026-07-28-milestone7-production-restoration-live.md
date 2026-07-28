# Milestone 7 production microphone restoration qualification — exact Pi

Date: 2026-07-28  
Reference Pi: Raspberry Pi Zero 2 W, Raspberry Pi OS Lite 32-bit Trixie, IMX219,
hardware H.264 (`/dev/video11`), and the matched USB microphone `08bb:2902` on
its recorded physical USB path.  
Installed release: `0.1.0.dev0-ce028ba96d40fb9d`  
Hash-closed bundle `SHA256SUMS` SHA-256:
`933cb9f72ce5e89eb65bf98a93698daa200dda8b05db45be75fd2f4d24769b05`

## Result

The final two-cycle logical microphone-loss/restoration qualification passed.
The authoritative retained result is
`artifacts/pi-m7-20260727/m7-production-restoration-result-72.json`, SHA-256:

```text
E3F16644C6AAC8BF847BBD12FEEB931FA818B0320A1FA6EB1F9AED486FE2910B
```

The program created five truthful finalized pairs, in this order:

| Sequence | Audio sidecar state | IDR first | Independently hardware-decoded | A/V stream-edge skew |
| ---: | --- | --- | --- | ---: |
| 345 | available | yes | yes | 76.001 ms |
| 346 | unavailable | yes | yes | n/a (video-only) |
| 347 | available | yes | yes | 71.958 ms |
| 348 | unavailable | yes | yes | n/a (video-only) |
| 349 | available | yes | yes | 84.291 ms |

Thus the observed sidecar audio states were `[true, false, true, false, true]`.
All A/V skews remained below the unchanged strict 100 ms acceptance bound.
Both logical loss handoffs held an exact forced-IDR/audio edge below that bound
(33.328 ms and 20.387 ms), retained the same camera and hardware encoder, and
closed the retired fragment. Both restoration handoffs used the fixed
three-slot graph, recycled the retired slot only after closure, retained
constant request-pad counts, began the successor with an IDR, and observed
audio and video buffers on the restored successor.

The matched microphone was re-authorized in the harness's `finally` path and
finished authorized (`1`). `dashcamd.service` was inactive/dead before and
after the run, with `NRestarts=0`; throttling was `throttled=0x0` before and
after. Root free space after the final deployment/qualification check was
2,699,141,120 bytes.

## Production fix qualified

The recorder keeps three complete immutable output generations rather than
mutating `splitmuxsink` request pads in place. At loss or restoration, it
holds a correlated forced IDR at the bounded audio edge, retires the old
generation, starts the prebuilt successor, and recycles the retired slot only
after its closure is proven. Camera and encoder ownership remain continuous.

The original whole-generation EOS approach exposed a source-EOS race: a
natural source EOS could arrive after the old open-state check but before the
whole-generation EOS was accepted. The production `_AudioEosArbiter` now
atomically reserves the generated EOS sequence number before dispatch. While
reserved, it accepts only that exact EOS; one late foreign/source EOS is
dropped and recorded, while a second foreign EOS or every other ambiguous
shape fails closed. The caller requires the exact resulting arbiter snapshot
before treating the A/V generation as retired.

The fix also accounts for an exact, benign timing shape where the same
generation EOS has already closed the retired fragment before video-closure
bookkeeping runs. The recorder accepts this only if it can prove the reserved
generation EOS, exact inactive-generation ownership, no remaining open
fragment, one recorded final location, and no current location owner. It then
records a completed reuse of that EOS rather than sending another video EOS.
Any open/closed hybrid or ownership drift remains a refusal.

The final harness stderr records the first generation's exact reservation and
reuse (`av_generation_retirement_eos_observed` followed by
`retiring_generation_eos_reused_for_video_closure`) and the bounded recycle of
each retired slot. The second cycle also completed through the ordinary
natural-boundary path, proving that the reservation does not replace the
existing safe natural-EOS path.

Focused unit coverage includes one late-source EOS followed by the reserved
generation EOS, refusal for non-pristine/duplicate shapes, and both legal
open-fragment and already-closed-fragment forms of exact generation-EOS reuse.

## Refused qualification iterations retained

The acceptance bounds were not weakened to obtain this pass.

- Result 68 (`m7-production-restoration-result-68.json`, SHA-256
  `B68DAB769CED467CFA7CD9BDC1F42CB345C193F3780B7794310C736272DCF9BE`)
  structurally completed two cycles and all five media pairs, but was refused
  because an A/V stream-edge skew reached 124.666 ms.
- Result 71 (`m7-production-restoration-result-71.json`, SHA-256
  `8B665C6B31BD71F512875F4C3C12F1B4E4E6FC489E2659CFAD9938165F41D20D`)
  likewise completed the structural handoffs but was refused at 100.666 ms.

Those results, their stderr, and result 72 are retained in
`artifacts/pi-m7-20260727/`. They demonstrate that the literal 100 ms media
gate continued to reject marginal samples; only result 72 satisfies it.

## Hash-closed deployment evidence

The exact release was installed through reviewed bundle 41. Its authoritative
dry-run, apply, idempotent dry-run, and idempotent apply records are:

- `artifacts/pi-m7-20260727/dashcam-app-plan-41.json`
- `artifacts/pi-m7-20260727/dashcam-app-apply-41.json`
- `artifacts/pi-m7-20260727/dashcam-app-plan-41-idempotent.json`
- `artifacts/pi-m7-20260727/dashcam-app-apply-41-idempotent.json`

They record release `0.1.0.dev0-ce028ba96d40fb9d`, zero APT download,
installation, and peak bytes, no service start request, and an idempotent
second apply with the same 2,699,157,504-byte root-free result. The live
qualification's subsequent 2,699,141,120-byte observation is reported above;
the small difference is run evidence, not a storage fallback.

## Scope and remaining limits

This is a controlled logical sysfs authorization/deauthorization test of the
already matched microphone. It does **not** prove any of the following, which
remain Milestone 7 work:

- owner-assisted physical USB unplug survival;
- microphone absence at boot;
- natural reassignment of the ALSA card index; or
- rejection of a wrong USB audio device.

It does not waive the product's bounded-retry, identity, storage, or physical
power-loss requirements. In particular, it is not evidence that arbitrary
physical removal has the same ALSA/GStreamer error timing as controlled sysfs
deauthorization.

## Validation performed

- `uv run --frozen pytest tests/unit/test_recorder_gstreamer.py
  tests/unit/test_recorder_runtime.py
  tests/unit/test_pi_m7_production_restoration_harness.py -q`:
  `367 passed`.
- `uv run --frozen ruff check src tests`: passed.
- `uv run --frozen mypy src tests/unit/test_recorder_gstreamer.py
  tests/unit/test_recorder_runtime.py`: passed with no issues in 71 source
  files.
- The complete local suite was attempted after these checks but did not
  complete because the existing Windows/WSL shell-test launcher stalled and
  the command hit its 60-second bound. This does not replace or weaken the
  focused green checks above.
- Parsed the three retained machine-readable restoration results and their
  result-71/result-72 stderr traces.
- Recomputed SHA-256 for results 68, 71, and 72; result 72 matches the hash
  above and declares `passed: true`.
- Read the four bundle-41 installer records and verified their release ID,
  no-start service contract, zero APT simulation growth, and idempotent apply
  shape.
- Reviewed the final recorder implementation and its focused arbiter and
  generation-closure unit coverage. No source, configuration, service, media,
  storage, or plan artifact was changed by this report.
