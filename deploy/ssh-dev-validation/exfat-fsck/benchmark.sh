#!/bin/bash
# Bounded performance probe for the already-verified live recording mount.
set -euo pipefail

readonly ROOT=/srv/dashcam
readonly UUID=7EED-3EA7
readonly CID=fe34325344000000200000031a0192d1
readonly RESERVE_BYTES=$((2 * 1024 * 1024 * 1024))
readonly TEST_BYTES=$((640 * 1024 * 1024))
readonly SUSTAINED_COUNT=128
readonly BURST_COUNT=8
readonly TOKEN="$(/usr/bin/date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
readonly TEST_DIR="$ROOT/.validation-write-$TOKEN"
CREATED=0
TEST_DEVICE=
TEST_INODE=

refuse() {
    printf 'result=refused reason=%q\n' "$*" >&2
    exit 2
}
[ "$#" -eq 0 ] || refuse "arguments/externally supplied paths are forbidden"

cleanup() {
    status=$?
    if [ "$CREATED" -eq 1 ]; then
        case "$TEST_DIR" in
            /srv/dashcam/.validation-write-*) ;;
            *) exit 3 ;;
        esac
        [ -d "$TEST_DIR" ] && [ ! -L "$TEST_DIR" ] || exit 3
        [ "$(/usr/bin/stat -c %d -- "$TEST_DIR")" = "$TEST_DEVICE" ] || exit 3
        [ "$(/usr/bin/stat -c %i -- "$TEST_DIR")" = "$TEST_INODE" ] || exit 3
        for file in "$TEST_DIR"/sustained.{pending,final}; do
            [ ! -e "$file" ] || /usr/bin/rm -- "$file" || status=1
        done
        for index in 0 1 2 3 4 5 6 7; do
            for file in "$TEST_DIR/burst-$index.pending" "$TEST_DIR/burst-$index.final"; do
                [ ! -e "$file" ] || /usr/bin/rm -- "$file" || status=1
            done
        done
        /usr/bin/rmdir -- "$TEST_DIR" || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

[ "$(id -u)" -eq 0 ] || refuse "run as root"
[ "$(/usr/bin/cat /sys/class/block/mmcblk0/device/cid)" = "$CID" ] ||
    refuse "card CID differs"
mount_json=$(
    /usr/bin/findmnt --json --mountpoint "$ROOT" \
        --output TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS
)
printf '%s' "$mount_json" | /usr/bin/python3 -c '
import json
import sys
value = json.load(sys.stdin)
rows = value.get("filesystems") if isinstance(value, dict) else None
if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
    raise SystemExit(2)
row = rows[0]
options = row.get("options", "").split(",")
expected = {
    "target": "/srv/dashcam",
    "source": "/dev/mmcblk0p3",
    "fstype": "exfat",
    "label": "DASHCAM",
    "uuid": "7EED-3EA7",
}
if any(row.get(key) != item for key, item in expected.items()):
    raise SystemExit(2)
if "rw" not in options or "ro" in options:
    raise SystemExit(2)
' || refuse "mount identity/options differ"
/usr/bin/python3 -c '
import json
from pathlib import Path
value = json.loads(Path("/srv/dashcam/.dashcam-volume").read_text(encoding="ascii"))
if value.get("serial") != "fe34325344000000200000031a0192d1":
    raise SystemExit(2)
if value.get("dashcam_uuid") != "7EED-3EA7":
    raise SystemExit(2)
' || refuse "recording-volume sentinel differs"

set +e
/usr/bin/systemctl is-active --quiet dashcamd.service
recorder_status=$?
set -e
case "$recorder_status" in 3|4) ;; *) refuse "recorder must be inactive/not-found" ;; esac

read -r capacity free < <(
    /usr/bin/df -B1 --output=size,avail "$ROOT" | /usr/bin/awk 'NR == 2 {print $1, $2}'
)
case "$capacity:$free" in *[!0-9:]*|:*) refuse "space observation is malformed" ;; esac
watermark=$((capacity * 15 / 100))
required_reserve=$RESERVE_BYTES
[ "$watermark" -le "$required_reserve" ] || required_reserve=$watermark
[ "$free" -ge $((TEST_BYTES + required_reserve)) ] ||
    refuse "bounded benchmark would violate reserve"

/usr/bin/mkdir -m 0700 -- "$TEST_DIR"
CREATED=1
TEST_DEVICE=$(/usr/bin/stat -c %d -- "$TEST_DIR")
TEST_INODE=$(/usr/bin/stat -c %i -- "$TEST_DIR")
[ "$TEST_DEVICE" = "$(/usr/bin/stat -c %d -- "$ROOT")" ] ||
    refuse "test directory is not on the recording mount"
printf 'test_dir=%s capacity_bytes=%s free_before_bytes=%s reserve_bytes=%s\n' \
    "$TEST_DIR" "$capacity" "$free" "$required_reserve"

write_start=$(/usr/bin/date +%s%N)
/usr/bin/timeout -k 10 300 /usr/bin/dd if=/dev/zero \
    of="$TEST_DIR/sustained.pending" bs=4194304 count="$SUSTAINED_COUNT" \
    conv=fdatasync status=none
write_end=$(/usr/bin/date +%s%N)
written=$(/usr/bin/stat -c %s "$TEST_DIR/sustained.pending")
[ "$written" -eq $((512 * 1024 * 1024)) ] || refuse "sustained byte count differs"
finalize_start=$(/usr/bin/date +%s%N)
/usr/bin/mv -- "$TEST_DIR/sustained.pending" "$TEST_DIR/sustained.final"
/usr/bin/timeout -k 5 30 /usr/bin/sync -f "$TEST_DIR/sustained.final"
finalize_end=$(/usr/bin/date +%s%N)
sustained_sha=$(
    /usr/bin/timeout -k 5 120 /usr/bin/sha256sum "$TEST_DIR/sustained.final" |
        /usr/bin/cut -d ' ' -f 1
)
printf 'phase=sustained bytes=%s write_latency_ns=%s finalize_latency_ns=%s sha256=%s\n' \
    "$written" "$((write_end - write_start))" \
    "$((finalize_end - finalize_start))" "$sustained_sha"

for index in 0 1 2 3 4 5 6 7; do
    write_start=$(/usr/bin/date +%s%N)
    /usr/bin/timeout -k 5 60 /usr/bin/dd if=/dev/zero \
        of="$TEST_DIR/burst-$index.pending" bs=1048576 count=16 \
        conv=fdatasync status=none
    write_end=$(/usr/bin/date +%s%N)
    finalize_start=$(/usr/bin/date +%s%N)
    /usr/bin/mv -- "$TEST_DIR/burst-$index.pending" "$TEST_DIR/burst-$index.final"
    /usr/bin/timeout -k 5 30 /usr/bin/sync -f "$TEST_DIR/burst-$index.final"
    finalize_end=$(/usr/bin/date +%s%N)
    printf 'phase=burst index=%s bytes=16777216 write_latency_ns=%s finalize_latency_ns=%s\n' \
        "$index" "$((write_end - write_start))" \
        "$((finalize_end - finalize_start))"
done

read -r _ free_after < <(
    /usr/bin/df -B1 --output=size,avail "$ROOT" | /usr/bin/awk 'NR == 2 {print $1, $2}'
)
[ "$free_after" -ge "$required_reserve" ] || refuse "reserve was violated"
printf 'free_after_bytes=%s reserve_preserved=true completed=true\n' "$free_after"
