# rex hang [pid]
# Attaches gdb to the running game (hung, not crashed), saves every thread's
# backtrace to _tools/logs/hang_<ts>.txt, then detaches and leaves it running.
# Summary marks threads whose innermost frame is in guest code as SPINNING
# (a thread blocked in a wait sits in libc/syscall instead) - a spinning
# guest thread is the usual sign of a codegen misread loop.
# Needs ptrace permission: kernel.yama.ptrace_scope=0, or run with sudo.
#
# rex hang --samples N [--interval S] [pid]
#   "Poor man's profiler": N snapshots, then per thread the most common
#   stack tops. Unlike perf it also shows where threads are *waiting*
#   (blocked or spinning), which is what you need for stutter / pacing issues.
load_project
samples=0
interval=0.5
pid=""
while [ $# -gt 0 ]; do
  case "$1" in
    --samples) samples="$2"; shift 2 ;;
    --interval) interval="$2"; shift 2 ;;
    *) pid="$1"; shift ;;
  esac
done
if [ -z "$pid" ]; then
  pids="$(pgrep -f "build/$PRESET/$PROJECT_NAME( |\$)")"
  [ "$(echo "$pids" | grep -c .)" = 1 ] || die "expected one running $PROJECT_NAME, found: ${pids:-none} (pass the pid)"
  pid="$pids"
fi
if [ "$samples" -gt 0 ]; then
  out="$OUT_DIR/stacks_$(date +%Y%m%d_%H%M%S).txt"
  echo "Taking $samples snapshots of $pid every ${interval}s ..."
  for ((i = 1; i <= samples; i++)); do
    echo "=== SAMPLE $i" >> "$out"
    gdb -q -p "$pid" -batch -ex "set pagination off" -ex "set debuginfod enabled off" \
      -ex "thread apply all bt 8" -ex detach -ex quit >> "$out" 2>&1
    grep -q "ptrace: Operation not permitted" "$out" &&
      die "ptrace denied: run with sudo or set kernel.yama.ptrace_scope=0"
    sleep "$interval"
  done
  # Per thread name: count each 'top 3 frames' signature across samples.
  awk -v n="$samples" '
    function fname(line,   f) {
      f = line
      sub(/^#[0-9]+ +(0x[0-9a-f]+ in )?/, "", f)
      sub(/ \(.*$/, "", f); sub(/ at .*$/, "", f); sub(/ from .*$/, "", f)
      return f
    }
    function flush() {
      if (tname != "") { count[tname SUBSEP (blocked ? "[blocked] " : "") sig]++; total[tname]++ }
      tname = ""; sig = ""; depth = 0; blocked = 0
    }
    /^Thread [0-9]+ \(Thread/ {
      flush()
      tname = $0; sub(/^.*\(LWP [0-9]+\) "/, "", tname); sub(/".*$/, "", tname)
      next
    }
    /^#[0-9]+ / {
      f = fname($0)
      # Skip generic libc/pthread wait plumbing to reach the real waiter.
      if (depth == 0 && f ~ /^(__syscall_cancel|__internal_syscall|__futex|futex_|clock_nanosleep|__GI_|__pthread_cond|pthread_cond|___pthread_cond|__lll_|__nanosleep|nanosleep|syscall|\?\?)/) {
        blocked = 1; next
      }
      if (depth < 3) { sig = sig (depth ? " <- " : "") f; depth++ }
      next
    }
    /^=== SAMPLE/ { flush() }
    END {
      flush()
      for (k in count) { split(k, a, SUBSEP); printf "%s\t%d\t%s\n", a[1], 100 * count[k] / total[a[1]], a[2] }
    }
  ' "$out" | sort -t$'\t' -k1,1 -k2,2nr |
    awk -F'\t' -v n="$samples" '{ if ($1 != prev) { print "\n" $1; prev = $1; shown = 0 }
                                   if (shown++ < 3) printf "  %3d%%  %s\n", $2, $3 }'
  echo "Raw stacks: $out"
  exit 0
fi

out="$OUT_DIR/hang_$(date +%Y%m%d_%H%M%S).txt"
echo "Attaching to $pid ..."
gdb -q -p "$pid" -batch -ex "set pagination off" -ex "set debuginfod enabled off" \
  -ex "info threads" -ex "thread apply all bt 40" -ex detach -ex quit > "$out" 2>&1
grep -q "ptrace: Operation not permitted" "$out" &&
  die "ptrace denied: run with sudo or set kernel.yama.ptrace_scope=0"

awk '
  function flush(   i, state) {
    if (!n) return
    state = (top ~ /sub_8[0-9A-Fa-f]{7}|xstart/) ? "SPINNING in guest code" : "blocked/host"
    printf "%s\n  [%s] top: %s\n  guest:", hdr, state, substr(top, 1, 140)
    for (i = 0; i < n; i++) printf " %s", g[i]
    print "\n"
  }
  /^Thread [0-9]+ \(Thread/ { flush(); hdr = $0; n = 0; top = ""; next }
  /^#[0-9]+ / {
    if (top == "") top = $0
    if (match($0, /sub_8[0-9A-Fa-f]{7}|xstart/)) g[n++] = substr($0, RSTART, RLENGTH)
  }
  END { flush() }
' "$out"
echo "Full dump: $out   (process $pid detached, still running)"
