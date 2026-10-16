"""Parser for rexglue-generated C++ (generated/<module>/*_recomp.*.cpp).

Each guest instruction is emitted as one `\\t// mnemonic operands` comment
followed by its C++; branch targets are emitted as `loc_XXXXXXXX:` labels.
That lets us recover the guest address of every instruction by counting
from the function start / the last label.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

FUNC_RE = re.compile(r"^DEFINE_REX_FUNC\((\w+)\)")
LABEL_RE = re.compile(r"^loc_([0-9A-Fa-f]+):")
INSN_RE = re.compile(r"^\t// ([a-z][a-z0-9.]*[+-]?)(?: (.*?))?\s*$")
REG_RE = re.compile(r"\{\s*0x([0-9A-Fa-f]+),\s*(\w+)\s*\}")
CALL_RE = re.compile(r"\b((?:sub_|__imp__)[0-9A-Za-z_]+)\(ctx, base\)")


@dataclass
class Insn:
    addr: int
    mnem: str
    ops: str
    line: int  # 0-based index into Function.lines


@dataclass
class Function:
    name: str
    addr: int
    path: Path
    first_line: int  # 1-based line number in path
    lines: list[str]
    insns: list[Insn] = field(default_factory=list)

    @property
    def size(self) -> int:
        return (self.insns[-1].addr + 4 - self.addr) if self.insns else 0

    def calls(self) -> set[str]:
        out = set()
        for line in self.lines:
            for m in CALL_RE.finditer(line):
                out.add(m.group(1))
        return out

    def asm(self) -> str:
        out = []
        labels = {i for i, l in enumerate(self.lines) if LABEL_RE.match(l)}
        for insn in self.insns:
            # print the label line if the instruction immediately follows one
            if insn.line - 1 in labels:
                out.append(self.lines[insn.line - 1])
            out.append(f"  {insn.addr:08X}  {insn.mnem:<8} {insn.ops}".rstrip())
        return "\n".join(out)


def load_registry(gen_dir: Path) -> dict[str, int]:
    """name -> guest address, from the *_init.cpp function table."""
    out = {}
    for p in gen_dir.glob("*_init.cpp"):
        for m in REG_RE.finditer(p.read_text(errors="replace")):
            out[m.group(2)] = int(m.group(1), 16)
    return out


def iter_functions(gen_dir: Path, registry: dict[str, int] | None = None):
    registry = registry if registry is not None else load_registry(gen_dir)
    files = sorted(gen_dir.glob("*_recomp.*.cpp"), key=lambda p: int(p.stem.rsplit(".", 1)[-1]))
    for path in files:
        cur = None
        with path.open(errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.rstrip("\n")
                if cur is None:
                    m = FUNC_RE.match(line)
                    if m:
                        name = m.group(1)
                        addr = registry.get(name)
                        if addr is None and name.startswith("sub_"):
                            addr = int(name[4:], 16)
                        cur = Function(name, addr or 0, path, lineno, [line])
                        pc = cur.addr
                    continue
                cur.lines.append(line)
                if line == "}":
                    yield cur
                    cur = None
                    continue
                m = LABEL_RE.match(line)
                if m:
                    pc = int(m.group(1), 16)
                    continue
                m = INSN_RE.match(line)
                if m:
                    cur.insns.append(Insn(pc, m.group(1), m.group(2) or "", len(cur.lines) - 1))
                    pc += 4


def iter_all_functions(gen_dirs):
    """iter_functions over every binary of a project (entrypoint + DLL modules)."""
    for gen_dir in gen_dirs:
        if gen_dir.exists():
            yield from iter_functions(gen_dir)
