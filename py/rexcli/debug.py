"""rex crash / hang / peek (Linux: gdb and systemd-coredump)."""
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .common import PRESET, PY_DIR, Ctx, die, helper, latest_run_number, load_project

GDB_BASE = ["gdb", "-q", "-batch", "-ex", "set pagination off", "-ex", "set debuginfod enabled off"]
GUEST_FN_RE = re.compile(r"sub_8[0-9A-Fa-f]{7}|xstart")
PTRACE_DENIED = "ptrace: Operation not permitted"


def _stamp() -> str:
    return f"{datetime.now():%Y%m%d_%H%M%S}"


def _ex(*commands: str) -> list[str]:
    args = []
    for c in commands:
        args += ["-ex", c]
    return args


def _game_pid(ctx: Ctx) -> int:
    """The single running instance of this project's binary (like pgrep -f)."""
    pattern = re.compile(rf"build/{re.escape(PRESET)}/{re.escape(ctx.name)}( |$)")
    pids = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        if pattern.search(args) and int(cmdline.parent.name) != os.getpid():
            pids.append(int(cmdline.parent.name))
    if len(pids) != 1:
        die(f"expected one running {ctx.name}, found: {' '.join(map(str, pids)) or 'none'} (pass the pid)")
    return pids[0]


# --------------------------------------------------------------------------
# rex crash [--no-wait]
# Post-mortem of the latest core dump of this project's binary (coredumpctl).
# Finds the frame that actually faulted (the one under "<signal handler
# called>" - the SDK's handler re-raises, so frame 0 is just raise()), shows
# its instructions and registers, maps it to the guest function, and lists
# the guest call chain. Full output saved to _tools/logs/crash_<ts>.txt.

SIGNAL_HINTS = {
    "4": ("SIGILL", "__builtin_trap: usually 'Switch case out of range' - a jump table that does not match "
                    "runtime (see 'rex lint --all' for TINY/DISPATCH switches)"),
    "6": ("SIGABRT", "abort/assert: check the log tail ('rex log | tail') for the assert or FATAL message"),
    "7": ("SIGBUS", "often a full /dev/shm from leaked guest memory: try 'rex clean-shm'"),
    "8": ("SIGFPE", "guest divides are guarded, so suspect unmasked MXCSR exceptions (FPSCR not initialized "
                    "on the thread) or host-side integer division"),
    "11": ("SIGSEGV", "if it happens only under gdb, re-run plainly: the SDK self-heals some faults that gdb "
                      "intercepts first"),
}


def cmd_crash(args: list[str]) -> int:
    ctx = load_project()
    if not shutil.which("coredumpctl"):
        die("coredumpctl not found (run this on the host, not in a sandbox)")
    wait_secs = 0 if args and args[0] == "--no-wait" else 10

    with tempfile.TemporaryDirectory() as tmp:
        core = Path(tmp) / "core"
        # systemd-coredump may still be writing the core right after the crash.
        for i in range(wait_secs + 1):
            if subprocess.run(["coredumpctl", "dump", str(ctx.bin), f"--output={core}"],
                              capture_output=True).returncode == 0:
                break
            if i < wait_secs:
                time.sleep(1)
        if not core.exists() or core.stat().st_size == 0:
            die(f"coredumpctl has no core for {ctx.bin}")

        def gdbq(*extra: str) -> str:
            r = subprocess.run([*GDB_BASE, *extra, str(ctx.bin), str(core)], capture_output=True, text=True,
                               errors="replace")
            return (r.stdout + r.stderr).rstrip("\n")

        bt = gdbq(*_ex("p $_siginfo.si_signo", "bt 60"))
        m = re.search(r"^\$1 = ([0-9]+)$", bt, re.M)
        signo = m.group(1) if m else ""
        bt_lines = bt.splitlines()
        fault_frame = "0"
        for i, line in enumerate(bt_lines):
            if "<signal handler called>" in line and i + 1 < len(bt_lines):
                fields = bt_lines[i + 1].lstrip("#").split()
                if fields:
                    fault_frame = fields[0]
                break
        detail = gdbq(*_ex(f"frame {fault_frame}", "x/8i $pc-16",
                           "info registers rip rsp rax rbx rcx rdx rsi rdi",
                           "echo \\n=== all threads ===\\n", "thread apply all bt 25"))

    out = ctx.out_dir / f"crash_{_stamp()}.txt"
    out.write_text(f"{bt}\n\n=== faulting frame #{fault_frame} ===\n{detail}\n")

    sig, hint = SIGNAL_HINTS.get(signo, (f"signal {signo or '?'}", ""))
    print(f"Signal: {sig}")
    if hint:
        print(f"Hint:   {hint}")
    print()
    print(f"Faulting frame #{fault_frame}:")
    frame_part = detail.split("=== all threads ===")[0].splitlines()
    print("\n".join([line for line in frame_part if re.match(r"^(#|=>|   0x)", line)][:12]))
    m = re.search(rf"^#{re.escape(fault_frame)}  *(0x[0-9a-f]+) in ", bt, re.M)
    if m:
        print(flush=True)
        helper("inspect_code.py", ctx.dir, "pc2func", m.group(1), ctx.bin)
    print()
    print("Guest call chain (innermost first):")
    for name in re.findall(r"in (sub_8[0-9A-Fa-f]{7}|xstart)", bt)[:20]:
        print(f"  {name}")
    print()
    print(f"Full dump: {out}")
    return 0


