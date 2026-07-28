#!/bin/bash
# Destructive only to disposable regular images and their validated loop devices.
set -euo pipefail

readonly IMAGE_BYTES=67108864
readonly COMMAND_TIMEOUT=120
readonly OUTPUT_LIMIT=131072
readonly EXPECTED_LABEL=DCFSCKTEST
readonly SELF="$(readlink -f -- "$0")"

if [ "${DASHCAM_FSCK_PRIVATE_NS:-0}" != 1 ]; then
    exec /usr/bin/unshare --mount --propagation private \
        /usr/bin/env DASHCAM_FSCK_PRIVATE_NS=1 "$SELF" "$@"
fi

[ "$(id -u)" -eq 0 ] || {
    printf '%s\n' "refused: run as root" >&2
    exit 2
}
/usr/bin/mount --make-rprivate /
[ "$#" -eq 0 ] || {
    printf '%s\n' "refused: arguments/externally supplied targets are forbidden" >&2
    exit 2
}

WORK=$(/usr/bin/mktemp -d /var/tmp/dashcam-exfat-fsck.XXXXXXXX)
MOUNT_POINT="$WORK/seed-mount"
ACTIVE_LOOP=
COMPLETED=0

refuse() {
    printf 'result=refused reason=%q\n' "$*" >&2
    exit 2
}

