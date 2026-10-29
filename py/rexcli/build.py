"""rex new / build / run / triage."""
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .common import (EXE_SUFFIX, IS_WINDOWS, PRESET, REXGLUE_BIN, SDK_LIB, Ctx, append_function, die,
                     env_with_sdk_libs, helper, latest_run_number, load_project, mtime, note,
                     rex_subcommand, run_log_bytes, sdk_lib_file, sync_gpu_plugin)


def _run_tee(cmd: list[str], out_path: Path) -> int:
    """Runs cmd, printing its combined output and saving it to out_path."""
    with open(out_path, "w", encoding="utf-8", newline="") as out:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for raw in proc.stdout:
            # Windows tools end lines with \r\n, and text-mode stdout would add
            # another \r: normalize to \n first.
            line = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
            sys.stdout.write(line)
            sys.stdout.flush()
            out.write(line)
        return proc.wait()


# --------------------------------------------------------------------------
# rex new <name> <xex_path> [--game-root DIR]
# Bootstraps a rexglue project in the current directory, covering the traps
# `rexglue init` leaves behind:
#  - init refuses to run if the manifest exists, and --force would overwrite
#    a hand-written one: an existing <name>_manifest.toml is kept aside and
#    restored;
#  - init omits GPU_PLUGINS, so the game can't load its renderer: added;
#  - manifest gets a header identifying the XEX and a trailing
#    [entrypoint.functions] table for `rex triage`;
#  - .gitignore and git init.

def cmd_new(args: list[str]) -> int:
    if len(args) < 2:
        die("usage: rex new <name> <xex_path> [--game-root DIR]")
    name, xex = args[0], args[1]
    game_root_args = []
    if len(args) > 2 and args[2] == "--game-root":
        if len(args) < 4 or not args[3]:
            die("--game-root needs a directory")
        game_root_args = ["--game-root", args[3]]
    if not Path(xex).is_file():
        die(f"XEX not found: {xex}")
    if not REXGLUE_BIN.is_file():
        die(f"rexglue CLI not found at {REXGLUE_BIN} (build/install the SDK, or set REXGLUE_SDK)")

    manifest = Path(f"{name}_manifest.toml")
    kept = None
    if manifest.exists():
        kept = Path(f"{manifest}.rex-keep")
        manifest.replace(kept)
        note(f"keeping existing {manifest} aside during init")
    rc = subprocess.run([str(REXGLUE_BIN), "init", "--project-name", name, "--xex-path", xex,
                         *game_root_args], env=env_with_sdk_libs()).returncode
    if kept:
        kept.replace(manifest)
        note(f"restored your {manifest}")
    if rc != 0:
        die("rexglue init failed")

    cmakelists = Path("CMakeLists.txt")
    if cmakelists.exists():
        with open(cmakelists, encoding="utf-8", newline="") as f:
            text = f.read()
        text = re.sub(r"^rexglue_setup_target\(([A-Za-z0-9_]+)\)(\r?)$",
                      r"rexglue_setup_target(\1 GPU_PLUGINS xenos)\2", text, flags=re.M)
        with open(cmakelists, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    if not cmakelists.exists() or "GPU_PLUGINS" not in cmakelists.read_text(encoding="utf-8"):
        note("warning: could not add GPU_PLUGINS xenos to CMakeLists.txt; add it by hand")

    if not kept:
        helper("newproject.py", manifest, xex)

    gitignore = Path(".gitignore")
    gitignore.touch()
    existing = gitignore.read_text(encoding="utf-8").splitlines()
    game_dir = os.path.dirname(xex) or "."
    with open(gitignore, "a", encoding="utf-8", newline="") as f:
        for line in (f"{game_dir}/", "out/", "tmp/", "_tools/logs/"):
            if line not in existing:
                f.write(line + "\n")
                existing.append(line)
    if subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True).returncode != 0:
        subprocess.run(["git", "init", "-q"])
    note("git repository ready")

    print(f"""
Project '{name}' created. Next:
  rex build          # first build: configure -> codegen -> configure -> build -> lint
                     # fix any 'UnresolvedCall' codegen errors by adding the
                     # targets to [entrypoint.functions] in {manifest}
  rex triage         # run/auto-fix loop until it gets past init
  rex sdk check      # if anything looks off with the SDK install""")
    return 0


