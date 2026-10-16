"""Offline analysis of rdc_dump.py output folders (plain Python, no RenderDoc).

  rdc_tools.py align <dumpA> <dumpB>
      Pair up render-target passes between two captures (e.g. the recomp vs a
      reference emulator) by matching their draw sequences (index counts), and
      print the event IDs of matching draws. Use those IDs with
      `rex rdc dump --events` to get per-draw detail from each capture.

  rdc_tools.py diff <dumpA> <eventA> <dumpB> <eventB>
      Compare one matched draw: constants, VS outputs, textures, blend state
      (resource IDs / file names normalised away), plus a summary of how the
      two translated shaders differ.
"""
import difflib
import re
import sys
from pathlib import Path

ACTION_RE = re.compile(r"^\s*(\d+)\s.*\[([^\]]*)\]\s+idx=(\d+)\s+inst=(\d+)\s*(.*)$")
RT_RE = re.compile(r"ResourceId::(\d+)\(([^)]*)\)")


def load_passes(dump: Path):
    """render target id -> {'desc', 'draws': [(eid, idx)]} in draw order."""
    passes = {}
    for line in (dump / "actions.txt").read_text().splitlines():
        m = ACTION_RE.match(line)
        if not m or "Drawcall" not in m.group(2):
            continue
        eid, idx, outs = int(m.group(1)), int(m.group(3)), m.group(5)
        rt = RT_RE.search(outs)
        if not rt:
            continue
        p = passes.setdefault(rt.group(1), {"desc": rt.group(2), "draws": []})
        p["draws"].append((eid, idx))
    return passes


def cmd_align(a: Path, b: Path):
    pa, pb = load_passes(a), load_passes(b)
    print(f"{a.name}: {len(pa)} render targets drawn to; {b.name}: {len(pb)}")
    print()
    used_b = set()
    pairs = []
    for ra, A in sorted(pa.items(), key=lambda kv: -len(kv[1]["draws"])):
        best = None
        sa = [i for _, i in A["draws"]]
        for rb, B in pb.items():
            if rb in used_b or A["desc"] != B["desc"]:
                continue
            sb = [i for _, i in B["draws"]]
            ratio = difflib.SequenceMatcher(None, sa, sb, autojunk=False).ratio()
            if best is None or ratio > best[0]:
                best = (ratio, rb)
        if best and best[0] > 0.5:
            used_b.add(best[1])
            pairs.append((ra, best[1], best[0]))
        status = f"-> {b.name} RT {best[1]}  ({best[0]:.0%} draw-sequence match)" if best and best[0] > 0.5 \
            else "-> no match (only partially captured on the other side?)"
        print(f"RT {ra} {A['desc']}: {len(A['draws'])} draws {status}")
    for ra, rb, ratio in pairs:
        A, B = pa[ra]["draws"], pb[rb]["draws"]
        sm = difflib.SequenceMatcher(None, [i for _, i in A], [i for _, i in B], autojunk=False)
        matched = []
        for blk in sm.get_matching_blocks():
            for k in range(blk.size):
                matched.append((A[blk.a + k], B[blk.b + k]))
        print(f"\n=== RT {ra} <-> RT {rb}: {len(matched)} matched draws "
              f"(of {len(A)} / {len(B)}); largest first")
        print(f"  {'idx':>7}  {a.name + ' eid':>14}  {b.name + ' eid':>14}")
        for (ea, ia), (eb, _) in sorted(matched, key=lambda m: -m[0][1])[:15]:
            print(f"  {ia:>7}  {ea:>14}  {eb:>14}")


def normalise(text: str) -> list[str]:
    text = re.sub(r"ResourceId::\d+", "RID", text)
    text = re.sub(r"event_\d+", "EVENT", text)
    text = re.sub(r"_\d+\.png", ".png", text)
    return text.splitlines()


def section(lines, start):
    out, on = [], False
    for l in lines:
        if l.startswith(start):
            on = True
        elif on and l.startswith("--- "):
            break
        if on:
            out.append(l)
    return out


def spirv_summary(path: Path):
    if not path.exists():
        return None
    t = path.read_text()
    return {
        "lines": t.count("\n"),
        "texture samples": len(re.findall(r"ImageSample\w*", t)),
        "fetch constant reads": sorted(set(re.findall(r"fetch_constants\[\d+\]\.\w", t))),
        "inputs": sorted(set(re.findall(r"xe_in_\w+", t))),
    }


NUM_RE = re.compile(r"[-+]?(?:\d+\.\d+|nan|inf)")


