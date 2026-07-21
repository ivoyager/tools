# bake_cubemap.py
# This file is part of I, Voyager (https://ivoyager.dev)
# *****************************************************************************
# Copyright 2019-2026 Charlie Whitfield
# Licensed under the Apache License, Version 2.0 (the "License").
# *****************************************************************************
"""Reproject an equirectangular body map into a Godot-native Cubemap.

Equirectangular maps pinch at the poles: the top texel row represents the whole
pole (radial starburst) and hardware filtering breaks where UV wraps or stretches.
A cubemap is sampled by a direction (`normalize(VERTEX)` in the shader), which is
exact on the sphere at every latitude with no pole singularity and no +/-180 seam.
This script bakes one channel (albedo, normal, ...) of one body into a 6-face
cube-strip PNG (default 3x2, faces in Godot order X+ X- Y+ Y- Z+ Z-), discoverable
by IVAssetPreloader from addons/ivoyager_assets/cubemaps/ like the equirect maps/
files. --retile converts an existing strip between layouts without an equirect
source (pure pixel rearrangement), for assets whose source is no longer available.

The strip imports as a CompressedCubemap through Godot's own layered-texture
importer, which slices, mips and VRAM-compresses it at import -- so the engine
decodes nothing at load, and each export platform gets a block format it supports
(S3TC/BPTC on desktop, ETC2/ASTC where that is what the GPU has). Note the importer
is named "cubemap_texture": "cubemap" is not a registered name and silently falls
back to the default png importer, yielding a plain 2D texture. The strip PNG stays
in the assets directory as inspectable, repairable source; exports carry only the
compressed cubemap.

Frame conventions (matched to the shared Godot SphereMesh the spheroid bodies use,
so reference_basis and map_offset carry over unchanged -- only the sampling method
differs from the equirect path):
  - The shader samples texture(cube, normalize(VERTEX)) where VERTEX is the unit
    SphereMesh local position. SphereMesh places north at +Y with
        y = cos(PI v),  x = sin(PI v) sin(TAU u),  z = sin(PI v) cos(TAU u)
    so a mesh-local direction d maps to the equirect map at
        lon01 = frac(atan2(d.x, d.z) / TAU),   lat01 = acos(d.y) / PI  (0 = north).
  - Godot cube face order is X+, X-, Y+, Y-, Z+, Z- with the standard GL per-face
    (sc, tc) axes; face_dir() is that convention's inverse. The exact in-face row
    flip and the normal-map tangent signs are calibrated once in-engine with
    --debug-cube (see the Moon prototype plan); adjust --flip-v / --normal-sign-*
    if the labeled faces or crater relief come out wrong.

Normal maps cannot be reprojected pixel-wise: the shipped equirect map is
tangent-space (R=east, G=north, B=up in the equirect frame), but a cube face has a
different tangent basis per direction. --normal bakes an OBJECT-space normal (in
unit-sphere/mesh space) instead -- i.e. it does the tangent->object rotation offline
with a clean analytic frame, sidestepping the mesh's degenerate polar TBN. The cube
shader then transforms that by MODELVIEW_NORMAL_MATRIX and writes NORMAL directly.

What is STORED, though, is the residual (normal minus the texel's own sphere
direction), decoded as normalize(dir + residual * normal_residual_scale). Storing the
normal itself puts the direction's full-range sweep into the data, which costs a
specular surface a visible 4x4 grid where the block codec fits each block's endpoints
independently -- see bake_normal_face(). --normal-residual-scale must match the
shader uniform of that name; the default 1.0 clips nothing in any shipped map.

Every channel map under maps/ converts, including shell overlays (Earth.clouds.albedo,
whose shell token rides along in the output name) and bodies with no surface albedo
(the Sun, emission-only). A shell overlay carries its coverage in ALPHA, so alpha is
reprojected alongside rgb and the strip imports BC7 -- see load_equirect().
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from build_body_model import bilinear_equirect  # vectorized, longitude-wrapping

Image.MAX_IMAGE_PIXELS = None  # planetary maps exceed Pillow's decompression guard

TAU = 2.0 * math.pi

# Godot / GL cube face order and layouts (columns, rows) for the source strip.
FACE_NAMES = ["posx", "negx", "posy", "negy", "posz", "negz"]
LAYOUTS = {"1x6": (1, 6), "6x1": (6, 1), "2x3": (2, 3), "3x2": (3, 2)}

# Godot's slices/arrangement enum. Its order is not documented; these values were read
# off the engine (only the matching one slices a strip into square faces). A layout
# missing here must raise rather than import as some other arrangement's garbage.
ARRANGEMENTS = {"1x6": 0, "2x3": 1, "3x2": 2, "6x1": 3}


# ----------------------------------------------------------------------------- cube geometry

def face_dir(face, u, v):
    """Direction that Godot's samplerCube maps face texel (u, v) in [0,1] to, using
    the standard GL per-face (sc, tc) axes. u, v are arrays; returns a stacked,
    normalized [..., 3] direction array."""
    sc = 2.0 * u - 1.0
    tc = 2.0 * v - 1.0
    one = np.ones_like(sc)
    if face == 0:    d = np.stack([one, -tc, -sc], -1)   # +X
    elif face == 1:  d = np.stack([-one, -tc, sc], -1)   # -X
    elif face == 2:  d = np.stack([sc, one, tc], -1)     # +Y
    elif face == 3:  d = np.stack([sc, -one, -tc], -1)   # -Y
    elif face == 4:  d = np.stack([sc, -tc, one], -1)    # +Z
    else:            d = np.stack([-sc, -tc, -one], -1)  # -Z
    return d / np.linalg.norm(d, axis=-1, keepdims=True)


def dir_to_lonlat(d):
    """Mesh-local direction [...,3] -> (lon01 wrapping, lat01 in [0,1], 0=north),
    the inverse of Godot SphereMesh's UV unwrap."""
    lon01 = (np.arctan2(d[..., 0], d[..., 2]) / TAU) % 1.0
    lat01 = np.arccos(np.clip(d[..., 1], -1.0, 1.0)) / math.pi
    return lon01, lat01