# --------------------------------------------------------------------------
# rex build [--force-codegen] [-DNAME=VALUE...]
# -D options go to every CMake configure step, e.g. a project option like
# `rex build -DBLADE_LAUNCHER=OFF`. CMake caches them, so later builds keep
# the value until you pass a different one.
# codegen -> configure -> build -> lint, in the order rexglue needs:
#  - the <name>_codegen target only exists once the build dir is configured,
#  - generated/<mod>/sources.cmake is only picked up at configure time, so a
#    configure must follow every codegen (else: undefined PPCImageConfig),
#  - codegen's stamp ignores the SDK itself, so after an SDK codegen change
#    it would report "up to date" and keep the old output.

def cmd_build(args: list[str]) -> int:
    ctx = load_project()
    force = False
    cmake_defs = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--force-codegen":
            force = True
        elif a == "-D" and i + 1 < len(args):
            cmake_defs.append(f"-D{args[i + 1]}")
            i += 1
        elif a.startswith("-D") and len(a) > 2:
            cmake_defs.append(a)
        else:
            die(f"build: unknown option '{a}'")
        i += 1

    stamp = ctx.gen_dir / "codegen.stamp"
    codegen_lib = SDK_LIB / sdk_lib_file("rexruntime", "")  # the rexglue CLI links the Release runtime
    if stamp.exists() and mtime(codegen_lib) > mtime(stamp):
        note("SDK Release runtime is newer than the last codegen; forcing codegen")
        force = True
    # One stamp per binary (entrypoint and each [[modules]] DLL).
    if force:
        gen = ctx.dir / "generated"
        for s in [*gen.glob("codegen.stamp"), *gen.glob("*/codegen.stamp")]:
            s.unlink(missing_ok=True)

    cmakelists = ctx.dir / "CMakeLists.txt"
    if not cmakelists.exists() or "GPU_PLUGINS" not in cmakelists.read_text(encoding="utf-8"):
        note("warning: CMakeLists.txt has no GPU_PLUGINS (rexglue_setup_target(... GPU_PLUGINS xenos)); "
             "the game will fail to load its GPU plugin")

    configure = ["cmake", "--preset", PRESET, "-DREXGLUE_RECOMP_DEBUG_INFO=full", *cmake_defs]
    cache = ctx.build_dir / "CMakeCache.txt"
    # A failed first configure leaves CMakeCache.txt but no build.ninja; treat
    # that as unconfigured, or the codegen step dies with a bare ninja error.
    if not cache.exists() or not (ctx.build_dir / "build.ninja").exists():
        if subprocess.run(configure).returncode != 0:
            die("initial configure failed")
    elif cmake_defs:
        # Apply new -D values before anything (including codegen) is built.
        if subprocess.run(configure, stdout=subprocess.DEVNULL).returncode != 0:
            die(f"configure with {' '.join(cmake_defs)} failed")
    # An empty CMAKE_CXX_FLAGS_<CONFIG> in the cache (seen on Ridge after its
    # first configure) silently builds everything at -O0 - several times slower.
    cache_text = cache.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^CMAKE_BUILD_TYPE:STRING=(.*)$", cache_text, re.M)
    bt_upper = (m.group(1).strip() if m else "").upper()
    want_flags = {"RELWITHDEBINFO": "-O2 -g -DNDEBUG", "RELEASE": "-O3 -DNDEBUG"}.get(bt_upper, "")
    m = re.search(rf"^CMAKE_CXX_FLAGS_{bt_upper}:STRING=(.*)$", cache_text, re.M) if bt_upper else None
    cur_flags = m.group(1).strip() if m else ""
    if want_flags and not re.search(r"-O[123s]", cur_flags):
        note(f"CMAKE_CXX_FLAGS_{bt_upper} is '{cur_flags}' (no optimization) - resetting to "
             f"'{want_flags}'; expect a full rebuild")
        if subprocess.run([*configure, f"-DCMAKE_CXX_FLAGS_{bt_upper}={want_flags}",
                           f"-DCMAKE_C_FLAGS_{bt_upper}={want_flags}"],
                          stdout=subprocess.DEVNULL).returncode != 0:
            die("reconfigure with optimization flags failed")

    # Codegen's Validate phase stops on branch targets that function discovery
    # missed ("UnresolvedCall: b 0xX from 0xY - target not in any function", a
    # tail-branch-only target). Add each to its binary's functions table and
    # re-run. Codegen stops at the first failing binary, so a DLL's batch only
    # appears once the entrypoint is clean.
    codegen_log = ctx.out_dir / "codegen.txt"
    unresolved_re = re.compile(r"^ +(0x[0-9A-Fa-f]{8}) from (0x[0-9A-Fa-f]{8}): .* target not in any function$",
                               re.M)
    pass_no = 1
    while True:
        rc = _run_tee(["cmake", "--build", "--preset", PRESET, "--target", f"{ctx.name}_codegen"],
                      codegen_log)
        if rc == 0:
            break
        log_text = codegen_log.read_text(encoding="utf-8", errors="replace")
        found = {}
        for t, frm in unresolved_re.findall(log_text):
            found.setdefault(t, frm)
        if not found:
            die(f"codegen failed (output: {codegen_log})")
        if pass_no > 20:
            die(f"codegen still reports unresolved calls after 20 passes (output: {codegen_log})")
        added = 0
        for t in sorted(found):
            frm = found[t]
            status, path = append_function(
                ctx, int(t, 16), f"Added by rex build: codegen UnresolvedCall, tail-branch target (b from {frm}).")
            if status == 0:
                added += 1
                note(f"added {t} to {path.name}")
            elif status == 1:
                die(f"codegen: unresolved call target {t} is in no binary's range")
            else:
                note(f"{t} is already in {path.name} but codegen still can't place it; "
                     f"it may be mid-function (rex containing {t})")
        if not added:
            die(f"codegen failed; no new unresolved-call targets to add (output: {codegen_log})")
        pass_no += 1
        note(f"added {added} function(s); re-running codegen (pass {pass_no})")

    if subprocess.run(configure, stdout=subprocess.DEVNULL).returncode != 0:
        die("configure failed")
    if subprocess.run(["cmake", "--build", "--preset", PRESET]).returncode != 0:
        die("build failed")
    sync_gpu_plugin(ctx)

    print("--- lint ---", flush=True)
    if helper("lint.py", ctx.dir).returncode != 0:
        note("lint found likely codegen misreads (see above)")
    return 0


