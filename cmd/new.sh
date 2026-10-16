# rex new <name> <xex_path> [--game-root DIR]
# Bootstraps a rexglue project in $PWD, covering the traps `rexglue init`
# leaves behind:
#  - init refuses to run if the manifest exists, and --force would overwrite
#    a hand-written one: an existing <name>_manifest.toml is kept aside and
#    restored;
#  - init omits GPU_PLUGINS, so the game can't load its renderer: added;
#  - manifest gets a header identifying the XEX and a trailing
#    [entrypoint.functions] table for `rex triage`;
#  - .gitignore and git init.
name="${1:-}"; xex="${2:-}"
[ -n "$name" ] && [ -n "$xex" ] || die "usage: rex new <name> <xex_path> [--game-root DIR]"
shift 2
game_root_args=()
if [ "${1:-}" = "--game-root" ]; then
  [ -n "${2:-}" ] || die "--game-root needs a directory"
  game_root_args=(--game-root "$2")
fi
[ -f "$xex" ] || die "XEX not found: $xex"
[ -x "$REXGLUE_BIN" ] || die "rexglue CLI not found at $REXGLUE_BIN (build/install the SDK, or set REXGLUE_SDK)"

manifest="${name}_manifest.toml"
kept=""
if [ -f "$manifest" ]; then
  kept="$manifest.rex-keep"
  mv "$manifest" "$kept"
  note "keeping existing $manifest aside during init"
fi
LD_LIBRARY_PATH="$SDK_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "$REXGLUE_BIN" init --project-name "$name" --xex-path "$xex" "${game_root_args[@]}"
rc=$?
if [ -n "$kept" ]; then
  mv -f "$kept" "$manifest"
  note "restored your $manifest"
fi
[ "$rc" = 0 ] || die "rexglue init failed"

sed -i -E "s/^rexglue_setup_target\(([A-Za-z0-9_]+)\)\$/rexglue_setup_target(\1 GPU_PLUGINS xenos)/" CMakeLists.txt
grep -q 'GPU_PLUGINS' CMakeLists.txt || note "warning: could not add GPU_PLUGINS xenos to CMakeLists.txt; add it by hand"

[ -z "$kept" ] && $REX_PY "$REXTOOLS_DIR/py/newproject.py" "$manifest" "$xex"

touch .gitignore
game_dir="$(dirname "$xex")"
for line in "$game_dir/" out/ tmp/ _tools/logs/; do
  grep -qxF "$line" .gitignore || echo "$line" >> .gitignore
done
git rev-parse --git-dir >/dev/null 2>&1 || git init -q && note "git repository ready"

cat <<EOF

Project '$name' created. Next:
  rex build          # first build: configure -> codegen -> configure -> build -> lint
                     # fix any 'UnresolvedCall' codegen errors by adding the
                     # targets to [entrypoint.functions] in $manifest
  rex triage         # run/auto-fix loop until it gets past init
  rex sdk check      # if anything looks off with the SDK install
EOF
