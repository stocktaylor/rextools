"""Shared setup for rextools commands: platform, SDK paths, project discovery.

Project discovery: $REX_PROJECT, else the nearest directory at or above the
current one containing a rexglue manifest (*.toml with a [project] table).

Overridable environment:
  REXGLUE_SDK    SDK checkout          (default: <rextools>/../rexglue-sdk)
  REX_PRESET     game build preset     (default: linux-amd64-relwithdebinfo, or
                                        win-<arm64|amd64>-relwithdebinfo on Windows)
  REX_LOG_LEVEL  run log level         (default: info; trace is very slow)
  REX_GPU        GPU plugin            (default: xenos)
  REX_PRESENT    vsync | uncapped      (default: vsync; uncapped redraws at 1000s of FPS)
"""
import filecmp
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

PY_DIR = Path(__file__).resolve().parent.parent
REXTOOLS_DIR = PY_DIR.parent
sys.path.insert(0, str(PY_DIR))
from rexinfo import Project  # noqa: E402

IS_WINDOWS = sys.platform == "win32"


class RexError(Exception):
    """A fatal error with a message for the user (printed as `rex: ...`)."""


def die(msg: str):
    raise RexError(msg)


def note(msg: str):
    print(f"rex: {msg}", file=sys.stderr, flush=True)


def _windows_native_arch() -> str:
    """arm64 or amd64 for the machine, even when Python runs emulated.

    An x64 Python on ARM64 sees PROCESSOR_ARCHITECTURE=AMD64 and
    platform.machine()=AMD64; IsWow64Process2 reports the native machine.
    """
    try:
        import ctypes
        from ctypes import wintypes

        process_machine = wintypes.USHORT()
        native_machine = wintypes.USHORT()
        kernel32 = ctypes.windll.kernel32
        if kernel32.IsWow64Process2(kernel32.GetCurrentProcess(), ctypes.byref(process_machine),
                                    ctypes.byref(native_machine)):
            return "arm64" if native_machine.value == 0xAA64 else "amd64"
    except (AttributeError, OSError):
        pass
    return "arm64" if os.environ.get("PROCESSOR_IDENTIFIER", "").startswith("ARM") else "amd64"


if IS_WINDOWS:
    DEFAULT_PRESET = f"win-{_windows_native_arch()}-relwithdebinfo"
    EXE_SUFFIX = ".exe"
    # The SDK presets name clang/clang++; LLVM's installer doesn't add itself to PATH.
    _llvm_bin = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LLVM" / "bin"
    if not shutil.which("clang++") and (_llvm_bin / "clang++.exe").exists():
        os.environ["PATH"] = f"{_llvm_bin}{os.pathsep}{os.environ.get('PATH', '')}"
else:
    _arch = "arm64" if platform.machine() in ("aarch64", "arm64") else "amd64"
    DEFAULT_PRESET = f"linux-{_arch}-relwithdebinfo"
    EXE_SUFFIX = ""

PRESET = os.environ.get("REX_PRESET") or DEFAULT_PRESET
SDK_DIR = Path(os.environ.get("REXGLUE_SDK") or (REXTOOLS_DIR.parent / "rexglue-sdk")).resolve()
# The SDK installs per platform, named like the preset without the config
# (linux-amd64, win-arm64, ...).
SDK_PLATFORM = PRESET.rsplit("-", 1)[0]
SDK_INSTALL = SDK_DIR / "out" / "install" / SDK_PLATFORM
SDK_LIB = SDK_INSTALL / ("bin" if IS_WINDOWS else "lib64")
REXGLUE_BIN = SDK_INSTALL / "bin" / f"rexglue{EXE_SUFFIX}"


def sdk_lib_file(name: str, config_suffix: str) -> str:
    """An SDK shared library's file name: librexruntimerd.so / rexruntimerd.dll."""
    return f"{name}{config_suffix}.dll" if IS_WINDOWS else f"lib{name}{config_suffix}.so"


def config_suffix() -> str:
    """The SDK library suffix of the preset's build config ("", "rd" or "d")."""
    if PRESET.endswith("relwithdebinfo"):
        return "rd"
    if PRESET.endswith("debug"):
        return "d"
    return ""


def env_with_sdk_libs(env: dict | None = None) -> dict:
    """The environment with the SDK libraries on the dynamic library search
    path: LD_LIBRARY_PATH, or PATH on Windows (where DLLs are found through it)."""
    env = dict(os.environ if env is None else env)
    if IS_WINDOWS:
        env["PATH"] = f"{SDK_LIB}{os.pathsep}{env.get('PATH', '')}"
    else:
        old = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = f"{SDK_LIB}:{old}" if old else str(SDK_LIB)
    return env


def helper(script: str, *args, **kwargs) -> subprocess.CompletedProcess:
    """Runs one of the standalone helper scripts in py/ (or renderdoc/) with
    this interpreter, isolated like `python3 -I -B`."""
    path = Path(script) if os.path.isabs(script) else PY_DIR / script
    return subprocess.run([sys.executable, "-I", "-B", str(path), *map(str, args)], **kwargs)


def rex_subcommand(*args, **kwargs) -> subprocess.CompletedProcess:
    """Runs another `rex` command in a child process (like calling `rex` again)."""
    return subprocess.run([sys.executable, "-I", "-B", str(PY_DIR / "rex.py"), *map(str, args)],
                          **kwargs)


def mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def find_project_dir() -> Path | None:
    if os.environ.get("REX_PROJECT"):
        return Path(os.environ["REX_PROJECT"]).resolve()
    d = Path.cwd().resolve()
    for candidate in (d, *d.parents):
        for f in candidate.glob("*.toml"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"^\[project\]", text, re.M):
                return candidate
    return None


class Ctx:
    """A loaded project: the manifest's paths plus the derived build paths."""

    def __init__(self, project: Project):
        self.project = project
        self.dir = project.dir
        self.name = project.name
        self.manifest = project.manifest
        self.game_root = project.game_root
        self.gen_dir = project.gen_dir
        self.funcs_file = project.funcs_file
        self.build_dir = self.dir / "out" / "build" / PRESET
        self.bin = self.build_dir / f"{self.name}{EXE_SUFFIX}"
        self.log_dir = self.build_dir / "logs"
        self.out_dir = self.dir / "_tools" / "logs"


def load_project() -> Ctx:
    d = find_project_dir()
    if d is None:
        die("not inside a rexglue project (no *_manifest.toml found); cd into one or set REX_PROJECT")
    try:
        project = Project(d)
    except Exception as e:  # noqa: BLE001 - any manifest problem is fatal here
        die(f"could not read manifest in {d}: {e}")
    ctx = Ctx(project)
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(ctx.dir)
    return ctx


def _run_log_files(ctx: Ctx):
    """(run number string, segment or None, path) for every run log."""
    rx = re.compile(rf"^{re.escape(ctx.name)}_([0-9]+)(?:\.([0-9]+))?\.log$")
    if not ctx.log_dir.is_dir():
        return
    for p in ctx.log_dir.iterdir():
        m = rx.match(p.name)
        if m:
            yield m.group(1), (int(m.group(2)) if m.group(2) else None), p


def latest_run_number(ctx: Ctx) -> str | None:
    """Highest run number N among <log_dir>/<name>_N[.k].log, or None."""
    runs = [n for n, _, _ in _run_log_files(ctx)]
    return max(runs, key=int) if runs else None


def run_log_bytes(ctx: Ctx, n: str) -> bytes:
    """One run's log with rotated segments merged in time order
    (<name>_N.K.log is older for larger K; <name>_N.log is newest)."""
    segs = sorted(((seg, p) for num, seg, p in _run_log_files(ctx) if num == n and seg is not None),
                  reverse=True)
    data = b"".join(p.read_bytes() for _, p in segs)
    newest = ctx.log_dir / f"{ctx.name}_{n}.log"
    if newest.exists():
        data += newest.read_bytes()
    return data


def sync_gpu_plugin(ctx: Ctx):
    """The SDK copies GPU plugins next to the game binary in a POST_BUILD step,
    which only runs when the game relinks. The game never links the plugin
    (it's dlopen'd), so an SDK reinstall leaves a stale copy that the game keeps
    loading. Refresh it whenever the installed plugin differs. On Windows the
    runtime DLL is also loaded from the copy next to the game, so it goes stale
    the same way."""
    suffix = config_suffix()
    names = [sdk_lib_file(f"rexgpu-{os.environ.get('REX_GPU') or 'xenos'}", suffix)]
    if IS_WINDOWS:
        names.append(sdk_lib_file("rexruntime", suffix))
    for name in names:
        src, dst = SDK_LIB / name, ctx.build_dir / name
        if not src.is_file() or not ctx.build_dir.is_dir():
            continue
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            try:
                shutil.copyfile(src, dst)
                note(f"refreshed stale {name} from the SDK install")
            except OSError as e:
                note(f"could not refresh {name}: {e}")


def append_function(ctx: Ctx, addr: int, comment: str) -> tuple[int, Path | None]:
    """Adds `<addr> = {}` to the functions table of the binary (entrypoint or a
    [[modules]] DLL) whose address range holds it, matching the file's key style.
    Returns (0, file) when added, (1, None) if no binary owns it (with a
    message), (2, file) if it is already listed."""
    addr_s = f"0x{addr:08X}"
    module = ctx.project.module_for(addr)
    funcs_file = module.funcs_file if module else None
    if funcs_file is None:
        print(f"{addr_s}: no recompiled binary with a functions table holds this address:")
        sys.stdout.flush()
        helper("rexinfo.py", ctx.dir, "--modules")
        print("If it is outside every range above, the code belongs to a module that is not in")
        print("the manifest (or was loaded from a guest path that doesn't match its guest_path).")
        print("Otherwise give that binary a functions table: the manifest's last table, or an")
        print("include file whose last table is [functions].")
        return 1, None
    text = funcs_file.read_text(encoding="utf-8", errors="replace")
    # Either key style: 0x8210EB48 = {} or "0x8210EB48" = {}
    if re.search(rf'^"?{addr_s}"? *=', text, re.M | re.I):
        return 2, funcs_file
    key = f'"{addr_s}"' if re.search(r'^"0x[0-9A-Fa-f]+" *=', text, re.M) else addr_s
    with open(funcs_file, "a", encoding="utf-8", newline="") as f:
        f.write(f"\n# {comment}\n{key} = {{}}\n")
    return 0, funcs_file