# --------------------------------------------------------------------------
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

WAIT_PLUMBING_RE = re.compile(
    r"^(__syscall_cancel|__internal_syscall|__futex|futex_|clock_nanosleep|__GI_|__pthread_cond|pthread_cond|"
    r"___pthread_cond|__lll_|__nanosleep|nanosleep|syscall|\?\?)")
THREAD_RE = re.compile(r"^Thread [0-9]+ \(Thread")


def _frame_name(line: str) -> str:
    f = re.sub(r"^#[0-9]+ +(0x[0-9a-f]+ in )?", "", line)
    f = re.sub(r" \(.*$", "", f)
    f = re.sub(r" at .*$", "", f)
    return re.sub(r" from .*$", "", f)


def _sample_summary(text: str) -> list[str]:
    count = defaultdict(int)
    total = defaultdict(int)
    state = {"tname": "", "sig": "", "depth": 0, "blocked": False}

    def flush():
        if state["tname"]:
            key = (state["tname"], ("[blocked] " if state["blocked"] else "") + state["sig"])
            count[key] += 1
            total[state["tname"]] += 1
        state.update(tname="", sig="", depth=0, blocked=False)

    for line in text.splitlines():
        if THREAD_RE.match(line):
            flush()
            m = re.search(r'\(LWP [0-9]+\) "([^"]*)', line)
            state["tname"] = m.group(1) if m else line
        elif re.match(r"^#[0-9]+ ", line):
            f = _frame_name(line)
            # Skip generic libc/pthread wait plumbing to reach the real waiter.
            if state["depth"] == 0 and WAIT_PLUMBING_RE.match(f):
                state["blocked"] = True
                continue
            if state["depth"] < 3:
                state["sig"] += (" <- " if state["depth"] else "") + f
                state["depth"] += 1
        elif line.startswith("=== SAMPLE"):
            flush()
    flush()

    out = []
    for tname in sorted(total):
        entries = sorted(((int(100 * c / total[tname]), sig) for (t, sig), c in count.items() if t == tname),
                         key=lambda e: -e[0])
        out.append(f"\n{tname}")
        for pct, sig in entries[:3]:
            out.append(f"  {pct:3d}%  {sig}")
    return out


def _hang_summary(text: str) -> list[str]:
    out = []
    hdr, top, guest = None, "", []

    def flush():
        if hdr is None:
            return
        state = "SPINNING in guest code" if GUEST_FN_RE.search(top) else "blocked/host"
        out.append(f"{hdr}\n  [{state}] top: {top[:140]}\n  guest:{''.join(' ' + g for g in guest)}\n")

    for line in text.splitlines():
        if THREAD_RE.match(line):
            flush()
            hdr, top, guest = line, "", []
        elif re.match(r"^#[0-9]+ ", line):
            if not top:
                top = line
            m = GUEST_FN_RE.search(line)
            if m:
                guest.append(m.group(0))
    flush()
    return out


