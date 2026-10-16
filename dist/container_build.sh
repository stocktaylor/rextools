#!/usr/bin/env bash
# Runs INSIDE the Steam Runtime SDK container (started by `rex dist`).
# Builds the rexglue SDK and the project with the container's toolchain, so
# the result only depends on libraries the Steam Runtime / SteamOS provide,
# then packages a self-contained folder:
#
#   <name>/<name>           executable (RUNPATH $ORIGIN)
#   <name>/lib*.so          rexglue runtime, Tracy client, GPU plugin
#   <name>/<name>.toml      default settings, loaded automatically from the exe dir
#   <name>/game/            the user puts their extracted game files here
#
# Uses separate build dirs (…/out/steamrt/…) so host builds are untouched.
#
# Environment (set by rex dist):
#   SDK_DIR PROJECT_DIR PROJECT_NAME BUILD_TYPE MARCH DIST_DIR TAG IMAGE
# `container_build.sh package` skips the builds (packaging only; for testing).
set -euo pipefail

: "${SDK_DIR:?}" "${PROJECT_DIR:?}" "${PROJECT_NAME:?}" "${BUILD_TYPE:=RelWithDebInfo}"
: "${MARCH:=x86-64-v2}" "${DIST_DIR:?}" "${TAG:=steamrt}" "${IMAGE:=unknown}"
SDK_BUILD="${SDK_BUILD:-$SDK_DIR/out/$TAG/build}"
SDK_INSTALL="${SDK_INSTALL:-$SDK_DIR/out/$TAG/install}"
PROJ_BUILD="${PROJ_BUILD:-$PROJECT_DIR/out/$TAG}"
export HOME="${HOME:-/tmp/home}"
mkdir -p "$HOME"

step() { printf '\n=== %s\n' "$*"; }
die() { echo "container_build: $*" >&2; exit 1; }

# Both variants build CMake's *Release* configuration: the SDK only installs
# its rexglue CLI (needed by the project's codegen step) for Release.
# "RelWithDebInfo" = Release config with -O2 -g, so crash backtraces work.
case "$BUILD_TYPE" in
  RelWithDebInfo) flags="-O2 -g -DNDEBUG" ;;
  Release)        flags="-O3 -DNDEBUG" ;;
  *) die "unsupported BUILD_TYPE $BUILD_TYPE (use RelWithDebInfo or Release)" ;;
esac
CMAKE_CONFIG=Release
suffix=""   # library postfix for the Release configuration

if [ "${1:-}" != package ]; then
  step "toolchain"
  for t in clang clang++ cmake ninja; do
    command -v "$t" >/dev/null || die "the container image has no '$t' - is this a Steam Runtime *SDK* image?"
  done
  # Informational only: `| head` makes the writer die of SIGPIPE, which
  # pipefail would turn into a fatal error.
  { clang++ --version | head -1; cmake --version | head -1; ldd --version | head -1; } || true

  step "rexglue SDK ($BUILD_TYPE: $flags, -march=$MARCH) -> $SDK_INSTALL"
  cmake -S "$SDK_DIR" -B "$SDK_BUILD" -G Ninja \
    -DCMAKE_BUILD_TYPE="$CMAKE_CONFIG" \
    -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
    -DCMAKE_C_FLAGS="-march=$MARCH" -DCMAKE_CXX_FLAGS="-march=$MARCH" \
    -DCMAKE_C_FLAGS_RELEASE="$flags" -DCMAKE_CXX_FLAGS_RELEASE="$flags" \
    -DCMAKE_CXX_STANDARD=23 \
    -DCMAKE_INSTALL_PREFIX="$SDK_INSTALL"
  cmake --build "$SDK_BUILD"
  rm -rf "$SDK_INSTALL"   # no stale exports from an earlier configuration
  cmake --install "$SDK_BUILD" >/dev/null

  step "$PROJECT_NAME ($BUILD_TYPE: $flags) -> $PROJ_BUILD"
  cmake -S "$PROJECT_DIR" -B "$PROJ_BUILD" -G Ninja \
    -DCMAKE_BUILD_TYPE="$CMAKE_CONFIG" \
    -DCMAKE_CXX_COMPILER=clang++ \
    -DCMAKE_CXX_FLAGS_RELEASE="$flags" \
    -DCMAKE_PREFIX_PATH="$SDK_INSTALL" \
    -DCMAKE_FIND_PACKAGE_NO_PACKAGE_REGISTRY=ON
  cmake --build "$PROJ_BUILD"
fi

step "package -> $DIST_DIR"
bin="$PROJ_BUILD/$PROJECT_NAME"
[ -x "$bin" ] || die "no executable at $bin"
sdk_lib="$SDK_INSTALL/lib64"
[ -d "$sdk_lib" ] || sdk_lib="$SDK_INSTALL/lib"
plugin="librexgpu-xenos${suffix}.so"

rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR/game"
cp "$bin" "$DIST_DIR/"
# Ship every NEEDED library that lives in the SDK install (transitively), and
# the dlopen'd GPU plugin. Everything else must come from the system.
queue=("$DIST_DIR/$PROJECT_NAME")
[ -f "$sdk_lib/$plugin" ] || die "GPU plugin $plugin not found in $sdk_lib"
cp "$sdk_lib/$plugin" "$DIST_DIR/"
queue+=("$DIST_DIR/$plugin")
# Recompiled guest DLLs ([[modules]]) are dlopen'd from the exe dir as
# lib<project>_<module>.so when the game loads them, so nothing NEEDs them.
for mod in "$PROJ_BUILD"/lib"${PROJECT_NAME}"_*.so; do
  [ -f "$mod" ] || continue
  cp "$mod" "$DIST_DIR/"
  queue+=("$DIST_DIR/$(basename "$mod")")
