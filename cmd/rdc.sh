# rex rdc list
#   Captures in _tools/captures, with size; small ones are flagged as likely
#   empty (the window re-presented without any game draws in that frame).
# rex rdc dump <capture.rdc> [--events 1,2,3] [--out DIR] [--max-png N]
#   Replays a capture inside qrenderdoc (headless) and writes text + PNGs:
#   action tree, render-target snapshots, and for --events: constants,
#   post-VS outputs, bound textures, blend state and SPIR-V of each draw.
#   Default output dir: next to the capture, <name>_dump/.
# rex rdc align <dumpA> <dumpB>
#   Match render-target passes and individual draws between two dumps
#   (e.g. this recomp vs Xenia) by their draw sequences.
# rex rdc diff <dumpA> <eventA> <dumpB> <eventB>
#   Compare one matched draw from each dump (inputs, textures, shaders).
#
# Capture with `rex run --renderdoc`.
RDC_DIR="$REXTOOLS_DIR/renderdoc"
sub="${1:-}"
shift || true

captures_dir() {
  if dir="$(find_project_dir 2>/dev/null)"; then
    echo "$dir/_tools/captures"
  else
    echo "$PWD"
  fi
}

case "$sub" in
  list)
    dir="$(captures_dir)"
    shopt -s nullglob
    files=("$dir"/*.rdc)
    [ ${#files[@]} -gt 0 ] || die "no captures in $dir"
    max=0
    for f in "${files[@]}"; do s=$(stat -c %s "$f"); [ "$s" -gt "$max" ] && max=$s; done
    for f in "${files[@]}"; do
      s=$(stat -c %s "$f")
      flag=""
      [ "$s" -lt $((max / 10)) ] && flag="  <- likely empty (no game draws), safe to delete"
      printf "%6d MB  %s%s\n" "$((s / 1048576))" "$(basename "$f")" "$flag"
    done
    ;;
  dump)
    cap="${1:-}"; shift || true
    [ -f "$cap" ] || die "usage: rex rdc dump <capture.rdc> [--events 1,2] [--out DIR] [--max-png N]"
    command -v qrenderdoc >/dev/null || die "qrenderdoc not found (sudo dnf install renderdoc)"
    events=""; out=""; maxpng=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --events) events="$2"; shift 2 ;;
        --out) out="$2"; shift 2 ;;
        --max-png) maxpng="$2"; shift 2 ;;
        *) die "rdc dump: unknown option '$1'" ;;
      esac
    done
    cap="$(readlink -f "$cap")"
    out="${out:-${cap%.rdc}_dump}"
    mkdir -p "$out"; out="$(readlink -f "$out")"
    rm -f "$out/error.txt"
    echo "Replaying $(basename "$cap") -> $out ..."
    RDC_IN="$cap" RDC_OUT="$out" RDC_EVENTS="$events" RDC_MAX_PNG="${maxpng:-40}" \
      qrenderdoc --python "$RDC_DIR/rdc_dump.py" >/dev/null 2>&1
    if [ -s "$out/error.txt" ]; then
      cat "$out/error.txt"; die "dump failed (see $out/error.txt)"
    fi
    [ -f "$out/summary.txt" ] || die "qrenderdoc produced no output (did it start?)"
    sed -n '1,/render-target snapshots/p' "$out/summary.txt"
    if [ -n "$events" ]; then
      echo; echo "Per-event detail:"
      for e in ${events//,/ }; do echo "  $out/event_$e.txt"; done
    fi
    echo; echo "Output: $out"
    ;;
  align)
    [ $# -eq 2 ] || die "usage: rex rdc align <dumpA> <dumpB>"
    $REX_PY "$RDC_DIR/rdc_tools.py" align "$1" "$2"
    ;;
  diff)
    [ $# -eq 4 ] || die "usage: rex rdc diff <dumpA> <eventA> <dumpB> <eventB>"
    $REX_PY "$RDC_DIR/rdc_tools.py" diff "$1" "$2" "$3" "$4"
    ;;
  *)
    sed -n '1,/^RDC_DIR=/p' "$REXTOOLS_DIR/cmd/rdc.sh" | sed '$d; s/^# \{0,1\}//'
    [ -n "$sub" ] && die "unknown rdc command '$sub'"
    ;;
esac
