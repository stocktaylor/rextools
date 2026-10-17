"""rextools: shared build / run / triage / inspection tooling for rexglue
recompilation projects. Run from anywhere inside a project directory.

  rex new <name> <xex> [--game-root DIR]   bootstrap a new project here
  rex build [--force-codegen]              codegen -> configure -> build -> lint
  rex run [game args...]                   run the game with standard flags
  rex run --renderdoc [game args...]       run under RenderDoc (F12 captures a frame)
  rex triage [timeout] [--hang-dump]       build/run loop: auto-add missing
                                           functions, stop on anything else
  rex lint [--all]                         scan generated code for misreads
  rex crash [--no-wait]                    post-mortem of the latest core dump (Linux)
  rex hang [pid]                           stacks of a running (hung) game (Linux)
  rex hang --samples N [--interval S]      where each thread spends time, incl. waits (Linux)
  rex peek [--func sub_X] [--thread T]     a guest thread's registers, stack frame, pointees (Linux)
  rex log [run#]                           print a run's log, rotations merged
  rex rdc list|dump|align|diff             analyse RenderDoc captures (rex rdc for help)
  rex funcs|asm <addr>...                  generated C++ / guest disassembly
  rex callers <addr|ImportName>            who calls a function or import
  rex pc2func <host_pc>                    host PC (backtrace/core) -> guest fn (Linux)
  rex containing <guest_addr>              which function holds an address
  rex modules                              binaries (exe + DLLs), ranges, functions tables
  rex clean-shm [--dry-run]                remove leaked /dev/shm guest memory (Linux)
  rex dist [--config C] [--march M]        portable build via Steam Runtime SDK container (Linux)
  rex sdk check                            is the installed SDK current?
  rex sdk patches status|apply             local SDK patches (sdk-patches/)
  rex sdk patches export <name> <file>...  save SDK changes as a patch

Runs on Linux and Windows (PowerShell, cmd or any shell): `rex` is a small
shim (rex / rex.cmd) that starts this script with Python 3.11+.
"""
import sys

if sys.version_info < (3, 11):
    sys.exit(f"rex: needs Python 3.11 or newer (for tomllib); this is {sys.version.split()[0]} "
             f"at {sys.executable}")

from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rexcli import common  # noqa: E402
from rexcli.common import IS_WINDOWS, RexError, die  # noqa: E402

WINDOWS_UNSUPPORTED = {
    "crash": "'rex crash' is Linux-only for now (it drives gdb / coredumpctl)",
    "hang": "'rex hang' is Linux-only for now (it drives gdb)",
    "peek": "'rex peek' is Linux-only for now (it drives gdb)",
    "pc2func": "'rex pc2func' is Linux-only for now (it reads ELF symbols with nm; Windows has PDBs)",
    "dist": "'rex dist' builds in a Linux (Steam Runtime) container; there's no Windows packaging yet",
}


def usage():
    print(__doc__.strip("\n"))


def dispatch(cmd: str, args: list[str]) -> int:
    if IS_WINDOWS and cmd in WINDOWS_UNSUPPORTED:
        die(WINDOWS_UNSUPPORTED[cmd])

    if cmd in ("new", "build", "run", "triage"):
        from rexcli import build
        return getattr(build, f"cmd_{cmd}")(args)
    if cmd in ("crash", "hang", "peek"):
        from rexcli import debug
        return getattr(debug, f"cmd_{cmd}")(args)
    if cmd in ("log", "clean-shm", "sdk", "rdc", "dist"):
        from rexcli import misc
        return getattr(misc, f"cmd_{cmd.replace('-', '_')}")(args)

    if cmd == "lint":
        ctx = common.load_project()
        return common.helper("lint.py", ctx.dir, *args).returncode
    if cmd in ("funcs", "asm", "callers", "containing"):
        ctx = common.load_project()
        return common.helper("inspect_code.py", ctx.dir, cmd, *args).returncode
    if cmd == "modules":
        ctx = common.load_project()
        return common.helper("rexinfo.py", ctx.dir, "--modules").returncode
    if cmd == "pc2func":
        ctx = common.load_project()
        if not args:
            die("usage: rex pc2func <host_pc>")
        return common.helper("inspect_code.py", ctx.dir, "pc2func", args[0], ctx.bin).returncode
    if cmd in ("help", "-h", "--help"):
        usage()
        return 0
    usage()
    die(f"unknown command '{cmd}'")
    return 1


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    try:
        return dispatch(cmd, args[1:]) or 0
    except RexError as e:
        sys.stdout.flush()
        print(f"rex: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
