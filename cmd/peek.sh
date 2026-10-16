# rex peek [--thread NAME] [--func sub_X] [--stack BYTES] [--addrs A,B]
#          [--wait-log REGEX] [pid]
# Attaches gdb to the running game and dumps one guest thread's PPC state:
# registers from the frame's ctx, the guest stack frame from r1, and the
# headers/contents of everything the frame points at (big-endian). For a
# spinning thread found by `rex hang`, this shows the values its loop tests.
# Saves to _tools/logs/peek_<ts>.txt, detaches, leaves the game running.
# --addrs takes EXPR[:LEN] items: hex, rN, rN+off, [EXPR] (see py/gdb_guest.py).
# --save EXPR:LEN:PATH[,...] writes raw guest memory to files (e.g. a frame).
# --wait-log REGEX waits until the running game's current log matches (e.g. the
# first "Unhandled guest access violation") and peeks at that moment, for
# problems that only happen sometimes. Ctrl-C to give up.
# Needs ptrace permission, like `rex hang`.
load_project
pid=""
wait_re=""
while [ $# -gt 0 ]; do
  case "$1" in
    --thread) export REX_PEEK_THREAD="$2"; shift 2 ;;
    --func) export REX_PEEK_FUNC="$2"; shift 2 ;;
    --stack) export REX_PEEK_STACK="$2"; shift 2 ;;
    --addrs) export REX_PEEK_ADDRS="$2"; shift 2 ;;
    --save) export REX_PEEK_SAVE="$2"; shift 2 ;;
    --wait-log) wait_re="$2"; shift 2 ;;
    *) pid="$1"; shift ;;
  esac
done
if [ -z "$pid" ]; then
  pids="$(pgrep -f "build/$PRESET/$PROJECT_NAME( |\$)")"
  [ "$(echo "$pids" | grep -c .)" = 1 ] || die "expected one running $PROJECT_NAME, found: ${pids:-none} (pass the pid)"
  pid="$pids"
fi
if [ -n "$wait_re" ]; then
  run="$(latest_run_number)"
  [ -n "$run" ] || die "no run log in $LOG_DIR"
  log="$LOG_DIR/${PROJECT_NAME}_${run}.log"   # newest segment; rotation renames older ones
  echo "Waiting for /$wait_re/ in $(basename "$log") (pid $pid) ..."
  until grep -qE -- "$wait_re" "$log" 2>/dev/null; do
    kill -0 "$pid" 2>/dev/null || die "game exited before the log matched"
    sleep 0.3
  done
  echo "Matched: $(grep -m1 -E -- "$wait_re" "$log" | cut -c1-200)"
fi
out="$OUT_DIR/peek_$(date +%Y%m%d_%H%M%S).txt"
gdb -q -p "$pid" -batch -ex "set pagination off" -ex "set debuginfod enabled off" \
  -x "$REXTOOLS_DIR/py/gdb_guest.py" -ex detach -ex quit > "$out" 2>&1
grep -q "ptrace: Operation not permitted" "$out" &&
  die "ptrace denied: run with sudo or set kernel.yama.ptrace_scope=0"
sed -n '/^thread /,$p' "$out" | grep -v '^\[Inferior\|^Detaching'
echo "Saved: $out   (process $pid detached, still running)"
