# rex run [game args...]
# Runs the game with the SDK on LD_LIBRARY_PATH and the flags every project
# needs. --gpu_plugin is required: without it you get a window with no video.
# Each default is only added if you didn't pass that flag yourself (the cvar
# parser rejects duplicates), e.g. `rex run --log_level trace` overrides info.
# Logs land in out/build/<preset>/logs/<name>_NNN.log (see `rex log`).
# Presents once per guest frame (--present_on_guest_swap) unless
# REX_PRESENT_ON_GUEST_SWAP=false.
# rex run --renderdoc [game args...]
# Runs under RenderDoc (renderdoccmd capture); press F12 in game to capture a
# frame into _tools/captures/. Forces an X11 window (RenderDoc has no Wayland
# support: without it the window never appears) and keeps vsync presentation
# (uncapped presents make most captures empty re-presents of the last frame).
# Afterwards lists the captures and flags the empty ones; analyse with rex rdc.
load_project
[ -x "$BIN" ] || die "no binary at $BIN (run 'rex build')"
renderdoc=0
args=()
for a in "$@"; do
  if [ "$a" = "--renderdoc" ]; then renderdoc=1; else args+=("$a"); fi
done
set -- "${args[@]+"${args[@]}"}"
sync_gpu_plugin
export LD_LIBRARY_PATH="$SDK_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

user_has() {  # --name or --name=value among the user's args
  local a
  for a in "${user_args[@]}"; do
    case "$a" in --"$1"|--"$1"=*) return 0 ;; esac
  done
  return 1
}
user_args=("$@")
defaults=()
user_has game_data_root     || defaults+=(--game_data_root "$GAME_ROOT")
user_has gpu_plugin         || defaults+=(--gpu_plugin "${REX_GPU:-xenos}")
user_has log_level          || defaults+=(--log_level "${REX_LOG_LEVEL:-info}")
user_has log_flush_interval || defaults+=(--log_flush_interval 1)
# Present at the display's refresh rate (FIFO) instead of redrawing the window
# as fast as possible (thousands of FPS, ~0.3 core + GPU for nothing).
# REX_PRESENT=uncapped restores the SDK default.
# One host present per guest frame (SDK present_on_guest_swap): frame-rate
# tools like MangoHud then measure the game, and VRR displays pace it exactly.
# REX_PRESENT_ON_GUEST_SWAP=false restores the decoupled UI-thread presenter.
user_has present_on_guest_swap || defaults+=(--present_on_guest_swap="${REX_PRESENT_ON_GUEST_SWAP:-true}")
if [ "${REX_PRESENT:-vsync}" = vsync ]; then
  user_has vulkan_allow_present_mode_immediate || defaults+=(--vulkan_allow_present_mode_immediate=false)
  user_has vulkan_allow_present_mode_mailbox   || defaults+=(--vulkan_allow_present_mode_mailbox=false)
fi
if [ "$renderdoc" = 1 ]; then
  command -v renderdoccmd >/dev/null || die "renderdoccmd not found (sudo dnf install renderdoc)"
  [ "${REX_PRESENT:-vsync}" = vsync ] || note "warning: REX_PRESENT=uncapped makes most captures empty"
  cap_dir="$PROJECT_DIR/_tools/captures"
  mkdir -p "$cap_dir"
  marker="$(mktemp)"
  echo "Running under RenderDoc - press F12 in game to capture a frame."
  SDL_VIDEO_DRIVER=x11 renderdoccmd capture --working-dir "$PROJECT_DIR" \
    --capture-file "$cap_dir/${PROJECT_NAME}_$(date +%Y%m%d_%H%M%S)" \
    "$BIN" "${defaults[@]}" "$@"
  echo
  new=$(find "$cap_dir" -maxdepth 1 -name '*.rdc' -newer "$marker" 2>/dev/null)
  rm -f "$marker"
  if [ -z "$new" ]; then
    echo "No captures taken."
  else
    echo "Captures from this run:"
    "$REXTOOLS_DIR/rex" rdc list | grep -F -f <(echo "$new" | xargs -n1 basename)
    echo "Next: rex rdc dump <capture.rdc>"
  fi
  exit 0
fi
exec "$BIN" "${defaults[@]}" "$@"