# --------------------------------------------------------------------------
# rex run [game args...]
# Runs the game with the SDK on the library path (LD_LIBRARY_PATH, or PATH on
# Windows) and the flags every project needs. --gpu_plugin is required:
# without it you get a window with no video. Each default is only added if you
# didn't pass that flag yourself (the cvar parser rejects duplicates), e.g.
# `rex run --log_level trace` overrides info.
# Logs land in out/build/<preset>/logs/<name>_NNN.log (see `rex log`).
# Presents once per guest frame (--present_on_guest_swap) unless
# REX_PRESENT_ON_GUEST_SWAP=false.
# rex run --renderdoc [game args...]
# Runs under RenderDoc (renderdoccmd capture); press F12 in game to capture a
# frame into _tools/captures/. On Linux forces an X11 window (RenderDoc has no
# Wayland support: without it the window never appears) and keeps vsync
# presentation (uncapped presents make most captures empty re-presents of the
# last frame). Afterwards lists the captures and flags the empty ones; analyse
# with rex rdc.

def game_command(ctx: Ctx, user_args: list[str]) -> tuple[list[str], dict]:
    """The game's command line (standard defaults + the user's args) and env."""
    def user_has(name: str) -> bool:
        return any(a == f"--{name}" or a.startswith(f"--{name}=") for a in user_args)

    defaults = []
    if not user_has("game_data_root"):
        defaults += ["--game_data_root", str(ctx.game_root)]
    if not user_has("gpu_plugin"):
        defaults += ["--gpu_plugin", os.environ.get("REX_GPU") or "xenos"]
    if not user_has("log_level"):
        defaults += ["--log_level", os.environ.get("REX_LOG_LEVEL") or "info"]
    if not user_has("log_flush_interval"):
        defaults += ["--log_flush_interval", "1"]
    # One host present per guest frame (SDK present_on_guest_swap): frame-rate
    # tools like MangoHud then measure the game, and VRR displays pace it exactly.
    # REX_PRESENT_ON_GUEST_SWAP=false restores the decoupled UI-thread presenter.
    if not user_has("present_on_guest_swap"):
        defaults.append(f"--present_on_guest_swap={os.environ.get('REX_PRESENT_ON_GUEST_SWAP') or 'true'}")
    # Present at the display's refresh rate (FIFO) instead of redrawing the
    # window as fast as possible (thousands of FPS, ~0.3 core + GPU for
    # nothing). REX_PRESENT=uncapped restores the SDK default. Vulkan only:
    # Windows builds render with D3D12 and have no such cvars.
    if (os.environ.get("REX_PRESENT") or "vsync") == "vsync" and not IS_WINDOWS:
        if not user_has("vulkan_allow_present_mode_immediate"):
            defaults.append("--vulkan_allow_present_mode_immediate=false")
        if not user_has("vulkan_allow_present_mode_mailbox"):
            defaults.append("--vulkan_allow_present_mode_mailbox=false")
    return [str(ctx.bin), *defaults, *user_args], env_with_sdk_libs()


