# Read-only capability probe

`scripts/capability_probe.py` prepares the Phase 0B evidence report but does not
prove any hardware requirement until it is run on the authorized reference Pi.
Local tests use recorded fixtures only.

The probe runs a fixed command allowlist without a shell and reads a fixed file
allowlist. Each command has a five-second deadline and bounded stdout/stderr;
each file read and report field is bounded. Missing tools and files are reported
as `unavailable`; empty results as `unknown`; and timeouts, non-zero exits, or
over-limit output as `error`. Common secret assignments, credential-bearing
URLs, and private-key blocks are redacted.

It never installs packages, invokes `sudo`, changes configuration, starts a
media stream, reads environment variables, writes device nodes, performs a
stress test, or discovers and opens arbitrary devices. Camera/audio commands
only request enumeration or metadata. Target-dependent capability decisions
remain `unknown` until the later measured validation gate.

After Pi access is explicitly authorized, run from an installed environment:

```console
python scripts/capability_probe.py
```

JSON is emitted to standard output. An optional report file must be an absolute
path ending in `.json`, have an existing parent directory, and not already
exist:

```console
python scripts/capability_probe.py --output /absolute/evidence/capabilities.json
```

The file is created exclusively with owner-only permissions and is never
overwritten. The JSON shape is compatible with
`schemas/capability-report-v1.schema.json`.
