#!/usr/bin/env bash
set -euo pipefail

# Tauri 2.11.2 creates absolute .DirIcon and .desktop symlinks in its AppDir.
# Replace them with files and rebuild before publishing so they remain usable
# when the AppImage is mounted outside the build machine.
BUNDLE_DIR="$(realpath "${1:?Usage: $0 <bundle-directory> <x86_64|aarch64>}")"
ARCH="${2:?Usage: $0 <bundle-directory> <x86_64|aarch64>}"
export ARCH

case "$ARCH" in
  x86_64) tool_sha256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0" ;;
  aarch64) tool_sha256="f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158" ;;
  *) echo "Unsupported AppImage architecture: $ARCH" >&2; exit 1 ;;
esac

shopt -s nullglob
appdirs=("$BUNDLE_DIR"/*.AppDir)
appimages=("$BUNDLE_DIR"/*.AppImage "$BUNDLE_DIR"/*.appimage)
if [ "${#appdirs[@]}" -ne 1 ] || [ "${#appimages[@]}" -ne 1 ]; then
  echo "Expected exactly one AppDir and one AppImage in $BUNDLE_DIR" >&2
  exit 1
fi
appdir="${appdirs[0]}"
appimage="${appimages[0]}"

desktop_sources=("$appdir"/usr/share/applications/*.desktop)
desktop_entries=("$appdir"/*.desktop)
if [ "${#desktop_sources[@]}" -ne 1 ] || [ "${#desktop_entries[@]}" -ne 1 ]; then
  echo "Expected exactly one application desktop entry and one root desktop entry" >&2
  exit 1
fi
desktop_name="$(basename "${desktop_entries[0]}")"
cp --remove-destination -- "${desktop_sources[0]}" "${desktop_entries[0]}"
desktop-file-validate "${desktop_entries[0]}"
icon_name="$(awk '
  /^\[Desktop Entry\]/ { entry = 1; next }
  /^\[/ { entry = 0 }
  entry && /^Icon=/ { sub(/^Icon=/, ""); print; exit }
' "${desktop_entries[0]}")"
if [[ -z "$icon_name" || "$icon_name" == */* || "$icon_name" == *.png ]]; then
  echo "Desktop Icon must be a name without a path or file extension: $icon_name" >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
icon_source="$script_dir/../../desktop/src-tauri/icons/128x128@2x.png"
cp --remove-destination -- "$icon_source" "$appdir/$icon_name.png"
cp --remove-destination -- "$icon_source" "$appdir/.DirIcon"

work_dir="$(mktemp -d "$BUNDLE_DIR/.appimage-finalize.XXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT

# Reuse the runtime Tauri shipped; appimagetool then needs no runtime download.
offset="$("$appimage" --appimage-offset)"
if [[ ! "$offset" =~ ^[1-9][0-9]*$ ]]; then
  echo "Invalid AppImage filesystem offset: $offset" >&2
  exit 1
fi
head -c "$offset" -- "$appimage" > "$work_dir/runtime"

# A local copy can be supplied for offline builds. Verify either copy against
# the published SHA-256 of the pinned official appimagetool release.
tool="${APPIMAGETOOL:-$work_dir/appimagetool.AppImage}"
if [ -z "${APPIMAGETOOL:-}" ]; then
  curl --fail --location --retry 3 --output "$tool" \
    "https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-$ARCH.AppImage"
fi
printf '%s  %s\n' "$tool_sha256" "$tool" | sha256sum --check
chmod +x "$tool"
"$tool" --appimage-extract-and-run --no-appstream --comp zstd \
  --runtime-file "$work_dir/runtime" "$appdir" "$work_dir/rebuilt.AppImage"

# Inspect the actual SquashFS at a new path. Checking the original AppDir alone
# would miss links which only work while the build directory still exists.
rebuilt_offset="$("$work_dir/rebuilt.AppImage" --appimage-offset)"
unsquashfs -no-progress -o "$rebuilt_offset" -d "$work_dir/check" \
  "$work_dir/rebuilt.AppImage" AppRun .DirIcon "$desktop_name" "$icon_name.png"
for entry in AppRun .DirIcon "$desktop_name" "$icon_name.png"; do
  if [ ! -f "$work_dir/check/$entry" ] || [ -L "$work_dir/check/$entry" ]; then
    echo "Missing or linked AppImage root file: $entry" >&2
    exit 1
  fi
done
test -x "$work_dir/check/AppRun"
desktop-file-validate "$work_dir/check/$desktop_name"
for icon in .DirIcon "$icon_name.png"; do
  if [ "$(file --brief --mime-type "$work_dir/check/$icon")" != "image/png" ]; then
    echo "AppImage icon is not a PNG: $icon" >&2
    exit 1
  fi
done
mv -- "$work_dir/rebuilt.AppImage" "$appimage"
echo "Rebuilt and verified AppImage: $appimage"
