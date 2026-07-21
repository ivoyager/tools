# unwrap_cubemap.py
# This file is part of I, Voyager (https://ivoyager.dev)
# *****************************************************************************
# Copyright 2019-2026 Charlie Whitfield
# Licensed under the Apache License, Version 2.0 (the "License").
# *****************************************************************************
"""Reproject a Cubemap face-strip back to an equirectangular map, optionally rotated.

Diagnostic for discussion #22: separates baked-in polar DATA artifacts from equirect
PROJECTION artifacts. With --rotate sideways the sphere is turned 90 deg so the
original poles land on the new EQUATOR (where equirect sampling is well-behaved, no
pole wrap) and the original equator lands at the new poles. View the result through
the ordinary equirect pipeline:
  - If the (former-pole) content at the new equator still shows radial smearing, the
    artifact is in the source DATA -- the cubemap cannot cure it; the map needs polar
    repair. (Meanwhile the former equator, now at the new poles, will show the normal
    equirect pinch: a control confirming that pinch is a projection effect on good data.)
  - If it looks clean, the artifact was purely equirect projection wrap, which the
    cubemap already removes.

Reads the same 1x6 vertical face strip bake_cubemap.py writes (Godot cube order
+X,-X,+Y,-Y,+Z,-Z), and emits a plain equirect albedo PNG + a lossless .import.
"""

import argparse
import math
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
TAU = 2.0 * math.pi

# 90 deg rotations of the SAMPLING direction. "sideways" = Rx(-90): (x,y,z)->(x,z,-y),
# which sends +Y (north pole) to +Z and -Y to -X-ish -- i.e. both poles onto the equator.
ROTATIONS = {
	"none": lambda d: d,
	"sideways": lambda d: np.stack([d[..., 0], d[..., 2], -d[..., 1]], -1),
}


def cube_forward(dirs):
	"""Direction [...,3] -> (face indices, u, v in [0,1]). Matches Godot's hardware cube
	map and _detail.cube.gdshaderinc cube_face_uv (validated in-engine)."""
	x, y, z = dirs[..., 0], dirs[..., 1], dirs[..., 2]
	ax, ay, az = np.abs(x), np.abs(y), np.abs(z)
	face = np.zeros(x.shape, np.int32)
	sc = np.zeros(x.shape)
	tc = np.zeros(x.shape)
	major = np.ones(x.shape)

	mx = (ax >= ay) & (ax >= az)
	my = (~mx) & (ay >= az)
	mz = (~mx) & (~my)
	px = mx & (x >= 0); nx = mx & (x < 0)
	face[px] = 0; sc[px] = -z[px]; tc[px] = -y[px]; major[px] = ax[px]
	face[nx] = 1; sc[nx] = z[nx]; tc[nx] = -y[nx]; major[nx] = ax[nx]
	py = my & (y >= 0); ny = my & (y < 0)
	face[py] = 2; sc[py] = x[py]; tc[py] = z[py]; major[py] = ay[py]
	face[ny] = 3; sc[ny] = x[ny]; tc[ny] = -z[ny]; major[ny] = ay[ny]
	pz = mz & (z >= 0); nz = mz & (z < 0)
	face[pz] = 4; sc[pz] = x[pz]; tc[pz] = -y[pz]; major[pz] = az[pz]
	face[nz] = 5; sc[nz] = -x[nz]; tc[nz] = -y[nz]; major[nz] = az[nz]

	u = sc / major * 0.5 + 0.5
	v = tc / major * 0.5 + 0.5
	return face, u, v


def sample_cube_strip(strip, dirs):
	"""Bilinear-sample a 1x6 vertical face strip [6*fs, fs, C] by direction. Clamps within
	each face (the diagnostic inspects face centers, not the 1-texel edges)."""
	face_size = strip.shape[1]
	face, u, v = cube_forward(dirs)
	fx = np.clip(u * face_size - 0.5, 0, face_size - 1)
	fy = np.clip(v * face_size - 0.5, 0, face_size - 1)
	x0 = np.floor(fx).astype(int); x1 = np.minimum(x0 + 1, face_size - 1)
	y0 = np.floor(fy).astype(int); y1 = np.minimum(y0 + 1, face_size - 1)
	tx = (fx - x0)[..., None]; ty = (fy - y0)[..., None]
	row0 = face * face_size + y0
	row1 = face * face_size + y1
	c00 = strip[row0, x0]; c10 = strip[row0, x1]
	c01 = strip[row1, x0]; c11 = strip[row1, x1]
	top = c00 * (1 - tx) + c10 * tx
	bot = c01 * (1 - tx) + c11 * tx
	return top * (1 - ty) + bot * ty


EQUIRECT_IMPORT = """[remap]

importer="texture"
type="CompressedTexture2D"

[params]

compress/mode=0
mipmaps/generate=true
detect_3d/compress_to=0
"""


def main():
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--strip", required=True, help="cubemap face-strip PNG (bake_cubemap.py output)")
	parser.add_argument("--name", required=True, help="output body prefix, e.g. Moon")
	parser.add_argument("--width", type=int, default=4096, help="output equirect width (height = width/2)")
	parser.add_argument("--rotate", choices=list(ROTATIONS), default="sideways")
	parser.add_argument("--out-dir", default="addons/ivoyager_assets/maps")
	args = parser.parse_args()

	strip = np.asarray(Image.open(args.strip).convert("RGB"), np.float64)
	out_w = args.width
	out_h = out_w // 2
	u = (np.arange(out_w) + 0.5) / out_w
	v = (np.arange(out_h) + 0.5) / out_h  # 0 = north
	grid_u, grid_v = np.meshgrid(u, v)

	# equirect pixel -> mesh direction (SphereMesh convention: north +Y)
	sin_p = np.sin(math.pi * grid_v)
	cos_p = np.cos(math.pi * grid_v)
	dirs = np.stack([sin_p * np.sin(TAU * grid_u), cos_p, sin_p * np.cos(TAU * grid_u)], -1)
	dirs = ROTATIONS[args.rotate](dirs)

	out = sample_cube_strip(strip, dirs)
	out_dir = Path(args.out_dir)
	out_dir.mkdir(parents=True, exist_ok=True)
	out_path = out_dir / f"{args.name}.albedo.{out_w}.png"
	Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB").save(out_path)
	Path(str(out_path) + ".import").write_text(EQUIRECT_IMPORT, encoding="utf8", newline="\n")
	print(f"rotate={args.rotate} -> {out_path}  ({out_w}x{out_h}, {out_path.stat().st_size / 1048576:.1f} MB)")


if __name__ == "__main__":
	main()
