# Configuration

Dashcam settings use a strict, versioned TOML document. The supported deployment
location is `/etc/dashcam/config.toml`; the complete version-1 reference file is
[`config/default.toml`](../config/default.toml).

## Loading and schema behavior

- `schema_version = 1` is required. Missing, non-integer, older unsupported, and
  future versions fail clearly; a future release can register ordered migrations
  from version N to N+1 in the dispatcher.
- Every root key, section, and section key is allow-listed. Unknown or missing
  keys and wrong TOML types are errors; booleans are not accepted as integers.
- Input is bounded to 64 KiB and must be UTF-8 TOML.
- Invalid startup configuration fails closed. It is never replaced by defaults.
- Local validation establishes only logical safety. Camera modes, hardware
  encoding, UART devices, audio devices, muxing, preview performance, and service
  behavior still require the authorized exact-Pi capability gate.

`config/default.toml` contains the product-contract defaults for video, audio,
GPS, time, overlay, preview, storage, network, and service settings. The typed
model also enforces these important invariants:

- Version 1 is hardware H.264 in MP4; software encoding cannot be enabled.
- Video and preview dimensions are even. Preview dimensions and frame rate cannot
  exceed the recording profile, and preview clients are limited to one or two.
- A keyframe interval cannot exceed one second at the configured frame rate.
- Canonical filenames remain UTC. Display timezone values must resolve through
  installed IANA timezone data; the target image must also supply the same
  declared `tzdata` dependency.
- Recording remains fixed to `/srv/dashcam`, exFAT label `DASHCAM`, with distinct
  mount verification required. There is no root-filesystem fallback.
- Low watermark is below high watermark; emergency free space is below minimum
  free space; service minimum restart backoff does not exceed its maximum.
- The AP address must be a usable private IPv4 CIDR interface.

Ranges are deliberately bounded, but acceptance of a value is not a hardware
support claim. Target capability checks may reject a logically valid profile.

## Updates and durability

Call `update_config_atomic(path, updates)` with a partial nested mapping. It loads
the existing file, merges only the requested values, validates the complete
candidate, writes a temporary file in the destination directory, applies mode
`0640`, flushes the file, and atomically replaces the destination. It then flushes
the parent directory where the host supports directory `fsync`.

Parsing, validation, temporary-write, flush, or replace failure before replacement
leaves the previous valid destination untouched and removes the temporary file.
The low-level `write_config_atomic` function applies the same validation and
persistence rules to a complete typed `DashcamConfig`. Its result reports whether
the parent-directory flush was supported by the host.

Only a privileged settings helper should own the deployed configuration and its
directory. Web input must remain subject to the same server-side allow-list and
validation; client-side validation is supplementary.

## Secrets

The TOML model has no AP passphrase, password, session key, token, or generic
extension field. Such keys are rejected, and serialization can emit only modeled
fields. Never pass secret values through configuration exceptions or logs.

Provision secrets separately as root-readable files:

```text
/etc/dashcam/secrets/ap-passphrase
/etc/dashcam/secrets/session-key
```

The secrets directory should be root-owned mode `0700`, and each secret file mode
`0600`. The AP passphrase must be unique per device or supplied during
installation; no universal default is allowed. Status/configuration APIs may
expose only a boolean such as `ap_passphrase_set`, computed from the secret store.
That indicator is runtime status and is intentionally not serialized in TOML.

Changing AP credentials, device bindings, or pipeline properties may require a
service restart or reboot; the eventual settings API must label that operational
effect. “Prepare SD card for removal” is an authenticated operation, never an
editable configuration key.