def numbers(lines):
    out = []
    for l in lines:
        out.extend(float(x) for x in NUM_RE.findall(l.split(":", 1)[-1]))
    return out


def compare_numbers(la, lb, label):
    na, nb = numbers(la), numbers(lb)
    if not na and not nb:
        print(f"\n## {label}: (none)")
        return
    if len(na) != len(nb):
        print(f"\n## {label}: DIFFERENT SHAPE ({len(na)} vs {len(nb)} values)")
        return
    worst, worst_i = 0.0, -1
    for i, (x, y) in enumerate(zip(na, nb)):
        if x != x and y != y:  # both NaN
            continue
        if (x != x) != (y != y):
            worst, worst_i = float("inf"), i
            break
        rel = abs(x - y) / max(1.0, abs(x), abs(y))
        if rel > worst:
            worst, worst_i = rel, i
    if worst < 0.01:
        verdict = "match"
    elif worst < 0.10:
        verdict = "close (typical when captures are a moment apart, e.g. camera moved)"
    else:
        verdict = "DIFFERENT"
    print(f"\n## {label}: {verdict} (max relative difference {worst:.2%} over {len(na)} values)")
    if verdict == "DIFFERENT":
        print(f"  first big difference at value {worst_i}: {na[worst_i]} vs {nb[worst_i]}")


def cbuffers(lines):
    """name -> rows, for every 'cbuffer N 'name' ...' block (first stage seen)."""
    blocks, cur = {}, None
    for l in lines:
        m = re.match(r"cbuffer \d+ '([^']+)'", l)
        if m:
            cur = m.group(1) if m.group(1) not in blocks else None
            if cur:
                blocks[cur] = []
        elif cur and l.startswith("  c"):
            blocks[cur].append(l)
        elif not l.startswith("  "):
            cur = None
    return blocks


def cmd_diff(a: Path, ea: str, b: Path, eb: str):
    ta = normalise((a / f"event_{ea}.txt").read_text())
    tb = normalise((b / f"event_{eb}.txt").read_text())
    print(f"A = {a.name} event {ea}    B = {b.name} event {eb}")
    print("(small differences are normal when captures were taken a moment apart)")

    compare_numbers(section(ta, "--- VS output")[2:], section(tb, "--- VS output")[2:],
                    "VS outputs (positions, texcoords, colours)")
    ca, cb = cbuffers(ta), cbuffers(tb)
    for name in sorted(set(ca) | set(cb)):
        if "system_constants" in name or "bool_loop" in name:
            print(f"\n## {name}: emulator-internal, layout differs between builds (not compared)")
            continue
        if name not in ca or name not in cb:
            print(f"\n## {name}: only in {'A' if name in ca else 'B'}")
            continue
        compare_numbers(ca[name], cb[name], f"{name} (game constants)" if "float" in name else name)

    for title in ("--- textures read by PS",):
        sa = [l for l in section(ta, title)[1:] if l.startswith("  tex")]
        sb = [l for l in section(tb, title)[1:] if l.startswith("  tex")]
        print(f"\n## textures read by PS: {'same' if sa == sb else 'DIFFERENT'}")
        if sa != sb:
            print("  A: " + ("; ".join(x.strip() for x in sa) or "(none)"))
            print("  B: " + ("; ".join(x.strip() for x in sb) or "(none)"))
            print("  (compare the PNGs: event_<id>_tex*.png in each dump)")
    ba = [l for l in ta if l.startswith("blend rt")]
    bb = [l for l in tb if l.startswith("blend rt")]
    print(f"\n## blend state: {'same' if ba == bb else 'DIFFERENT'}")
    if ba != bb:
        print("  A: " + "; ".join(ba) + "\n  B: " + "; ".join(bb))

    for stage in ("vs", "ps"):
        sa = spirv_summary(a / f"event_{ea}_{stage}.spvasm")
        sb = spirv_summary(b / f"event_{eb}_{stage}.spvasm")
        if not sa or not sb:
            continue
        print(f"\n## {stage.upper()} translated shader (A | B)")
        for k in sa:
            mark = "" if sa[k] == sb[k] else "   <-- differs"
            print(f"  {k}: {sa[k]}  |  {sb[k]}{mark}")
    print(f"\nIf the inputs match but the output differs, read the shaders:\n"
          f"  diff {a}/event_{ea}_ps.spvasm {b}/event_{eb}_ps.spvasm")


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "align":
        cmd_align(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 6 and sys.argv[1] == "diff":
        cmd_diff(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), sys.argv[5])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
