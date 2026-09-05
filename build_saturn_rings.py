# build_saturn_rings.py
# This file is part of I, Voyager (https://ivoyager.dev)
# *****************************************************************************
# Copyright 2019-2026 Charlie Whitfield
# Licensed under the Apache License, Version 2.0 (the "License").
# *****************************************************************************
"""Build Saturn's ring texture from Bjoern Joensson's radial ring profiles.

    source_data/rings/*.txt  ->  addons/ivoyager_assets/rings/saturn.rings.<w>.exr

ONE file, imported as a CompressedTexture2DArray of three <w> x 1 layers:

    layer 0  backscatter    the lit side seen from near the sun's direction
    layer 1  forwardscatter the lit side at phase angle 139 deg
    layer 2  unlitside      the side away from the sun, lit through the rings

    rgb  LINEAR radiance, PREMULTIPLIED (the ring's own light over black sky)
    a    1 - transparency: the fraction of the background this radius occludes,
         at NORMAL incidence

Source: https://bjj.mmedia.is/data/s_rings/ -- five text files of 13177 values each,
5.000 km apart, spanning 74510 to 140390 km from Saturn's centre. Three are radial
brightness profiles Joensson measured in Voyager images (Voyager 2 NAC for
backscatter, Voyager 1 WAC for the other two); `transparency` is the Voyager stellar
occultation normal optical depth from the PDS Rings Node, converted to transmission
and hand-cleaned in the gaps; `color` is a per-radius tint from a Cassini colour
image, normalized to peak 1. Rendering the rings needs all of them together, which is
what this bakes.

WHAT THE FILE MEANS, AND WHY THAT IS NOT WHAT THE OLD ONE MEANT
---------------------------------------------------------------
THE PROFILES ARE PREMULTIPLIED, AND THE SOURCE SAYS SO IN THE DATA. A brightness
profile is exactly 0 at all 1031 radii where transparency is exactly 1 -- material
absent, not material dark -- so it is an observed image brightness that already
carries the ring's own coverage, and the pair (brightness, 1 - transparency) is a
premultiplied RGBA. Joensson's page says the same in words: "it's really not possible
to use this data alone, you need the transparency profile as well". Composite it with
`blend_premul_alpha` (radiance + T * background), never by multiplying rgb by alpha
again -- that darkens every radius by its own opacity, which costs the faint C ring
and the Cassini Division almost everything they have.

THE PROFILES ARE LINEAR, WHICH IS TESTABLE RATHER THAN ASSUMED. Fitting the
single-scattering slab model to the profiles against transmission -- `K (1 - T^k)`
lit, `K (T^a - T^b)` unlit -- prefers the values as published over an sRGB-decoded
reading of them (unlit R2 0.821 against 0.667, and the decoded fit is degenerate,
a = b, K = 125). So the file is linear light and the sampler must NOT be
`source_color`. `--verify` re-runs that fit.

HALF FLOAT IS WHAT MAKES BOTH OF THOSE STORABLE. An 8-bit file must choose: store
linear and band the faint rings (a half-code at reflectance 0.05 is 1.3 display
codes, at 0.01 it is 3.3), or store sRGB-encoded and let the engine average the
ENCODED codes when it generates mipmaps, which is the one thing a radial profile
must not do -- a distant ring is nothing but its own mip chain. Godot imports .exr
to FORMAT_RGBAH and mips it in linear light, so neither trade is needed.

WHAT A PHOTOMETRIC OVERHAUL GETS FROM THIS FILE. Alpha is `1 - exp(-tau_normal)`,
so `tau = -ln(1 - a)` recovers the optical depth the occultation measured, and a
slant path is `(1 - a)^(1/mu)` -- which is what `_sun_occlusion.gdshaderinc` already
does for the sun leg. The per-radius tint survives as the chromaticity of layer 0
(the scalar profile cannot tint it), and layers 0 and 1 are two phase samples of the
same surface. Nothing published here is thrown away and nothing is baked in: the
1.05 forward-scatter reddening and every brightness boost stay shader uniforms.

NO PADDING, AND NO CONSTANT SHARED WITH THE ENGINE. The retired asset padded 5 % of
the span onto each end with transparent black and hard-coded that fraction in the
converter AND in rings.gd. The texture now spans exactly the data: texel i is the
sample at 74510 + 5 i km, so the texture's edges sit half a sample outside the
table's own `inner_radius` and `outer_radius` and rings.gd derives that from the
width it loads. The shader fades the last texel out over its own screen footprint.

Radial resolution is Joensson's 5 km and is NOT resampled to a power of two: 13177
samples mip to 14 levels perfectly well, and interpolating to 16384 would add a
resampling generation for no information. Note that only `transparency` is really
5 km data -- the three imaging profiles change every 6 to 12 samples, having been
resampled up to match it -- so alpha is the sharpest channel in the file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exr_writer
from _project import project_dir

SOURCE_DIR = Path(__file__).resolve().parent / "source_data" / "rings"
OUT_DIR = Path("addons/ivoyager_assets/rings")
NAME = "saturn.rings"

# Joensson's own unlit-side colour; his colour profile is for the lit side only.
UNLIT_COLOR = np.array([1.0, 0.97075, 0.952])

# The radial span the five files cover, from the source page. rings.tsv carries the
# same two numbers as Saturn's ring inner_radius/outer_radius.
INNER_RADIUS_KM = 74510.0
OUTER_RADIUS_KM = 140390.0

LAYERS = ("backscattered", "forwardscattered", "unlitside")
COLOR_FILE = "sat_rings_color"  # the publisher's own name; the other four match theirs

IMPORT_TEMPLATE = """[remap]

