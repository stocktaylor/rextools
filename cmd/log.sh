# rex log [run#]
# Prints a run's log with its rotated 5MB segments merged in order. Defaults
# to the latest run. Pipe it: `rex log | grep -i xam`.
load_project
n="${1:-$(latest_run_number)}"
[ -n "$n" ] || die "no logs in $LOG_DIR"
n=$(printf '%03d' "$((10#$n))")
cat_run_log "$n"
