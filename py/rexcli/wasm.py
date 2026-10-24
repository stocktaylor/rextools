"""rex wasm build|run|dawn|browser, rex dawn build|run: WebAssembly and WebGPU
builds of a project (the SDK's wasm branch; see its docs/WASM.md).

WebAssembly builds use Emscripten (emsdk: $EMSDK, or emsdk next to rextools or
its parent) and the SDK in-tree (REXSDK_DIR), as there is no packaged wasm SDK.
Codegen still runs natively, through a rexglue built from the same SDK sources:
$REX_HOST_REXGLUE, else the newer of the SDK's build tree and install ones.

  rex wasm build [--web] [--force-codegen] [-DNAME=VAL]
      out/build/wasm64: runs under node, reading the game folder from disk.
      --web: out/build/wasm64-web, for the browser, with the game folder
      packaged into <name>.data, which the page loads before the game and
      mounts at /game (Emscripten's file_packager), so only for small games
      until the SDK can stream data.
      -D options go to CMake (cached), e.g. -DBLADE_LAUNCHER=OFF.
  rex wasm run [game args...]       under node, null GPU, headless
  rex wasm dawn [game args...]      under node with Dawn's WebGPU (the `webgpu`
                                    npm package, installed into the SDK's
                                    out/deps on first use), the null GPU's
                                    wireframes going through the WebGPU presenter
  rex wasm browser [game args...]   serves the --web build cross-origin isolated
                                    (emrun) and opens it in the default browser;
                                    its output comes back here; Ctrl-C stops it
  rex dawn build [-DNAME=VAL]       native build with WebGPU (Dawn) in
                                    out/build/<preset>-dawn, SDK in-tree
  rex dawn run [game args...]       run it with the null GPU presenting through
                                    WebGPU (on Linux an X11 window: the prebuilt
                                    Dawn has no Wayland)
"""
import glob
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from .common import (EXE_SUFFIX, IS_WINDOWS, PRESET, REXGLUE_BIN, REXTOOLS_DIR, SDK_DIR, Ctx,
                     die, load_project, mtime, note)

WASM_FLAGS = "-sMEMORY64 -pthread -msimd128 -fwasm-exceptions -sSUPPORT_LONGJMP=wasm"
NODE_WEBGPU_VERSION = "0.6.2"  # dawn-gpu/node-webgpu, Dawn for node
BROWSER_PORT = 6931


# --------------------------------------------------------------------------
# Tools

def find_emsdk() -> Path:
    candidates = [os.environ.get("EMSDK"), os.environ.get("REX_EMSDK"),
                  REXTOOLS_DIR.parent / "emsdk", REXTOOLS_DIR.parent.parent / "emsdk",
                  Path.home() / "emsdk"]
    for c in candidates:
        if c and (Path(c) / "upstream" / "emscripten" / "emcc.py").is_file():
            return Path(c).resolve()
    die("emsdk not found: set EMSDK (see the SDK's docs/WASM.md for the pinned version)")


def emsdk_env(emsdk: Path) -> dict:
    """What emsdk_env.sh would set: emcc and emsdk's node on PATH, its config."""
    env = dict(os.environ)
    env["EMSDK"] = str(emsdk)
    env["EM_CONFIG"] = str(emsdk / ".emscripten")
    paths = [str(emsdk / "upstream" / "emscripten"), str(emsdk)]
    node = emsdk_node(emsdk)
    if node:
        paths.insert(0, str(node.parent))
        env["EMSDK_NODE"] = str(node)
    env["PATH"] = os.pathsep.join([*paths, env.get("PATH", "")])
    return env


def emsdk_node(emsdk: Path) -> Path | None:
    """emsdk's own node (24+): memory64 needs a newer node than most systems have."""
    exe = "node.exe" if IS_WINDOWS else "node"
    found = sorted(glob.glob(str(emsdk / "node" / "*" / "bin" / exe)) +
                   glob.glob(str(emsdk / "node" / "*" / exe)))
    return Path(found[-1]) if found else None


def host_rexglue() -> Path:
    """A native rexglue from the SDK sources the wasm build compiles."""
    if os.environ.get("REX_HOST_REXGLUE"):
        return Path(os.environ["REX_HOST_REXGLUE"])
    platform_dir = PRESET.rsplit("-", 1)[0]
    candidates = [p for p in (SDK_DIR / "out" / platform_dir / "Release" / f"rexglue{EXE_SUFFIX}",
                              REXGLUE_BIN) if p.is_file()]
    if not candidates:
        die("no native rexglue: build the SDK natively first (or set REX_HOST_REXGLUE)")
    return max(candidates, key=mtime)


# --------------------------------------------------------------------------
# Building