importer="2d_array_texture"
type="CompressedTexture2DArray"

[params]

compress/mode=0
compress/high_quality=false
compress/lossy_quality=0.7
compress/hdr_compression=0
compress/channel_pack=1
mipmaps/generate=true
mipmaps/limit=-1
slices/horizontal=1
slices/vertical=3
"""

# Published normal optical depths, for the registration check in --verify. Ranges are
# deliberately loose: they test that the profile is on the right radii, not the data.
# (Colwell et al. 2009, "The Structure of Saturn's Rings".)
ZONES = (
    ("C ring",           74658, 91975,  0.02, 0.40),
    ("B ring",           91975, 117507, 0.40, 8.00),
    ("Cassini Division", 117507, 122340, 0.02, 0.40),
    ("A ring",           122340, 136780, 0.30, 1.20),
)


def read_profiles(source_dir):
    """The five source files as float arrays, checked for equal length."""
    def read(name):
        text = (source_dir / f"{name}.txt").read_text()
        return np.array([float(value) for value in text.split()], dtype=np.float64)

    profiles = {name: read(name) for name in ("transparency", *LAYERS)}
    profiles["color"] = read(COLOR_FILE).reshape(-1, 3)
    width = profiles["transparency"].size
    for name, values in profiles.items():
        if len(values) != width:
            sys.exit(f"{name}.txt has {len(values)} rows, transparency.txt has {width}")
    return profiles, width


def build_rgba(profiles):
    """The (3, width, 4) array the file holds: one row per layer, rgb premultiplied
    linear radiance, alpha the occluded fraction at normal incidence."""
    color = profiles["color"]
    opacity = 1.0 - profiles["transparency"]
    rows = [
        color * profiles["backscattered"][:, None],
        color * profiles["forwardscattered"][:, None],
        UNLIT_COLOR * profiles["unlitside"][:, None],
    ]
    rgba = np.empty((3, len(opacity), 4), dtype=np.float32)
    for index, rgb in enumerate(rows):
        rgba[index, :, :3] = rgb
        rgba[index, :, 3] = opacity
    return rgba


def optical_depth(transparency):
    """Normal optical depth. Transparency 0 means the occultation saw nothing through
    this radius, which is a floor on tau and not a measurement of it, so it maps to
    inf and every statistic below is a median rather than a mean."""
    with np.errstate(divide="ignore"):
        return -np.log(transparency)


def report(profiles, width, rgba):
    radius = INNER_RADIUS_KM + np.arange(width) * (
        (OUTER_RADIUS_KM - INNER_RADIUS_KM) / (width - 1))
    print(f"  {width} samples, {radius[0]:.0f} to {radius[-1]:.0f} km, "
          f"{radius[1] - radius[0]:.3f} km apart")
    for name in LAYERS:
        values = profiles[name]
        print(f"  {name:<16} {values.min():.4f} to {values.max():.4f}, "
              f"mean {values.mean():.4f}, {int((values == 0).sum())} empty")
    tau = optical_depth(profiles["transparency"])
    print(f"  {'optical depth':<16} median {np.median(tau):.3f}, "
          f"{int(np.isinf(tau).sum())} radii fully opaque")
    for name, inner, outer, low, high in ZONES:
        zone = tau[(radius >= inner) & (radius <= outer)]
        median = float(np.median(zone))
        verdict = "ok" if low <= median <= high else f"OUTSIDE {low}-{high}"
        print(f"    {name:<17} median tau {median:.3f}   {verdict}")
    for index, name in enumerate(LAYERS):
        rgb = rgba[index, :, :3]
        print(f"  layer {index} {name:<16} rgb max {rgb.max():.4f}, "
              f"mean {rgb.mean():.4f}")


def verify_linearity(profiles):
    """Refit the single-scattering slab model both ways, as the docstring claims."""
    try:
        from scipy.optimize import curve_fit
    except ImportError:
        print("  (scipy not installed; skipping the linearity fit)")
        return
    transparency = profiles["transparency"]
    radius = INNER_RADIUS_KM + np.arange(transparency.size) * 5.0
    # Where one material broadly dominates, and neither end of the tau range is a floor.
    keep = ((transparency > 0.001) & (transparency < 0.999)
            & (radius >= 92000) & (radius <= 136775))
    kept = transparency[keep]

    def srgb_to_linear(value):
        value = np.clip(value, 0.0, 1.0)
        return np.where(value <= 0.04045, value / 12.92, ((value + 0.055) / 1.055) ** 2.4)

    def fit(model, observed, guess):
        parameters, _ = curve_fit(model, kept, observed, p0=guess, maxfev=40000)
        residual = observed - model(kept, *parameters)
        return 1.0 - residual.var() / observed.var()

    lit = lambda t, k, exponent: k * (1.0 - t ** exponent)
    unlit = lambda t, k, a, b: k * (t ** a - t ** b)
    for name, model, guess in (("backscattered", lit, [1.0, 2.0]),
                               ("unlitside", unlit, [1.0, 1.0, 3.0])):
        values = profiles[name][keep]
        as_published = fit(model, values, guess)
        as_encoded = fit(model, srgb_to_linear(values), guess)
        verdict = "linear" if as_published > as_encoded else "SRGB-ENCODED?"
        print(f"  {name:<16} slab-model R2: as published {as_published:.4f}, "
              f"sRGB-decoded {as_encoded:.4f}   -> {verdict}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR,
                        help=f"directory holding the five .txt profiles (default {SOURCE_DIR})")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="output directory (default <project>/addons/ivoyager_assets/rings)")
    parser.add_argument("--verify", action="store_true",
                        help="also refit the slab model and re-read the written file")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    arguments = parser.parse_args()

    if not arguments.source_dir.is_dir():
        sys.exit(f"No ring source data at '{arguments.source_dir}'.\n"
                 f"Download the five .txt profiles from https://bjj.mmedia.is/data/s_rings/ "
                 f"into that directory.")
    profiles, width = read_profiles(arguments.source_dir)
    rgba = build_rgba(profiles)
    print(f"Saturn rings, from {arguments.source_dir}:")
    report(profiles, width, rgba)
    if arguments.verify:
        verify_linearity(profiles)

    out_dir = arguments.out_dir if arguments.out_dir else project_dir() / OUT_DIR
    out_path = out_dir / f"{NAME}.{width}.exr"
    if arguments.dry_run:
        print(f"  (dry run; would write {out_path})")
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    exr_writer.write_rgba_half(out_path, rgba)
    exr_writer.verify_roundtrip(out_path, rgba)
    import_path = Path(str(out_path) + ".import")
    # Godot finishes an .import with a uid, a remap path and a [deps] block on reimport, and
    # all three asset trees keep that finished form -- so an existing one whose parameters
    # already match is left alone, and only a fresh asset gets the params-only stub the
    # editor completes.
    wanted = [line for line in IMPORT_TEMPLATE.splitlines() if "=" in line]
    existing = import_path.read_text(encoding="utf8") if import_path.is_file() else ""
    if all(line in existing for line in wanted):
        print(f"  wrote {out_path} ({out_path.stat().st_size} bytes); .import already current")
    else:
        import_path.write_text(IMPORT_TEMPLATE, encoding="utf8", newline="\n")
        print(f"  wrote {out_path} ({out_path.stat().st_size} bytes) and a params-only "
              f".import; reimport the project to complete it")
    stale = sorted(path for path in out_dir.glob(f"{NAME}.*") if path != out_path
                   and path != import_path)
    if stale:
        print(f"  NOTE: {len(stale)} other {NAME}.* file(s) remain in {out_dir}; the "
              f"preloader matches by prefix and may load either. Delete the retired set.")


if __name__ == "__main__":
    main()
