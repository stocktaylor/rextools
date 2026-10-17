# rextools

Shared tooling for rexglue recompilation projects. It grew out of bringing up
Tokyo Xtreme Racer and Ridge Racer 6, and covers the
boot-to-gameplay loop: build, run, find out why it died, and fix it.

Every command finds the project from the current directory (the nearest
`*_manifest.toml` with a `[project]` table), so nothing is per-game.

## Setup

Put the rextools folder on your PATH permanently.

Linux:

```bash
echo 'export PATH="/home/tstock/Source/Xenia Recomps/rextools:$PATH"' >> ~/.bashrc
source ~/.bashrc
cd Ridge && rex help
```

Windows (PowerShell; open a new terminal afterwards):

```powershell
[Environment]::SetEnvironmentVariable("Path", "C:\path\to\rextools;" + [Environment]::GetEnvironmentVariable("Path", "User"), "User")
```

rextools expects the rexglue SDK checkout next to it (`../rexglue-sdk`), built
and installed. Point `REXGLUE_SDK` elsewhere if it isn't there.

## Dependencies

The commands are Python (standard library only, so there's nothing to
`pip install`) in `py/rex.py` and `py/rexcli/`. `rex` (Linux) and `rex.cmd`
(Windows: PowerShell or cmd) are small shims that start it. Linux is the
primary platform; Windows runs most commands (see [Windows](#windows) below).
One Fedora command covers everything:

```bash
sudo dnf install python3 cmake ninja-build clang git gdb binutils perf renderdoc podman systemd
```

| Needed for | Tools | Notes |
|---|---|---|
| Everything | Python **3.11+** | 3.11 for `tomllib` (manifest parsing) |
| `rex new`, `rex build`, `rex triage` | CMake ≥ 3.25, Ninja, clang/clang++, git | What the SDK's generated presets use. `rex new` also needs the SDK's `rexglue` CLI installed |
| `rex crash` | gdb, `coredumpctl` (systemd-coredump) | Core dumps must be collected by systemd-coredump (Fedora default) |
| `rex hang`, `rex hang --samples`, `rex peek` | gdb | Needs ptrace permission: `kernel.yama.ptrace_scope=0` or `sudo` |
| `rex pc2func` | `nm` (binutils) | |
| `rex run --renderdoc`, `rex rdc dump` | RenderDoc (`renderdoccmd`, `qrenderdoc`; tested with 1.45) | Fedora's package has no standalone Python module; `rdc dump` runs inside `qrenderdoc --python`. `rdc align`/`diff`/`list` need only Python |
| Performance work | `perf` | Not called by `rex`; see the `perf` commands under Notes |
| `rex sdk ...` | git | The SDK must be a git checkout (patch status / apply / export) |
| `rex dist` | podman (preferred) or docker | Pulls `registry.gitlab.steamos.cloud/steamrt/steamrt4/sdk` by default. Everything else (clang, cmake, ninja) comes from the image |

Not needed: Pillow, a standalone `renderdoc` Python module, `bc`, or any
Python packages. `py/pngcrop.py` covers cropping PNGs without PIL.

### Windows

Run `rex` from PowerShell or cmd (`rex.cmd` is picked up from PATH). Needed:
Python 3.11+ (the `py` launcher is preferred, so the Microsoft Store
`python3` placeholder doesn't matter), CMake, Ninja, and LLVM (clang). Tested
on Windows 11 ARM64 (Snapdragon X, Adreno X1-85) with the native `win-arm64`
presets.

Handled automatically:

- **Preset**: `win-arm64-relwithdebinfo` or `win-amd64-relwithdebinfo` from the
  native CPU (via `IsWow64Process2`, so an emulated x64 Python on ARM64 still
  picks arm64). `REX_PRESET` still overrides.
- **SDK layout**: `out/install/<platform>/bin/*.dll` instead of `lib64/*.so`;
  game binaries are `<name>.exe`. `rex build`/`rex run` refresh both the GPU
  plugin and the runtime DLL next to the game, since Windows loads them from there.
- **clang**: `C:\Program Files\LLVM\bin` is put on PATH if clang isn't already.
- `rex run` drops the Vulkan present-mode flags (Windows renders with D3D12)
  and waits for the game, returning its exit code.
- `rex triage` reports a crash by its exception code (e.g. `0xC0000005
  (access violation)`); there's no post-mortem on Windows yet.
- `rex clean-shm` is a no-op: guest memory is a pagefile-backed mapping freed
  at exit.

Works: `new`, `build`, `run`, `triage`, `log`, `lint`, `funcs`/`asm`,
`callers`, `containing`, `modules`, `sdk`, `rdc list`. `rdc align`/`diff`
are pure Python and should work, but are untested on Windows (no RenderDoc
captures yet). PowerShell 5.1 shows anything `rex` prints to stderr (the
`rex: ...` notes) in red as a `NativeCommandError` when redirected; that's
cosmetic.

Not on Windows yet: `crash`, `hang`, `peek` (gdb / coredumpctl), `pc2func`
(reads ELF symbols with `nm`; Windows binaries carry PDBs) and `dist` (Linux
container). They stop with a message saying so.

## Typical bring-up of a new game

```bash
mkdir NewGame && cd NewGame && cp -r /path/to/dump game
rex new newgame game/default.xex   # init + GPU plugin + manifest header + git
rex build                          # auto-adds codegen 'UnresolvedCall' targets
rex triage                         # loop until it runs past init
rex triage 120 --hang-dump         # if it then shows a black screen / stalls
```

## Commands

| Command | What it does |
|---|---|
| `rex new <name> <xex> [--game-root D]` | `rexglue init` in `$PWD` plus the fixes init leaves out (see below) |
| `rex build [--force-codegen]` | configure (first time) → codegen → configure → build → lint |
| `rex run [args]` | runs with the SDK libs, `--gpu_plugin xenos`, info logging, 1s log flush |
| `rex triage [secs] [--hang-dump] [--no-build]` | build/run loop: auto-adds unregistered-function addresses, post-mortems crashes, optionally dumps stacks at the timeout |
| `rex lint [--all]` | scans generated code for jump tables that are really computed calls |
| `rex crash` | latest core dump: real faulting frame, guest function, guest call chain, signal-specific hint |
| `rex hang [pid]` | attaches to the running game, all thread stacks, flags threads spinning in guest code |
| `rex hang --samples N [--interval S]` | N stack snapshots summarised per thread, including where threads *wait*, which `perf` can't show |
| `rex peek [--func sub_X] [--thread T] [--addrs A,B]` | attaches to the running game: one guest thread's PPC registers (from `ctx`), its stack frame, and the header + contents of everything those point at. For seeing what a spinning loop is testing |
| `rex log [run#]` | one run's log with its rotated 5MB segments merged |
| `rex run --renderdoc [args]` | run under RenderDoc; F12 saves a frame to `_tools/captures/`, empty captures flagged afterwards |
| `rex rdc list` | captures with sizes; flags likely-empty ones |
| `rex rdc dump <cap.rdc> [--events a,b]` | headless replay via `qrenderdoc --python`: action tree, render-target PNGs; per event: constants, post-VS outputs, textures, blend, SPIR-V |
| `rex rdc align <dumpA> <dumpB>` | match passes and draws between two captures (e.g. recomp vs Xenia) by draw sequence |
| `rex rdc diff <dumpA> <evA> <dumpB> <evB>` | compare one matched draw: inputs (with tolerance), textures, translated shaders |
| `rex funcs` / `rex asm <addr>...` | generated C++ / guest disassembly with addresses |
| `rex callers <addr\|Import>` | callers of a function; for an import (`XamContentCreate`) resolves the thunk first |
| `rex pc2func <host_pc>` | host PC from a backtrace → guest function |
| `rex containing <addr>` | which generated function holds a guest address |
| `rex modules` | the project's binaries (entrypoint + `[[modules]]` DLLs): address range, generated dir, functions file |
| `rex clean-shm` | removes leaked `/dev/shm/xenia_memory_*` (4.5GB each; a full tmpfs causes SIGBUS) |
| `rex dist [--config C] [--march M] [--image I] [--no-pull] [--shell]` | builds SDK + project inside a Steam Runtime SDK container and packages `dist/<name>-<tag>/` (+ `.tar.gz`): exe, its SDK libs, `<name>.toml` defaults, empty `game/`, README for testers |
| `rex sdk check` | are the installed SDK libs newer than their sources? local patch status |
| `rex sdk patches status\|apply\|export` | local SDK fixes kept as `sdk-patches/*.patch` |

Environment: `REX_PROJECT` (project dir), `REXGLUE_SDK` (default
`../rexglue-sdk`), `REX_PRESET` (default `linux-amd64-relwithdebinfo`),
`REX_LOG_LEVEL` (default `info`; `trace` writes MBs per second), `REX_GPU` (default `xenos`), `REX_PRESENT` (default `vsync`; `uncapped` redraws the window at thousands of FPS).

## Portable builds for other machines (`rex dist`)

`rex dist` builds inside the Steam Runtime SDK container, so the result only
needs libraries that SteamOS and the Steam Runtime provide. A host build
(Fedora 44) needs glibc 2.43, which is too new for most other systems.

```bash
rex build          # once: generates the code the container build uses
rex dist           # -> dist/<name>-steamrt4/ and dist/<name>-steamrt4.tar.gz
```

The folder runs standalone: the tester puts the extracted game in `game/`
(so that `game/default.xex` exists) and runs `./<name>`. How that works:
- the app loads `<exe dir>/<name>.toml` automatically, and `rex dist` writes
  the flags `rex run` normally passes into it (GPU plugin, vsync present,
  log level);
- the project defaults `game_data_root` to `<exe dir>/game` (Ridge does this
  in `src/ridge_app.h` `OnConfigurePaths`; new projects need the same
  override);
- the executable and SDK libraries use `RUNPATH $ORIGIN`, and the GPU plugin is
  loaded from the exe dir, so the folder is relocatable;
- logs go to `<exe dir>/logs/`, saves to `~/.local/share/<name>/`.

It builds in `rexglue-sdk/out/<tag>/` and `<project>/out/<tag>/`, separate from
the host builds, and uses a throwaway HOME so the SDK's install step doesn't
register the container SDK in your `~/.cmake` package registry.
`BUILD_INFO.txt` in the output lists the glibc/libstdc++ versions it needs.

## Traps these tools encode

- **`rexglue init` omits `GPU_PLUGINS xenos`.** Symptom: "Failed to load GPU
  plugin 'xenos'". `rex new` adds it and `rex build` warns.
- **Configure before the first codegen.** The generated sources never get
  linked (`undefined reference to PPCImageConfig`). `rex build` always
  reconfigures after codegen.
- **Codegen's stamp ignores the SDK.** After an SDK codegen fix, codegen still
  says "up to date". `rex build` forces it when the SDK's Release runtime
  is newer than the stamp.
- **Multi-config SDK.** The `rexglue` CLI (codegen) uses the *Release*
  `librexruntime.so`, while games use *RelWithDebInfo* `librexruntimerd.so`. A
  fix needs the install for the config that uses it. `rex sdk check` tells you which.
- **Unoptimised build.** Ridge's first configure left
  `CMAKE_CXX_FLAGS_RELWITHDEBINFO` empty in the CMake cache, so everything
  (12k recompiled functions included) was built at `-O0`, with choppy audio
  and slow gameplay. `rex build` checks for this and resets the flags.
- **Stale GPU plugin copy.** `rexglue_setup_target(... GPU_PLUGINS xenos)`
  copies the plugin next to the game in a POST_BUILD step. That step only
  runs when the game relinks, and the game never links the plugin, so an SDK
  reinstall leaves the old copy in place and the game keeps loading it. The
  runtime library is unaffected because it comes from the SDK via
  `LD_LIBRARY_PATH`. `rex run` and `rex build` refresh the copy when it
  differs.
- **Computed calls misread as jump tables.** An interpreter or handler-table
  dispatch (`lwzx` → `mtctr` → `bctr`) is emitted as a 1–2 case `switch`
  looping back to the function start, which leaves a black screen with the
  main thread spinning. `rex lint` flags it (DISPATCH) and prints the
  `indirect_calls` entry to add. That needs the `indirect-calls-honored`
  SDK patch, because upstream parses `indirect_calls` but ignores it.
- **Missing functions.** Functions reached only by vtables or tail branches
  log `Call to invalid or unregistered function at guest address X`.
  `rex triage` adds each one.
- **gdb vs the SDK's fault handler.** The SDK self-heals some SIGSEGVs that a
  debugger intercepts first, so triage runs the game plainly and `rex crash`
  works post-mortem.
- **Stale precompiled headers after a container image update.** A newer Steam
  Runtime image gives every system header a new mtime, and clang rejects the
  container build's PCHs ("file '/usr/include/stdint.h' has been modified since
  the precompiled header was built"); ninja doesn't track files inside the
  image. `rex dist` records the image ID in each container build tree and
  starts the trees afresh when it changes.
- **Leaked guest memory** in `/dev/shm` after crashes. Triage cleans it
  before each run.

## SDK patches (`sdk-patches/`)

Local fixes to rexglue-sdk v0.10.0, all found bringing up Ridge Racer 6:

| Patch | Fixes |
|---|---|
| `indirect-calls-honored` | `indirect_calls` in the manifest was parsed but never used |
| `host-thread-fpscr-init` | host threads running guest callbacks (audio) start with FPSCR 0, so enabling flush mode unmasks every MXCSR exception (SIGFPE in audio code) |
| `xam-content-async-status` | async `XamContentCreate` failures must report `ERROR_FUNCTION_FAILED` in the overlapped status, otherwise games report save data as corrupt |
| `spirv-texture-exp-adjust` | Vulkan shader translator read the texture `exp_adjust` from fetch-constant word 4 (the LOD-bias bits) instead of word 3. Textures with a LOD bias get scaled by a huge or tiny power of two, so environment geometry renders solid white or black (Ridge Racer 6). The D3D12 translator already reads word 3 |
| `posix-wait-no-spin` | Linux wait code rounded sub-millisecond remainders down to 0 ms, so every 1 ms alertable wait slice became a `sleep_for(0)` busy spin. The audio worker burned a full core doing nothing |
| `alloc-fixed-no-proc-maps` | Committing memory inside an existing reservation parsed the whole of `/proc/self/maps` with `sscanf` to check the range was mapped, about 0.5 core during level loads. `mprotect` already fails with `ENOMEM` on unmapped pages, so the parse was redundant |
| `renderdoc-attach-only` | The SDK `dlopen`'d `librenderdoc.so` normally, which *loads* RenderDoc wherever it's on the library path (SteamOS ships it), so every run on a Steam Deck showed RenderDoc's overlay. Now it only attaches when RenderDoc is already loaded |
| `xma-set-loop-data` | `XMASetLoopData` read its argument as a whole XMA context instead of the 12-byte `XMA_LOOP_DATA`, so looping streamed audio (Ridge Racer 6's music) stopped after one pass. From Sirhalo23's SDK fork |
| `save-thumbnail-outside-package` | Save thumbnails were written *inside* the save folder, so games that list their save folder saw `__thumbnail.png`. Ridge Racer 6 reports "corrupted" when it's listed first. Now stored next to the `.header` record, existing ones are moved there when a save is opened, and deleting a save deletes it. From Sirhalo23's SDK notes |
| `xgetlanguage-user-language` | `XGetLanguage` was hard-wired to English; now answers with `user_language` (as `ExGetXConfigSetting` already did). From Sirhalo23's SDK notes |
| `shm-unlink-on-create` | The guest-memory backing file `/dev/shm/xenia_memory_*` (~4.5GB) was only unlinked when `Memory` was destroyed, so every crashed or killed game or codegen run leaked one until tmpfs filled and the next run (Crash's first codegen) died with SIGBUS. Now unlinked right after creation; the fd and mappings keep it alive and the kernel frees it on any exit. Makes `rex clean-shm` unnecessary (found on Burnout Crash) |
| `dll-module-dlopen-path` | Recompiled guest DLL modules (`[[modules]]`) were loaded with `dlopen("<project>_<module>")`: no `lib` prefix, no `.so`, no directory. That works with Windows `LoadLibrary`, but on Linux no DLL module could ever load ("Failed to load shared library for module"). Now resolved to `<exe dir>/lib<name>.so` (`.dylib` on macOS), like the GPU plugin loader (Burnout Crash) |
| `jump-table-li-addis` | Jump-table detection only knew a table address built as `lis rT,hi` + `addi rT,rT,lo` with the table in `lwzx`'s rA. Burnout Crash's DLL builds it as `li rT,lo` + `addis rT,rT,hi` with the table in rB (~100 switches). Missed switches were emitted as indirect tail calls; GapFill then made the first case a separate function overlapping the real one, so branches back into the real function hit `REX_FATAL("Unresolved branch ...")`. Both detectors now accept that form and take the index from the other `lwzx` operand |
| `switch-dispatch-on-ctr` | Jump tables were emitted as `switch (index_register) { case 0..N }`, which assumes the index register still holds the unscaled index at the `bctr`. Burnout Crash's DLL scales it in place and reuses it as the load destination (`rlwinm r3,r3,2` / `lwzx r3,r3,rT`), so every switch fell to `default: __builtin_trap()`; inside a guest SEH scope that SIGILL becomes a caught exception and the game loops forever. Now emitted as `switch (ctr) { case <target>: }`: CTR holds the loaded target for every table type. Pair with `jump-table-li-addis` |
| `thread-initial-frame` | Guest threads started with `r1 = stack_base`, the very top of the stack, so the first guest frame's back-chain pointed at the no-access guard page instead of 0. Guest stack walkers (Burnout Crash's exception backtrace capture, `sub_82651230`) followed it there and faulted; with the global lock held, every other thread queued behind it (static loading screen). Threads now start 0x80 below `stack_base` on a zeroed frame whose back-chain is 0, like the kernel's thread startup frame |
| `timer-queue-blocking-wait` | The SDK timer thread (`rex::thread::TimerQueue`) waited for the next due timer with disruptorplus's *spin* wait strategy, busy-waiting ~7% of a core whenever the game ran (Burnout Crash profile). Switched to its blocking strategy: same interface, sleeps on a condition variable until the next timer is due or one is published (publish and the destructor's shutdown kick both signal it). Also fixes vendored `thirdparty/disruptorplus` `blocking_wait_strategy`, whose timed `wait_until`/`wait_for` passed the predicate before the deadline and never compiled (unused until now) |
| `vpkuwus-alias-upstream-6319e23` | Backport of upstream rexglue 6319e23 (#449): `vpkuwus`/`vpkuhus` packed element-by-element straight into vD, so when vD aliased vA or vB later elements read already-overwritten data. Burnout Crash's VP6 FMV decoder has 12 aliased `vpkuwus128` (e.g. `v63,v63,v61`): key frames decoded, every predicted frame degraded into green blocks. Drop when rebasing onto a release that has it |
| `present-on-guest-swap` | Two fixes for frame pacing. (1) The guest vblank timer polled in 1 ms sleeps, so vblanks landed up to ~1 ms late and an oversleep fired several back to back: uneven guest frame times even at a "solid 60". It now sleeps to exact deadlines (1 us timer slack on Linux) and resyncs instead of bursting after a stall. (2) New cvar `present_on_guest_swap` (default off): one host present per guest frame. The guest output thread presents on each swap even with FIFO vsync, and ImGui's continuous repaint (always on, the achievement toast dialog never closes) waits for the next guest frame while the game is presenting. Frame-rate overlays (MangoHud) then measure the game, and VRR shows each frame as it's ready |

| `posix-winsock-ioctl` | Guest `ioctlsocket` passes Winsock command values (`FIONBIO` 0x8004667E, `FIONREAD` 0x4004667F) with a big-endian argument; the POSIX layer handed them straight to `ioctl()`, which fails with ENOTTY. Now `FIONBIO` maps to `fcntl(O_NONBLOCK)` and `FIONREAD` is answered big-endian. London 2012's network init requires `FIONBIO` to succeed or the game quits at boot |
| `posix-privileged-port-bind` | Titles bind system-link/VDP ports below 1024 (London 2012: UDP 1000-1003), which needs root on Linux, so every bind failed with EACCES and the game's Init returned false (main exits with `[XAPI RETURN VALUE] 0`). On EACCES the host socket is now bound at port + `net_privileged_port_offset` (cvar, default 10000, 0 disables); the guest keeps its own port number |

| `xam-content-open-file` | `XamContentOpenFile` was a placeholder that always returned `ERROR_FILE_NOT_FOUND` (inherited from Xenia). It mounts an STFS package file (a guest path, e.g. `game:\Database.xmplr`) as `root:`. London 2012's Kinect runtime (`NuiInitialize`) loads its model packages this way even with no sensor attached; when it failed, the input manager never activated, `hudinput.lua`'s `HUDInput:WAIT` spun on `GestureRecogniser:IsTracking()`, and every event start screen ignored the controller (also the cause of the long-standing Xenia report, game-compatibility #783). Now mounts with `StfsContainerDevice` and is closed by `XamContentClose` |

| `kernel-ldi-decompression` | `LDICreateDecompression` / `LDIDecompress` / `LDIResetDecompression` / `LDIDestroyDecompression` (the CAB-era LZX API games use for XMemCompress'd data) were bare stubs, so callers saw garbage results. Implemented on the SDK's libmspack LZX decoder as a streaming context (one compressed block per call, window/Huffman state kept between calls). London 2012's Kinect runtime unpacks its model database (`stexemplar:\database.gmsodf`) this way; pair with `xam-content-open-file` |

| `function-table-image-heap` | The recompiled-code function table is placed right after the image (`image_base + image_size`) but was always allocated from the `0x80000000` heap, so an image outside it failed at startup (`AllocFixed invalid base alignment ... heap=80000000-8FFFFFFF`, `Runtime setup failed: C0000001`). System images load in the `0x90000000` heap (the Blades dashboard `dash.xex` at `0x92000000`). Now allocated and released through the heap that holds the address |

| `guest-av-log-ratelimit` | An unhandled guest access violation that the guest catches and retries (an SEH `__try` around a loop) logged one error per attempt, about 700k lines a second, which rotated the start-up log away before anyone could read it (Blades dashboard: `read of guest 0x00000008`). Repeats of the same fault on a thread now log the first 8, then every power of two, with the count (`(xN)`) |

| `unhandled-fault-no-refault-loop` | When no handler claimed a SIGSEGV/SIGILL, the POSIX signal handler simply returned, so the faulting instruction re-ran and faulted again forever: any real guest null dereference became a silent 100%-CPU hang (black screen, no crash, no core). Now the original disposition is restored before returning, so the re-run crashes normally and `rex crash` can analyse it (Blades dashboard: XUI element with a null class pointer, `read of guest 0x00000008`) |

| `xam-resource-locator-log` | Diagnostic: the `XamBuild*ResourceLocator` debug line named only the container (`gamercrd`), not the resource after `#`, so a failed XUI scene lookup couldn't be traced to the name asked for. Now logs the full locator (`file://media:/gamercrd.xzp#GamerCard.xur`) and the buffer size |

| `mapped-wstring-byteswap` | `mapped_wstring::value()` (`MappedPtr<char16_t>`) returned a view of the guest's big-endian UTF-16 without swapping, unlike Xenia's string argument types. Every caller using `.value()` got byte-swapped text: `XamBuild{Gamercard,SharedSystem,Xam}ResourceLocator` built `file://media:/gamercrd.xzp#䜀愀洀...`, so XUI never found `GamerCard.xur` and the Blades dashboard dereferenced the failed scene (null `+12`). The keyboard UI's default text and `OutputDebugStringW` were wrong too. `value()` now returns a host-order `std::u16string` copy |

| `window-set-desired-size` | `Window` had `SetFullscreen` but no way to resize after creation (`--window_width/height` are read only at startup). Adds public `Window::SetDesiredLogicalSize(w, h)` with an `ApplyNewDesiredLogicalSize` hook, implemented for SDL with `SDL_SetWindowSize` (same coordinate convention as `OpenImpl`). The Blades dashboard uses it to apply Display > Screen Resolution to its own window |

| `debug-overlay-guest-fps` | The debug overlay only printed `Guest: N FPS` through `ReXApp::SetGuestFrameStats`, which no project calls, so it never showed a frame rate. Falls back to the guest swap-to-swap frame time it already graphs (`kFrameTimeUs`, set on `XE_SWAP`), averaged over the graph's samples. (Draw/Verts/Stalls/Tex/PipeCache stay 0 on Linux: those counters are only instrumented in the D3D12 backend) |

| `d3d12-shared-memory-typed-views` | Windows on Snapdragon (Adreno X1-85): every game rendered black, though the guest ran normally at 60 fps. The Qualcomm D3D12 driver mishandles raw (`ByteAddressBuffer`) views larger than 256 MB: a 512 MB raw SRV returns wrong data everywhere and a raw UAV wraps at 2^28 bytes (typed views of any size, and raw views of at most 256 MB, are fine). Shared memory is one 512 MB raw view, so vertex fetch read garbage and memexport wrote to the wrong place. Translated shaders now access shared memory through R32_UINT typed views (one load/store per dword, atomics by dword index) when the adapter is Qualcomm (vendor `0x5143` or `0x4D4F4351` "QCOM"), or with `--d3d12_shared_memory_typed_views=true`; other GPUs keep raw views |

After `rex sdk patches apply`, install both configs:
`cmake --build --preset linux-amd64-release --target install` and
`cmake --build --preset linux-amd64-relwithdebinfo --target install`.
All of these are general bugs and candidates for upstream.

## Status

### Implemented

| Area | Commands | State |
|---|---|---|
| Project discovery | all | Works for Ridge, Tokyo, and include-file layouts (Simpsons) |
| Bootstrap | `rex new` | Manifest/CMake post-processing tested; full `rexglue init` run not yet exercised |
| Build pipeline | `rex build` | Used on Ridge: configure/codegen/configure/build ordering, SDK-staleness forced codegen, post-build lint |
| Run | `rex run` | Used on Ridge |
| Triage loop | `rex triage` | Used on Ridge; classifier paths (missing function, repeat address, timeout, other error) tested with a stub binary |
| Codegen lint | `rex lint` | DISPATCH / STALE / NOBCTR / TINY checks; tested against a fixture of Ridge's original dispatch bug; Ridge and Tokyo lint clean |
| Post-mortem | `rex crash` | Faulting frame below the signal handler, pc→guest function, guest chain, per-signal hints |
| Hang analysis | `rex hang` | All-thread stacks, flags threads spinning in guest code |
| Wait/stutter profiling | `rex hang --samples N` | N gdb snapshots summarised per thread (top-3 frames, libc wait plumbing skipped, `[blocked]` marked). Shows where threads wait, which `perf` can't |
| Code inspection | `rex funcs` `asm` `callers` `pc2func` `containing` | Verified against hand-found Ridge/Tokyo results |
| Logs | `rex log` | Merges rotated segments of a run |
| Housekeeping | `rex clean-shm` | Ported from Tokyo |
| SDK management | `rex sdk check`, `rex sdk patches` | Install staleness per config; 10 local patches stored and status-checked |
| Capture analysis | `rex run --renderdoc`, `rex rdc list/dump/align/diff` | Capture with the right settings; headless dump; align and diff two captures. Built from the Ridge vs Xenia Edge investigation (`align`/`diff` validated on those dumps; `dump`/`--renderdoc` wiring tested with stand-in binaries) |
| Images | `py/pngcrop.py` | Pure-Python PNG crop/upscale (no PIL needed) |
| Portable builds | `rex dist` | Packaging step tested against the host build; the container build itself is not yet run (image path unverified) |

### Planned

Roughly in priority order.

1. ~~**Auto-fix codegen `UnresolvedCall` errors in `rex build`.**~~ Done:
   `rex build` parses Validate's `b 0xX from 0xY - target not in any function`
   lines, appends each target to the functions table of the binary that holds
   it, and re-runs codegen (output in `_tools/logs/codegen.txt`).
2. **Automatic hang detection in `rex triage`.** If the main guest thread
   stops logging while the GPU keeps presenting, dump stacks without waiting
   for the timeout. Then run `rex lint --all` against the functions the
   spinning thread is in.
3. **Kernel/XAM call tracing.** An SDK cvar that logs every import call with
   its arguments and result, plus a `rex trace <ImportName>` filter. That
   would have found Ridge's save-data bug in one run instead of three (we
   added temporary logging by hand).
4. **More lint checks**, each learned from a real bug:
   - `bctr` targets that land inside another function;
   - functions in the manifest whose detected size looks wrong (absorbed by
     or overlapping a neighbour);
   - host-thread callbacks into guest code: list the XHostThread-dispatched
     guest entry points, which need the FPSCR patch.
5. **RenderDoc follow-ups.** The core is done (`rex run --renderdoc`, `rex rdc`).
   Still to do: a helper for capturing a *reference* emulator (Xenia) the
   same way, which today means running `renderdoccmd capture` by hand, and
   `rex rdc diff` printing the exact SPIR-V lines that differ around texture
   fetches, not just a summary.
6. **Save/content inspection.** `rex saves` lists what the game created under
   the user data root (`~/.local/share/<name>/content/...`), with headers
   decoded.
7. **Log-level presets.** `rex run --quiet` (info), so normal play doesn't
   produce rotating 5MB trace logs, and trace only when triaging.
8. **Per-project wrappers / cleanup.** Retire Ridge's `_tools/` and Tokyo's
   `_tools/` and `_buildTools/` in favour of `rex`, or reduce them to
   one-line wrappers.
9. **Multi-module (DLL) projects.** Done for triage, lint, `funcs`/`asm`/
   `callers`/`containing` (Burnout Crash: game code is in `DLL/CRASH.DLL.XEX`).
   Each binary's range comes from its XEX header; triage appends a missing
   function to the functions table of the binary that contains it (the
   manifest's last table, or an include ending in `[functions]`). Still to do:
   `rex new` adding modules (init's `--scan-dll` only finds `*.dll`, not
   `*.dll.xex`), and `pc2func` for PCs inside a module's `.so` (gdb already
   names those frames).
10. **Upstream the SDK patches** (all three are general rexglue bugs) and
    drop each from `sdk-patches/` once it's in a release.
11. **Portability.** The commands are Python and run natively on Windows
    (PowerShell / cmd) for build/run/inspect (see [Windows](#windows)). Still to do there: `crash` (WER
    minidumps + cdb), `hang`/`peek` (cdb, or small DbgHelp helpers), `pc2func`
    (PDB symbols via `llvm-symbolizer`), and Windows packaging for `dist`.
    macOS paths for the SDK install are also missing.

## Notes

- **Profiling CPU use** (what the Ridge optimisation work used). The game must be running:
  ```bash
  PID=$(pgrep -f build/linux-amd64-relwithdebinfo/<name>)
  perf stat -e task-clock -p $PID -- sleep 20             # "CPUs utilized" = cores busy
  perf record -F 999 -g -p $PID -o _tools/logs/perf.data -- sleep 20
  perf record -F 999 -g -o _tools/logs/perf_startup.data -- rex run   # from launch
  ```
  `perf` can't unwind through recompiled code (no frame pointers), so callers
  of guest functions show as `[unknown]`. Use `rex callers` instead.

- `rex hang` (and `triage --hang-dump`) need ptrace: `kernel.yama.ptrace_scope=0` or sudo.
- `rex crash` needs `coredumpctl` (systemd-coredump), so run it on the host.
- Instruction addresses in `rex asm`/`lint` are recovered by counting the
  generated `// insn` comments from each label. That's exact within a
  contiguous block. In about 0.1% of cases blocks aren't contiguous, and the
  count resyncs at the next label.
