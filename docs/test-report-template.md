# Test report template

Copy this template for each validated run. Mark unrun sections `not run` and state
why; do not infer target results from local fixtures.

## Run identity

- Report ID:
- Date/time (UTC):
- Operator:
- Git revision and working-tree state:
- Plan item(s):
- Result: pass / fail / blocked / partial

## Environment

- Test category: local unit / local integration / static quality / Pi capability /
  Pi functional-performance / Pi destructive / Windows interoperability
- Host OS and Python/tool versions:
- Pi model, OS image, architecture, kernel: (Pi-only)
- Camera, audio, GPS/UART, power hardware: (Pi-only)
- Recording media identity, filesystem, label, and free space: (Pi-only)
- Windows edition/build and reader: (Windows-only)

## Procedure and evidence

- Preconditions and authorization:
- Exact commands or test invocation:
- Fixtures/test data:
- Artifacts, logs, metrics, and checksums:
- Relevant report/file paths:

## Measurements (Pi when applicable)

| Metric | Result | Requirement/decision | Notes |
| --- | --- | --- | --- |
| Capture mode and frame rate |  | 1080p30 target |  |
| Encoder/device/caps |  | Hardware H.264 required |  |
| Bitrate and clip duration |  | 8 Mbit/s target; 60 s clips |  |
| Dropped frames |  | Record measured value |  |
| CPU, memory, temperature/throttling |  | Record measured value |  |
| Storage throughput/free space |  | Record measured value |  |
| A/V skew |  | Record measured value |  |
| GPS/time/overlay correctness |  | Record measured value |  |
| Preview latency/backpressure |  | Record measured value |  |
| Windows read/copy/open |  | Record measured value |  |

## Failure and recovery behavior

- Fault injected or observed:
- Recording impact:
- Optional subsystem impact:
- Storage/rootfs fallback prevented:
- Recovery/reconciliation result:

## Conclusion

- Acceptance criteria evaluated:
- Deviations, risk, and proposed remedy:
- Follow-up plan items:
- Reviewer/sign-off:
