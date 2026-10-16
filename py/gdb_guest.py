"""gdb script: dump guest (PPC) state of a running rexglue game. Used by `rex peek`.

Runs inside gdb (gdb -p PID -batch -x gdb_guest.py). Settings come from the
environment:
  REX_PEEK_THREAD  substring of the host thread name   (default "Main XThread")
  REX_PEEK_FUNC    guest function to stop at, e.g. sub_8A6018C8
                   (default: innermost frame running guest code)
  REX_PEEK_STACK   bytes of the guest stack frame to dump from r1 (default 512)
  REX_PEEK_ADDRS   extra memory to dump, comma separated EXPR[:LEN] (LEN default
                   16, max 4096). EXPR is a hex address, a register (r31), either
                   plus/minus an offset (r31+0x5C, r1+92), or [EXPR] to read the
                   32-bit guest pointer stored there: r31:128,[r31+92]:64
  REX_PEEK_SAVE    raw dumps to files, comma separated EXPR:LEN:PATH (LEN up to
                   64 MiB, hex ok): 0x4A000000:0xE1000:/tmp/y.bin
Prints the PPC registers from the frame's ctx, the frame's stack words, and
for every word that looks like a guest pointer the 8 bytes before it (object
or array header) and 16 after. Guest memory is big-endian.
"""
import os
import re

import gdb

GUEST_BASE = 0x100000000
FN_RE = re.compile(r"(sub_[0-9A-Fa-f]{8}|xstart)")


def be32(addr):
    b = gdb.selected_inferior().read_memory(GUEST_BASE + addr, 4).tobytes()
    return int.from_bytes(b, "big")


def hexdump(addr, n):
    try:
        b = gdb.selected_inferior().read_memory(GUEST_BASE + addr, n).tobytes()
    except gdb.MemoryError:
        return "(unmapped)"
    return " ".join(b[i:i + 4].hex() for i in range(0, len(b), 4))


EXPR_RE = re.compile(r"^(r\d+|lr|ctr|(?:0x)?[0-9A-Fa-f]+)\s*(?:([+-])\s*((?:0x)?[0-9A-Fa-f]+))?$")


def eval_expr(expr, regs):
    """hex | rN | ctr | lr, optionally +/- offset (0x.. hex, else decimal); [x] dereferences."""
    expr = expr.strip()
    if expr.startswith("[") and expr.endswith("]"):
        return be32(eval_expr(expr[1:-1], regs))
    m = EXPR_RE.match(expr)
    if not m:
        raise ValueError(f"bad address expression '{expr}'")
    base, sign, off = m.groups()
    if base in regs:
        v = regs[base] & 0xFFFFFFFF
    elif base.startswith("r") or base in ("lr", "ctr"):
        raise ValueError(f"register {base} not available")
    else:
        v = int(base, 16)
    if off:
        n = int(off, 16) if off.lower().startswith("0x") else int(off)
        v = v + n if sign == "+" else v - n
    return v & 0xFFFFFFFF


def looks_like_pointer(v):
    # guest heaps / XEX images: 0x30000000-0x4FFFFFFF, 0x70000000-0x7FFFFFFF,
    # 0x80000000-0x9FFFFFFF, physical 0xA0000000-0xFFFFFFFF
    return v >= 0x30000000 and v & 3 == 0


def reg(ctx, name):
    # GPRs/ctr are rex::ppc::Register unions; lr is a plain uint64_t. At -O2 a
    # hot loop may hold some in host registers, so ctx can lag; memory doesn't.
    try:
        v = ctx[name]
        try:
            v = v["u64"]
        except gdb.error:
            pass
        return int(v) & 0xFFFFFFFFFFFFFFFF
    except gdb.error:
        return None


def do_saves(saves, regs):
    for spec in saves:
        try:
            expr, length, path = spec.split(":", 2)
            addr = eval_expr(expr, regs)
            n = min(int(length, 0), 64 << 20)
            data = gdb.selected_inferior().read_memory(GUEST_BASE + addr, n).tobytes()
            with open(path, "wb") as fh:
                fh.write(data)
            print(f"\nsaved {n} bytes from {addr:08X} to {path}")
        except (ValueError, gdb.MemoryError, OSError) as e:
            print(f"\nsave {spec}: {e}")


