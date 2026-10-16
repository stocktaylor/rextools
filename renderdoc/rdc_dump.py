"""Dump a RenderDoc capture to text + PNGs for offline analysis.

Runs inside qrenderdoc's embedded Python (no separate renderdoc module
needed). Driven by environment variables:

  RDC_IN      capture file (.rdc)                                   required
  RDC_OUT     output directory                                      required
  RDC_EVENTS  comma-separated event IDs to dump in detail           optional
              (post-VS outputs, bound textures, constant buffers)
  RDC_MAX_PNG max number of render-target PNGs in the overview      optional, default 40

  RDC_IN=x.rdc RDC_OUT=dir qrenderdoc --python rdc_dump.py

Outputs (in RDC_OUT):
  summary.txt          capture info, action counts, render targets used
  actions.txt          full action tree: event id, name, flags, indices, outputs
  rt_<eid>_<res>.png   each render target as it looks after the last draw into
                       it in a run of consecutive draws (shows the frame being built)
  event_<eid>.txt      detail for each RDC_EVENTS entry
  event_<eid>_tex*.png textures bound at that event
  error.txt            if something failed (with traceback)
"""
import os
import struct
import sys
import traceback

import renderdoc as rd

IN = os.environ.get("RDC_IN", "")
OUT = os.environ.get("RDC_OUT", "")
EVENTS = [int(e) for e in os.environ.get("RDC_EVENTS", "").replace(" ", "").split(",") if e]
MAX_PNG = int(os.environ.get("RDC_MAX_PNG", "40"))


def write(name, text):
    with open(os.path.join(OUT, name), "w") as fh:
        fh.write(text)


def ok(result):
    """ResultDetails (newer API) or ReplayStatus/ResultCode enum (older)."""
    if hasattr(result, "OK"):
        return result.OK()
    code = getattr(result, "code", result)
    return str(code).endswith("Succeeded")


def action_name(a, sfile):
    if hasattr(a, "GetName"):
        return a.GetName(sfile)
    return getattr(a, "name", "?")


def flag_names(flags):
    names = []
    for n in ("Clear", "Drawcall", "Dispatch", "Copy", "Resolve", "Present", "PushMarker",
              "SetMarker", "PassBoundary", "Indexed", "Instanced"):
        f = getattr(rd.ActionFlags, n, None)
        if f is not None and (flags & f):
            names.append(n)
    return "|".join(names)


def is_draw(a):
    return bool(a.flags & rd.ActionFlags.Drawcall)


def walk(actions, depth=0):
    for a in actions:
        yield a, depth
        yield from walk(a.children, depth + 1)


def tex_desc(tex_by_id, rid):
    t = tex_by_id.get(rid)
    if not t:
        return "?"
    fmt = t.format.Name() if hasattr(t.format, "Name") else str(t.format)
    return f"{t.width}x{t.height} {fmt}"


def save_png(controller, rid, path):
    ts = rd.TextureSave()
    ts.resourceId = rid
    ts.destType = rd.FileType.PNG
    ts.mip = 0
    ts.slice.sliceIndex = 0
    ts.alpha = rd.AlphaMapping.Discard
    controller.SaveTexture(ts, path)


def floats(data, n=None):
    count = len(data) // 4
    if n is not None:
        count = min(count, n)
    return struct.unpack_from("<%df" % count, data, 0)


def fmt_vec(vals):
    return " ".join("%10.4f" % v for v in vals)