def _run_under_renderdoc(ctx: Ctx, argv: list[str], env: dict, prefix: str, cwd: Path) -> int:
    """Runs argv under renderdoccmd, capturing into _tools/captures/<prefix>_*,
    then lists the captures this run took (flagging empty ones)."""
    import shutil
    if not shutil.which("renderdoccmd"):
        die("renderdoccmd not found (install RenderDoc and put it on PATH)")
    cap_dir = ctx.dir / "_tools" / "captures"
    cap_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    print("Running under RenderDoc - press F12 in game to capture a frame.", flush=True)
    if not IS_WINDOWS:
        env["SDL_VIDEO_DRIVER"] = "x11"  # RenderDoc has no Wayland support
    capture_file = cap_dir / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"
    subprocess.run(["renderdoccmd", "capture", "--working-dir", str(cwd),
                    "--capture-file", str(capture_file), *argv], env=env)
    print()
    new = sorted(p.name for p in cap_dir.glob("*.rdc") if p.stat().st_mtime >= started)
    if not new:
        print("No captures taken.")
    else:
        from .misc import rdc_list_lines
        print("Captures from this run:")
        for line in rdc_list_lines(cap_dir):
            if any(n in line for n in new):
                print(line)
        print("Next: rex rdc dump <capture.rdc>")
    return 0


def cmd_run(args: list[str]) -> int:
    ctx = load_project()
    if not ctx.bin.is_file():
        die(f"no binary at {ctx.bin} (run 'rex build')")
    renderdoc = "--renderdoc" in args
    user_args = [a for a in args if a != "--renderdoc"]
    sync_gpu_plugin(ctx)
    argv, env = game_command(ctx, user_args)

    if renderdoc:
        if (os.environ.get("REX_PRESENT") or "vsync") != "vsync":
            note("warning: REX_PRESENT=uncapped makes most captures empty")
        return _run_under_renderdoc(ctx, argv, env, ctx.name, ctx.dir)

    return _exec(argv, env)