validate_work_path() {
    case "$1" in
        "$WORK"/*) ;;
        *) refuse "path escaped disposable work directory" ;;
    esac
    [ ! -L "$1" ] || refuse "work path is a symlink"
}

detach_loop() {
    if [ -n "$ACTIVE_LOOP" ]; then
        case "$ACTIVE_LOOP" in
            /dev/loop[0-9]*) ;;
            *) refuse "active loop name changed" ;;
        esac
        /usr/bin/timeout -k 5 30 /usr/sbin/losetup -d "$ACTIVE_LOOP" ||
            refuse "could not detach exact disposable loop"
        ACTIVE_LOOP=
    fi
}

cleanup() {
    status=$?
    if /usr/bin/mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
        /usr/bin/timeout -k 5 30 /usr/bin/umount "$MOUNT_POINT" || status=1
    fi
    if [ -n "$ACTIVE_LOOP" ]; then
        case "$ACTIVE_LOOP" in
            /dev/loop[0-9]*)
                /usr/bin/timeout -k 5 30 /usr/sbin/losetup -d "$ACTIVE_LOOP" ||
                    status=1
                ;;
            *) status=1 ;;
        esac
    fi
    case "$WORK" in
        /var/tmp/dashcam-exfat-fsck.*)
            [ ! -L "$WORK" ] && /usr/bin/rm -rf --one-file-system -- "$WORK" || status=1
            ;;
        *) status=1 ;;
    esac
    [ "$COMPLETED" -eq 1 ] || printf '%s\n' "completed=false"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

run_bounded() {
    output=$1
    shift
    validate_work_path "$output"
    set +e
    (
        ulimit -f 128
        /usr/bin/timeout -k 10 "$COMMAND_TIMEOUT" "$@"
    ) >"$output" 2>&1
    status=$?
    set -e
    bytes=$(/usr/bin/stat -c %s -- "$output")
    [ "$bytes" -le "$OUTPUT_LIMIT" ] || refuse "command output exceeded bound"
    RUN_STATUS=$status
    RUN_OUTPUT_BYTES=$bytes
    RUN_OUTPUT_SHA=$(/usr/bin/sha256sum "$output" | /usr/bin/cut -d ' ' -f 1)
}

validate_image() {
    image=$1
    validate_work_path "$image"
    [ -f "$image" ] || refuse "image is not regular"
    [ "$(/usr/bin/stat -c %h -- "$image")" = 1 ] || refuse "image has multiple links"
    [ "$(/usr/bin/stat -c %s -- "$image")" = "$IMAGE_BYTES" ] ||
        refuse "image size changed"
}

attach_image() {
    image=$1
    validate_image "$image"
    [ -z "$ACTIVE_LOOP" ] || refuse "a loop is already active"
    ACTIVE_LOOP=$(
        /usr/bin/timeout -k 5 30 /usr/sbin/losetup \
            --find --show --nooverlap -- "$image"
    )
    case "$ACTIVE_LOOP" in
        /dev/loop[0-9]*) ;;
        *) refuse "losetup returned a foreign device" ;;
    esac
    backing=$(
        /usr/bin/timeout -k 5 30 /usr/sbin/losetup \
            --noheadings --output BACK-FILE "$ACTIVE_LOOP" |
            /usr/bin/sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
    )
    [ "$(/usr/bin/readlink -f -- "$backing")" = "$(/usr/bin/readlink -f -- "$image")" ] ||
        refuse "loop backing file differs"
    [ "$(/usr/sbin/blockdev --getsize64 "$ACTIVE_LOOP")" = "$IMAGE_BYTES" ] ||
        refuse "loop size differs"
}

snapshot() {
    case_name=$1
    phase=$2
    image=$3
    printf 'case=%s phase=%s image_sha256=%s\n' \
        "$case_name" "$phase" "$(/usr/bin/sha256sum "$image" | /usr/bin/cut -d ' ' -f 1)"
    run_bounded "$WORK/$case_name-$phase-wipefs.txt" \
        /usr/sbin/wipefs --json "$ACTIVE_LOOP"
    printf 'case=%s phase=%s wipefs_status=%s wipefs_bytes=%s wipefs_sha256=%s\n' \
        "$case_name" "$phase" "$RUN_STATUS" "$RUN_OUTPUT_BYTES" "$RUN_OUTPUT_SHA"
    run_bounded "$WORK/$case_name-$phase-blkid.txt" \
        /usr/sbin/blkid -p -o export "$ACTIVE_LOOP"
    printf 'case=%s phase=%s blkid_status=%s blkid_bytes=%s blkid_sha256=%s\n' \
        "$case_name" "$phase" "$RUN_STATUS" "$RUN_OUTPUT_BYTES" "$RUN_OUTPUT_SHA"
}

run_fsck() {
    case_name=$1
    mode=$2
    run_bounded "$WORK/$case_name-fsck-${mode#-}.txt" \
        /usr/sbin/fsck.exfat "$mode" "$ACTIVE_LOOP"
    printf 'case=%s fsck_mode=%s status=%s output_bytes=%s output_sha256=%s\n' \
        "$case_name" "$mode" "$RUN_STATUS" "$RUN_OUTPUT_BYTES" "$RUN_OUTPUT_SHA"
}

/usr/bin/mkdir -m 0700 "$MOUNT_POINT"
SEED="$WORK/seed.img"
/usr/bin/truncate -s "$IMAGE_BYTES" "$SEED"
attach_image "$SEED"
# The harness's sole format operation creates only the disposable seed.
run_bounded "$WORK/mkfs.txt" /usr/sbin/mkfs.exfat -L "$EXPECTED_LABEL" "$ACTIVE_LOOP"
[ "$RUN_STATUS" -eq 0 ] || refuse "seed format failed"
printf 'seed_mkfs_count=1 status=%s output_sha256=%s\n' "$RUN_STATUS" "$RUN_OUTPUT_SHA"
/usr/bin/timeout -k 5 30 /usr/bin/mount -t exfat \
    -o rw,nosuid,nodev,noexec "$ACTIVE_LOOP" "$MOUNT_POINT"
/usr/bin/dd if=/dev/zero of="$MOUNT_POINT/payload.bin" bs=1048576 count=4 \
    conv=fsync status=none
/usr/bin/timeout -k 5 30 /usr/bin/umount "$MOUNT_POINT"
detach_loop

for case_name in clean repairable failed; do
    /usr/bin/cp --reflink=never -- "$SEED" "$WORK/$case_name.img"
    validate_image "$WORK/$case_name.img"
done

# Corrupt one byte of only the main boot-region checksum. The intact backup
# region gives fsck.exfat a deterministic repair source.
checksum_byte=$(/usr/bin/od -An -tu1 -j 5632 -N 1 "$WORK/repairable.img")
case "$checksum_byte" in
    *[!0-9\ ]*|"") refuse "could not read the main boot checksum byte" ;;
esac
checksum_byte=${checksum_byte// /}
replacement=255
[ "$checksum_byte" -ne "$replacement" ] || replacement=0
/usr/bin/printf '%b' "\\$(/usr/bin/printf '%03o' "$replacement")" |
    /usr/bin/dd of="$WORK/repairable.img" bs=1 seek=5632 \
        conv=notrunc status=none

# Make both root-directory cluster fields invalid while retaining exFAT signatures.
printf '\000\000\000\000' | /usr/bin/dd of="$WORK/failed.img" bs=1 seek=96 \
    conv=notrunc status=none
printf '\000\000\000\000' | /usr/bin/dd of="$WORK/failed.img" bs=1 seek=6240 \
    conv=notrunc status=none

attach_image "$WORK/clean.img"
snapshot clean before "$WORK/clean.img"
clean_before=$(/usr/bin/sha256sum "$WORK/clean.img" | /usr/bin/cut -d ' ' -f 1)
run_fsck clean -n
[ "$RUN_STATUS" -eq 0 ] || refuse "clean image did not pass read-only fsck"
clean_after=$(/usr/bin/sha256sum "$WORK/clean.img" | /usr/bin/cut -d ' ' -f 1)
[ "$clean_before" = "$clean_after" ] || refuse "read-only clean fsck changed image"
snapshot clean after "$WORK/clean.img"
detach_loop

attach_image "$WORK/repairable.img"
snapshot repairable before "$WORK/repairable.img"
repair_before=$(/usr/bin/sha256sum "$WORK/repairable.img" | /usr/bin/cut -d ' ' -f 1)
run_fsck repairable -n
[ "$RUN_STATUS" -ne 0 ] || refuse "repairable image was not observed dirty"
run_fsck repairable -y
case "$RUN_STATUS" in 0|1) ;; *) refuse "repairable image was not safely repaired" ;; esac
repair_after=$(/usr/bin/sha256sum "$WORK/repairable.img" | /usr/bin/cut -d ' ' -f 1)
[ "$repair_before" != "$repair_after" ] || refuse "repair did not change dirty image"
run_fsck repairable-final -n
[ "$RUN_STATUS" -eq 0 ] || refuse "repaired image is not clean"
snapshot repairable after "$WORK/repairable.img"
detach_loop

attach_image "$WORK/failed.img"
snapshot failed before "$WORK/failed.img"
failed_before=$(/usr/bin/sha256sum "$WORK/failed.img" | /usr/bin/cut -d ' ' -f 1)
failed_blkid_before=$RUN_OUTPUT_SHA
run_fsck failed -p
case "$RUN_STATUS" in 0|1) refuse "unrepairable image unexpectedly passed/repaired" ;; esac
failed_after=$(/usr/bin/sha256sum "$WORK/failed.img" | /usr/bin/cut -d ' ' -f 1)
[ "$failed_before" = "$failed_after" ] ||
    refuse "failed fsck mutated image before refusing"
snapshot failed after "$WORK/failed.img"
[ "$failed_blkid_before" = "$RUN_OUTPUT_SHA" ] ||
    refuse "failed case filesystem identity changed"
detach_loop

printf '%s\n' "auto_format_count=0_for_all_fsck_cases"
printf '%s\n' "completed=true"
COMPLETED=1