def face_texel_grid(face, size, supersample, flip_v):
    """Directions at each supersampled texel center of one face. Returns [n, n, 3]
    with n = size * supersample; row = v (top-to-bottom unless flip_v)."""
    n = size * supersample
    centers = (np.arange(n) + 0.5) / n
    u = centers
    v = centers[::-1] if flip_v else centers
    grid_u, grid_v = np.meshgrid(u, v)
    return face_dir(face, grid_u, grid_v)


def area_downsample(hires, supersample):
    """Box-average [n, n, C] -> [size, size, C] (n = size * supersample)."""
    n = hires.shape[0]
    size = n // supersample
    channels = hires.shape[2]
    blocks = hires.reshape(size, supersample, size, supersample, channels)
    return blocks.mean(axis=(1, 3))


# ----------------------------------------------------------------------------- sampling

def load_equirect(path, keep_alpha=True):
    """Load an equirect map as [H,W,3] or [H,W,4] float64 in [0,255].

    A shell overlay (Earth.clouds.albedo) is all-white rgb with its cloud coverage in
    alpha, so dropping alpha would render a solid white deck. Alpha that carries no
    information is dropped anyway: an opaque alpha channel would still cost the strip
    BC7's 8 bpp where rgb alone compresses to BC1's 4 bpp."""
    image = Image.open(path)
    has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    if not (keep_alpha and has_alpha):
        return np.asarray(image.convert("RGB"), np.float64)
    array = np.asarray(image.convert("RGBA"), np.float64)
    return array if array[..., 3].min() < 255.0 else array[..., :3]


def sample_equirect(img, lon01, lat01):
    """Sample a [H,W] or [H,W,C] equirect image at fractional (lon01, lat01) arrays,
    per channel via the shared longitude-wrapping bilinear sampler."""
    if img.ndim == 2:
        return bilinear_equirect(img, lon01, lat01)
    channels = [bilinear_equirect(img[..., c], lon01, lat01) for c in range(img.shape[2])]
    return np.stack(channels, -1)


def bake_color_face(face, size, supersample, flip_v, equirect_rgb):
    """One color face (albedo/roughness/emission): reproject + supersample +
    area-average. Returns [size,size,3] in [0,255]."""
    directions = face_texel_grid(face, size, supersample, flip_v)
    lon01, lat01 = dir_to_lonlat(directions)
    hires = sample_equirect(equirect_rgb, lon01, lat01)
    return area_downsample(hires, supersample)


