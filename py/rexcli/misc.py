"""rex log / clean-shm / sdk / rdc / dist."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .common import (IS_WINDOWS, REXTOOLS_DIR, SDK_DIR, SDK_LIB, SDK_PLATFORM, PRESET, die,
                     find_project_dir, helper, latest_run_number, load_project, mtime, note,
                     run_log_bytes, sdk_lib_file)


# --------------------------------------------------------------------------
# rex log [run#]
# Prints a run's log with its rotated 5MB segments merged in order. Defaults
# to the latest run. Pipe it: `rex log | grep -i xam` (or Select-String).

def cmd_log(args: list[str]) -> int:
    ctx = load_project()
    n = args[0] if args else latest_run_number(ctx)
    if not n:
        die(f"no logs in {ctx.log_dir}")
    if not n.isdigit():
        die(f"log: '{n}' is not a run number")
    sys.stdout.flush()
    sys.stdout.buffer.write(run_log_bytes(ctx, f"{int(n):03d}"))
    sys.stdout.flush()
    return 0


# --------------------------------------------------------------------------
# rex clean-shm [--dry-run]
# A crashed or killed game (or rexglue) never shm_unlink()s its guest memory
# backing file /dev/shm/xenia_memory_<tick> (~4.5GB each). Enough of them fill
# tmpfs, and new runs then die with SIGBUS on writes the host can't back -
# which looks like a memory bug but isn't. Removes every such file that no
# running process still maps. Does not need a project.
# Windows backs guest memory with a pagefile-backed file mapping that is freed
# when the process exits, so there is nothing to clean up there.

def cmd_clean_shm(args: list[str]) -> int:
    if IS_WINDOWS:
        print("Nothing to clean on Windows: guest memory is freed when the game exits.")
        return 0
    dry = bool(args) and args[0] == "--dry-run"
    files = sorted(Path("/dev/shm").glob("xenia_memory_*"))
    if not files:
        print("No xenia_memory_* files in /dev/shm.")
        return 0
    maps = []
    for p in Path("/proc").glob("[0-9]*/maps"):
        try:
            maps.append(p.read_text(errors="replace"))
        except OSError:
            pass
    for f in files:
        if any(f.name in m for m in maps):
            print(f"In use, kept: {f}")
        elif dry:
            print(f"Would remove: {f}")
        else:
            f.unlink(missing_ok=True)
            print(f"Removed: {f}")
    sys.stdout.flush()
    subprocess.run(["df", "-h", "/dev/shm"])
    return 0


# --------------------------------------------------------------------------
# rex sdk check
#   Is the installed SDK built from the current sources? The SDK build is
#   multi-config: the rexglue CLI (codegen) links the *Release* runtime
#   (librexruntime.so / rexruntime.dll) while games link the *RelWithDebInfo*
#   one (librexruntimerd.so / rexruntimerd.dll) - each needs its own install
#   after a source change.
# rex sdk patches status|apply
#   Local fixes kept as rextools/sdk-patches/*.patch, so a fresh SDK clone
#   (or `git checkout .`) can get them back.
# rex sdk patches export <name> <file>...
#   Save the SDK's current diff of the given files as sdk-patches/<name>.patch.

PATCH_DIR = REXTOOLS_DIR / "sdk-patches"


def _git(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(SDK_DIR), *args], **kwargs)


def _newest_src(*dirs: str) -> int:
    """Newest mtime among SDK sources under the given dirs."""
    newest = 0
    for d in dirs:
        for root, _, files in os.walk(SDK_DIR / d):
            for f in files:
                if f.endswith((".cpp", ".h", ".inc", ".inja")):
                    newest = max(newest, mtime(Path(root) / f))
    return newest


def _patches_status():
    patches = sorted(PATCH_DIR.glob("*.patch"))
    if not patches:
        print(f"  (no patches in {PATCH_DIR})")
        return
    for p in patches:
        quiet = {"capture_output": True}
        if _git("apply", "--check", "-R", str(p), **quiet).returncode == 0:
            print(f"  applied      {p.name}")
        elif _git("apply", "--check", str(p), **quiet).returncode == 0:
            print(f"  NOT APPLIED  {p.name}   (rex sdk patches apply)")
        else:
            print(f"  CONFLICT     {p.name}   (SDK changed underneath it; re-apply by hand)")


def _sdk_check() -> int:
    all_src = _newest_src("src", "include", "resources")
    codegen_src = _newest_src("src/codegen", "include/rex/codegen", "resources/templates")
    ok = True

    def check_lib(lib: str, src_mtime: int, preset: str, purpose: str):
        nonlocal ok
        t = mtime(SDK_LIB / lib)
        if t == 0:
            print(f"  MISSING  {lib} ({purpose})")
            ok = False
        elif src_mtime > t:
            print(f"  STALE    {lib} ({purpose}) - run: cmake --build --preset {preset} --target install")
            ok = False
        else:
            print(f"  ok       {lib} ({purpose})")

    print(f"SDK: {SDK_DIR}")
    check_lib(sdk_lib_file("rexruntime", ""), codegen_src, f"{SDK_PLATFORM}-release",
              "rexglue CLI / codegen")
    check_lib(sdk_lib_file("rexruntime", "rd"), all_src, f"{SDK_PLATFORM}-relwithdebinfo",
              "games, relwithdebinfo")
    # Only matters for projects built with a debug preset (REX_PRESET).
    if PRESET.endswith("debug"):
        check_lib(sdk_lib_file("rexruntime", "d"), all_src, f"{SDK_PLATFORM}-debug", "games, debug")
    print()
    describe = _git("describe", "--tags", "--always", capture_output=True, text=True).stdout.strip()
    print(f"Local changes vs {describe}:")
    status = _git("status", "--short", "--", "src", "include", "resources",
                  capture_output=True, text=True).stdout
    for line in status.splitlines():
        print(f"  {line}")
    print()
    _patches_status()
    return 0 if ok else 1


def cmd_sdk(args: list[str]) -> int:
    if not (SDK_DIR / ".git").exists():
        die(f"SDK checkout not found at {SDK_DIR} (set REXGLUE_SDK)")
    sub = args[0] if args else "check"
    sub2 = args[1] if len(args) > 1 else ""
    if sub == "check":
        return _sdk_check()
    if sub == "patches" and sub2 in ("status", ""):
        print(f"Patches in {PATCH_DIR}:")
        _patches_status()
        return 0
    if sub == "patches" and sub2 == "apply":
        for p in sorted(PATCH_DIR.glob("*.patch")):
            if _git("apply", "--check", "-R", str(p), capture_output=True).returncode == 0:
                print(f"already applied: {p.name}")
            elif _git("apply", str(p)).returncode == 0:
                print(f"applied: {p.name}")
            else:
                print(f"FAILED:  {p.name}")
        print("Rebuild and install both configs: release (codegen) and relwithdebinfo (games).")
        return 0
    if sub == "patches" and sub2 == "export":
        if len(args) < 4 or not args[2]:
            die("usage: rex sdk patches export <name> <file>...")
        name, files = args[2], args[3:]
        out = PATCH_DIR / f"{name}.patch"
        diff = _git("diff", "--", *files, capture_output=True).stdout
        if not diff:
            die("no changes in those files")
        out.write_bytes(diff)
        print(f"wrote {out}")
        return 0
    die("usage: rex sdk check | rex sdk patches status|apply|export")
    return 1


# --------------------------------------------------------------------------
RDC_HELP = """\
rex rdc list
  Captures in _tools/captures, with size; small ones are flagged as likely
  empty (the window re-presented without any game draws in that frame).
