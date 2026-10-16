# rex sdk check
#   Is the installed SDK built from the current sources? The SDK build is
#   multi-config: the rexglue CLI (codegen) links the *Release* runtime
#   (librexruntime.so) while games link the *RelWithDebInfo* one
#   (librexruntimerd.so) - each needs its own install after a source change.
# rex sdk patches status|apply
#   Local fixes kept as rextools/sdk-patches/*.patch, so a fresh SDK clone
#   (or `git checkout .`) can get them back.
# rex sdk patches export <name> <file>...
#   Save the SDK's current diff of the given files as sdk-patches/<name>.patch.
PATCH_DIR="$REXTOOLS_DIR/sdk-patches"
[ -d "$SDK_DIR/.git" ] || die "SDK checkout not found at $SDK_DIR (set REXGLUE_SDK)"

newest_src() {  # newest mtime among SDK sources under the given dirs
  find "$@" -type f \( -name '*.cpp' -o -name '*.h' -o -name '*.inc' -o -name '*.inja' \) \
    -printf '%T@\n' 2>/dev/null | sort -n | tail -1 | cut -d. -f1
}

sdk_check() {
  cd "$SDK_DIR" || exit 1
  local all codegen ok=1
  all="$(newest_src src include resources)"
  codegen="$(newest_src src/codegen include/rex/codegen resources/templates)"
  check_lib() {  # lib, sources-mtime, preset, purpose
    local t; t="$(mtime "$SDK_LIB/$1")"
    if [ "$t" = 0 ]; then
      echo "  MISSING  $1 ($4)"; ok=0
    elif [ "${2:-0}" -gt "$t" ]; then
      echo "  STALE    $1 ($4) - run: cmake --build --preset $3 --target install"; ok=0
    else
      echo "  ok       $1 ($4)"
    fi
  }
  echo "SDK: $SDK_DIR"
  check_lib librexruntime.so "$codegen" linux-amd64-release "rexglue CLI / codegen"
  check_lib librexruntimerd.so "$all" linux-amd64-relwithdebinfo "games, relwithdebinfo"
  # Only matters for projects built with a debug preset (REX_PRESET).
  case "$PRESET" in
    *debug) check_lib librexruntimed.so "$all" linux-amd64-debug "games, debug" ;;
  esac
  echo
  echo "Local changes vs $(git describe --tags --always 2>/dev/null):"
  git status --short -- src include resources | sed 's/^/  /'
  echo
  patches_status
  [ "$ok" = 1 ]
}

patches_status() {
  shopt -s nullglob
  local p any=0
  for p in "$PATCH_DIR"/*.patch; do
    any=1
    if git -C "$SDK_DIR" apply --check -R "$p" 2>/dev/null; then
      echo "  applied      $(basename "$p")"
    elif git -C "$SDK_DIR" apply --check "$p" 2>/dev/null; then
      echo "  NOT APPLIED  $(basename "$p")   (rex sdk patches apply)"
    else
      echo "  CONFLICT     $(basename "$p")   (SDK changed underneath it; re-apply by hand)"
    fi
  done
  [ "$any" = 1 ] || echo "  (no patches in $PATCH_DIR)"
}

case "${1:-check} ${2:-}" in
  "check "*)
    sdk_check ;;
  "patches status"|"patches ")
    echo "Patches in $PATCH_DIR:"; patches_status ;;
  "patches apply")
    for p in "$PATCH_DIR"/*.patch; do
      if git -C "$SDK_DIR" apply --check -R "$p" 2>/dev/null; then
        echo "already applied: $(basename "$p")"
      elif git -C "$SDK_DIR" apply "$p"; then
        echo "applied: $(basename "$p")"
      else
        echo "FAILED:  $(basename "$p")"
      fi
    done
    echo "Rebuild and install both configs: release (codegen) and relwithdebinfo (games)." ;;
  "patches export")
    name="${3:-}"; [ -n "$name" ] && [ $# -ge 4 ] || die "usage: rex sdk patches export <name> <file>..."
    shift 3
    git -C "$SDK_DIR" diff -- "$@" > "$PATCH_DIR/$name.patch"
    [ -s "$PATCH_DIR/$name.patch" ] || { rm -f "$PATCH_DIR/$name.patch"; die "no changes in those files"; }
    echo "wrote $PATCH_DIR/$name.patch" ;;
  *)
    die "usage: rex sdk check | rex sdk patches status|apply|export" ;;
esac