def dump_event(controller, eid, tex_by_id, buf_by_id, sfile):
    controller.SetFrameEvent(eid, True)
    pipe = controller.GetPipelineState()
    lines = [f"=== event {eid}"]

    # Shaders + reflection
    stages = [("VS", rd.ShaderStage.Vertex), ("PS", rd.ShaderStage.Pixel)]
    for sname, stage in stages:
        refl = pipe.GetShaderReflection(stage)
        sid = pipe.GetShader(stage)
        lines.append(f"\n--- {sname} shader {sid}")
        if refl is None:
            continue
        if stage == rd.ShaderStage.Vertex:
            lines.append("outputs: " + ", ".join(
                f"{s.varName or s.semanticName}[{s.compCount}]" for s in refl.outputSignature))
        if stage == rd.ShaderStage.Pixel:
            lines.append("inputs: " + ", ".join(
                f"{s.varName or s.semanticName}[{s.compCount}]" for s in refl.inputSignature))

        # Constant blocks: raw float dump of the bound range
        for i, cb in enumerate(refl.constantBlocks):
            try:
                used = pipe.GetConstantBlock(stage, i, 0)
                desc = getattr(used, "descriptor", used)
                rid = getattr(desc, "resource", getattr(desc, "resourceId", rd.ResourceId.Null()))
                off = getattr(desc, "byteOffset", 0)
                size = getattr(desc, "byteSize", 0) or cb.byteSize
                if rid == rd.ResourceId.Null() or not size:
                    lines.append(f"cbuffer {i} '{cb.name}': not bound / inline")
                    continue
                size = min(size, 256 * 16)
                data = controller.GetBufferData(rid, off, size)
                vals = floats(data)
                lines.append(f"cbuffer {i} '{cb.name}' res={rid} off={off} size={size}")
                for r in range(0, min(len(vals), 256 * 4), 4):
                    row = vals[r:r + 4]
                    if any(row):
                        lines.append(f"  c{r // 4:<3} {fmt_vec(row)}")
            except Exception as e:  # keep going on API differences
                lines.append(f"cbuffer {i} '{cb.name}': error {e}")

    # Post-VS outputs for the first vertices
    lines.append("\n--- VS output (first 6 vertices)")
    try:
        mesh = controller.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
        if mesh.vertexResourceId != rd.ResourceId.Null() and mesh.vertexByteStride:
            stride = mesh.vertexByteStride
            data = controller.GetBufferData(mesh.vertexResourceId, mesh.vertexByteOffset,
                                            stride * 6)
            refl = pipe.GetShaderReflection(rd.ShaderStage.Vertex)
            names = [f"{s.varName or s.semanticName}[{s.compCount}]" for s in refl.outputSignature] \
                if refl else []
            lines.append(f"stride={stride} outputs={names}")
            for v in range(min(6, len(data) // stride)):
                vals = floats(data[v * stride:(v + 1) * stride])
                lines.append(f"  v{v}: " + " | ".join(fmt_vec(vals[i:i + 4])
                                                     for i in range(0, len(vals), 4)))
        else:
            lines.append("  (no post-VS data)")
    except Exception as e:
        lines.append(f"  error {e}")

    # Textures bound for reading (PS), saved as PNG
    lines.append("\n--- textures read by PS")
    try:
        ro = pipe.GetReadOnlyResources(rd.ShaderStage.Pixel)
        n = 0
        for used in ro:
            desc = getattr(used, "descriptor", None)
            rid = getattr(desc, "resource", None) if desc is not None else None
            if rid is None:  # older API: BoundResourceArray
                for res in getattr(used, "resources", []):
                    rid = res.resourceId
            if rid is None or rid == rd.ResourceId.Null() or rid not in tex_by_id:
                continue
            path = f"event_{eid}_tex{n}_{int(rid)}.png"
            lines.append(f"  tex{n}: {rid} {tex_desc(tex_by_id, rid)} -> {path}")
            try:
                save_png(controller, rid, os.path.join(OUT, path))
            except Exception as e:
                lines.append(f"    save failed: {e}")
            n += 1
        if n == 0:
            lines.append("  (none found)")
    except Exception as e:
        lines.append(f"  error {e}")

    # Shader disassembly (SPIR-V) for diffing translators
    try:
        targets = controller.GetDisassemblyTargets(True)
        target = targets[0] if targets else ""
        pipe_obj = pipe.GetGraphicsPipelineObject()
        for sname, stage in stages:
            refl = pipe.GetShaderReflection(stage)
            if refl is None:
                continue
            text = controller.DisassembleShader(pipe_obj, refl, target)
            path = f"event_{eid}_{sname.lower()}.spvasm"
            write(path, text)
            lines.append(f"{sname} disassembly ({target}) -> {path}")
    except Exception as e:
        lines.append(f"disassembly error: {e}")

    # Blend / colour-write state
    try:
        for i, b in enumerate(pipe.GetColorBlends()):
            cb, ab = b.colorBlend, b.alphaBlend
            lines.append(f"blend rt{i}: enabled={b.enabled} color={cb.source}*{cb.operation}*{cb.destination} "
                         f"alpha={ab.source}*{ab.operation}*{ab.destination} writeMask={b.writeMask:#x}")
    except Exception as e:
        lines.append(f"blend state error: {e}")

    # Render target after this draw
    try:
        for i, o in enumerate(pipe.GetOutputTargets()):
            rid = getattr(o, "resource", getattr(o, "resourceId", rd.ResourceId.Null()))
            if rid != rd.ResourceId.Null():
                path = f"event_{eid}_rt{i}.png"
                save_png(controller, rid, os.path.join(OUT, path))
                lines.append(f"render target {i}: {rid} {tex_desc(tex_by_id, rid)} -> {path}")
    except Exception as e:
        lines.append(f"render target error: {e}")

    write(f"event_{eid}.txt", "\n".join(lines) + "\n")


def main():
    os.makedirs(OUT, exist_ok=True)
    cap = rd.OpenCaptureFile()
    res = cap.OpenFile(IN, "", None)
    if not ok(res):
        raise RuntimeError(f"OpenFile failed: {res}")
    res, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if not ok(res):
        raise RuntimeError(f"OpenCapture failed: {res}")

    sfile = controller.GetStructuredFile()
    tex_by_id = {t.resourceId: t for t in controller.GetTextures()}
    buf_by_id = {b.resourceId: b for b in controller.GetBuffers()}
    roots = controller.GetRootActions()

    # Action tree
    lines = []
    draws = []
    counts = {}
    rt_usage = {}
    for a, depth in walk(roots):
        fl = flag_names(a.flags)
        outs = [o for o in a.outputs if o != rd.ResourceId.Null()]
        out_s = " ".join(f"{o}({tex_desc(tex_by_id, o)})" for o in outs)
        lines.append(f"{a.eventId:>7} {'  ' * depth}{action_name(a, sfile)}  [{fl}] "
                     f"idx={a.numIndices} inst={a.numInstances} {out_s}")
        for k in fl.split("|"):
            if k:
                counts[k] = counts.get(k, 0) + 1
        if is_draw(a):
            draws.append(a)
            for o in outs:
                rt_usage[o] = rt_usage.get(o, 0) + 1
    write("actions.txt", "\n".join(lines) + "\n")

    summary = [f"capture: {IN}",
               f"api: {controller.GetAPIProperties().pipelineType}",
               f"actions: {sum(1 for _ in walk(roots))}",
               "flag counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
               "", "render targets drawn to (draw count, desc):"]
    for rid, n in sorted(rt_usage.items(), key=lambda kv: -kv[1]):
        summary.append(f"  {rid}: {n} draws, {tex_desc(tex_by_id, rid)}")

    # PNG of each render target at the end of each run of consecutive draws into it
    pngs = []
    prev_rt = None
    prev_eid = None
    for a in draws:
        rt = next((o for o in a.outputs if o != rd.ResourceId.Null()), None)
        if prev_rt is not None and rt != prev_rt:
            pngs.append((prev_eid, prev_rt))
        prev_rt, prev_eid = rt, a.eventId
    if prev_rt is not None:
        pngs.append((prev_eid, prev_rt))
    summary.append("")
    summary.append(f"render-target snapshots ({min(len(pngs), MAX_PNG)} of {len(pngs)}):")
    for eid, rid in pngs[:MAX_PNG]:
        controller.SetFrameEvent(eid, True)
        path = f"rt_{eid:07d}_{int(rid)}.png"
        try:
            save_png(controller, rid, os.path.join(OUT, path))
            summary.append(f"  after event {eid}: {rid} {tex_desc(tex_by_id, rid)} -> {path}")
        except Exception as e:
            summary.append(f"  after event {eid}: save failed: {e}")
    write("summary.txt", "\n".join(summary) + "\n")

    for eid in EVENTS:
        try:
            dump_event(controller, eid, tex_by_id, buf_by_id, sfile)
        except Exception:
            write(f"event_{eid}.txt", traceback.format_exc())

    controller.Shutdown()
    cap.Shutdown()


try:
    if not IN or not OUT:
        raise RuntimeError("set RDC_IN and RDC_OUT")
    main()
    print(f"rdc_dump: wrote {OUT}")
except Exception:
    os.makedirs(OUT or ".", exist_ok=True)
    with open(os.path.join(OUT or ".", "error.txt"), "w") as fh:
        fh.write(traceback.format_exc())
    print("rdc_dump: FAILED, see error.txt")
finally:
    sys.stdout.flush()
    os._exit(0)  # don't open the qrenderdoc UI