def main():
    want_thread = os.environ.get("REX_PEEK_THREAD", "Main XThread")
    want_func = os.environ.get("REX_PEEK_FUNC", "")
    stack_bytes = int(os.environ.get("REX_PEEK_STACK", "512"))
    extra = [a.strip() for a in os.environ.get("REX_PEEK_ADDRS", "").split(",") if a.strip()]
    saves = [a.strip() for a in os.environ.get("REX_PEEK_SAVE", "").split(",") if a.strip()]

    thread = next((t for t in gdb.selected_inferior().threads() if want_thread in (t.name or "")), None)
    if thread is None:
        print(f"no thread named like '{want_thread}'")
        do_saves(saves, {})
        return
    thread.switch()
    frame = gdb.newest_frame()
    while frame is not None:
        name = frame.name() or ""
        m = FN_RE.search(name)
        if m and (not want_func or want_func in name):
            break
        frame = frame.older()
    if frame is None:
        print(f"thread '{thread.name}': no guest frame{' ' + want_func if want_func else ''}")
        do_saves(saves, {})
        return
    frame.select()
    sal = frame.find_sal()
    print(f"thread '{thread.name}' frame {frame.name()} "
          f"({sal.symtab.filename.rsplit('/', 1)[-1] if sal.symtab else '?'}:{sal.line})")
    ctx = frame.read_var("ctx")

    regs = {}
    for i in range(32):
        v = reg(ctx, f"r{i}")
        if v is not None:
            regs[f"r{i}"] = v
    for name in ("ctr", "lr"):
        v = reg(ctx, name)
        if v is not None:
            regs[name] = v
    print("\nregisters:")
    items = list(regs.items())
    for i in range(0, len(items), 4):
        print("  " + "  ".join(f"{k:>3}={v & 0xFFFFFFFF:08X}" for k, v in items[i:i + 4]))
    try:
        print(f"  cr0: lt={int(ctx['cr0']['lt'])} gt={int(ctx['cr0']['gt'])} eq={int(ctx['cr0']['eq'])}")
    except gdb.error:
        pass

    r1 = regs.get("r1", 0) & 0xFFFFFFFF
    print(f"\nstack frame r1={r1:08X} (+0..+{stack_bytes}):")
    pointers = set()
    for off in range(0, stack_bytes, 16):
        words = []
        for k in range(0, 16, 4):
            try:
                w = be32(r1 + off + k)
            except gdb.MemoryError:
                w = None
            words.append("????????" if w is None else f"{w:08X}")
            if w is not None and looks_like_pointer(w):
                pointers.add(w)
        print(f"  +{off:03X} ({off:4d}): {' '.join(words)}")
    for v in regs.values():
        if looks_like_pointer(v & 0xFFFFFFFF):
            pointers.add(v & 0xFFFFFFFF)

    print("\npointed-to memory: [p-8 .. p) | [p .. p+16)")
    for p in sorted(pointers):
        print(f"  {p:08X}: {hexdump(p - 8, 8)} | {hexdump(p, 16)}")

    for spec in extra:
        expr, _, length = spec.partition(":")
        try:
            addr = eval_expr(expr, regs)
        except (ValueError, gdb.MemoryError) as e:
            print(f"\n{spec}: {e}")
            continue
        n = min(int(length, 0) if length else 16, 4096)
        print(f"\n{expr} = {addr:08X} ({n} bytes):")
        for off in range(0, n, 16):
            chunk = min(16, n - off)
            text = ""
            try:
                b = gdb.selected_inferior().read_memory(GUEST_BASE + addr + off, chunk).tobytes()
                text = "".join(chr(c) if 32 <= c < 127 else "." for c in b)
            except gdb.MemoryError:
                pass
            print(f"  +{off:03X}: {hexdump(addr + off, chunk):<36} {text}")


    do_saves(saves, regs)

main()
