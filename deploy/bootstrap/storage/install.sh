#!/bin/bash
set -euo pipefail

die() {
  printf 'dashcam storage payload install refused: %s\n' "$*" >&2
  exit 2
}

[ "$#" -eq 1 ] || die "usage: install.sh ROOTFS"
case "$1" in
  /*) ;;
  *) die "ROOTFS must be absolute" ;;
esac

rootfs_input="${1%/}"
[ -n "${rootfs_input}" ] || die "the host root is not an installation target"
[ ! -L "${rootfs_input}" ] || die "ROOTFS must not be a symbolic link"
[ -d "${rootfs_input}" ] || die "ROOTFS must be an existing directory"
rootfs="$(realpath -e -- "${rootfs_input}")"
[ "${rootfs}" = "${rootfs_input}" ] || die "ROOTFS must be canonical"
[ "${rootfs}" != "/" ] || die "the host root is not an installation target"

self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

safe_dir() {
  local relative="$1"
  local current="${rootfs}"
  local component
  local old_ifs="${IFS}"
  local -a components

  case "${relative}" in
    /*|""|*..*) die "unsafe relative directory: ${relative}" ;;
  esac
  IFS='/'
  read -r -a components <<<"${relative}"
  IFS="${old_ifs}"
  for component in "${components[@]}"; do
    [ -n "${component}" ] || die "empty path component in ${relative}"
    current="${current}/${component}"
    [ ! -L "${current}" ] || die "symbolic directory refused: ${current}"
    if [ -e "${current}" ]; then
      [ -d "${current}" ] || die "non-directory target refused: ${current}"
    else
      install -d -m 0755 "${current}"
    fi
  done
}

set_dir() {
  local relative="$1"
  local mode="$2"
  local owner="$3"
  safe_dir "${relative}"
  chown --no-dereference "${owner}" "${rootfs}/${relative}"
  chmod "${mode}" "${rootfs}/${relative}"
}

regular_source() {
  local source="$1"
  [ ! -L "${source}" ] || die "symbolic source refused: ${source}"
  [ -f "${source}" ] || die "non-regular source refused: ${source}"
  [ "$(stat -c '%h' -- "${source}")" -eq 1 ] ||
    die "multiply-linked source refused: ${source}"
}

install_regular() {
  local source="$1"
  local relative="$2"
  local mode="$3"
  local owner="$4"
  local destination="${rootfs}/${relative}"
  local parent="${relative%/*}"
  local temporary

  regular_source "${source}"
  safe_dir "${parent}"
  [ ! -L "${destination}" ] || die "symbolic destination refused: ${destination}"
  if [ -e "${destination}" ]; then
    [ -f "${destination}" ] || die "non-regular destination refused: ${destination}"
    [ "$(stat -c '%h' -- "${destination}")" -eq 1 ] ||
      die "multiply-linked destination refused: ${destination}"
  fi
  temporary="${destination}.dashcam-install.$$"
  [ ! -e "${temporary}" ] && [ ! -L "${temporary}" ] ||
    die "temporary destination already exists: ${temporary}"
  install -m "${mode}" "${source}" "${temporary}"
  chown --no-dereference "${owner}" "${temporary}"
  mv -T -- "${temporary}" "${destination}"
}

target_group_field() {
  local name="$1"
  local field="$2"
  awk -F: -v name="${name}" -v field="${field}" \
    '$1 == name { if (++found > 1) exit 3; value = $field }
     END { if (found != 1) exit 2; print value }' \
    "${rootfs}/etc/group"
}

target_passwd_field() {
  local name="$1"
  local field="$2"
  awk -F: -v name="${name}" -v field="${field}" \
    '$1 == name { if (++found > 1) exit 3; value = $field }
     END { if (found != 1) exit 2; print value }' \
    "${rootfs}/etc/passwd"
}

ensure_group() {
  local name="$1"
  if ! target_group_field "${name}" 3 >/dev/null 2>&1; then
    groupadd --root "${rootfs}" --system "${name}"
  fi
  target_group_field "${name}" 3 >/dev/null ||
    die "group identity is ambiguous or missing: ${name}"
}

ensure_group dashcam
ensure_group dashcam-storage

dashcam_gid="$(target_group_field dashcam 3)"
storage_gid="$(target_group_field dashcam-storage 3)"
case "${dashcam_gid}:${storage_gid}" in
  *[!0-9:]*|:|*:|*::* ) die "target group IDs are invalid" ;;
esac

if ! target_passwd_field dashcam 3 >/dev/null 2>&1; then
  useradd \
    --root "${rootfs}" \
    --system \
    --gid dashcam \
    --groups dashcam-storage \
    --home-dir /var/lib/dashcam \
    --shell /usr/sbin/nologin \
    --no-create-home \
    dashcam
fi

dashcam_uid="$(target_passwd_field dashcam 3)" ||
  die "dashcam user identity is ambiguous or missing"
[ "$(target_passwd_field dashcam 4)" = "${dashcam_gid}" ] ||
  die "dashcam primary group is not the target dashcam group"
[ "$(target_passwd_field dashcam 6)" = "/var/lib/dashcam" ] ||
  die "dashcam home is not /var/lib/dashcam"
[ "$(target_passwd_field dashcam 7)" = "/usr/sbin/nologin" ] ||
  die "dashcam shell is not /usr/sbin/nologin"
case "${dashcam_uid}" in
  ""|*[!0-9]*) die "target dashcam UID is invalid" ;;
esac

storage_members="$(target_group_field dashcam-storage 4)"
case ",${storage_members}," in
  *,dashcam,*) ;;
  *)
    usermod --root "${rootfs}" --append --groups dashcam-storage dashcam
    storage_members="$(target_group_field dashcam-storage 4)"
    case ",${storage_members}," in
      *,dashcam,*) ;;
      *) die "dashcam is not a member of dashcam-storage" ;;
    esac
    ;;
esac

set_dir "etc/dashcam" 0750 "0:${dashcam_gid}"
set_dir "var/lib/dashcam" 0750 "${dashcam_uid}:${dashcam_gid}"
set_dir "var/lib/dashcam/provisioning" 0700 "0:0"
set_dir "var/lib/dashcam/network" 0700 "0:0"
set_dir "srv/dashcam" 0550 "0:${storage_gid}"
set_dir "etc/systemd/system" 0755 "0:0"
set_dir "etc/systemd/system/multi-user.target.wants" 0755 "0:0"

install_regular \
  "${self_dir}/authorized-exact-card-v1.json" \
  "etc/dashcam/bootstrap-v1-authorization.json" 0400 "0:0"
install_regular \
  "${rootfs}/opt/dashcam/app/config/default.toml" \
  "etc/dashcam/config.toml" 0640 "0:${dashcam_gid}"
install_regular \
  "${rootfs}/opt/dashcam/app/systemd/dashcam-storage-check.service" \
  "etc/systemd/system/dashcam-storage-check.service" 0644 "0:0"
install_regular \
  "${rootfs}/opt/dashcam/app/systemd/dashcamd.service" \
  "etc/systemd/system/dashcamd.service" 0644 "0:0"

install_bootstrap_unit() {
  local name="$1"
  local source="${self_dir}/${name}"
  local rendered="${rootfs}/etc/systemd/system/.${name}.rendered.$$"

  regular_source "${source}"
  [ ! -e "${rendered}" ] && [ ! -L "${rendered}" ] ||
    die "temporary unit target already exists: ${rendered}"
  sed \
    -e 's#^After=local-fs\.target$#After=local-fs.target cloud-final.service#' \
    -e 's#^After=local-fs\.target dashcam-bootstrap-stage-a\.service$#After=local-fs.target cloud-final.service dashcam-bootstrap-stage-a.service#' \
    -e 's#^Wants=local-fs\.target$#Wants=local-fs.target cloud-final.service#' \
    -e 's#^ExecStart=/usr/bin/python3 -m dashcam\.provisioning\.bootstrap#ExecStart=/opt/dashcam/venv/bin/python -m dashcam.provisioning.bootstrap#' \
    "${source}" >"${rendered}"
  [ "$(grep -c '^ExecStart=/opt/dashcam/venv/bin/python -m dashcam.provisioning.bootstrap' "${rendered}")" -eq 1 ] ||
    die "bootstrap unit interpreter transformation failed: ${name}"
  [ "$(grep -c '^After=.*cloud-final\.service' "${rendered}")" -eq 1 ] ||
    die "bootstrap unit cloud-final ordering transformation failed: ${name}"
  [ "$(grep -c '^Wants=.*cloud-final\.service' "${rendered}")" -eq 1 ] ||
    die "bootstrap unit cloud-final activation transformation failed: ${name}"
  install_regular "${rendered}" "etc/systemd/system/${name}" 0644 "0:0"
  rm -f -- "${rendered}"
}

enable_unit() {
  local name="$1"
  local link="${rootfs}/etc/systemd/system/multi-user.target.wants/${name}"
  local expected="../${name}"

  if [ -L "${link}" ]; then
    [ "$(readlink -- "${link}")" = "${expected}" ] ||
      die "foreign enablement link refused: ${link}"
  elif [ -e "${link}" ]; then
    die "non-symbolic enablement target refused: ${link}"
  else
    ln -s -- "${expected}" "${link}"
  fi
}

install_bootstrap_unit dashcam-bootstrap-stage-a.service
install_bootstrap_unit dashcam-bootstrap-stage-b.service
enable_unit dashcam-bootstrap-stage-a.service
enable_unit dashcam-bootstrap-stage-b.service
enable_unit dashcam-storage-check.service

# The storage check is guarded by the Stage B completion marker and performs
# only a bounded fail-closed write probe on the verified recording mount.
# dashcamd.service remains disabled until its production media runtime gate is
# complete.