def cmd_hang(args: list[str]) -> int:
    ctx = load_project()
    samples, interval, pid = 0, 0.5, None
    i = 0
    while i < len(args):
        if args[i] == "--samples" and i + 1 < len(args):
            samples, i = int(args[i + 1]), i + 2
        elif args[i] == "--interval" and i + 1 < len(args):
            interval, i = float(args[i + 1]), i + 2
        else:
            pid, i = int(args[i]), i + 1
    if pid is None:
        pid = _game_pid(ctx)

    if samples > 0:
        out = ctx.out_dir / f"stacks_{_stamp()}.txt"
        print(f"Taking {samples} snapshots of {pid} every {interval}s ...", flush=True)
        with open(out, "a") as f:
            for n in range(1, samples + 1):
                f.write(f"=== SAMPLE {n}\n")
                f.flush()
                r = subprocess.run([*GDB_BASE[:2], "-p", str(pid), *GDB_BASE[2:],
                                    *_ex("thread apply all bt 8", "detach", "quit")],
                                   capture_output=True, text=True, errors="replace")
                f.write(r.stdout + r.stderr)
                f.flush()
                if PTRACE_DENIED in r.stdout + r.stderr:
                    die("ptrace denied: run with sudo or set kernel.yama.ptrace_scope=0")
                time.sleep(interval)
        print("\n".join(_sample_summary(out.read_text(errors="replace"))))
        print(f"Raw stacks: {out}")
        return 0

    out = ctx.out_dir / f"hang_{_stamp()}.txt"
    print(f"Attaching to {pid} ...", flush=True)
    r = subprocess.run([*GDB_BASE[:2], "-p", str(pid), *GDB_BASE[2:],
                        *_ex("info threads", "thread apply all bt 40", "detach", "quit")],
                       capture_output=True, text=True, errors="replace")
    text = r.stdout + r.stderr
    out.write_text(text)
    if PTRACE_DENIED in text:
        die("ptrace denied: run with sudo or set kernel.yama.ptrace_scope=0")
    print("\n".join(_hang_summary(text)))
    print(f"Full dump: {out}   (process {pid} detached, still running)")
    return 0


# --------------------------------------------------------------------------
# rex peek [--thread NAME] [--func sub_X] [--stack BYTES] [--addrs A,B]
#          [--wait-log REGEX] [pid]
# Attaches gdb to the running game and dumps one guest thread's PPC state:
# registers from the frame's ctx, the guest stack frame from r1, and the
# headers/contents of everything the frame points at (big-endian). For a
# spinning thread found by `rex hang`, this shows the values its loop tests.
# Saves to _tools/logs/peek_<ts>.txt, detaches, leaves the game running.
# --addrs takes EXPR[:LEN] items: hex, rN, rN+off, [EXPR] (see py/gdb_guest.py).
# --save EXPR:LEN:PATH[,...] writes raw guest memory to files (e.g. a frame).
# --wait-log REGEX waits until the running game's current log matches (e.g. the
# first "Unhandled guest access violation") and peeks at that moment, for
# problems that only happen sometimes. Ctrl-C to give up.
# Needs ptrace permission, like `rex hang`.

PEEK_OPTIONS = {"--thread": "REX_PEEK_THREAD", "--func": "REX_PEEK_FUNC", "--stack": "REX_PEEK_STACK",
                "--addrs": "REX_PEEK_ADDRS", "--save": "REX_PEEK_SAVE"}


def cmd_peek(args: list[str]) -> int:
    ctx = load_project()
    env = dict(os.environ)
    pid, wait_re = None, ""
    i = 0
    while i < len(args):
        a = args[i]
        if a in PEEK_OPTIONS and i + 1 < len(args):
            env[PEEK_OPTIONS[a]], i = args[i + 1], i + 2
        elif a == "--wait-log" and i + 1 < len(args):
            wait_re, i = args[i + 1], i + 2
        else:
            pid, i = int(a), i + 1
    if pid is None:
        pid = _game_pid(ctx)

    if wait_re:
        run = latest_run_number(ctx)
        if not run:
            die(f"no run log in {ctx.log_dir}")
        log = ctx.log_dir / f"{ctx.name}_{run}.log"  # newest segment; rotation renames older ones
        rx = re.compile(wait_re)
        print(f"Waiting for /{wait_re}/ in {log.name} (pid {pid}) ...", flush=True)
        while True:
            try:
                text = log.read_text(errors="replace")
            except OSError:
                text = ""
            m = rx.search(text)
            if m:
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.start())
                print(f"Matched: {text[line_start:line_end if line_end >= 0 else None][:200]}")
                break
            try:
                os.kill(pid, 0)
            except OSError:
                die("game exited before the log matched")
            time.sleep(0.3)

    out = ctx.out_dir / f"peek_{_stamp()}.txt"
    r = subprocess.run([*GDB_BASE[:2], "-p", str(pid), *GDB_BASE[2:], "-x", str(PY_DIR / "gdb_guest.py"),
                        *_ex("detach", "quit")], capture_output=True, text=True, errors="replace", env=env)
    text = r.stdout + r.stderr
    out.write_text(text)
    if PTRACE_DENIED in text:
        die("ptrace denied: run with sudo or set kernel.yama.ptrace_scope=0")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("thread ")), len(lines))
    print("\n".join(line for line in lines[start:] if not line.startswith(("[Inferior", "Detaching"))))
    print(f"Saved: {out}   (process {pid} detached, still running)")
    return 0
