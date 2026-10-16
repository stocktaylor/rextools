# rex build [--force-codegen]
# codegen -> configure -> build -> lint, in the order rexglue needs:
#  - the <name>_codegen target only exists once the build dir is configured,
#  - generated/<mod>/sources.cmake is only picked up at configure time, so a
#    configure must follow every codegen (else: undefined PPCImageConfig),
#  - codegen's stamp ignores the SDK itself, so after an SDK codegen change
#    it would report "up to date" and keep the old output.
load_project
force=0
for a in "$@"; do
  case "$a" in
    --force-codegen) force=1 ;;
    *) die "build: unknown option '$a'" ;;
  esac
done

stamp="$GEN_DIR/codegen.stamp"
codegen_lib="$SDK_LIB/librexruntime.so"   # the rexglue CLI links the Release runtime
if [ -f "$stamp" ] && [ "$(mtime "$codegen_lib")" -gt "$(mtime "$stamp")" ]; then
  note "SDK Release runtime is newer than the last codegen; forcing codegen"
  force=1
fi
# One stamp per binary (entrypoint and each [[modules]] DLL).
[ "$force" = 1 ] && find "$PROJECT_DIR/generated" -maxdepth 2 -name codegen.stamp -delete 2>/dev/null

grep -q 'GPU_PLUGINS' CMakeLists.txt 2>/dev/null ||
  note "warning: CMakeLists.txt has no GPU_PLUGINS (rexglue_setup_target(... GPU_PLUGINS xenos)); the game will fail to load its GPU plugin"

if [ ! -f "$BUILD_DIR/CMakeCache.txt" ]; then
  cmake --preset "$PRESET" -DREXGLUE_RECOMP_DEBUG_INFO=full || die "initial configure failed"
fi
# An empty CMAKE_CXX_FLAGS_<CONFIG> in the cache (seen on Ridge after its
# first configure) silently builds everything at -O0 - several times slower.
build_type="$(sed -n 's/^CMAKE_BUILD_TYPE:STRING=//p' "$BUILD_DIR/CMakeCache.txt")"
bt_upper="$(echo "$build_type" | tr '[:lower:]' '[:upper:]')"
case "$bt_upper" in
  RELWITHDEBINFO) want_flags="-O2 -g -DNDEBUG" ;;
  RELEASE) want_flags="-O3 -DNDEBUG" ;;
  *) want_flags="" ;;
esac
cur_flags="$(sed -n "s/^CMAKE_CXX_FLAGS_${bt_upper}:STRING=//p" "$BUILD_DIR/CMakeCache.txt")"
if [ -n "$want_flags" ] && ! echo "$cur_flags" | grep -q -- '-O[123s]'; then
  note "CMAKE_CXX_FLAGS_${bt_upper} is '${cur_flags}' (no optimization) - resetting to '$want_flags'; expect a full rebuild"
  cmake --preset "$PRESET" -DREXGLUE_RECOMP_DEBUG_INFO=full \
    "-DCMAKE_CXX_FLAGS_${bt_upper}=$want_flags" "-DCMAKE_C_FLAGS_${bt_upper}=$want_flags" >/dev/null ||
    die "reconfigure with optimization flags failed"
fi
# Codegen's Validate phase stops on branch targets that function discovery
# missed ("UnresolvedCall: b 0xX from 0xY - target not in any function", a
# tail-branch-only target). Add each to its binary's functions table and
# re-run. Codegen stops at the first failing binary, so a DLL's batch only
# appears once the entrypoint is clean.
codegen_log="$OUT_DIR/codegen.txt"
for ((pass = 1; ; pass++)); do
  cmake --build --preset "$PRESET" --target "${PROJECT_NAME}_codegen" 2>&1 | tee "$codegen_log"
  [ "${PIPESTATUS[0]}" = 0 ] && break
  targets="$(sed -nE 's/^ +(0x[0-9A-Fa-f]{8}) from 0x[0-9A-Fa-f]{8}: .* target not in any function$/\1/p' "$codegen_log" | sort -u)"
  [ -n "$targets" ] || die "codegen failed (output: $codegen_log)"
  [ "$pass" -le 20 ] || die "codegen still reports unresolved calls after 20 passes (output: $codegen_log)"
  added=0
  for t in $targets; do
    from="$(grep -oE "^ +$t from 0x[0-9A-Fa-f]{8}" "$codegen_log" | head -1 | awk '{print $3}')"
    append_function "$t" "Added by rex build: codegen UnresolvedCall, tail-branch target (b from $from)."
    case $? in
      0) added=$((added + 1)); note "added $t to $(basename "$APPENDED_TO")" ;;
      1) die "codegen: unresolved call target $t is in no binary's range" ;;
      2) note "$t is already in $(basename "$APPENDED_TO") but codegen still can't place it; it may be mid-function (rex containing $t)" ;;
    esac
  done
  [ "$added" -gt 0 ] || die "codegen failed; no new unresolved-call targets to add (output: $codegen_log)"
  note "added $added function(s); re-running codegen (pass $((pass + 1)))"
done
cmake --preset "$PRESET" -DREXGLUE_RECOMP_DEBUG_INFO=full >/dev/null || die "configure failed"
cmake --build --preset "$PRESET" || die "build failed"
sync_gpu_plugin

echo "--- lint ---"
$REX_PY "$REXTOOLS_DIR/py/lint.py" "$PROJECT_DIR" || note "lint found likely codegen misreads (see above)"
exit 0
