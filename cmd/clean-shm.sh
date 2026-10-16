# rex clean-shm [--dry-run]
# A crashed or killed game (or rexglue) never shm_unlink()s its guest memory
# backing file /dev/shm/xenia_memory_<tick> (~4.5GB each). Enough of them fill
# tmpfs, and new runs then die with SIGBUS on writes the host can't back -
# which looks like a memory bug but isn't. Removes every such file that no
# running process still maps. Does not need a project.
dry=0
[ "${1:-}" = "--dry-run" ] && dry=1
shopt -s nullglob
files=(/dev/shm/xenia_memory_*)
shopt -u nullglob
if [ ${#files[@]} -eq 0 ]; then
  echo "No xenia_memory_* files in /dev/shm."
  exit 0
fi
for f in "${files[@]}"; do
  name="$(basename "$f")"
  if grep -qs "$name" /proc/[0-9]*/maps; then
    echo "In use, kept: $f"
  elif [ "$dry" = 1 ]; then
    echo "Would remove: $f"
  else
    rm -f "$f" && echo "Removed: $f"
  fi
done
df -h /dev/shm