# --------------------------------------------------------------------------
# rex xenia [--renderdoc] [xenia args...]
# Runs the reference emulator on the project's XEX (the manifest's
# [entrypoint] file_path): <project>/xenia/xenia_edge, started from that
# folder (so its config, log and portable data stay there) with that folder
# first on the library path (the SDL it ships with). --renderdoc captures
# into _tools/captures/xenia_* like `rex run --renderdoc`, for
# `rex rdc align` against the recomp.

def cmd_xenia(args: list[str]) -> int:
    ctx = load_project()
    xenia_dir = ctx.dir / "xenia"
    exe = xenia_dir / f"xenia_edge{EXE_SUFFIX}"
    if not exe.is_file():
        die(f"no Xenia at {exe}")
    xex = ctx.project.modules[0].xex
    if not xex.is_file():
        die(f"no XEX at {xex} (the manifest's [entrypoint] file_path)")
    renderdoc = "--renderdoc" in args
    argv = [str(exe), *[a for a in args if a != "--renderdoc"], str(xex)]
    env = dict(os.environ)
    if not IS_WINDOWS:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            p for p in (str(xenia_dir), env.get("LD_LIBRARY_PATH")) if p)
    os.chdir(xenia_dir)

    if renderdoc:
        return _run_under_renderdoc(ctx, argv, env, "xenia", xenia_dir)

    return _exec(argv, env)


def _exec(argv: list[str], env: dict) -> int:
    """Replaces this process with argv (Windows: runs it and waits)."""
    sys.stdout.flush()
    if IS_WINDOWS:
        # No real exec on Windows: wait for the game and pass on its exit code.
        try:
            return subprocess.run(argv, env=env).returncode
        except KeyboardInterrupt:
            return 130
    os.execve(argv[0], argv, env)
    return 0  # not reached


# --------------------------------------------------------------------------
# rex triage [timeout_secs] [--hang-dump] [--no-build]
# Build, run, classify the outcome, and fix what can be fixed automatically:
#
#   "Call to invalid or unregistered function at guest address X" in the log
#       -> add X to the functions table, rebuild, go again
#   killed by a signal / crashed -> `rex crash` post-mortem (Linux), stop
#   still running at the timeout -> treated as "got past init"; with
#       --hang-dump, dump all thread stacks first (`rex hang`, Linux), then stop
#   anything else       -> show the log tail, stop
#
# Runs the game plainly (no debugger attached): the SDK's fault handler
# self-heals some faults that a debugger would intercept first.

WINDOWS_EXCEPTIONS = {
    0xC0000005: "access violation",
    0xC000001D: "illegal instruction",
    0xC0000094: "integer divide by zero",
    0xC00000FD: "stack overflow",
    0xC0000409: "fail-fast / stack buffer overrun (abort)",
    0x80000003: "breakpoint",
    0xC0000374: "heap corruption",
}


