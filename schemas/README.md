# Machine-readable report formats

This directory defines versioned JSON Schema Draft 2020-12 formats:

- `test-result-v1.schema.json` records a bounded test-suite run, its cases, measurements, warnings, environment, and relative evidence-artifact paths.
- `capability-report-v1.schema.json` records a bounded capability probe. It keeps raw observations separate from evaluated decisions and explicitly supports `not_probed` and `unknown` states.
- `clip-sidecar-v1.schema.json` records the portable, bounded metadata representation paired with one clip. Cross-field time and filename invariants are additionally enforced by the typed runtime model.

Neither schema, a report that validates against it, fixtures, or local test output proves Raspberry Pi capability, performance, media behavior, power-loss behavior, or Windows interoperability. Such claims require the target measurements and retained artifacts specified by the project plan.

## Versioning

The report formats require `schema_version: 1`, a UTC `generated_at_utc` timestamp, and a producer name, version, and build ID. Every version 1 document must validate against its corresponding `*-v1.schema.json` file. A breaking change creates a new, immutable major-versioned schema file and increments `schema_version`; it does not silently reinterpret version 1 documents. Additive optional fields may be introduced only in a new schema version because the current formats intentionally use `additionalProperties: false`.

## Boundedness and paths

All strings and arrays have explicit maximum sizes. Reports use relative, forward-slash evidence paths only; absolute paths, backslashes, and upward traversal are rejected. Producers should retain only the evidence that the report references and should avoid embedding large logs or media in JSON.

## Privacy

Do not put credentials, tokens, AP passwords, private keys, unrestricted environment-variable dumps, personal identifiers, or private network details in either report or an evidence artifact. Report only the minimum non-secret facts needed to diagnose a result. The `environment` and raw-observation fields are deliberately closed objects/scalars to discourage accidental secret capture.

## Capability reports

For every capability decision, include the observation IDs and artifact paths that justify it. If a section or capability was not inspected, use `not_probed`; use `unknown` when it was inspected but could not be determined. At the current local-only gate, reports must not represent fixtures or laptop data as Pi findings.
