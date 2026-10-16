"""Project discovery for rextools: reads a rexglue manifest and reports paths.

Usage:
  python3 -I rexinfo.py <project_dir>                  # prints shell assignments
  python3 -I rexinfo.py <project_dir> --module-for A   # binary holding guest address A:
                                                       # "<label>\t<funcs_file or ->"
  python3 -I rexinfo.py <project_dir> --modules        # one line per binary
Also imported by the other rextools Python scripts.

A project is the [entrypoint] binary plus any [[modules]] (guest DLLs). Each
binary has its own generated-code directory, functions table and address
range (read from its XEX header), so tooling picks the binary by address.
"""
import re
import shlex
import struct
import sys
import tomllib
from pathlib import Path

TABLE_RE = re.compile(r"^\s*\[\[?([^\[\]]+)\]\]?\s*(#.*)?$")


def find_manifest(project_dir: Path) -> Path:
    for p in sorted(project_dir.glob("*.toml")):
        try:
            data = tomllib.loads(p.read_text())
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data.get("project"), dict):
            return p
    raise SystemExit(f"rextools: no rexglue manifest (*.toml with [project]) in {project_dir}")


def last_table(path: Path) -> str | None:
    """Name of the last [table] or [[array-table]] header in a TOML file."""
    last = None
    for line in path.read_text().splitlines():
        m = TABLE_RE.match(line)
        if m:
            last = m.group(1).strip()
    return last


def xex_range(path: Path) -> tuple[int, int] | None:
    """(image_base, image_size) from an XEX2 header, or None."""
    try:
        with path.open("rb") as fh:
            d = fh.read(0x10000)
    except OSError:
        return None
    if d[:4] != b"XEX2":
        return None
    sec_off, n = struct.unpack(">II", d[16:24])
    base = None
    for i in range(n):
        key, val = struct.unpack(">II", d[24 + i * 8:32 + i * 8])
        if key == 0x00010201:
            base = val
    if base is None:
        return None
    return base, struct.unpack(">I", d[sec_off + 4:sec_off + 8])[0]


class Module:
    """One recompiled binary: the [entrypoint] or a [[modules]] DLL."""

    def __init__(self, manifest_dir: Path, tbl: dict, label: str):
        self.label = label  # "entrypoint" or the DLL's guest_path
        self.tbl = tbl
        self.xex = (manifest_dir / tbl.get("file_path", "")).resolve()
        self.gen_dir = (manifest_dir / tbl.get("out_directory_path", "generated/default")).resolve()
        self.includes = [(manifest_dir / i).resolve() for i in tbl.get("includes", [])]
        self.range = xex_range(self.xex)
        self.funcs_file = None

    def contains(self, addr: int) -> bool:
        return self.range is not None and self.range[0] <= addr < self.range[0] + self.range[1]

    def _tables(self):
        yield self.tbl
        for inc in self.includes:
            if inc.exists():
                yield tomllib.loads(inc.read_text())

    def indirect_calls(self) -> set[int]:
        out = set()
        for t in self._tables():
            out |= set(t.get("indirect_calls", []))
        return out

    def known_functions(self) -> set[int]:
        out = set()
        for t in self._tables():
            out |= {int(key, 16) for key in t.get("functions", {})}
        return out


class Project:
    def __init__(self, project_dir: Path):
        self.dir = project_dir.resolve()
        self.manifest = find_manifest(self.dir)
        self.data = tomllib.loads(self.manifest.read_text())
        self.name = self.data["project"]["name"]
        root = self.manifest.parent
        self.game_root = (root / self.data["project"].get("game_root", "game")).resolve()

        self.modules = [Module(root, self.data.get("entrypoint", {}), "entrypoint")]
        for mod in self.data.get("modules", []):
            self.modules.append(Module(root, mod, mod.get("guest_path", "?")))

        # Where new functions can be appended for each binary: the manifest
        # itself if that binary's functions sub-table is the manifest's last
        # table ([entrypoint.functions], or [modules.functions] for the last
        # [[modules]] entry), else an include whose last table is [functions].
        manifest_last = last_table(self.manifest)
        for i, m in enumerate(self.modules):
            if (i == 0 and manifest_last == "entrypoint.functions") or \
               (i > 0 and i == len(self.modules) - 1 and manifest_last == "modules.functions"):
                m.funcs_file = self.manifest
                continue
            for inc in m.includes:
                if inc.exists() and last_table(inc) == "functions":
                    m.funcs_file = inc
                    break

        ep = self.modules[0]
        self.gen_dir = ep.gen_dir
        self.includes = ep.includes
        self.funcs_file = ep.funcs_file

    @property
    def gen_dirs(self) -> list[Path]:
        return [m.gen_dir for m in self.modules]

    def module_for(self, addr: int) -> Module | None:
        m = next((m for m in self.modules if m.contains(addr)), None)
        # Single-binary project whose XEX isn't on disk: nothing to choose between.
        if m is None and len(self.modules) == 1 and self.modules[0].range is None:
            m = self.modules[0]
        return m

    def indirect_calls(self) -> set[int]:
        return set().union(*(m.indirect_calls() for m in self.modules))

    def known_functions(self) -> set[int]:
        return set().union(*(m.known_functions() for m in self.modules))


def main():
    args = sys.argv[1:]
    proj = Project(Path(args[0] if args and not args[0].startswith("--") else "."))
    if "--module-for" in args:
        m = proj.module_for(int(args[args.index("--module-for") + 1], 16))
        if m is None:
            print("-\t-")
            return 1
        print(f"{m.label}\t{m.funcs_file or '-'}")
        return 0
    if "--modules" in args:
        for m in proj.modules:
            rng = f"{m.range[0]:08X}-{m.range[0] + m.range[1]:08X}" if m.range else "?"
            gen = m.gen_dir.relative_to(proj.dir) if m.gen_dir.is_relative_to(proj.dir) else m.gen_dir
            funcs = m.funcs_file.name if m.funcs_file else "-"
            print(f"{m.label:<24} {rng}  gen={gen}  funcs={funcs}")
        return 0
    vals = {
        "PROJECT_DIR": proj.dir,
        "PROJECT_NAME": proj.name,
        "MANIFEST": proj.manifest,
        "GAME_ROOT": proj.game_root,
        "GEN_DIR": proj.gen_dir,
        "FUNCS_FILE": proj.funcs_file or "",
    }
    for k, v in vals.items():
        print(f"{k}={shlex.quote(str(v))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