done
while [ ${#queue[@]} -gt 0 ]; do
  f="${queue[0]}"; queue=("${queue[@]:1}")
  for lib in $(readelf -d "$f" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p'); do
    if [ -f "$sdk_lib/$lib" ] && [ ! -f "$DIST_DIR/$lib" ]; then
      cp -L "$sdk_lib/$lib" "$DIST_DIR/"
      queue+=("$DIST_DIR/$lib")
    fi
  done
done

# Default settings: the app loads <exe dir>/<name>.toml automatically.
# A project can ship its own as packaging/<name>.toml.
if [ -f "$PROJECT_DIR/packaging/$PROJECT_NAME.toml" ]; then
  cp "$PROJECT_DIR/packaging/$PROJECT_NAME.toml" "$DIST_DIR/"
else
  cat > "$DIST_DIR/$PROJECT_NAME.toml" <<EOF
# Default settings for $PROJECT_NAME. Any command-line flag can be set here
# (same name, without the leading --). Command-line flags still win.
gpu_plugin = "xenos"
log_level = "info"
# Present at the display refresh rate instead of redrawing as fast as possible.
vulkan_allow_present_mode_immediate = false
vulkan_allow_present_mode_mailbox = false
# One present per game frame: steady pacing, VRR-friendly, and frame-rate
# overlays (MangoHud) measure the game itself.
present_on_guest_swap = true
EOF
fi

cat > "$DIST_DIR/game/PUT_GAME_FILES_HERE.txt" <<EOF
Put your extracted game files in this folder, so that default.xex sits
directly inside it (this folder/default.xex).
EOF

# Requirements report: the newest symbol versions the binaries need.
glibc=$(objdump -T "$DIST_DIR"/"$PROJECT_NAME" "$DIST_DIR"/*.so 2>/dev/null \
        | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1 || true)
glibcxx=$(objdump -T "$DIST_DIR"/"$PROJECT_NAME" "$DIST_DIR"/*.so 2>/dev/null \
        | grep -o 'GLIBCXX_[0-9.]*' | sort -uV | tail -1 || true)
system_libs=$(for f in "$DIST_DIR/$PROJECT_NAME" "$DIST_DIR"/*.so; do
                readelf -d "$f" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p'
              done | sort -u | while read -r l; do [ -f "$DIST_DIR/$l" ] || echo "$l"; done | tr '\n' ' ')

# XBLA projects set license_mask in their toml: say which mode the build ships in.
license_note=""
license_value="$(sed -n 's/^license_mask *= *\([0-9xXa-fA-F]*\).*/\1/p' "$DIST_DIR/$PROJECT_NAME.toml" | head -1)"
if [ -n "$license_value" ]; then
  if [ $((license_value & 1)) = 1 ]; then
    license_note="
Full game: this Xbox Live Arcade title ships unlocked (license_mask = 1 in
$PROJECT_NAME.toml). Set it to 0 to play the trial version instead.
"
  else
    license_note="
Full game: this is an Xbox Live Arcade title and runs as the trial unless you
set license_mask = 1 in $PROJECT_NAME.toml (only if you own the full game).
"
  fi
fi

cat > "$DIST_DIR/README.txt" <<EOF
$PROJECT_NAME - test build
==========================

1. Put your extracted game files in the "game" folder next to this file
   (game/default.xex must exist).
2. Run ./$PROJECT_NAME

Settings:  $PROJECT_NAME.toml (next to the executable)
Logs:      logs/ (next to the executable) - include the newest one in bug reports
Saves:     ~/.local/share/$PROJECT_NAME/
$license_note
Requirements: 64-bit Linux, a Vulkan driver, glibc >= ${glibc#GLIBC_},
libstdc++ >= ${glibcxx#GLIBCXX_}, and these system libraries: $system_libs
If ./$PROJECT_NAME fails with "version GLIBC_... not found", the system is too
old to run it directly: add it to Steam as a non-Steam game and, under
Properties > Compatibility, force the Steam Linux Runtime that matches the
build ($TAG), which provides these libraries.
EOF

cat > "$DIST_DIR/BUILD_INFO.txt" <<EOF
project:     $PROJECT_NAME
built:       $(date -u +%Y-%m-%dT%H:%M:%SZ)
build type:  $BUILD_TYPE  ($flags, -march=$MARCH)
image:       $IMAGE
toolchain:   $(clang++ --version 2>/dev/null | head -1 || echo unknown)
sdk:         $(git -C "$SDK_DIR" describe --tags --always --dirty 2>/dev/null || echo unknown)
needs:       ${glibc:-?} ${glibcxx:-?}
system libs: $system_libs
EOF

tarball="$DIST_DIR.tar.gz"
tar -czf "$tarball" -C "$(dirname "$DIST_DIR")" "$(basename "$DIST_DIR")"

step "done"
ls -la "$DIST_DIR"
echo
cat "$DIST_DIR/BUILD_INFO.txt"
echo
echo "Folder:  $DIST_DIR"
echo "Archive: $tarball"
