These fixtures support local image-builder tests only.

The official 549,086,704-byte archive and 2,675,965,952-byte raw image are not
checked into the repository. Unit tests construct bounded temporary regular
files and use locally derived manifests. Such fixtures prove parser, hashing,
geometry, path-safety, and planning logic only; they are not release-image or
hardware provisioning evidence.
