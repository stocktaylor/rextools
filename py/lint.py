"""Lint rexglue-generated code for patterns that compile fine but misbehave.

Checks:
  DISPATCH  switch whose every case jumps back to the function start: a
            computed tail-call (interpreter dispatch, handler table) misread
            as a jump table. Hangs the game in a tight loop. Fix: add the bctr
            address to indirect_calls (needs the indirect_calls SDK patch).
  TINY      switch with <= 2 cases. Often the same misread; check.
  STALE     a bctr listed in indirect_calls still emitted as a switch: the
            SDK patch is missing or codegen ran with an old SDK/stamp.
  NOBCTR    an indirect_calls address that is not a bctr in any function.
  MISSEDSW  a bctr emitted as an indirect call although it looks like a
            bounds-checked jump table (cmplwi rI,N with N >= 2, then lwzx /
            mtctr / bctr). Its cases are missing from the function, and GapFill
            may turn the first case into an overlapping function whose branches
            back hit REX_FATAL "Unresolved branch". Fix: teach the SDK's jump
            table detector the form, or add a [[switch_tables]] entry.
  UNRESOLV  a branch codegen could not place (REX_FATAL "Unresolved branch"
            when taken). Usually the target is inside the function but the
            branch was attributed to an overlapping function registered
            mid-way through it, e.g. a switch case label added to a
            functions table; the hint names such entries.

Usage: python3 -I lint.py <project_dir> [--all]
Exit status 1 if DISPATCH, STALE or MISSEDSW findings exist.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gencode  # noqa: E402
from rexinfo import Project  # noqa: E402

SWITCH_RE = re.compile(r"^\tswitch \(ctx\.")
GOTO_RE = re.compile(r"goto loc_([0-9A-Fa-f]+);")
UNRESOLVED_RE = re.compile(r'REX_FATAL\("Unresolved branch from 0x([0-9A-Fa-f]+) to 0x([0-9A-Fa-f]+)"\)')
CMP_IMM_RE = re.compile(r"(?:cr\d+,)?r\d+,(\d+)$")


def missed_switch(fn, k) -> bool:
    """insn k is a bctr: does it look like a bounds-checked jump table?"""
    insn = fn.insns[k]
    if any(SWITCH_RE.match(l) for l in fn.lines[insn.line:insn.line + 3]):
        return False
    window = fn.insns[max(0, k - 10):k]
    mtctr = next((w for w in reversed(window) if w.mnem == "mtctr"), None)
    if not mtctr:
        return False
    ctr_src = mtctr.ops.strip()
    # the register moved to CTR must come straight from the indexed load
    src = next((w for w in reversed(window[:window.index(mtctr)])
                if w.ops.replace(" ", "").split(",")[0] == ctr_src), None)
    if not src or src.mnem != "lwzx":
        return False
    for w in reversed(window):
        if w.mnem in ("cmplwi", "cmpwi"):
            m = CMP_IMM_RE.search(w.ops.replace(" ", ""))
            return bool(m) and int(m.group(1)) >= 2
    return False


def scan(proj: Project):
    indirect = proj.indirect_calls()
    known = proj.known_functions()
    findings = []
    bctrs = set()
    for fn in gencode.iter_all_functions(proj.gen_dirs):
        for idx, line in enumerate(fn.lines):
            m = UNRESOLVED_RE.search(line)
            if m:
                src, dst = int(m.group(1), 16), int(m.group(2), 16)
                hint = ""
                if fn.addr in known and fn.addr > dst:
                    hint = f"; {fn.name} is in a functions table and starts after the target: mid-function entry?"
                findings.append(("UNRESOLV", f"{fn.name} {src:08X}->{dst:08X} "
                                 f"({fn.path.name}:{fn.first_line + idx})",
                                 "branch target not in this function" + hint))
        by_line = {i.line: i for i in fn.insns}
        last_insn = None
        for idx, line in enumerate(fn.lines):
            if idx in by_line:
                last_insn = by_line[idx]
                if last_insn.mnem == "bctr":
                    bctrs.add(last_insn.addr)
                    k = fn.insns.index(last_insn)
                    if last_insn.addr not in indirect and missed_switch(fn, k):
                        findings.append(("MISSEDSW", f"{fn.name} bctr@{last_insn.addr:08X} "
                                         f"({fn.path.name}:{fn.first_line + idx})",
                                         "bounds-checked lwzx/bctr emitted as an indirect call"))
            if not SWITCH_RE.match(line) or not last_insn or last_insn.mnem != "bctr":
                continue
            targets = []
            for l2 in fn.lines[idx + 1:]:
                if l2.strip().startswith("default:"):
                    break
                m = GOTO_RE.search(l2)
                if m:
                    targets.append(int(m.group(1), 16))
            distinct = set(targets)
            where = f"{fn.name} bctr@{last_insn.addr:08X} ({fn.path.name}:{fn.first_line + idx})"
            if last_insn.addr in indirect:
                findings.append(("STALE", where, "listed in indirect_calls but still a switch"))
            elif distinct and distinct == {fn.addr}:
                findings.append(("DISPATCH", where, f"{len(targets)} case(s), all -> function start",
                                 last_insn.addr))
            elif len(targets) <= 2:
                findings.append(("TINY", where, f"{len(targets)} case(s) -> "
                                 + ", ".join(f"{t:08X}" for t in sorted(distinct))))
    for addr in sorted(indirect - bctrs):
        findings.append(("NOBCTR", f"{addr:08X}", "in indirect_calls but no bctr there"))
    return findings


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_all = "--all" in sys.argv
    proj = Project(Path(args[0] if args else "."))
    if not any(any(d.glob("*_recomp.*.cpp")) for d in proj.gen_dirs if d.exists()):
        print(f"lint: no generated code in {proj.gen_dir} (run codegen first)")
        return 0
    findings = scan(proj)
    order = {"STALE": 0, "DISPATCH": 1, "MISSEDSW": 2, "UNRESOLV": 3, "NOBCTR": 4, "TINY": 5}
    findings.sort(key=lambda f: order[f[0]])
    shown = [f for f in findings if show_all or f[0] != "TINY"]
    n_tiny = sum(1 for f in findings if f[0] == "TINY")
    for f in shown:
        print(f"  {f[0]:<8} {f[1]}  {f[2]}")
    dispatch = [f[3] for f in findings if f[0] == "DISPATCH"]
    if dispatch:
        print("\n  Suggested manifest addition (the [entrypoint] or [[modules]] table owning each\n"
              "  address, see 'rex modules'; needs the indirect_calls SDK patch):")
        print("  indirect_calls = [")
        bad = {int(f[1], 16) for f in findings if f[0] == "NOBCTR"}
        for a in sorted((set(dispatch) | proj.indirect_calls()) - bad):
            print(f"      0x{a:08X},")
        print("  ]")
    if n_tiny and not show_all:
        print(f"  ({n_tiny} TINY switch(es) not shown; --all to list. Usually fine, check if a hang points at one.)")
    if not shown:
        print("  lint: clean")
    return 1 if any(f[0] in ("STALE", "DISPATCH", "MISSEDSW") for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