def _parse_build_args(args: list[str], allow_web: bool) -> tuple[bool, bool, list[str]]:
    web = force = False
    defs = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--web" and allow_web:
            web = True
        elif a == "--force-codegen":
            force = True
        elif a == "-D" and i + 1 < len(args):
            defs.append(f"-D{args[i + 1]}")
            i += 1
        elif a.startswith("-D") and len(a) > 2:
            defs.append(a)
        else:
            die(f"unknown option '{a}'")
        i += 1
    return web, force, defs


def _build(ctx: Ctx, build_dir: Path, configure: list[str], env: dict, force: bool) -> None:
    """configure -> codegen -> configure -> build, as rex build does (codegen's
    target exists only once configured; generated sources are picked up only
    at configure time)."""
    if force:
        gen = ctx.dir / "generated"
        for s in [*gen.glob("codegen.stamp"), *gen.glob("*/codegen.stamp")]:
            s.unlink(missing_ok=True)
    first = not (build_dir / "build.ninja").exists()
    if subprocess.run(configure, env=env, stdout=None if first else subprocess.DEVNULL).returncode:
        die("configure failed")
    if subprocess.run(["cmake", "--build", str(build_dir), "--target", f"{ctx.name}_codegen"],
                      env=env).returncode:
        die("codegen failed (for unresolved calls, `rex build` adds the missing functions)")
    if subprocess.run(configure, env=env, stdout=subprocess.DEVNULL).returncode:
        die("configure failed")
    if subprocess.run(["cmake", "--build", str(build_dir), "--parallel"], env=env).returncode:
        die("build failed")


def wasm_build_dir(ctx: Ctx, web: bool) -> Path:
    return ctx.dir / "out" / "build" / ("wasm64-web" if web else "wasm64")


