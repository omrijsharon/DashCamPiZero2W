# SSH-development application bundle

Build a closed transfer directory on the Windows development host:

```text
python deploy/ssh-dev-app/prepare-bundle.py REPOSITORY OUTPUT \
  --tzdata-wheel C:\path\to\tzdata-2026.3-py2.py3-none-any.whl
```

The tzdata wheel is an explicit offline input. The builder does not download it
and accepts only the exact 348,168-byte SHA-256 identity pinned by `uv.lock`.
By default the application wheel is built from the current working tree with
`uv build`; `--app-wheel` exists for a previously reviewed working-tree build.

After copying the resulting directory to the Pi, explicitly refresh the APT
indexes once with `sudo apt-get update`. Preserve that command's result, then
run
`sudo python3 install.py --dry-run > /tmp/dashcam-app-plan.json` first and
review/preserve its JSON. Only then run
`sudo python3 install.py --apply --approved-plan /tmp/dashcam-app-plan.json`.
Apply refuses package/version, APT simulation, manifest, storage, or service
drift from that exact dry run and never performs `apt-get update`. Never
refresh APT indexes between the approved dry run and apply; refresh first and
generate a new plan instead.
The installer is live-Pi-only. It verifies the hash-closed bundle, exact OS and
storage handoff, root headroom, and dormant service state. It installs and
enables `dashcam-storage-check.service`, `dashcam-network-fallback.service`,
and the reviewed `dashcamd.service` for a later boot. The recorder unit treats
storage preflight as a soft ordered dependency so `dashcamd` can report
`STORAGE_FAULT`, but its own fresh mount verification still blocks all camera
and media writes. The web and prepare-removal units remain absent. Apply does
not start, restart, or otherwise activate any service, so it cannot change the
current Wi-Fi mode or begin recording in the current boot. The newly enabled
recorder is eligible to start automatically on the next boot.
The release venv deliberately includes the declared system Python site packages
so the in-process PyGObject GStreamer backend can use the OS ABI. Before a new
release is finalized, the staging venv must import GI/Gst 1.0, initialize
GStreamer, and discover the `queue`, `libcamerasrc`, `v4l2h264enc`, `alsasrc`,
`audioconvert`, `audioresample`, `voaacenc`, and `aacparse` factories. The
installer first verifies the existing `dashcam` account and `video` group,
idempotently adds that reviewed account membership when needed, then runs this
factory smoke as `dashcam` with its initialized account groups. The installed
unit supplies the separate explicit `audio`, `video`, `render`, `dialout`, and
`dashcam-storage` supplementary-group contract. `dialout` is granted only to
the recorder so its receive-only GPS UART can open `/dev/serial0`; web/helper
services do not receive it. The smoke does not construct a pipeline or open
camera, audio, or UART hardware.
