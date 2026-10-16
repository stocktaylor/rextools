#!/usr/bin/env bash
# rextools: shared build / run / triage / inspection tooling for rexglue
# recompilation projects. Run from anywhere inside a project directory.
#
#   rex new <name> <xex> [--game-root DIR]   bootstrap a new project in $PWD
#   rex build [--force-codegen]              codegen -> configure -> build -> lint
#   rex run [game args...]                   run the game with standard flags
#   rex run --renderdoc [game args...]       run under RenderDoc (F12 captures a frame)
#   rex triage [timeout] [--hang-dump]       build/run loop: auto-add missing
#                                            functions, stop on anything else
#   rex lint [--all]                         scan generated code for misreads
#   rex crash [--no-wait]                    post-mortem of the latest core dump
#   rex hang [pid]                           stacks of a running (hung) game
#   rex hang --samples N [--interval S]      where each thread spends time, incl. waits
#   rex peek [--func sub_X] [--thread T]     a guest thread's registers, stack frame, pointees
#   rex log [run#]                           print a run's log, rotations merged
#   rex rdc list|dump|align|diff             analyse RenderDoc captures (rex rdc for help)
#   rex funcs|asm <addr>...                  generated C++ / guest disassembly
#   rex callers <addr|ImportName>            who calls a function or import
#   rex pc2func <host_pc>                    host PC (backtrace/core) -> guest fn
#   rex containing <guest_addr>              which function holds an address
#   rex modules                              binaries (exe + DLLs), ranges, functions tables
#   rex clean-shm [--dry-run]                remove leaked /dev/shm guest memory
#   rex dist [--config C] [--march M]        portable build via Steam Runtime SDK container
#   rex sdk check                            is the installed SDK current?
#   rex sdk patches status|apply             local SDK patches (sdk-patches/)
#   rex sdk patches export <name> <file>...  save SDK changes as a patch
set -uo pipefail

REXTOOLS_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=lib/common.sh
source "$REXTOOLS_DIR/lib/common.sh"

usage() { sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; }

cmd="${1:-help}"
shift || true
case "$cmd" in
  new|build|run|triage|crash|hang|peek|log|clean-shm|sdk|rdc|dist)
    # shellcheck disable=SC1090
    source "$REXTOOLS_DIR/cmd/$cmd.sh"
    ;;
  lint)
    load_project
    $REX_PY "$REXTOOLS_DIR/py/lint.py" "$PROJECT_DIR" "$@"
    ;;
  funcs|asm|callers|containing)
    load_project
    $REX_PY "$REXTOOLS_DIR/py/inspect_code.py" "$PROJECT_DIR" "$cmd" "$@"
    ;;
  modules)
    load_project
    $REX_PY "$REXTOOLS_DIR/py/rexinfo.py" "$PROJECT_DIR" --modules
    ;;
  pc2func)
    load_project
    $REX_PY "$REXTOOLS_DIR/py/inspect_code.py" "$PROJECT_DIR" pc2func "$1" "$BIN"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage; die "unknown command '$cmd'"
    ;;
esac
