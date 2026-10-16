# Shared setup for rextools commands. Sourced by `rex`, not run directly.
#
# Project discovery: $REX_PROJECT, else the nearest directory at or above
# $PWD containing a rexglue manifest (*.toml with a [project] table).
#
# Overridable environment:
#   REXGLUE_SDK    SDK checkout          (default: <rextools>/../rexglue-sdk)
#   REX_PRESET     game build preset     (default: linux-amd64-relwithdebinfo)
#   REX_LOG_LEVEL  run log level         (default: info; trace is very slow)
#   REX_GPU        GPU plugin            (default: xenos)
#   REX_PRESENT    vsync | uncapped      (default: vsync; uncapped redraws at 1000s of FPS)

REX_PY="python3 -I -B"
PRESET="${REX_PRESET:-linux-amd64-relwithdebinfo}"
SDK_DIR="${REXGLUE_SDK:-$(cd "$REXTOOLS_DIR/.." && pwd)/rexglue-sdk}"
SDK_INSTALL="$SDK_DIR/out/install/linux-amd64"
SDK_LIB="$SDK_INSTALL/lib64"
REXGLUE_BIN="$SDK_INSTALL/bin/rexglue"

die() { echo "rex: $*" >&2; exit 1; }
note() { echo "rex: $*" >&2; }

find_project_dir() {
  if [ -n "${REX_PROJECT:-}" ]; then
    (cd "$REX_PROJECT" && pwd); return
  fi
  local d="$PWD"
  while [ "$d" != "/" ]; do
    for f in "$d"/*.toml; do
      [ -f "$f" ] && grep -q '^\[project\]' "$f" && { echo "$d"; return; }
    done
    d="$(dirname "$d")"
  done
  return 1
}

# Loads PROJECT_DIR PROJECT_NAME MANIFEST GAME_ROOT GEN_DIR FUNCS_FILE and
# derived build paths. Commands that need a project call this first.
load_project() {
  local dir
  dir="$(find_project_dir)" || die "not inside a rexglue project (no *_manifest.toml found); cd into one or set REX_PROJECT"
  eval "$($REX_PY "$REXTOOLS_DIR/py/rexinfo.py" "$dir")" || die "could not read manifest in $dir"
  BUILD_DIR="$PROJECT_DIR/out/build/$PRESET"
  BIN="$BUILD_DIR/$PROJECT_NAME"
  LOG_DIR="$BUILD_DIR/logs"
  OUT_DIR="$PROJECT_DIR/_tools/logs"
  mkdir -p "$OUT_DIR"
  cd "$PROJECT_DIR" || die "cannot cd to $PROJECT_DIR"
}

# Highest run number N among $LOG_DIR/<name>_N[.k].log, or empty.
latest_run_number() {
  find "$LOG_DIR" -maxdepth 1 -name "${PROJECT_NAME}_*.log" -printf '%f\n' 2>/dev/null \
    | sed -n "s/^${PROJECT_NAME}_\([0-9]\+\)\(\.[0-9]\+\)\?\.log\$/\1/p" | sort -n | tail -1
}

# Print one run's log with rotated segments merged in time order
# (<name>_N.K.log is older for larger K; <name>_N.log is newest).
cat_run_log() {
  local n="$1"
  local segs
  segs=$(find "$LOG_DIR" -maxdepth 1 -name "${PROJECT_NAME}_${n}.*.log" -printf '%f\n' \
         | sed -n "s/^${PROJECT_NAME}_${n}\.\([0-9]\+\)\.log\$/\1/p" | sort -rn)
  for k in $segs; do cat "$LOG_DIR/${PROJECT_NAME}_${n}.${k}.log"; done
  [ -f "$LOG_DIR/${PROJECT_NAME}_${n}.log" ] && cat "$LOG_DIR/${PROJECT_NAME}_${n}.log"
}

mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }

# The SDK copies GPU plugins next to the game binary in a POST_BUILD step,
# which only runs when the game relinks. The game never links the plugin
# (it's dlopen'd), so an SDK reinstall leaves a stale copy that the game keeps
# loading. Refresh it whenever the installed plugin differs.
sync_gpu_plugin() {
  local suffix=""
  case "$PRESET" in
    *relwithdebinfo) suffix="rd" ;;
    *debug) suffix="d" ;;
  esac
  local name="librexgpu-${REX_GPU:-xenos}${suffix}.so"
  local src="$SDK_LIB/$name" dst="$BUILD_DIR/$name"
  [ -f "$src" ] && [ -d "$BUILD_DIR" ] || return 0
  if ! cmp -s "$src" "$dst"; then
    cp -f "$src" "$dst" && note "refreshed stale GPU plugin $name from the SDK install"
  fi
}

# append_function <addr> <comment>
# Adds <addr> = {} to the functions table of the binary (entrypoint or a
# [[modules]] DLL) whose address range holds it, matching the file's key style.
# Returns 1 (with a message) if no binary owns it, 2 if it is already listed.
# Sets APPENDED_TO to the file written.
append_function() {
  local addr comment file key
  addr="$(printf '0x%08X' "$1")"; comment="$2"
  file="$($REX_PY "$REXTOOLS_DIR/py/rexinfo.py" "$PROJECT_DIR" --module-for "$addr" | cut -f2)"
  APPENDED_TO="$file"
  if [ -z "$file" ] || [ "$file" = - ]; then
    echo "$addr: no recompiled binary with a functions table holds this address:"
    $REX_PY "$REXTOOLS_DIR/py/rexinfo.py" "$PROJECT_DIR" --modules
    echo "If it is outside every range above, the code belongs to a module that is not in"
    echo "the manifest (or was loaded from a guest path that doesn't match its guest_path)."
    echo "Otherwise give that binary a functions table: the manifest's last table, or an"
    echo "include file whose last table is [functions]."
    return 1
  fi
  # Either key style: 0x8210EB48 = {} or "0x8210EB48" = {}
  grep -qiE "^\"?$addr\"? *=" "$file" && return 2
  key="$addr"
  grep -qE '^"0x[0-9A-Fa-f]+" *=' "$file" && key="\"$addr\""
  printf '\n# %s\n%s = {}\n' "$comment" "$key" >> "$file"
}
