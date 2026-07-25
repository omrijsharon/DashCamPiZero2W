#!/bin/bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: prepare-stage.sh REPOSITORY NEW_STAGE_DIR WHEELHOUSE" >&2
  exit 2
fi

repository="$(realpath "$1")"
new_stage="$(realpath -m "$2")"
wheelhouse="$(realpath "$3")"
template="$(cd "$(dirname "${BASH_SOURCE[0]}")/pi-gen-stage" && pwd)"

test -d "${repository}/.git"
test -f "${repository}/uv.lock"
test -f "${repository}/deploy/bootstrap/image/build-requirements.json"
test -f "${repository}/deploy/bootstrap/image/source.json"
test -d "${repository}/deploy/bootstrap/storage"
test -d "${repository}/deploy/bootstrap/network"
test -d "${wheelhouse}"
if [ -e "${new_stage}" ]; then
  echo "new stage path already exists: ${new_stage}" >&2
  exit 2
fi
case "${new_stage}" in
  /dev/*|/sys/*|/proc/*) echo "device/kernel path refused: ${new_stage}" >&2; exit 2 ;;
esac

git -C "${repository}" diff --quiet
git -C "${repository}" diff --cached --quiet
test -z "$(git -C "${repository}" ls-files --others --exclude-standard)"
app_commit="$(git -C "${repository}" rev-parse --verify HEAD)"
case "${app_commit}" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
  *) echo "full Git commit is required" >&2; exit 2 ;;
esac
test "${#app_commit}" -eq 40

for input in \
  "${repository}/deploy/bootstrap/storage" \
  "${repository}/deploy/bootstrap/network" \
  "${wheelhouse}"; do
  if find "${input}" \( -type l -o -type b -o -type c -o -type p -o -type s \) \
    -print -quit | grep -q .; then
    echo "special or symbolic build input refused: ${input}" >&2
    exit 2
  fi
done

cp -a --no-dereference "${template}" "${new_stage}"
assets="${new_stage}/files"
rm -f "${assets}/README.md"
install -d -m 0755 \
  "${assets}/repository" \
  "${assets}/wheelhouse" \
  "${assets}/build-metadata"
git -C "${repository}" archive --format=tar "${app_commit}" |
  tar -x --no-same-owner --no-same-permissions -C "${assets}/repository"
cp -a --no-dereference "${wheelhouse}/." "${assets}/wheelhouse/"
cp -a --no-dereference \
  "${repository}/deploy/bootstrap/storage" \
  "${repository}/deploy/bootstrap/network" \
  "${assets}/"
install -m 0644 "${repository}/uv.lock" "${assets}/build-metadata/uv.lock"
install -m 0644 \
  "${repository}/deploy/bootstrap/image/build-requirements.json" \
  "${assets}/build-metadata/build-requirements.json"
install -m 0644 \
  "${repository}/deploy/bootstrap/image/source.json" \
  "${assets}/build-metadata/official-source.json"
install -m 0755 \
  "${repository}/deploy/bootstrap/image/transform-cmdline.py" \
  "${assets}/transform-cmdline.py"

lock_hash="$(sha256sum "${repository}/uv.lock" | cut -d ' ' -f 1)"
mapfile -t app_wheels < <(
  find "${assets}/wheelhouse" -maxdepth 1 -type f \
    -name 'dashcam_pizero2w-*.whl' -printf '%f\n' | sort
)
[ "${#app_wheels[@]}" -eq 1 ] || {
  echo "wheelhouse must contain exactly one DashCam application wheel" >&2
  exit 2
}
app_wheel_hash="$(
  sha256sum "${assets}/wheelhouse/${app_wheels[0]}" | cut -d ' ' -f 1
)"
printf '%s  %s\n' \
  "${app_wheel_hash}" \
  "wheelhouse/${app_wheels[0]}" \
  > "${assets}/build-metadata/app-wheel.sha256"
PYTHONPATH="${repository}/src" python3 - \
  "${assets}" "${app_commit}" "${lock_hash}" "${app_wheel_hash}" <<'PY'
import sys
from pathlib import Path

from dashcam.provisioning.bootstrap_image import (
    SourceMetadata,
    package_inventory_bytes,
)

assets = Path(sys.argv[1])
metadata = SourceMetadata(
    app_commit=sys.argv[2],
    package_lock_sha256=sys.argv[3],
    app_wheel_sha256=sys.argv[4],
)
(assets / "build-metadata/source.json").write_bytes(metadata.canonical_bytes())
(assets / "build-metadata/package-inventory.json").write_bytes(package_inventory_bytes())
PY

(
  cd "${assets}"
  find . -type f \
    ! -path './READY' \
    ! -path './build-metadata/stage-files.sha256' \
    -print0 |
    sort -z |
    xargs -0 sha256sum > build-metadata/stage-files.sha256
)
stage_hash="$(sha256sum "${assets}/build-metadata/stage-files.sha256" | cut -d ' ' -f 1)"
printf '%s\n' "${stage_hash}" > "${assets}/READY"
