#!/bin/sh
# Install only the reviewed SSH-dev bootstrap payload.  This does not arm or run it.
set -eu

PAYLOAD=${1:-"$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"}
EXPECTED_FILES='README.md arm-cmdline.py authorized-exact-card-ssh-dev-v1.json bootstrap.py install.sh recover-exfat-reconciliation-refusal.py recover-fssize-refusal.py'

fail() {
    printf '%s\n' "ssh-dev installer refused: $*" >&2
    exit 2
}

[ "$(id -u)" -eq 0 ] || fail "must run as root on the live Pi"
[ -z "${DASHCAM_OFFLINE_ROOT:-}" ] || fail "offline target roots are not supported"
[ -r /proc/1/stat ] || fail "a live Linux /proc is required"
case "$PAYLOAD" in
    /*) ;;
    *) fail "payload directory must be absolute" ;;
esac
[ -d "$PAYLOAD" ] && [ ! -L "$PAYLOAD" ] || fail "payload directory is unsafe"
[ "$(readlink -f -- "$PAYLOAD")" = "$PAYLOAD" ] || fail "payload directory chain is unsafe"

safe_payload_file() {
    [ -f "$PAYLOAD/$1" ] && [ ! -L "$PAYLOAD/$1" ] || fail "payload file is unsafe: $1"
    [ "$(stat -c %h -- "$PAYLOAD/$1")" = 1 ] || fail "payload file has multiple links: $1"
}

for payload_file in README.md arm-cmdline.py authorized-exact-card-ssh-dev-v1.json bootstrap.py install.sh recover-exfat-reconciliation-refusal.py recover-fssize-refusal.py SHA256SUMS; do
    safe_payload_file "$payload_file"
done

actual=$(awk '{print $2}' "$PAYLOAD/SHA256SUMS" | tr '\n' ' ' | sed 's/ $//')
[ "$actual" = "$EXPECTED_FILES" ] || fail "manifest allowlist is not exact"
(cd "$PAYLOAD" && sha256sum -c --status SHA256SUMS) || fail "manifest verification failed"

local_count() {
    awk -F: -v wanted="$1" '$1 == wanted { count++ } END { print count + 0 }' "$2"
}

local_line() {
    awk -F: -v wanted="$1" '$1 == wanted { print; exit }' "$2"
}

numeric_id() {
    case "$1" in
        ''|*[!0-9]*) fail "identity has a non-numeric ID" ;;
    esac
}

ensure_group() {
    group_name=$1
    group_count=$(local_count "$group_name" /etc/group)
    [ "$group_count" -le 1 ] || fail "multiple local group identities: $group_name"
    if [ "$group_count" -eq 0 ]; then
        getent group "$group_name" >/dev/null && fail "conflicting non-local group: $group_name"
        groupadd --system "$group_name"
    fi
    [ "$(local_count "$group_name" /etc/group)" -eq 1 ] || fail "local group creation failed"
    group_line=$(local_line "$group_name" /etc/group)
    group_gid=$(printf '%s\n' "$group_line" | cut -d: -f3)
    numeric_id "$group_gid"
    nss_line=$(getent group "$group_name") || fail "local group is missing from NSS"
    [ "$nss_line" = "$group_line" ] || \
        fail "NSS group identity conflicts with local identity"
}

ensure_group dashcam
dashcam_gid=$group_gid
ensure_group dashcam-storage
storage_gid=$group_gid
[ "$dashcam_gid" != "$storage_gid" ] || fail "dashcam groups must have distinct GIDs"

user_count=$(local_count dashcam /etc/passwd)
[ "$user_count" -le 1 ] || fail "multiple local dashcam users"
if [ "$user_count" -eq 0 ]; then
    getent passwd dashcam >/dev/null && fail "conflicting non-local dashcam user"
    useradd --system --gid "$dashcam_gid" --home /var/lib/dashcam --shell /usr/sbin/nologin \
        --no-create-home dashcam
fi
[ "$(local_count dashcam /etc/passwd)" -eq 1 ] || fail "local dashcam user creation failed"
user_line=$(local_line dashcam /etc/passwd)
user_gid=$(printf '%s\n' "$user_line" | cut -d: -f4)
user_home=$(printf '%s\n' "$user_line" | cut -d: -f6)
user_shell=$(printf '%s\n' "$user_line" | cut -d: -f7)
numeric_id "$user_gid"
[ "$user_gid" = "$dashcam_gid" ] || fail "dashcam user has a conflicting primary GID"
[ "$user_home" = /var/lib/dashcam ] || fail "dashcam user has a conflicting home"
[ "$user_shell" = /usr/sbin/nologin ] || fail "dashcam user has a conflicting shell"
nss_user=$(getent passwd dashcam) || fail "local dashcam user is missing from NSS"
[ "$nss_user" = "$user_line" ] || fail "NSS user identity conflicts with local identity"
if ! awk -F: '$1 == "dashcam-storage" { n = split($4, members, ","); for (i = 1; i <= n; i++) if (members[i] == "dashcam") found = 1 } END { exit !found }' /etc/group; then
    usermod -a -G dashcam-storage dashcam
fi
awk -F: '$1 == "dashcam-storage" { n = split($4, members, ","); for (i = 1; i <= n; i++) if (members[i] == "dashcam") found = 1 } END { exit !found }' /etc/group || \
    fail "dashcam user lacks local dashcam-storage membership"
[ "$(getent group dashcam)" = "$(local_line dashcam /etc/group)" ] || \
    fail "NSS dashcam group conflicts after validation"
[ "$(getent group dashcam-storage)" = "$(local_line dashcam-storage /etc/group)" ] || \
    fail "NSS dashcam-storage group conflicts after validation"

install -d -o root -g dashcam-storage -m 0750 /opt/dashcam-bootstrap /etc/dashcam
install -d -o dashcam -g dashcam -m 0750 /var/lib/dashcam
install -d -o root -g dashcam-storage -m 0750 /var/lib/dashcam/provisioning
# Keep the underlying rootfs directory non-writable by the recorder account.
# Stage B's verified exFAT mount supplies write access only after provisioning.
install -d -o root -g dashcam-storage -m 0550 /srv/dashcam
install -o root -g dashcam-storage -m 0640 "$PAYLOAD/bootstrap.py" /opt/dashcam-bootstrap/bootstrap.py
install -o root -g dashcam-storage -m 0750 "$PAYLOAD/arm-cmdline.py" /opt/dashcam-bootstrap/arm-cmdline.py
install -o root -g dashcam-storage -m 0750 "$PAYLOAD/recover-fssize-refusal.py" \
    /opt/dashcam-bootstrap/recover-fssize-refusal.py
install -o root -g dashcam-storage -m 0750 \
    "$PAYLOAD/recover-exfat-reconciliation-refusal.py" \
    /opt/dashcam-bootstrap/recover-exfat-reconciliation-refusal.py
install -o root -g dashcam-storage -m 0640 "$PAYLOAD/authorized-exact-card-ssh-dev-v1.json" \
    /etc/dashcam/bootstrap-v1-authorization.json

printf '%s\n' 'SSH-dev bootstrap payload installed; no trigger was armed and no storage action ran.'
