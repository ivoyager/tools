# build_shape_model.py
# This file is part of I, Voyager (https://ivoyager.dev)
# *****************************************************************************
# Copyright 2019-2026 Charlie Whitfield
# Licensed under the Apache License, Version 2.0 (the "License").
# *****************************************************************************
"""Build an ivoyager body model (.glb) from a measured vertex-facet shape model
(stereophotoclinometry: PDS Gaskell '..._ver###q.tab' via --tab, or a triangle
.obj via --obj as JHU/APL's SBMT distributes them), for genuinely irregular
bodies where no DEM/displaced-sphere applies (e.g. Phoebe, Phobos, Deimos).

Each shape vertex is in a body-fixed Cartesian frame (X=lon 0, Y=lon 90E, Z=north).
We place every vertex by its own (longitude, latitude, radius) as the displaced
SphereMesh: north -> +Y and phi = -lon - pi/2, the direction where Godot's
SphereMesh carries that longitude's texture u, with the UV that same unwrap,
u = (lon - 180)/360. IVBodyVisual applies the identical basis to a mesh body as to
the generic spheroid (rotY(-90 deg) then rotX(+90 deg)), so every vertex renders
at its own east longitude -- and the albedo must be CENTRED on the prime meridian
(the standard published convention; roll a 0-360-from-left-edge sheet by 180
first). Winding is forced outward; the albedo is embedded; no normal map (the
mesh carries all relief). --lon-offset-deg / --flip-u are escape hatches to match
an albedo whose longitude origin/handedness differs (verify in-engine).

phi was `math.pi + lon` until 2026-08-19 -- every SPC body mirrored about the
sub-parent meridian AND turned 180 deg -- and `-lon` with a left-edge UV until
2026-08-20, when the engine unified the mesh and spheroid frames. The check:
recover each built vertex's longitude through the placement's own inverse and
register the radius field against the source shape over a (lon, sin lat) grid,
sweeping roll and both handednesses; it must peak at roll 0, unmirrored.
"""

import argparse
import io
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_body_model as bbm  # write_glb, vertex_normals (same authoring frame)

Image.MAX_IMAGE_PIXELS = None


def read_gaskell_ver_tab(path):
    """Parse a Gaskell vertex-facet .tab. Returns (vertices_km[N,3], faces0[M,3])."""
    lines = Path(path).read_text().splitlines()
    nvert = int(lines[0].split()[0])
    verts = np.array([ln.split()[1:4] for ln in lines[1:1 + nvert]], dtype=np.float64)
    nplate = int(lines[1 + nvert].split()[0])
    fstart = 2 + nvert
    faces = np.array([ln.split()[1:4] for ln in lines[fstart:fstart + nplate]], dtype=np.int64)
    return verts, faces - 1  # 1-based -> 0-based


def read_obj(path):
    """Parse a triangle .obj shape model. Returns (vertices_km[N,3], faces0[M,3]).

    Only 'v' and 'f' are read; an SPC shape carries no UVs or normals of its own.
    A negative face index is relative to the vertices seen so far, so resolving it
    means knowing the count at that line -- these files never use the form, and
    silently mis-resolving it would corrupt the mesh, so it is rejected.
    """
    verts, faces = [], []
    for line in Path(path).read_text().splitlines():
        if line.startswith("v "):
            verts.append(line.split()[1:4])
        elif line.startswith("f "):
            faces.append([f.split("/")[0] for f in line.split()[1:4]])
    faces = np.array(faces, dtype=np.int64)
    if faces.size and faces.min() < 1:
        raise ValueError(f"{path}: relative (negative) face indices are not supported")
    return np.array(verts, dtype=np.float64), faces - 1


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True, help="Body file prefix, e.g. Phoebe")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--tab", help="Gaskell vertex-facet .tab")
    source.add_argument("--obj", help="Triangle .obj shape model, vertices in km")
    p.add_argument("--albedo", help="Equirectangular albedo to embed")
    p.add_argument("--lon-offset-deg", type=float, default=0.0)
    p.add_argument("--flip-u", action="store_true")
    p.add_argument("--metallic", type=float, default=0.0)
    p.add_argument("--roughness", type=float, default=0.9)
    p.add_argument("--model-scale", type=int, default=1000)
    p.add_argument("--out-dir", default=None)
    args = p.parse_args()

    verts, faces = read_gaskell_ver_tab(args.tab) if args.tab else read_obj(args.obj)
    print(f"loaded {len(verts)} vertices, {len(faces)} plates")

    # body-fixed -> per-vertex longitude/latitude/radius
    x, y, z = verts[:, 0], verts[:, 1], verts[:, 2]
    r = np.linalg.norm(verts, axis=1)
    lat = np.arcsin(np.clip(z / r, -1.0, 1.0))
    lon = np.arctan2(y, x)  # radians, east-positive

    # place as the displaced SphereMesh: north -> +Y, phi = -lon - pi/2 puts the vertex
    # at the SphereMesh direction of its own longitude's texture u, and the UV below is
    # that same unwrap -- the engine's shared spheroid basis then renders lon at lon.
    phi = -lon - math.pi / 2 + math.radians(args.lon_offset_deg)
    cl = np.cos(lat)
    pos = np.stack([cl * np.cos(phi), np.sin(lat), cl * np.sin(phi)], -1) * r[:, None]
    pos = pos.astype(np.float32)

    u = ((np.degrees(lon) - 180.0) % 360.0) / 360.0
    if args.flip_u:
        u = 1.0 - u
    uv = np.stack([u, (math.pi / 2 - lat) / math.pi], -1).astype(np.float32)

    # guard outward winding; this placement is a rotation, so a source already wound
    # outward stays so (before the phi fix it was a reflection, and did flip)
    a, b, c = pos[faces[:, 0]], pos[faces[:, 1]], pos[faces[:, 2]]
    if np.einsum("ij,ij->i", a.astype(np.float64), np.cross(b, c).astype(np.float64)).sum() < 0:
        faces = faces[:, ::-1].copy()
    faces = faces.astype(np.uint32)
    nrm = bbm.vertex_normals(pos.astype(np.float64), faces)

    albedo_bytes, albedo_mime = None, "image/png"
    if args.albedo:
        ext = Path(args.albedo).suffix.lower()
        if ext in (".jpg", ".jpeg"):
            albedo_bytes = Path(args.albedo).read_bytes(); albedo_mime = "image/jpeg"
        else:
            im = Image.open(args.albedo).convert("RGB")
            buf = io.BytesIO(); im.save(buf, "PNG"); albedo_bytes = buf.getvalue()

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        "addons/ivoyager_assets/models") / args.name.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_glb = out_dir / f"{args.name}.1_{args.model_scale}.glb"
    bbm.write_glb(str(out_glb), pos, nrm, uv, faces, albedo_bytes, albedo_mime, None,
                  args.metallic, args.roughness, args.name)

    ext_km = (pos.max(0) - pos.min(0))
    print(f"verts={len(pos)} faces={len(faces)} extent_km "
          f"X={ext_km[0]:.1f} Y={ext_km[1]:.1f} Z={ext_km[2]:.1f}  "
          f"radius[{r.min():.1f},{r.max():.1f}]  {out_glb.stat().st_size / 1048576:.1f}MB")
    print(f"\nAdd to file_adjustments.tsv:\n\t{out_glb.name}\t{args.model_scale}\t")


if __name__ == "__main__":
    main()