def bake_normal_face(face, size, supersample, flip_v, equirect_normal, sign_east, sign_north):
    """One object-space (unit-sphere/mesh-space) normal face, returned as the RESIDUAL
    from the texel's own sphere direction (caller scales and encodes it).

    Storing the normal itself would put the sphere direction -- which sweeps the full
    [-1,1] range across a face -- into the data, and both the 8-bit encoding and the block
    codec then have to resolve the terrain against that carrier. At 8 bits one step is
    0.45 deg of tilt, against source relief whose median tilt is 0.32 deg, and every 4x4
    block has a gradient whose endpoints it fits independently of its neighbours. A
    specular lobe turns those seams into a visible grid. The residual is near zero over
    flat ground, which is why a tangent-space map (stored around a constant) does not
    show this, and it stays frame-free so cross-face filtering is still valid."""
    directions = face_texel_grid(face, size, supersample, flip_v)
    lon01, lat01 = dir_to_lonlat(directions)
    tangent = sample_equirect(equirect_normal, lon01, lat01) / 255.0 * 2.0 - 1.0  # [n,n,3]

    lon_angle = TAU * lon01
    colat = math.pi * lat01
    cos_lon, sin_lon = np.cos(lon_angle), np.sin(lon_angle)
    cos_colat, sin_colat = np.cos(colat), np.sin(colat)
    east = np.stack([cos_lon, np.zeros_like(cos_lon), -sin_lon], -1)
    north = np.stack([-cos_colat * sin_lon, sin_colat, -cos_colat * cos_lon], -1)
    up = directions

    obj = (sign_east * tangent[..., 0:1]) * east \
        + (sign_north * tangent[..., 1:2]) * north \
        + tangent[..., 2:3] * up
    obj = area_downsample(obj, supersample)
    obj /= np.clip(np.linalg.norm(obj, axis=-1, keepdims=True), 1e-12, None)
    return obj - face_texel_grid(face, size, 1, flip_v)  # deviation from the smooth sphere


