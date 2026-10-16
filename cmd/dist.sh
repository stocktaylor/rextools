# rex dist [--image IMG] [--config RelWithDebInfo|Release] [--march x86-64-v2|v3]
#          [--no-pull] [--shell]
# Builds the SDK and the project inside a Steam Runtime SDK container and
# packages a folder you can copy to any Linux machine (e.g. a Steam Deck):
#   dist/<name>-<tag>/<name>   + libs + <name>.toml + game/ + README.txt
# The user puts their extracted game files in game/ and runs ./<name>.
#
# Defaults: Steam Runtime 4 SDK image, RelWithDebInfo (keeps symbols for crash
# backtraces), -march=x86-64-v2. The Steam Deck also handles x86-64-v3.
# --shell drops into the container instead of building (for debugging).
#
# Container engine: podman if available (rootless, files stay yours), else
# docker (run as your uid). Override with REX_CONTAINER=docker|podman.
load_project

image="${REX_DIST_IMAGE:-registry.gitlab.steamos.cloud/steamrt/steamrt4/sdk:latest}"
config=RelWithDebInfo
march=x86-64-v2
pull=1
shell=0
while [ $# -gt 0 ]; do
  case "$1" in
    --image) image="$2"; shift 2 ;;
    --config) config="$2"; shift 2 ;;
    --march) march="$2"; shift 2 ;;
    --no-pull) pull=0; shift ;;
    --shell) shell=1; shift ;;
    *) die "dist: unknown option '$1' (see the top of cmd/dist.sh)" ;;
  esac
done

engine="${REX_CONTAINER:-}"
if [ -z "$engine" ]; then
  if command -v podman >/dev/null; then engine=podman
  elif command -v docker >/dev/null; then engine=docker
  else die "need podman or docker (sudo dnf install podman)"; fi
fi
[ -f "$GEN_DIR/sources.cmake" ] || die "no generated code yet - run 'rex build' once first"
[ -d "$SDK_DIR" ] || die "SDK checkout not found at $SDK_DIR"

# Short tag for directory names: steamrt4, sniper, ... from the image path.
tag="$(echo "$image" | sed -E 's#.*/steamrt/([^/]+)/sdk.*#\1#; s#[:/].*##')"
[ -n "$tag" ] || tag=steamrt
dist_dir="$PROJECT_DIR/dist/${PROJECT_NAME}-${tag}"

if [ "$pull" = 1 ]; then
  echo "Pulling $image ..."
  "$engine" pull "$image" || die "could not pull $image.
If the image path is wrong, find the current one in Valve's steamrt docs and
pass --image, or set REX_DIST_IMAGE. Steam Runtime 3 (older glibc, wider
compatibility) is registry.gitlab.steamos.cloud/steamrt/sniper/sdk:latest"
fi

# A new image changes every system header's mtime, and clang then rejects the
# container builds' precompiled headers ("file '/usr/include/stdint.h' has been
# modified since the precompiled header was built"); ninja doesn't track files
# inside the image, so it never rebuilds them. Start the container build trees
# afresh whenever the image differs from the one they were built with.
image_id="$("$engine" image inspect --format '{{.Id}}' "$image" 2>/dev/null)"
if [ -n "$image_id" ]; then
  for build_tree in "$SDK_DIR/out/$tag/build" "$PROJECT_DIR/out/$tag"; do
    stamp="$build_tree/.rex-dist-image"
    if [ -d "$build_tree" ] && [ "$(cat "$stamp" 2>/dev/null)" != "$image_id" ]; then
      note "container image changed: removing stale build tree $build_tree"
      rm -rf "$build_tree"
    fi
    mkdir -p "$build_tree"
    echo "$image_id" > "$stamp"
  done
fi

user_args=()
if [ "$engine" = podman ]; then
  user_args=(--userns=keep-id)
else
  user_args=(--user "$(id -u):$(id -g)")
fi

run_args=(run --rm -i
  --security-opt label=disable          # SELinux: don't relabel your source tree
  "${user_args[@]}"
  -e HOME=/tmp/home
  -e SDK_DIR="$SDK_DIR" -e PROJECT_DIR="$PROJECT_DIR" -e PROJECT_NAME="$PROJECT_NAME"
  -e BUILD_TYPE="$config" -e MARCH="$march" -e DIST_DIR="$dist_dir"
  -e TAG="$tag" -e IMAGE="$image"
  -v "$SDK_DIR:$SDK_DIR" -v "$PROJECT_DIR:$PROJECT_DIR" -v "$REXTOOLS_DIR:$REXTOOLS_DIR"
  -w "$PROJECT_DIR")
# Projects using the shared launch menu build it from ../rexlauncher.
rexlauncher_dir="$(dirname "$REXTOOLS_DIR")/rexlauncher"
if [ -d "$rexlauncher_dir" ]; then
  run_args+=(-v "$rexlauncher_dir:$rexlauncher_dir")
fi

if [ "$shell" = 1 ]; then
  exec "$engine" "${run_args[@]}" -t "$image" bash
fi
echo "Building in $image with $engine (first run builds the whole SDK: several minutes) ..."
"$engine" "${run_args[@]}" "$image" bash "$REXTOOLS_DIR/dist/container_build.sh" ||
  die "container build failed (see above). 'rex dist --shell' opens the same container to investigate."