def cmd_triage(args: list[str]) -> int:
    ctx = load_project()
    timeout_s = 60
    hang_dump = False
    build = True
    for a in args:
        if a == "--hang-dump":
            hang_dump = True
        elif a == "--no-build":
            build = False
        elif a.isdigit():
            timeout_s = int(a)
        else:
            die(f"triage: unknown option '{a}'")
    os.environ["REX_PROJECT"] = str(ctx.dir)
    # Unattended runs: skip the in-window launch menu of projects that use rexlauncher.
    os.environ["REXLAUNCHER_SKIP"] = "1"

    for iteration in range(1, 101):
        print(f"=== triage iteration {iteration} ===", flush=True)
        if build or iteration > 1:
            if rex_subcommand("build").returncode != 0:
                die("build failed - fix it, then re-run triage")
        if not IS_WINDOWS:
            rex_subcommand("clean-shm", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        before = latest_run_number(ctx)
        if not ctx.bin.is_file():
            die(f"no binary at {ctx.bin} (run 'rex build')")
        sync_gpu_plugin(ctx)
        argv, env = game_command(ctx, [])
        stdout_path = ctx.out_dir / "triage_stdout.txt"
        with open(stdout_path, "wb") as out:
            proc = subprocess.Popen(argv, env=env, stdout=out, stderr=subprocess.STDOUT)
            status = None
            for _ in range(timeout_s):
                if proc.poll() is not None:
                    break
                time.sleep(1)
            if proc.poll() is None:
                status = "timeout"
                if hang_dump and not IS_WINDOWS:
                    rex_subcommand("hang", proc.pid)
                proc.kill()
                proc.wait()
            else:
                status = proc.returncode

        run = latest_run_number(ctx)
        if run is None or run == before:
            print(f"--- no new log was written (exit status {status}). stdout/stderr:")
            lines = stdout_path.read_text(encoding="utf-8", errors="replace").splitlines()
            print("\n".join(lines[-20:]))
            return 1
        log_path = ctx.out_dir / "triage_run.log"
        log_path.write_bytes(run_log_bytes(ctx, run))
        log = log_path.read_text(encoding="utf-8", errors="replace")
        shown = f"still running after {timeout_s}s" if status == "timeout" else str(status)
        print(f"--- run {run} finished: {shown}", flush=True)

        m = re.search(r"Call to invalid or unregistered function at guest address (0x[0-9A-Fa-f]+)", log)
        if m:
            addr = int(m.group(1), 16)
            addr_s = f"0x{addr:08X}"
            # A target that is already a branch label inside some function is
            # not a function start: usually a switch case reached through a
            # jump table that codegen missed (emitted as an indirect call).
            # Registering it would split that function and leave branches back
            # into it unresolved.
            label_re = re.compile(rf"^loc_{addr_s[2:]}:", re.M)
            is_label = any(label_re.search(p.read_text(encoding="utf-8", errors="replace"))
                           for p in (ctx.dir / "generated").rglob("*_recomp.*.cpp"))
            if is_label:
                print(f"{addr_s} is unregistered, but it is a branch label inside an existing function:")
                sys.stdout.flush()
                helper("inspect_code.py", ctx.dir, "containing", addr_s)
                print("Not adding it as a function. Likely a jump table codegen missed: check")
                print("'rex lint' for MISSEDSW at that function's bctr.")
                return 1
            rc, path = append_function(ctx, addr, "Added by rex triage: unregistered indirect-call target.")
            if rc == 1:
                return 1
            if rc == 2:
                print(f"{addr_s} is already in {path.name} but still unregistered: the auto-detected")
                print(f"boundary is wrong, or it is mid-function. Look with: rex containing {addr_s}; rex asm {addr_s}")
                return 1
            print(f"Added {addr_s} to {path.name}; rebuilding.")
            continue

        if status == "timeout":
            print(f"Ran {timeout_s}s without a fatal error - looks past init. Stopping.")
            return 0
        print("--- last errors in log:")
        errors = [line for line in log.splitlines() if re.search(r"\[(critical|error)\]", line)]
        print("\n".join(errors[-5:]))
        print("--- log tail:")
        print("\n".join(log.splitlines()[-15:]))
        if IS_WINDOWS:
            code = status & 0xFFFFFFFF
            if code:
                name = WINDOWS_EXCEPTIONS.get(code, "")
                print()
                print(f"--- exited with 0x{code:08X}{f' ({name})' if name else ''}; "
                      "no post-mortem on Windows yet")
        elif status < 0:
            print()
            print(f"--- killed by signal {-status}; post-mortem:", flush=True)
            rex_subcommand("crash")
        return 1
    die("triage: 100 iterations without settling")
    return 1