WEB_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{name}</title>
<style>html, body {{ margin: 0; height: 100%; background: #000; }}
canvas {{ display: block; width: 100vw; height: 100vh; }}</style>
</head>
<body>
<canvas id="canvas" tabindex="-1"></canvas>
<script>var Module = {{ canvas: document.getElementById("canvas") }};</script>
<script src="{name}.data.js"></script>
<script src="{name}.js"></script>
</body>
</html>
"""


def cmd_wasm_build(args: list[str]) -> int:
    ctx = load_project()
    web, force, defs = _parse_build_args(args, allow_web=True)
    emsdk = find_emsdk()
    env = emsdk_env(emsdk)
    build_dir = wasm_build_dir(ctx, web)
    link = WASM_FLAGS
    extra = []
    if web:
        # The page (WEB_PAGE) hands its canvas to main's pthread, which passes
        # it to the WebGPU device thread; --emrun takes the program's arguments
        # from the URL and sends its output back to `rex wasm browser`. The game
        # data is packaged separately (_package_game_data), whose loader needs
        # the file system API exported.
        link += " -sOFFSCREENCANVAS_SUPPORT -sFORCE_FILESYSTEM --emrun"
        extra = ["-DREXGLUE_WASM_NODE=OFF"]
    configure = [
        "cmake", "-S", str(ctx.dir), "-B", str(build_dir), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_TOOLCHAIN_FILE={emsdk / 'upstream/emscripten/cmake/Modules/Platform/Emscripten.cmake'}",
        "-DEMSCRIPTEN_SYSTEM_PROCESSOR=wasm64",
        f"-DCMAKE_C_FLAGS={WASM_FLAGS}", f"-DCMAKE_CXX_FLAGS={WASM_FLAGS}",
        f"-DCMAKE_EXE_LINKER_FLAGS={link}",
        f"-DREXSDK_DIR={SDK_DIR}", f"-DREXGLUE_HOST_REXGLUE={host_rexglue()}",
        "-DREXGLUE_USE_VULKAN=OFF", "-DREXGLUE_ENABLE_TRACY=OFF", "-DREXGLUE_ENABLE_FIDELITYFX=OFF",
        "-DREXGLUE_USE_WEBGPU=ON", *extra, *defs,
    ]
    note(f"building {build_dir.relative_to(ctx.dir)} (codegen with {host_rexglue()})")
    _build(ctx, build_dir, configure, env, force)
    if web:
        _package_game_data(ctx, build_dir, emsdk, env)
        (build_dir / f"{ctx.name}.html").write_text(WEB_PAGE.format(name=ctx.name), encoding="utf-8")
        note(f"page: {build_dir / f'{ctx.name}.html'} (run it with `rex wasm browser`)")
    return 0


def _package_game_data(ctx: Ctx, build_dir: Path, emsdk: Path, env: dict) -> None:
    """<name>.data and its loader <name>.data.js: the game folder, mounted at
    /game in the page's in-memory file system before the game starts. Kept out
    of the link (--preload-file) so new data needs no relink, and so CMake's
    compiler checks don't see it."""
    packager = emsdk / "upstream" / "emscripten" / "tools" / "file_packager.py"
    data = build_dir / f"{ctx.name}.data"
    size = sum(f.stat().st_size for f in ctx.game_root.rglob("*") if f.is_file())
    note(f"packaging {ctx.game_root} ({size / 2**20:.1f} MB) into {data.name}")
    cmd = [sys.executable, str(packager), data.name, "--preload", f"{ctx.game_root}@/game",
           f"--js-output={ctx.name}.data.js", "--no-node"]
    if subprocess.run(cmd, cwd=build_dir, env=env).returncode:
        die("packaging the game data failed")


def cmd_dawn_build(args: list[str]) -> int:
    ctx = load_project()
    _, force, defs = _parse_build_args(args, allow_web=False)
    build_dir = dawn_build_dir(ctx)
    build_type = {"debug": "Debug", "release": "Release"}.get(PRESET.rsplit("-", 1)[-1],
                                                               "RelWithDebInfo")
    configure = [
        "cmake", "-S", str(ctx.dir), "-B", str(build_dir), "-G", "Ninja",
        f"-DCMAKE_BUILD_TYPE={build_type}",
        "-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++",
        f"-DREXSDK_DIR={SDK_DIR}", f"-DREXGLUE_HOST_REXGLUE={host_rexglue()}",
        "-DREXGLUE_USE_WEBGPU=ON", "-DREXGLUE_ENABLE_TRACY=OFF", *defs,
    ]
    if not IS_WINDOWS and "amd64" in PRESET:
        configure += ["-DCMAKE_C_FLAGS=-march=x86-64-v2", "-DCMAKE_CXX_FLAGS=-march=x86-64-v2"]
    note(f"building {build_dir.relative_to(ctx.dir)} (SDK in-tree, with Dawn)")
    _build(ctx, build_dir, configure, dict(os.environ), force)
    return 0


def dawn_build_dir(ctx: Ctx) -> Path:
    return ctx.dir / "out" / "build" / f"{PRESET}-dawn"


# --------------------------------------------------------------------------
# Running

def _user_has(user_args: list[str], name: str) -> bool:
    return any(a == f"--{name}" or a.startswith(f"--{name}=") for a in user_args)


def _defaults(user_args: list[str], pairs: list[tuple[str, str]]) -> list[str]:
    """--name=value for each pair the user didn't pass (duplicates are rejected)."""
    return [f"--{n}={v}" for n, v in pairs if not _user_has(user_args, n)]


def _wasm_log(build_dir: Path, name: str) -> Path:
    logs = build_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.log"


def _run_node(ctx: Ctx, user_args: list[str], dawn: bool) -> int:
    build_dir = wasm_build_dir(ctx, web=False)
    script = build_dir / f"{ctx.name}.js"
    if not script.is_file():
        die(f"no {script.relative_to(ctx.dir)} (run 'rex wasm build')")
    emsdk = find_emsdk()
    env = emsdk_env(emsdk)
    node = emsdk_node(emsdk)
    if not node:
        die(f"emsdk has no node under {emsdk / 'node'} (emsdk install the pinned SDK)")
    log = _wasm_log(build_dir, ctx.name)
    # Logs go to a file (the console only shows startup); the executable's
    # folder can't be found under node, so name the file.
    pairs = [("gpu_plugin", os.environ.get("REX_GPU") or "xenos"), ("gpu_backend", "null"),
             ("video_driver", "dummy"), ("game_data_root", str(ctx.game_root)),
             ("log_level", os.environ.get("REX_LOG_LEVEL") or "info"), ("log_file", str(log))]
    node_args = [str(node)]
    if dawn:
        env["REX_DAWN_NODE"] = str(node_webgpu_addon(emsdk))
        node_args += ["-r", str(REXTOOLS_DIR / "node" / "dawn-preload.cjs")]
        pairs.append(("null_gpu_presenter", "webgpu"))
    argv = [*node_args, str(script), *_defaults(user_args, pairs), *user_args]
    note(f"log: {log}")
    sys.stdout.flush()
    try:
        return subprocess.run(argv, env=env, cwd=build_dir).returncode
    except KeyboardInterrupt:
        return 130


def node_webgpu_addon(emsdk: Path) -> Path:
    """dawn.node from the `webgpu` npm package, installed into the SDK's out/deps."""
    deps = SDK_DIR / "out" / "deps" / "node-webgpu"
    plat = "darwin-universal" if sys.platform == "darwin" else \
        f"{'win32' if IS_WINDOWS else 'linux'}-{'arm64' if 'arm64' in PRESET else 'x64'}"
    addon = deps / "node_modules" / "webgpu" / "dist" / plat / "dawn.node"
    if addon.is_file():
        return addon
    npm = shutil.which("npm", path=emsdk_env(emsdk)["PATH"])
    if not npm:
        die("npm not found (it comes with emsdk's node)")
    note(f"installing Dawn for node (webgpu@{NODE_WEBGPU_VERSION}, ~140 MB) into {deps}")
    deps.mkdir(parents=True, exist_ok=True)
    if subprocess.run([npm, "install", "--no-audit", "--no-fund", f"webgpu@{NODE_WEBGPU_VERSION}"],
                      cwd=deps, env=emsdk_env(emsdk)).returncode or not addon.is_file():
        die(f"installing webgpu@{NODE_WEBGPU_VERSION} failed")
    return addon


def cmd_wasm_browser(args: list[str]) -> int:
    ctx = load_project()
    build_dir = wasm_build_dir(ctx, web=True)
    page = build_dir / f"{ctx.name}.html"
    if not page.is_file():
        die(f"no {page.relative_to(ctx.dir)} (run 'rex wasm build --web')")
    emsdk = find_emsdk()
    env = emsdk_env(emsdk)
    emrun = emsdk / "upstream" / "emscripten" / ("emrun.bat" if IS_WINDOWS else "emrun")
    # Game data is packaged at /game; the null GPU presents through WebGPU and
    # falls back to the canvas's 2D framebuffer where the browser has none.
    pairs = [("gpu_plugin", os.environ.get("REX_GPU") or "xenos"), ("gpu_backend", "null"),
             ("null_gpu_presenter", "webgpu"), ("game_data_root", "/game"),
             ("log_level", os.environ.get("REX_LOG_LEVEL") or "info")]
    program_args = [*_defaults(args, pairs), *args]
    query = "&".join(program_args)
    url = f"http://localhost:{BROWSER_PORT}/{page.name}" + (f"?{query}" if query else "")
    # Serve only (emrun's own browser launching picks a fixed browser); open the
    # desktop's default browser ourselves once the server is up.
    server = subprocess.Popen([str(emrun), "--no_browser", "--port", str(BROWSER_PORT),
                               "--serve_after_close", str(page)], env=env, cwd=build_dir)
    try:
        time.sleep(1.0)
        if server.poll() is not None:
            die("emrun exited (is the port in use?)")
        note(f"opening {url}")
        note("Firefox on Linux needs dom.webgpu.enabled and dom.webgpu.workers.enabled for WebGPU; "
             "Ctrl-C stops the server")
        webbrowser.open(url)
        return server.wait()
    except KeyboardInterrupt:
        server.terminate()
        return 130


def cmd_dawn_run(args: list[str]) -> int:
    ctx = load_project()
    build_dir = dawn_build_dir(ctx)
    binary = build_dir / f"{ctx.name}{EXE_SUFFIX}"
    if not binary.is_file():
        die(f"no {binary.relative_to(ctx.dir)} (run 'rex dawn build')")
    pairs = [("gpu_plugin", os.environ.get("REX_GPU") or "xenos"), ("gpu_backend", "null"),
             ("null_gpu_presenter", "webgpu"), ("game_data_root", str(ctx.game_root)),
             ("log_level", os.environ.get("REX_LOG_LEVEL") or "info")]
    if not IS_WINDOWS:
        pairs.append(("video_driver", "x11"))  # the prebuilt Dawn has no Wayland
    env = dict(os.environ)
    # The in-tree SDK builds its runtime into its own output folder.
    platform_dir = PRESET.rsplit("-", 1)[0]
    libs = SDK_DIR / "out" / platform_dir
    if IS_WINDOWS:
        env["PATH"] = f"{libs}{os.pathsep}{env.get('PATH', '')}"
    else:
        env["LD_LIBRARY_PATH"] = f"{libs}:{env['LD_LIBRARY_PATH']}" if env.get("LD_LIBRARY_PATH") \
            else str(libs)
    argv = [str(binary), *_defaults(args, pairs), *args]
    sys.stdout.flush()
    try:
        return subprocess.run(argv, env=env, cwd=build_dir).returncode
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------

def cmd_wasm(args: list[str]) -> int:
    sub = args[0] if args else ""
    if sub == "build":
        return cmd_wasm_build(args[1:])
    if sub == "run":
        return _run_node(load_project(), args[1:], dawn=False)
    if sub == "dawn":
        return _run_node(load_project(), args[1:], dawn=True)
    if sub == "browser":
        return cmd_wasm_browser(args[1:])
    print(__doc__.strip("\n"))
    return 0 if sub in ("", "help", "-h", "--help") else 1


def cmd_dawn(args: list[str]) -> int:
    sub = args[0] if args else ""
    if sub == "build":
        return cmd_dawn_build(args[1:])
    if sub == "run":
        return cmd_dawn_run(args[1:])
    print(__doc__.strip("\n"))
    return 0 if sub in ("", "help", "-h", "--help") else 1
