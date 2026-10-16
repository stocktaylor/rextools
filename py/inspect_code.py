"""Look things up in a project's generated code and built binary.

Usage (project dir comes first):
  inspect_code.py <proj> funcs   <addr|sub_X>...   full generated C++ of each function
  inspect_code.py <proj> asm     <addr|sub_X>...   guest disassembly only, with addresses
  inspect_code.py <proj> callers <addr|sub_X|ImportName>
                                  who calls it; for an import (e.g. XamContentCreate)
                                  resolves the thunk and lists the thunk's callers
  inspect_code.py <proj> pc2func <host_pc> <binary>
                                  map a host PC from a backtrace/core to the guest function
  inspect_code.py <proj> containing <guest_addr>
                                  which generated function contains a guest address
"""
import bisect
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gencode  # noqa: E402
from rexinfo import Project  # noqa: E402


def norm(name: str) -> str:
    if name.startswith(("sub_", "__imp__", "xstart")):
        return name
    if re.fullmatch(r"(0x)?[0-9A-Fa-f]{1,8}", name):
        return f"sub_{int(name, 16):08X}"
    return f"__imp__{name}"


def load(proj):
    return {f.name: f for f in gencode.iter_all_functions(proj.gen_dirs)}


def cmd_funcs(proj, names, asm_only):
    funcs = load(proj)
    for raw in names:
        f = funcs.get(norm(raw))
        if not f:
            print(f"=== {raw}: not found in generated code\n")
            continue
        print(f"=== {f.name}  {f.addr:08X}-{f.addr + f.size:08X}  ({f.path.name}:{f.first_line})")
        print(f.asm() if asm_only else "\n".join(f.lines))
        print()


def cmd_callers(proj, raw):
    target = norm(raw)
    funcs = load(proj)
    calls = {f.name: f.calls() for f in funcs.values()}

    def callers_of(name):
        return sorted(n for n, c in calls.items() if name in c and n != name)

    direct = callers_of(target)
    if target.startswith("__imp__"):
        print(f"{target} is called from {len(direct)} thunk/function(s):")
        for t in direct:
            up = callers_of(t)
            print(f"  {t}  <- {' '.join(up) if up else '(no direct callers: indirect only)'}")
    else:
        print(f"{target} <- {' '.join(direct) if direct else '(no direct callers: reached indirectly)'}")


def cmd_pc2func(proj, pc_str, binary):
    pc = int(pc_str, 16)
    out = subprocess.run(["nm", "-n", "--defined-only", binary], capture_output=True, text=True,
                         check=True).stdout
    addrs, names = [], []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] in "tTwW":
            addrs.append(int(parts[0], 16))
            names.append(parts[2])
    i = bisect.bisect_right(addrs, pc) - 1
    if i < 0:
        print(f"{pc:#x}: no symbol")
        return
    name = names[i].removeprefix("__imp__")
    print(f"{pc:#x} = {names[i]}+{pc - addrs[i]:#x}")
    f = load(proj).get(name)
    if f:
        print(f"guest function {f.name} at {f.addr:08X} ({f.path.name}:{f.first_line})")


def cmd_containing(proj, addr_str):
    addr = int(addr_str, 16)
    for f in gencode.iter_all_functions(proj.gen_dirs):
        if f.addr <= addr < f.addr + f.size:
            print(f"{addr:08X} is in {f.name} ({f.addr:08X}-{f.addr + f.size:08X}, "
                  f"{f.path.name}:{f.first_line})")
            return
    print(f"{addr:08X}: not inside any generated function")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    proj = Project(Path(sys.argv[1]))
    cmd, args = sys.argv[2], sys.argv[3:]
    if cmd in ("funcs", "asm") and args:
        cmd_funcs(proj, args, cmd == "asm")
    elif cmd == "callers" and len(args) == 1:
        cmd_callers(proj, args[0])
    elif cmd == "pc2func" and len(args) == 2:
        cmd_pc2func(proj, args[0], args[1])
    elif cmd == "containing" and len(args) == 1:
        cmd_containing(proj, args[0])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
