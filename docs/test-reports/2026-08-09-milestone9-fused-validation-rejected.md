# Milestone 9 fused-validation candidate rejection (2026-08-09)

## Identity and scope

The verified Pi moved to `192.168.68.112`. Its Wi-Fi MAC
`2c:cf:67:98:4c:49`, board serial `00000000db28ffe4`, and pinned ED25519 host
key fingerprint `SHA256:iNlz0NDhUbn+GfH5Nbb5v9nImSX+zFujVDSqvcHSMOg` remain
unchanged. The prior `.107` address was explicitly refused: it presented
foreign MAC `88:a2:9e:84:b3:a5` and a different SSH key. It is not this Pi.

This report records a bounded same-boot screening experiment before the
Section C1 paired ten-clip matrix. It used the active recorder path with the
same camera, active GPS input, USB audio, hardware H.264, verified exFAT,
power source, and v2 privacy-safe resource probe. It does not satisfy or
replace the prespecified Section C1 matrix.

## Accepted same-boot baseline

The installed accepted baseline was
`0.1.0.dev0-5f95dd806342ac9e`. Its 75 one-Hz samples after the 30-second
warm-up measured mean recorder CPU **85.4950452289784%**, p95
**92.98829937528733%**, and maximum **102.98586220379906%**. It advanced
2,102 encoded frames with zero drops, pipeline/service restarts, renderer
failures, renderer sync failures, or throttling.

The v2 per-thread window attributed `task0` **43.471124711243085%**. Renderer
latency totalled 1,144,046,845 ns over 2,102 rendered frames, about
0.544 ms/frame. The privacy-safe result and samples SHA-256 values are,
respectively,
`549a410f9297c89a06e5836674152bbac61006230f5ffa07091a94ca0d507fd1` and
`9c18c142dfc23c7c51451be577e8ec6ca83febb6b96f48c0ff53ba661e611d3c`.

## Candidate and result

Git commit `a9871cf` fused native overlay frame validation. The resulting
hash-closed candidate was `0.1.0.dev0-850b8609266e3aaf`, with manifest
SHA-256 `251294bd4d9bdb295a16a46a4f23df25cc17b25848462c86c03c73e999a6860d`;
its `SHA256SUMS` and wheel hashes are
`c6b6b36cb66ca79453e178d532d68862a5da542a11b2b2b0366963667fc365f8` and
`b90a707b683ed81fda0545654f88208503400b8762b357b1212fcba2f9f69e18`,
respectively.
Exact-version apply and the independent idempotent plan/apply made zero
package changes and started no service.

Its first and only allowed 75-sample run measured mean CPU
**84.9216014576128%**, p95 **93.98716483080271%**, and maximum
**98.98752143505672%**. It advanced 2,104 encoded frames. There were zero
pipeline/service restarts, renderer failures, renderer sync failures, and
throttle observations. `task0` was **42.36544435595774%**. Renderer latency
was 1,031,574,334 ns over 2,103 rendered frames, about **0.491 ms/frame**.

The candidate nevertheless fails screening for two independent reasons:

- Its p95 was 93.98716483080271%, **0.99886545551538 percentage points above**
  the same-boot baseline: a regression rather than the required 2 percentage
  point screening improvement.
- The first post-warm-up observation already reported one
  `encoder-input-pts-gap` drop. The event occurred before the sampled window,
  but it still prevents the required zero-drop screening claim.

Candidate result SHA-256 is
`0a80cd2dc03da87e608e70308772e2d36f6ed9e08710bb0a38adaa5148534b0d`; samples
SHA-256 is
`27ee42b04099392c46a3bb4bffca3d34c99b65c953ac8df663f1171589433fb8`.
Candidate run 2 and the formal paired ten-clip matrix were intentionally not
run.

## Rollback and retained evidence

Rollback used the verified `5f95` bundle (manifest
`619fe30e8123e0ceaec55269de0a6faf6ec88ccb4859a98bbef2d87776dbb655`,
`SHA256SUMS`
`a42983edbf0c85acc44609c7961fe48ab9847ff03d339ab05e8c40dbed1c24c8`)
through its exact plan/apply. The resulting accepted state is
`0.1.0.dev0-5f95dd806342ac9e`, config SHA-256
`1276363286475bccf85e70332ec893846e3fe3572e8184991843400ac4d6c4b8`,
enabled/inactive `dashcamd` with zero restarts, and 2,685,423,616 root bytes
free. The rollback apply SHA-256 is
`e4b6b8bb7b50fb835f1963b2ad00b186e3dea96b4e1096e14d81e4435d13a3f4`.

The candidate installed directory was removed recoverably only after the
rollback proved the accepted release. Its bundle, Git commit, and local
privacy-safe evidence were retained. The pre-candidate exFAT archive is
16,875,520 bytes with SHA-256
`f914265bd4569ecd9b2375ba1210df81bbb2520e61f98b605f475fd73f39993e`;
the local ignored evidence root
is `artifacts/pi-m9-20260809/dashcam-m9-fused-evidence-a9871cf/`. Its archive
`artifacts/pi-m9-20260809/m9-fused-evidence-a9871cf.tgz` has SHA-256
`078b21c1ba4fd407547bcb8169acf3211c68f3d31bdf6c3a49410211dc3baf2f`.

The candidate source remains in Git as rejected evidence. Do not revert the
source merely to mirror the accepted installed release; do not treat the
candidate as installed. Milestone 9, its resource task, and its exit gate
remain unchecked. No product specification change is implied.