def bake_debug_face(face, size):
    """A solid-color labeled face for in-engine convention calibration: the face
    name, an arrow pointing toward +v (down the stored image), and a corner dot at
    texel (0,0) so the row flip and per-face axes are unambiguous."""
    colors = [(210, 60, 60), (150, 20, 20), (60, 200, 60),
              (20, 120, 20), (70, 90, 220), (30, 40, 130)]
    img = Image.new("RGB", (size, size), colors[face])
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size // 12, size // 12], fill=(255, 255, 0))  # (0,0) corner
    mid = size // 2
    draw.line([(mid, size // 4), (mid, 3 * size // 4)], fill=(255, 255, 255), width=max(2, size // 128))
    draw.polygon([(mid - size // 20, 3 * size // 4 - size // 20),
                  (mid + size // 20, 3 * size // 4 - size // 20),
                  (mid, 3 * size // 4)], fill=(255, 255, 255))
    draw.text((mid - size // 8, size // 2 - size // 16),
              FACE_NAMES[face].upper(), fill=(255, 255, 255))
    return np.asarray(img, np.float64)


def bake_debug_normal_face(face, size, flip_v):
    """Zero relief, so a debug body lights like a plain sphere and its albedo face
    pattern stays visible. Zero residual IS the smooth sphere; see bake_normal_face."""
    return np.zeros((size, size, 3))


# ----------------------------------------------------------------------------- strip + import

def arrange_strip(faces, layout):
    """Tile the six [size,size,C] face arrays into the source strip for `layout`."""
    cols, rows = LAYOUTS[layout]
    size = faces[0].shape[0]
    channels = faces[0].shape[2]
    strip = np.zeros((rows * size, cols * size, channels), faces[0].dtype)
    for index, face in enumerate(faces):
        r, c = divmod(index, cols)
        strip[r * size:(r + 1) * size, c * size:(c + 1) * size] = face
    return strip


# Godot slices, mips and VRAM-compresses the strip at import and stores a
# CompressedCubemap, so the engine decodes nothing at load. The importer is named
# "cubemap_texture" -- "cubemap" is not a registered name and silently falls back to the
# default png "texture" importer, which is why an earlier version of this template
# produced plain 2D textures. compress/mode=2 is VRAM Compressed; high_quality picks BC7
# over BC1, which the 3-channel object-space normal needs (BC1's 5:6:5 bands it badly).
# This importer has no normal_map param, so it can never force 2-channel RGTC -- which
# could not hold an object-space normal at all.
IMPORT_TEMPLATE = """[remap]

importer="cubemap_texture"
type="CompressedCubemap"

[params]

compress/mode=2
compress/high_quality={high_quality}
compress/lossy_quality=0.7
compress/uastc_level=0
compress/rdo_quality_loss=0.0
compress/hdr_compression=1
compress/channel_pack={channel_pack}
mipmaps/generate=true
mipmaps/limit=-1
slices/arrangement={arrangement}
"""


def write_import(png_path, layout, linear, high_quality):
    """Write the strip's CompressedCubemap .import. normal and roughness data are linear
    (channel_pack=1); albedo/emission color packs sRGB-friendly (0)."""
    text = IMPORT_TEMPLATE.format(
        high_quality="true" if high_quality else "false",
        channel_pack=1 if linear else 0,
        arrangement=ARRANGEMENTS[layout],
    )
    Path(str(png_path) + ".import").write_text(text, encoding="utf8", newline="\n")


def infer_layout(width, height):
    """The layout whose (cols, rows) tile width x height into square faces."""
    for name, (cols, rows) in LAYOUTS.items():
        if width % cols == 0 and height % rows == 0 and width // cols == height // rows:
            return name
    sys.exit(f"{width}x{height} is not a cube strip in any known layout")


def save_strip(strip, out_path, layout, linear, is_normal):
    """Write the assembled strip and its .import. A strip that kept alpha must import
    BC7 (high_quality): mode 2 would otherwise pick BC3, which costs the same 8 bpp and
    resolves alpha worse. Object-space normals take BC7 for their own reason (BC1's
    5:6:5 bands them), so the two cases share the flag."""
    has_alpha = strip.shape[2] == 4
    Image.fromarray(np.clip(strip, 0, 255).astype(np.uint8),
                    "RGBA" if has_alpha else "RGB").save(out_path)
    write_import(out_path, layout, linear, is_normal or has_alpha)


def retile(strip_path, layout, out_dir):
    """Rearrange an existing strip's six faces into `layout` and rewrite its .import.
    No resampling and no equirect source -- for assets whose source is gone."""
    image = load_equirect(strip_path).astype(np.uint8)  # a strip, but the same loader rules
    height, width = image.shape[:2]
    source_layout = infer_layout(width, height)
    cols, _rows = LAYOUTS[source_layout]
    size = width // cols
    faces = []
    for index in range(6):
        row, col = divmod(index, cols)
        faces.append(image[row * size:(row + 1) * size, col * size:(col + 1) * size])
    tag = split_map_name(Path(strip_path).name)[1]
    linear = True if tag == "normal" else COLOR_CHANNELS[tag]
    out_path = Path(out_dir) / Path(strip_path).name
    save_strip(arrange_strip(faces, layout), out_path, layout, linear, tag == "normal")
    print(f"  retile {out_path.name}: {source_layout} -> {layout}  (face {size})")


# ----------------------------------------------------------------------------- channels

# Channels this baker converts. Color channels reproject like albedo; "normal"
# reprojects to object space. `linear` sets the strip's import color-space.
COLOR_CHANNELS = {"albedo": False, "roughness": True, "emission": False}  # tag -> linear
CHANNEL_TAGS = ("normal", *COLOR_CHANNELS)
MAP_EXTS = (".jpg", ".jpeg", ".png")


def face_size_for(source_width):
    """Cube face edge: the smallest power of two at or above source_width/4.

    A quarter of the width puts the face's average angular density at the source's
    equator density. The power of two is not a preference: Godot's cubemap_texture
    importer stores faces at a power of two and upscales anything else at import, so
    a 450 face occupies exactly the VRAM of a 512 one while carrying 450 texels of
    detail through an extra resample. Rounding up bakes directly at the size that
    ships, and the gap it closes can be wide: a 720 face is stored at 1024, so half
    its texels would be interpolation.
    """
    return 1 << (max(4, source_width // 4) - 1).bit_length()


def bake_channel(name, tag, equirect_path, size, supersample, flip_v, layout, out_dir,
                 sign_east, sign_north, residual_scale=1.0):
    """Bake one channel of one body to a face strip + .import in out_dir. `name` is the
    file prefix INCLUDING any shell token, e.g. "Earth" or "Earth.clouds"."""
    # face_size_for() never returns a non-power-of-two, so only --face-size reaches this.
    if size & (size - 1):
        print(f"    WARNING: face {size} is not a power of two; the importer will store it"
              f" at {1 << (size - 1).bit_length()}, so it costs that VRAM either way"
              f" -- see face_size_for()")
    # An object-space normal has no alpha to carry, and reading one would put a fourth
    # component through the rotate-and-renormalize path that has no meaning there.
    equirect = load_equirect(equirect_path, keep_alpha=tag != "normal")
    if tag == "normal":
        faces = [bake_normal_face(f, size, supersample, flip_v, equirect, sign_east, sign_north)
                 for f in range(6)]
        peak = max(float(np.abs(face).max()) for face in faces)
        # The shader multiplies the decoded residual by this same scale, so the two must
        # agree; it is a shader uniform (normal_residual_scale) defaulting to 1.0.
        if peak > residual_scale:
            print(f"    WARNING: relief peaks at {peak:.3f}, above --normal-residual-scale"
                  f" {residual_scale}; steepest slopes will be flattened")
        else:
            print(f"    relief peaks at {peak:.3f} of the {residual_scale} encoding range")
        faces = [np.clip(face / residual_scale, -1.0, 1.0) * 0.5 + 0.5 for face in faces]
        strip = arrange_strip(faces, layout) * 255.0
        linear = True
    else:
        faces = [bake_color_face(f, size, supersample, flip_v, equirect) for f in range(6)]
        strip = arrange_strip(faces, layout)
        linear = COLOR_CHANNELS[tag]
    out_path = out_dir / f"{name}.{tag}.{size}.png"
    # A strip of the same channel at a DIFFERENT face size is not overwritten, and
    # IVAssetPreloader would then pick between them arbitrarily -- with a stale one
    # possibly predating an encoding change. Name it rather than delete it.
    for stale in sorted(out_dir.glob(f"{name}.{tag}.*.png")):
        if stale != out_path:
            print(f"    WARNING: stale {stale.name} remains alongside {out_path.name};"
                  f" delete it (with its .import) or the preloader may load either")
    save_strip(strip, out_path, layout, linear, tag == "normal")
    print(f"  {tag}: {equirect.shape[1]}x{equirect.shape[0]}"
          f"{'+a' if equirect.shape[2] == 4 else ''} -> 6x {size}  "
          f"({out_path.name}, {out_path.stat().st_size / 1048576:.1f} MB)")


def split_map_name(filename):
    """(name, tag) for a channel map filename, or None if it isn't one. `name` is
    everything before the tag, so a shell token stays attached to the body prefix
    ("Earth.clouds") and the output strip inherits the whole thing. The <res> token
    after the tag is conventional but optional, as it is for IVAssetPreloader."""
    parts = filename.split(".")
    for index in (-3, -2):  # <name>.<tag>.<res>.<ext> or <name>.<tag>.<ext>
        if len(parts) + index >= 1 and parts[index] in CHANNEL_TAGS:
            return ".".join(parts[:index]), parts[index]
    return None


def scan_maps(maps_dir):
    """{name: {tag: Path}} for every channel map directly under maps_dir."""
    bodies = {}
    for path in sorted(Path(maps_dir).iterdir()):
        if not path.is_file() or path.suffix.lower() not in MAP_EXTS:
            continue
        split = split_map_name(path.name)
        if split:
            bodies.setdefault(split[0], {})[split[1]] = path
    return bodies


# ----------------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", help="Output file prefix, e.g. Moon or Earth.clouds"
                        " -- INCLUDE the shell token when converting a shell overlay,"
                        " or the strip overwrites the body's surface map (single-body mode)")
    parser.add_argument("--albedo", help="Equirectangular color map to reproject")
    parser.add_argument("--normal", help="Equirectangular tangent-space normal map to reproject")
    parser.add_argument("--roughness", help="Equirectangular roughness map (linear)")
    parser.add_argument("--emission", help="Equirectangular emission / night-lights map")
    parser.add_argument("--batch", metavar="MAPS_DIR", nargs="?",
                        const="addons/ivoyager_assets/maps",
                        help="convert every surface map under MAPS_DIR (default addons/ivoyager_assets/maps)")
    parser.add_argument("--face-size", type=int, default=None,
                        help="cube face edge; default is the power of two at or above"
                             " source_width/4 per channel. A non-power-of-two is upscaled"
                             " at import, so it buys nothing -- see face_size_for()")
    parser.add_argument("--supersample", type=int, default=4,
                        help="per-face oversample before area-averaging (anti-aliases the pole faces)")
    parser.add_argument("--layout", choices=list(LAYOUTS), default="3x2")
    parser.add_argument("--retile", metavar="STRIP_PNG", nargs="+",
                        help="rearrange existing cube strips into --layout (no equirect source)")
    parser.add_argument("--flip-v", action="store_true",
                        help="store faces bottom-to-top (calibration; see --debug-cube)")
    parser.add_argument("--normal-residual-scale", type=float, default=1.0,
                        help="half-range the normal residual is encoded over; MUST match the"
                             " shader's normal_residual_scale uniform. 1.0 never clips; smaller"
                             " resolves gentle relief finer (the 8-bit step is 0.45 deg of tilt"
                             " at 1.0) but flattens slopes beyond it")
    parser.add_argument("--normal-sign-east", type=int, choices=[1, -1], default=1)
    parser.add_argument("--normal-sign-north", type=int, choices=[1, -1], default=1)
    parser.add_argument("--debug-cube", action="store_true",
                        help="emit a labeled 6-face calibration cube instead of a body map")
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else Path("addons/ivoyager_assets/cubemaps")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.debug_cube:
        size = args.face_size or 512
        albedo_faces = [bake_debug_face(face, size) for face in range(6)]
        albedo_path = out_dir / f"{args.name}.albedo.{size}.png"
        save_strip(arrange_strip(albedo_faces, args.layout), albedo_path, args.layout,
                   linear=False, is_normal=False)
        normal_faces = [bake_debug_normal_face(face, size, args.flip_v) for face in range(6)]
        normal_path = out_dir / f"{args.name}.normal.{size}.png"
        save_strip(arrange_strip(normal_faces, args.layout) * 255.0, normal_path, args.layout,
                   linear=True, is_normal=True)
        print(f"debug cube -> {albedo_path} (+ smooth normal)  ({args.layout}, face {size})")
        return

    if args.retile:
        for strip_path in args.retile:
            retile(strip_path, args.layout, out_dir)
        return

    if args.batch:
        bodies = scan_maps(args.batch)
        for name in sorted(bodies):
            channels = bodies[name]
            print(f"{name}: {', '.join(sorted(channels))}")
            for tag in sorted(channels):
                width = Image.open(channels[tag]).size[0]
                size = args.face_size or face_size_for(width)
                bake_channel(name, tag, channels[tag], size, args.supersample, args.flip_v,
                             args.layout, out_dir, args.normal_sign_east, args.normal_sign_north,
                             args.normal_residual_scale)
        return

    if not args.name:
        sys.exit("give --name (with channel maps), --batch, or --debug-cube")
    channel_args = {"albedo": args.albedo, "normal": args.normal,
                    "roughness": args.roughness, "emission": args.emission}
    if not any(channel_args.values()):
        sys.exit("give at least one of --albedo / --normal / --roughness / --emission")
    for tag, equirect_path in channel_args.items():
        if not equirect_path:
            continue
        width = Image.open(equirect_path).size[0]
        size = args.face_size or face_size_for(width)
        bake_channel(args.name, tag, equirect_path, size, args.supersample, args.flip_v,
                     args.layout, out_dir, args.normal_sign_east, args.normal_sign_north,
                     args.normal_residual_scale)


if __name__ == "__main__":
    main()
