# Generated stage assets

This directory is intentionally empty in source control except for this note.
A reviewed Linux build wrapper creates a fresh stage copy and populates it with:

- `READY`, written last;
- `repository/`, from a clean full Git commit;
- `wheelhouse/`, the offline production dependency closure from `uv.lock`;
- `storage/`, copied from `deploy/bootstrap/storage`;
- `network/`, copied from `deploy/bootstrap/network`;
- `build-metadata/source.json`;
- `build-metadata/package-inventory.json`;
- `build-metadata/uv.lock`;
- `transform-cmdline.py`, the checked wrapper calling the tested Python transform.

Assets must be inventories and hashed before pi-gen starts. Symlinks and device
nodes are refused. Generated assets are build products and are not committed.
