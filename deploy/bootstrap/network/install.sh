#!/bin/bash
set -euo pipefail

die() {
  printf 'dashcam network payload install refused: %s\n' "$*" >&2
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
  safe_dir "${relative}"
  chown --no-dereference 0:0 "${rootfs}/${relative}"
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
  chown --no-dereference 0:0 "${temporary}"
  mv -T -- "${temporary}" "${destination}"
}

safe_dir "var/lib/dashcam"
set_dir "var/lib/dashcam/network" 0700
set_dir "etc/NetworkManager" 0755
set_dir "etc/NetworkManager/system-connections" 0700
set_dir "etc/systemd/system" 0755
set_dir "etc/systemd/system/multi-user.target.wants" 0755

install_regular \
  "${self_dir}/dashcam-network-fallback.service" \
  "etc/systemd/system/dashcam-network-fallback.service" 0644

link="${rootfs}/etc/systemd/system/multi-user.target.wants/dashcam-network-fallback.service"
expected="../dashcam-network-fallback.service"
if [ -L "${link}" ]; then
  [ "$(readlink -- "${link}")" = "${expected}" ] ||
    die "foreign enablement link refused: ${link}"
elif [ -e "${link}" ]; then
  die "non-symbolic enablement target refused: ${link}"
else
  ln -s -- "${expected}" "${link}"
fi
