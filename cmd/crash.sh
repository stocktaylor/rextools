# rex crash [--no-wait]
# Post-mortem of the latest core dump of this project's binary (coredumpctl).
# Finds the frame that actually faulted (the one under "<signal handler
# called>" - the SDK's handler re-raises, so frame 0 is just raise()), shows
# its instructions and registers, maps it to the guest function, and lists
# the guest call chain. Full output saved to _tools/logs/crash_<ts>.txt.
load_project
command -v coredumpctl >/dev/null || die "coredumpctl not found (run this on the host, not in a sandbox)"
wait_secs=10
[ "${1:-}" = "--no-wait" ] && wait_secs=0

core="$(mktemp)"
trap 'rm -f "$core"' EXIT
# systemd-coredump may still be writing the core right after the crash.
for ((i = 0; i <= wait_secs; i++)); do
  coredumpctl dump "$BIN" --output="$core" >/dev/null 2>&1 && break
  [ "$i" -lt "$wait_secs" ] && sleep 1
done
[ -s "$core" ] || die "coredumpctl has no core for $BIN"

out="$OUT_DIR/crash_$(date +%Y%m%d_%H%M%S).txt"
gdbq() { gdb -q -batch -ex "set pagination off" -ex "set debuginfod enabled off" "$@" "$BIN" "$core" 2>&1; }

bt="$(gdbq -ex "p \$_siginfo.si_signo" -ex "bt 60")"
signo="$(echo "$bt" | sed -n 's/^\$1 = \([0-9]\+\)$/\1/p')"
fault_frame="$(echo "$bt" | awk '/<signal handler called>/ {getline; sub(/^#/, ""); print $1; exit}')"
fault_frame="${fault_frame:-0}"
detail="$(gdbq -ex "frame $fault_frame" -ex "x/8i \$pc-16" -ex "info registers rip rsp rax rbx rcx rdx rsi rdi" \
               -ex "echo \n=== all threads ===\n" -ex "thread apply all bt 25")"
{ echo "$bt"; echo; echo "=== faulting frame #$fault_frame ==="; echo "$detail"; } > "$out"

case "$signo" in
  4)  sig="SIGILL";  hint="__builtin_trap: usually 'Switch case out of range' - a jump table that does not match runtime (see 'rex lint --all' for TINY/DISPATCH switches)";;
  6)  sig="SIGABRT"; hint="abort/assert: check the log tail ('rex log | tail') for the assert or FATAL message";;
  7)  sig="SIGBUS";  hint="often a full /dev/shm from leaked guest memory: try 'rex clean-shm'";;
  8)  sig="SIGFPE";  hint="guest divides are guarded, so suspect unmasked MXCSR exceptions (FPSCR not initialized on the thread) or host-side integer division";;
  11) sig="SIGSEGV"; hint="if it happens only under gdb, re-run plainly: the SDK self-heals some faults that gdb intercepts first";;
  *)  sig="signal ${signo:-?}"; hint="";;
esac

echo "Signal: $sig"
[ -n "$hint" ] && echo "Hint:   $hint"
echo
echo "Faulting frame #$fault_frame:"
echo "$detail" | sed -n '1,/=== all threads ===/p' | sed '$d' | grep -E '^(#|=>|   0x)' | head -12
pc="$(echo "$bt" | sed -n "s/^#$fault_frame  *\(0x[0-9a-f]\+\) in .*/\1/p" | head -1)"
if [ -n "$pc" ]; then
  echo
  $REX_PY "$REXTOOLS_DIR/py/inspect_code.py" "$PROJECT_DIR" pc2func "$pc" "$BIN"
fi
echo
echo "Guest call chain (innermost first):"
echo "$bt" | grep -oE 'in (sub_8[0-9A-Fa-f]{7}|xstart)' | awk '{print "  " $2}' | head -20
echo
echo "Full dump: $out"
