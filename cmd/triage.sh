# rex triage [timeout_secs] [--hang-dump] [--no-build]
# Build, run, classify the outcome, and fix what can be fixed automatically:
#
#   "Call to invalid or unregistered function at guest address X" in the log
#       -> add X to the functions table, rebuild, go again
#   killed by a signal  -> `rex crash` post-mortem, stop
#   still running at the timeout -> treated as "got past init"; with
#       --hang-dump, dump all thread stacks first (`rex hang`), then stop
#   anything else       -> show the log tail, stop
#
# Runs the game plainly (no debugger attached): the SDK's SIGSEGV handler
# self-heals some faults that a debugger would intercept first.
load_project
timeout_s=60
hang_dump=0
build=1
for a in "$@"; do
  case "$a" in
    --hang-dump) hang_dump=1 ;;
    --no-build) build=0 ;;
    ''|*[!0-9]*) die "triage: unknown option '$a'" ;;
    *) timeout_s="$a" ;;
  esac
done
export REX_PROJECT="$PROJECT_DIR"
# Unattended runs: skip the in-window launch menu of projects that use rexlauncher.
export REXLAUNCHER_SKIP=1
rex_cmd="$REXTOOLS_DIR/rex"

for ((iter = 1; iter <= 100; iter++)); do
  echo "=== triage iteration $iter ==="
  if [ "$build" = 1 ] || [ "$iter" -gt 1 ]; then
    "$rex_cmd" build || die "build failed - fix it, then re-run triage"
  fi
  "$rex_cmd" clean-shm >/dev/null 2>&1

  before="$(latest_run_number)"
  "$rex_cmd" run > "$OUT_DIR/triage_stdout.txt" 2>&1 &
  pid=$!
  status=""
  for ((s = 0; s < timeout_s; s++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    status=timeout
    [ "$hang_dump" = 1 ] && "$rex_cmd" hang "$pid"
    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
  else
    wait "$pid"
    status=$?
  fi

  run="$(latest_run_number)"
  if [ -z "$run" ] || [ "$run" = "$before" ]; then
    echo "--- no new log was written (exit status $status). stdout/stderr:"
    tail -20 "$OUT_DIR/triage_stdout.txt"
    exit 1
  fi
  log="$OUT_DIR/triage_run.log"
  cat_run_log "$run" > "$log"
  echo "--- run $run finished: ${status/timeout/still running after ${timeout_s}s}"

  addr="$(grep -oP 'Call to invalid or unregistered function at guest address \K0x[0-9A-Fa-f]+' "$log" | head -1)"
  if [ -n "$addr" ]; then
    addr="$(printf '0x%08X' "$addr")"
    # A target that is already a branch label inside some function is not a
    # function start: usually a switch case reached through a jump table that
    # codegen missed (emitted as an indirect call). Registering it would split
    # that function and leave branches back into it unresolved.
    if grep -rqs --include='*_recomp.*.cpp' "^loc_${addr#0x}:" "$PROJECT_DIR/generated"; then
      echo "$addr is unregistered, but it is a branch label inside an existing function:"
      $REX_PY "$REXTOOLS_DIR/py/inspect_code.py" "$PROJECT_DIR" containing "$addr"
      echo "Not adding it as a function. Likely a jump table codegen missed: check"
      echo "'rex lint' for MISSEDSW at that function's bctr."
      exit 1
    fi
    append_function "$addr" "Added by rex triage: unregistered indirect-call target."
    case $? in
      1) exit 1 ;;
      2) echo "$addr is already in $(basename "$APPENDED_TO") but still unregistered: the auto-detected"
         echo "boundary is wrong, or it is mid-function. Look with: rex containing $addr; rex asm $addr"
         exit 1 ;;
    esac
    echo "Added $addr to $(basename "$APPENDED_TO"); rebuilding."
    continue
  fi

  if [ "$status" = timeout ]; then
    echo "Ran ${timeout_s}s without a fatal error - looks past init. Stopping."
    exit 0
  fi
  echo "--- last errors in log:"
  grep -E '\[(critical|error)\]' "$log" | tail -5
  echo "--- log tail:"
  tail -15 "$log"
  if [ "$status" -gt 128 ]; then
    echo
    echo "--- killed by signal $((status - 128)); post-mortem:"
    "$rex_cmd" crash
  fi
  exit 1
done
die "triage: 100 iterations without settling"
