#!/bin/bash
set -euo pipefail

# Official pi-gen provides ROOTFS_DIR and BOOTFS_DIR to custom stages.
: "${ROOTFS_DIR:?pi-gen ROOTFS_DIR is required}"
: "${BOOTFS_DIR:?pi-gen BOOTFS_DIR is required}"
: "${DASHCAM_DEBIAN_SNAPSHOT:?immutable Debian snapshot URL is required}"
: "${DASHCAM_RASPBERRYPI_SNAPSHOT:?immutable Raspberry Pi snapshot URL is required}"

case "${ROOTFS_DIR}" in /*) ;; *) echo "ROOTFS_DIR must be absolute" >&2; exit 2 ;; esac
case "${BOOTFS_DIR}" in /*) ;; *) echo "BOOTFS_DIR must be absolute" >&2; exit 2 ;; esac
test ! -L "${ROOTFS_DIR}"
test ! -L "${BOOTFS_DIR}"
test -d "${ROOTFS_DIR}"
test -d "${BOOTFS_DIR}"
test "$(realpath -e -- "${ROOTFS_DIR}")" = "${ROOTFS_DIR%/}"
test "$(realpath -e -- "${BOOTFS_DIR}")" = "${BOOTFS_DIR%/}"
test "${ROOTFS_DIR%/}" != "/"
test "${BOOTFS_DIR%/}" != "/"

stage_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
assets="${stage_dir}/files"
test -f "${assets}/READY"
test -d "${assets}/repository"
test -d "${assets}/wheelhouse"
test -d "${assets}/storage"
test -d "${assets}/network"
test -f "${assets}/build-metadata/source.json"
test -f "${assets}/build-metadata/official-source.json"
test -f "${assets}/build-metadata/build-requirements.json"
test -f "${assets}/build-metadata/package-inventory.json"
test -f "${assets}/build-metadata/uv.lock"
test -f "${assets}/build-metadata/app-wheel.sha256"
(
  cd "${assets}"
  expected_stage_hash="$(cat READY)"
  case "${expected_stage_hash}" in
    *[!0-9a-f]*|"") echo "invalid READY digest" >&2; exit 2 ;;
  esac
  test "${#expected_stage_hash}" -eq 64
  actual_stage_hash="$(sha256sum build-metadata/stage-files.sha256 | cut -d ' ' -f 1)"
  test "${actual_stage_hash}" = "${expected_stage_hash}"
  sha256sum --check --strict build-metadata/stage-files.sha256
  sha256sum --check --strict build-metadata/app-wheel.sha256
)
if find "${assets}" \( -type l -o -type b -o -type c -o -type p -o -type s \) \
  -print -quit | grep -q .; then
  echo "special or symbolic stage asset refused" >&2
  exit 2
fi
test -f "${ROOTFS_DIR}/usr/lib/systemd/system/cloud-final.service"
test -f "${ROOTFS_DIR}/etc/cloud/cloud.cfg"
test -f "${ROOTFS_DIR}/etc/cloud/cloud.cfg.d/99_raspberry-pi.cfg"
test -f \
  "${ROOTFS_DIR}/usr/lib/python3/dist-packages/cloudinit/config/cc_raspberry_pi.py"
test -f \
  "${ROOTFS_DIR}/usr/lib/python3/dist-packages/cloudinit/distros/raspberry_pi_os.py"
test -f "${BOOTFS_DIR}/meta-data"
test -f "${BOOTFS_DIR}/network-config"
test -f "${BOOTFS_DIR}/user-data"
cloud_init_identity="$(
  chroot "${ROOTFS_DIR}" dpkg-query -W -f='${Version}\t${Architecture}\n' cloud-init
)"
raspberrypi_sys_mods_identity="$(
  chroot "${ROOTFS_DIR}" dpkg-query -W -f='${Version}\t${Architecture}\n' raspberrypi-sys-mods
)"
[ "${cloud_init_identity}" = $'25.2-1~bpo13+1+rpt20\tall' ] || {
  echo "pinned cloud-init package identity changed" >&2
  exit 1
}
[ "${raspberrypi_sys_mods_identity}" = $'1:20260612\tarmhf' ] || {
  echo "pinned raspberrypi-sys-mods package identity changed" >&2
  exit 1
}

for target in \
  "${ROOTFS_DIR}/opt" \
  "${ROOTFS_DIR}/opt/dashcam" \
  "${ROOTFS_DIR}/opt/dashcam/app" \
  "${ROOTFS_DIR}/opt/dashcam/bootstrap" \
  "${ROOTFS_DIR}/opt/dashcam/build-metadata" \
  "${ROOTFS_DIR}/opt/dashcam/wheelhouse"; do
  test ! -L "${target}"
  if [ -e "${target}" ]; then
    test -d "${target}"
  fi
done

install -d -m 0755 \
  "${ROOTFS_DIR}/opt/dashcam/app" \
  "${ROOTFS_DIR}/opt/dashcam/bootstrap"
cp -a --no-dereference "${assets}/repository/." "${ROOTFS_DIR}/opt/dashcam/app/"
cp -a --no-dereference "${assets}/storage" "${ROOTFS_DIR}/opt/dashcam/bootstrap/"
cp -a --no-dereference "${assets}/network" "${ROOTFS_DIR}/opt/dashcam/bootstrap/"
install -d -m 0755 "${ROOTFS_DIR}/opt/dashcam/build-metadata"
install -m 0644 "${assets}/build-metadata/"* "${ROOTFS_DIR}/opt/dashcam/build-metadata/"
install -d -m 0755 "${ROOTFS_DIR}/opt/dashcam/wheelhouse"
cp -a --no-dereference "${assets}/wheelhouse/." "${ROOTFS_DIR}/opt/dashcam/wheelhouse/"

snapshot_sources="${ROOTFS_DIR}/etc/apt/sources.list.d/dashcam-bootstrap.sources"
test -f "${snapshot_sources}"
grep -Fqx "URIs: ${DASHCAM_DEBIAN_SNAPSHOT}" "${snapshot_sources}"
grep -Fqx "URIs: ${DASHCAM_RASPBERRYPI_SNAPSHOT}" "${snapshot_sources}"
if find "${ROOTFS_DIR}/etc/apt" -maxdepth 2 \
  \( -name '*.list' -o -name '*.sources' \) \
  ! -path "${snapshot_sources}" -print -quit | grep -q .; then
  echo "moving apt source remained enabled" >&2
  exit 1
fi

mapfile -t required_packages < <(
  sed -e '/^[[:space:]]*$/d' -e '/^[[:space:]]*#/d' "${stage_dir}/00-packages"
)
[ "${#required_packages[@]}" -gt 0 ]
chroot "${ROOTFS_DIR}" apt-get update
chroot "${ROOTFS_DIR}" apt-get install \
  --yes \
  --no-install-recommends \
  "${required_packages[@]}"

mapfile -t app_wheels < <(
  find "${ROOTFS_DIR}/opt/dashcam/wheelhouse" -maxdepth 1 -type f \
    -name 'dashcam_pizero2w-*.whl' -printf '%f\n' | sort
)
[ "${#app_wheels[@]}" -eq 1 ]
chroot "${ROOTFS_DIR}" python3 -m venv /opt/dashcam/venv
chroot "${ROOTFS_DIR}" /opt/dashcam/venv/bin/python -m pip install \
  --disable-pip-version-check \
  --no-index \
  --no-cache-dir \
  --find-links /opt/dashcam/wheelhouse \
  "/opt/dashcam/wheelhouse/${app_wheels[0]}"
chroot "${ROOTFS_DIR}" /opt/dashcam/venv/bin/python -c \
  'import dashcam; print(dashcam.__file__)' \
  > "${ROOTFS_DIR}/opt/dashcam/build-metadata/import-smoke.txt"
chroot "${ROOTFS_DIR}" dpkg-query -W -f='${binary:Package}\t${Version}\n' \
  > "${ROOTFS_DIR}/opt/dashcam/build-metadata/dpkg-versions.tsv"

# Payload owners provide install.sh. They install only ordinary post-root units
# and must be idempotent when invoked against a pi-gen root filesystem.
/bin/bash "${ROOTFS_DIR}/opt/dashcam/bootstrap/storage/install.sh" "${ROOTFS_DIR}"
/bin/bash "${ROOTFS_DIR}/opt/dashcam/bootstrap/network/install.sh" "${ROOTFS_DIR}"

test -L \
  "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/dashcam-bootstrap-stage-a.service"
test -L \
  "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/dashcam-bootstrap-stage-b.service"
test -L \
  "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/dashcam-network-fallback.service"
test -L \
  "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/dashcam-storage-check.service"
grep -Fqx \
  'ConditionPathExists=/var/lib/dashcam/provisioning/layout-v1.complete.json' \
  "${ROOTFS_DIR}/etc/systemd/system/dashcam-storage-check.service"
test ! -e "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/dashcamd.service"
if grep -R -E -I -l \
  'cloud-init[[:space:]]+(clean|disabled)|systemctl[[:space:]]+(disable|mask)[[:space:]]+cloud-' \
  "${ROOTFS_DIR}/opt/dashcam/bootstrap" 2>/dev/null; then
  echo "DashCam payload must preserve Raspberry Pi Imager cloud-init customization" >&2
  exit 1
fi

cmdline="${BOOTFS_DIR}/cmdline.txt"
test -f "${cmdline}"
boot_before="$(mktemp "${ROOTFS_DIR}/opt/dashcam/build-metadata/boot-before.XXXXXX")"
boot_after="$(mktemp "${ROOTFS_DIR}/opt/dashcam/build-metadata/boot-after.XXXXXX")"
find "${BOOTFS_DIR}" -type f ! -path "${cmdline}" -print0 |
  sort -z |
  xargs -0 sha256sum >"${boot_before}"
PYTHONPATH="${assets}/repository/src" python3 "${assets}/transform-cmdline.py" "${cmdline}"
find "${BOOTFS_DIR}" -type f ! -path "${cmdline}" -print0 |
  sort -z |
  xargs -0 sha256sum >"${boot_after}"
cmp --silent "${boot_before}" "${boot_after}" || {
  echo "non-cmdline boot/seed file changed during DashCam stage" >&2
  exit 1
}
rm -f -- "${boot_before}" "${boot_after}"

# Explicit tripwires against accidentally reviving the retired architecture.
if find "${ROOTFS_DIR}/etc/initramfs-tools" -type f -print0 2>/dev/null |
  xargs -0 grep -Il 'dashcam' | grep -q .; then
  echo "DashCam initramfs content is forbidden" >&2
  exit 1
fi
if grep -R -E -I -l 'dashcam-bounded-provision|firstboot-initramfs' \
  "${ROOTFS_DIR}/etc" "${BOOTFS_DIR}" 2>/dev/null; then
  echo "retired DashCam initramfs trigger is forbidden" >&2
  exit 1
fi

find "${ROOTFS_DIR}/opt/dashcam/wheelhouse" -maxdepth 1 -type f -delete
rmdir "${ROOTFS_DIR}/opt/dashcam/wheelhouse"