rex rdc dump <capture.rdc> [--events 1,2,3] [--out DIR] [--max-png N]
  Replays a capture inside qrenderdoc (headless) and writes text + PNGs:
  action tree, render-target snapshots, and for --events: constants,
  post-VS outputs, bound textures, blend state and SPIR-V of each draw.
  Default output dir: next to the capture, <name>_dump/.
rex rdc align <dumpA> <dumpB>
  Match render-target passes and individual draws between two dumps
  (e.g. this recomp vs Xenia) by their draw sequences.
rex rdc diff <dumpA> <eventA> <dumpB> <eventB>
  Compare one matched draw from each dump (inputs, textures, shaders).

Capture with `rex run --renderdoc`."""

RDC_DIR = REXTOOLS_DIR / "renderdoc"


def _captures_dir() -> Path:
    d = find_project_dir()
    return d / "_tools" / "captures" if d else Path.cwd()


def rdc_list_lines(cap_dir: Path) -> list[str]:
    files = sorted(cap_dir.glob("*.rdc"))
    if not files:
        return []
    sizes = {f: f.stat().st_size for f in files}
    biggest = max(sizes.values())
    lines = []
    for f in files:
        s = sizes[f]
        flag = "  <- likely empty (no game draws), safe to delete" if s < biggest // 10 else ""
        lines.append(f"{s // 1048576:6d} MB  {f.name}{flag}")
    return lines


def cmd_rdc(args: list[str]) -> int:
    sub = args[0] if args else ""
    rest = args[1:]
    if sub == "list":
        d = _captures_dir()
        lines = rdc_list_lines(d)
        if not lines:
            die(f"no captures in {d}")
        print("\n".join(lines))
        return 0
    if sub == "dump":
        if not rest or not Path(rest[0]).is_file():
            die("usage: rex rdc dump <capture.rdc> [--events 1,2] [--out DIR] [--max-png N]")
        cap = Path(rest[0]).resolve()
        if not shutil.which("qrenderdoc"):
            die("qrenderdoc not found (install RenderDoc and put it on PATH)")
        events, out, maxpng = "", None, "40"
        i = 1
        while i < len(rest):
            opt = rest[i]
            if opt in ("--events", "--out", "--max-png") and i + 1 < len(rest):
                if opt == "--events":
                    events = rest[i + 1]
                elif opt == "--out":
                    out = Path(rest[i + 1])
                else:
                    maxpng = rest[i + 1]
                i += 2
            else:
                die(f"rdc dump: unknown option '{opt}'")
        out = (out or cap.with_name(f"{cap.stem}_dump")).resolve()
        out.mkdir(parents=True, exist_ok=True)
        (out / "error.txt").unlink(missing_ok=True)
        print(f"Replaying {cap.name} -> {out} ...", flush=True)
        env = dict(os.environ, RDC_IN=str(cap), RDC_OUT=str(out), RDC_EVENTS=events, RDC_MAX_PNG=maxpng)
        subprocess.run(["qrenderdoc", "--python", str(RDC_DIR / "rdc_dump.py")], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        err = out / "error.txt"
        if err.exists() and err.stat().st_size:
            print(err.read_text(errors="replace"))
            die(f"dump failed (see {err})")
        summary = out / "summary.txt"
        if not summary.exists():
            die("qrenderdoc produced no output (did it start?)")
        for line in summary.read_text(errors="replace").splitlines():
            print(line)
            if "render-target snapshots" in line:
                break
        if events:
            print()
            print("Per-event detail:")
            for e in events.split(","):
                print(f"  {out / f'event_{e}.txt'}")
        print()
        print(f"Output: {out}")
        return 0
    if sub == "align":
        if len(rest) != 2:
            die("usage: rex rdc align <dumpA> <dumpB>")
        return helper(str(RDC_DIR / "rdc_tools.py"), "align", *rest).returncode
    if sub == "diff":
        if len(rest) != 4:
            die("usage: rex rdc diff <dumpA> <eventA> <dumpB> <eventB>")
        return helper(str(RDC_DIR / "rdc_tools.py"), "diff", *rest).returncode
    print(RDC_HELP)
    if sub:
        die(f"unknown rdc command '{sub}'")
    return 0


# --------------------------------------------------------------------------
# rex dist [--image IMG] [--config RelWithDebInfo|Release] [--march x86-64-v2|v3]
#          [--no-pull] [--shell]
# Builds the SDK and the project inside a Steam Runtime SDK container and
# packages a folder you can copy to any Linux machine (e.g. a Steam Deck):
#   dist/<name>-<tag>/<name>   + libs + <name>.toml + game/ + README.txt
# The user puts their extracted game files in game/ and runs ./<name>.
#
# Defaults: Steam Runtime 4 SDK image, RelWithDebInfo (keeps symbols for crash
# backtraces), -march=x86-64-v2. The Steam Deck also handles x86-64-v3.
# --shell drops into the container instead of building (for debugging).
#
# Container engine: podman if available (rootless, files stay yours), else
# docker (run as your uid). Override with REX_CONTAINER=docker|podman.

def cmd_dist(args: list[str]) -> int:
    ctx = load_project()
    image = os.environ.get("REX_DIST_IMAGE") or "registry.gitlab.steamos.cloud/steamrt/steamrt4/sdk:latest"
    config, march, pull, shell = "RelWithDebInfo", "x86-64-v2", True, False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--image", "--config", "--march") and i + 1 < len(args):
            if a == "--image":
                image = args[i + 1]
            elif a == "--config":
                config = args[i + 1]
            else:
                march = args[i + 1]
            i += 2
        elif a == "--no-pull":
            pull, i = False, i + 1
        elif a == "--shell":
            shell, i = True, i + 1
        else:
            die(f"dist: unknown option '{a}' (see the top of py/rexcli/misc.py)")

    engine = os.environ.get("REX_CONTAINER") or ""
    if not engine:
        if shutil.which("podman"):
            engine = "podman"
        elif shutil.which("docker"):
            engine = "docker"
        else:
            die("need podman or docker (sudo dnf install podman)")
    if not (ctx.gen_dir / "sources.cmake").exists():
        die("no generated code yet - run 'rex build' once first")
    if not SDK_DIR.is_dir():
        die(f"SDK checkout not found at {SDK_DIR}")

    # Short tag for directory names: steamrt4, sniper, ... from the image path.
    tag = re.sub(r"[:/].*", "", re.sub(r".*/steamrt/([^/]+)/sdk.*", r"\1", image)) or "steamrt"
    dist_dir = ctx.dir / "dist" / f"{ctx.name}-{tag}"

    if pull:
        print(f"Pulling {image} ...", flush=True)
        if subprocess.run([engine, "pull", image]).returncode != 0:
            die(f"could not pull {image}.\n"
                "If the image path is wrong, find the current one in Valve's steamrt docs and\n"
                "pass --image, or set REX_DIST_IMAGE. Steam Runtime 3 (older glibc, wider\n"
                "compatibility) is registry.gitlab.steamos.cloud/steamrt/sniper/sdk:latest")

    # A new image changes every system header's mtime, and clang then rejects
    # the container builds' precompiled headers ("file '/usr/include/stdint.h'
    # has been modified since the precompiled header was built"); ninja doesn't
    # track files inside the image, so it never rebuilds them. Start the
    # container build trees afresh whenever the image differs from the one
    # they were built with.
    image_id = subprocess.run([engine, "image", "inspect", "--format", "{{.Id}}", image],
                              capture_output=True, text=True).stdout.strip()
    if image_id:
        for build_tree in (SDK_DIR / "out" / tag / "build", ctx.dir / "out" / tag):
            stamp = build_tree / ".rex-dist-image"
            old = stamp.read_text().strip() if stamp.exists() else None
            if build_tree.is_dir() and old != image_id:
                note(f"container image changed: removing stale build tree {build_tree}")
                shutil.rmtree(build_tree)
            build_tree.mkdir(parents=True, exist_ok=True)
            stamp.write_text(image_id + "\n")

    user_args = ["--userns=keep-id"] if engine == "podman" else ["--user", f"{os.getuid()}:{os.getgid()}"]
    run_args = ["run", "--rm", "-i",
                "--security-opt", "label=disable",  # SELinux: don't relabel your source tree
                *user_args,
                "-e", "HOME=/tmp/home",
                "-e", f"SDK_DIR={SDK_DIR}", "-e", f"PROJECT_DIR={ctx.dir}", "-e", f"PROJECT_NAME={ctx.name}",
                "-e", f"BUILD_TYPE={config}", "-e", f"MARCH={march}", "-e", f"DIST_DIR={dist_dir}",
                "-e", f"TAG={tag}", "-e", f"IMAGE={image}",
                "-v", f"{SDK_DIR}:{SDK_DIR}", "-v", f"{ctx.dir}:{ctx.dir}",
                "-v", f"{REXTOOLS_DIR}:{REXTOOLS_DIR}",
                "-w", str(ctx.dir)]
    # Projects using the shared launch menu build it from ../rexlauncher.
    rexlauncher_dir = REXTOOLS_DIR.parent / "rexlauncher"
    if rexlauncher_dir.is_dir():
        run_args += ["-v", f"{rexlauncher_dir}:{rexlauncher_dir}"]

    if shell:
        return subprocess.run([engine, *run_args, "-t", image, "bash"]).returncode
    print(f"Building in {image} with {engine} (first run builds the whole SDK: several minutes) ...",
          flush=True)
    if subprocess.run([engine, *run_args, image, "bash",
                       str(REXTOOLS_DIR / "dist" / "container_build.sh")]).returncode != 0:
        die("container build failed (see above). 'rex dist --shell' opens the same container to investigate.")
    return 0
