# build_saturn_rings.py
# This file is part of I, Voyager (https://ivoyager.dev)
# *****************************************************************************
# Copyright 2019-2026 Charlie Whitfield
# Licensed under the Apache License, Version 2.0 (the "License").
# *****************************************************************************
"""Build Saturn's ring texture from Bjoern Joensson's radial ring profiles.

    source_data/rings/*.txt  ->  addons/ivoyager_assets/rings/saturn.rings.<w>.exr

ONE file, imported as a CompressedTexture2DArray of three <w> x 1 layers:

    layer 0  backscatter    the lit side at phase angle 0 deg
    layer 1  forwardscatter the lit side at phase angle 139 deg
    layer 2  unlitside      the side away from the sun, lit through the rings

    rgb  LINEAR SCATTERING STRENGTH -- the ring's own light DIVIDED BY the
         single-scattering slab's geometry term at the geometry the profile was
         observed at, so the shader can re-apply that term at the angles it is
         actually rendering. See RE-REFERENCING below.
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
to use this data alone, you need the transparency profile as well". So the RENDERED
value composites with `blend_premul_alpha` (radiance + T * background) and is never
multiplied by alpha again -- that darkens every radius by its own opacity, which costs
the faint C ring and the Cassini Division almost everything they have. The stored rgb
is a step back from that radiance (see RE-REFERENCING), and the slab geometry term the
shader multiplies it by is what carries the coverage back in.

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

RE-REFERENCING: THE PROFILES ARE OBSERVATIONS AT ONE GEOMETRY, AND A RENDERER NEEDS
THEM AT ANOTHER
-------------------------------------------------------------------------------
A published profile is brightness at whatever ring opening angle its images were
taken at, so shipping it as-is freezes that geometry -- which is exactly what the
retired shader did, and why its rings held one brightness from a 26 deg opening down
to 0.5 deg while the path through the layer grew 50x. What has to be stored instead
is the part that does NOT depend on geometry.

The single-scattering slab separates them. Emergent brightness is

    lit    I = S * mu0/(mu+mu0) * (1 - exp(-tau (1/mu + 1/mu0)))
    unlit  I = S * mu0/(mu0-mu) * (exp(-tau/mu0) - exp(-tau/mu))

with mu, mu0 the sines of the camera's and the sun's elevation above the ring plane
and tau the normal optical depth alpha already carries. Everything after S is
geometry; S is the ring's own scattering strength (single-scattering albedo times
phase function), a property of the particles. So the build divides the published
profile by its own geometry term and stores S; the shader multiplies the term back
at the angles it is rendering. THE DIVISION IS THE WHOLE POINT OF THE ASSET.

The observing geometry is not published, so it is FITTED FROM THE DATA -- the slab
model's tau dependence is a one-parameter family for the lit case (`k = 1/mu + 1/mu0`,
which is all the radial shape can determine) and two for the unlit one, and the
transparency profile supplies tau at every radius. That the fit works at all is the
check on the whole construction: one exponent explains most of each profile's radial
contrast, and the quotient that is left comes out nearly FLAT across the C ring, the
B ring, the Cassini Division and the A ring (medians 0.66 to 1.01), which is what
identical ice particles at wildly different optical depths should look like. Fitted:

    backscatter      k = 3.62   (mu = mu0 = 0.552, a 33.5 deg opening)   R2 0.873
    forwardscatter   k = 5.99   (mu = mu0 = 0.334, a 19.5 deg opening)   R2 0.676
    unlitside        1/mu0 = 1.92, 1/mu = 17.7  (31.3 deg and 3.2 deg)   R2 0.795

Two things about that. The lit fits determine only the SUM `1/mu + 1/mu0`, so mu =
mu0 is assumed to split it -- which is exactly right for the backscatter profile (the
source states it is phase 0, where the sun and the camera ARE in the same direction)
and is a pure level convention for the other, absorbed by rings.tsv's
`forward_level`. And the fitted geometry is an EFFECTIVE one: it is what reproduces
the published radial contrast, which need not be the true encounter geometry, since
multiple scattering and the B ring's self-gravity wakes both hold contrast that
single scattering would have flattened. Rendering AT the fitted geometry reproduces
the published profile exactly; away from it the model moves by textbook single
scattering, and the direction of that -- optically thin rings brightening toward
grazing while the B ring saturates -- is the inversion every low-opening image shows.

THE UNLIT SIDE HAS A FLOOR SINGLE SCATTERING CANNOT REACH, AND WITHOUT IT THE
DIVISION EXPLODES. Binned against tau, the unlit profile falls to 0.046 by tau 1.8
and then stays there, flat to tau 3.6, where transmission alone would have fallen
another 40x. So the densest B ring's unlit face is ~800x brighter than the model
allows and the quotient reaches 1819. Whether that residual is multiple scattering,
light leaking between self-gravity wakes, or Joensson's own image floor, the data
cannot say -- but it is real, it is nearly constant, and a model without it is
useless there. Adding it as a constant `unlit_floor` bounds the quotient at 8.5 and,
because the shader carries the SAME constant, makes the dense B ring's unlit face
render at its published value instead of at a ratio of two near-zeros. THIS IS THE
ONE FITTED VALUE THE SHADER ALSO NEEDS: it goes in rings.tsv, and the run prints the
cell to paste.

AND THE DENOMINATOR IS SMOOTHED TO THE NUMERATOR'S OWN RESOLUTION. Only
`transparency` is really 5 km data; the imaging profiles change every 5 to 9 samples,
having been resampled up to match it. Dividing a coarse numerator by a sharp
denominator puts a spike wherever a gap edge falls between the two grids -- measured,
the quotient's maximum was 52 against a median of 0.81. Smoothing the geometry term
by each profile's OWN median run length (5, 9 and 7 samples, measured here rather
than assumed) takes those maxima to 6.0, 4.4 and 4.4 and moves the median by 0.004.

AND DO NOT CHASE A FLAT QUOTIENT PAST THAT, BECAUSE THE REMAINING TILT IS THE SIGNAL.
Binned against tau, the stored strength is flat for the unlit layer (0.59 to 0.84 over
four decades of tau) and flat for both lit layers above tau 0.2 -- but below it the
backscatter rises to 2.2 against 1.6, and the FORWARD layer rises to 4.0 against 1.3.
Smoothing wider flattens the backscatter (1.74 at a 355 km box) and barely touches the
forward one (2.88), which is what says they are different things: the first is residual
grid mismatch and the second is physics. Forward scattering is small particles, and the
optically thin regions -- the C ring, the Cassini Division, the F ring region -- are
exactly where the dust fraction is highest, which is why they blaze in a high-phase
image and are nearly invisible in a low-phase one. So the box stays at each profile's
own resolution and the tilt is stored.

What the shader still gets straight from alpha: `tau = -ln(1 - a)`, and a slant path
is `(1 - a)^(1/mu)` -- which is what `_sun_occlusion.gdshaderinc` already does for the
sun leg. The per-radius tint survives as the chromaticity of the two lit layers (the
scalar profiles cannot tint themselves), and nothing about appearance is baked in: the
phase function, its opposition surge, the forward reddening and every level are
rings.tsv cells.

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


def optical_depth(transparency):
    """Normal optical depth. Transparency 0 means the occultation saw nothing through
    this radius, which is a floor on tau and not a measurement of it, so it maps to
    inf and every statistic below is a median rather than a mean."""
    with np.errstate(divide="ignore"):
        return -np.log(transparency)


def sample_run_length(values):
    """The profile's own radial resolution: the median run of identical consecutive
    values. The imaging profiles were resampled up to the occultation's 5 km grid, so
    this is how many of those samples one real measurement spans."""
    change = np.flatnonzero(np.diff(values) != 0.0)
    if change.size < 3:
        return 1
    return int(np.median(np.diff(change))) | 1  # odd, for a centred box


def fit_reference_geometry(profiles):
    """The geometry each published profile was observed at, fitted from its own tau
    dependence. Returns a dict of layer name -> (mu, mu0, floor); see RE-REFERENCING.

    Only radii with material AND a measured transmission vote: transparency exactly 1
    is empty space (both sides of the ratio are 0) and exactly 0 is the occultation
    seeing nothing through, a lower bound on tau rather than a measurement of it.
    """
    from scipy.optimize import curve_fit

    transparency = profiles["transparency"]
    tau = optical_depth(transparency)
    measured = (transparency > 0.0) & (transparency < 1.0)
    x, geometry = tau[measured], {}

    def report(name, r_squared, mu, mu0, floor):
        print(f"    {name:<16} mu {mu:.4f} ({np.degrees(np.arcsin(mu)):5.2f} deg), "
              f"mu0 {mu0:.4f} ({np.degrees(np.arcsin(mu0)):5.2f} deg), "
              f"floor {floor:.4f}   R2 {r_squared:.3f}")

    for name in LAYERS[:2]:
        observed = profiles[name][measured]
        # k = 1/mu + 1/mu0 is all the radial shape determines; mu = mu0 splits it.
        (level, k), _ = curve_fit(lambda t, level, k: level * (1.0 - t ** k),
                                  transparency[measured], observed, p0=[0.85, 4.0],
                                  maxfev=200000)
        r_squared = 1.0 - (observed - level * (1.0 - transparency[measured] ** k)).var() \
                / observed.var()
        mu = 2.0 / k
        if mu >= 1.0:
            sys.exit(f"{name}: fitted k = {k:.3f} needs an elevation above 90 deg")
        geometry[name] = (mu, mu, 0.0)
        report(name, r_squared, mu, mu, 0.0)

    name = LAYERS[2]
    observed = profiles[name][measured]
    unlit = lambda t, level, a, b, floor: level * (t ** a - t ** b) + floor
    (level, a, b, floor), _ = curve_fit(unlit, transparency[measured], observed,
                                        p0=[1.0, 1.5, 20.0, 0.04], maxfev=200000)
    r_squared = 1.0 - (observed - unlit(transparency[measured], level, a, b, floor)).var() \
            / observed.var()
    mu0, mu = 1.0 / min(a, b), 1.0 / max(a, b)  # the larger elevation is the sun's leg
    # The fit's floor is in units of the profile; the shader's is in units of the geometry
    # term, which carries the mu0/(mu0-mu) prefactor the fit folded into `level`. It is also
    # stated PER UNIT mu0, because a floor that does not vanish with the sun's elevation
    # survives an equinox that takes every other term to zero -- and then the two faces of
    # one ring disagree under geometry that is symmetric between them (rendered: a black
    # silhouette on the lit side against a grey band on the unlit one). Diffusely
    # transmitted radiance goes as the flux that entered, which is mu0, exactly as the
    # single-scattering terms do at small mu0.
    floor_in_geometry = floor / level * (mu0 / (mu0 - mu)) / mu0
    geometry[name] = (mu, mu0, floor_in_geometry)
    report(name, r_squared, mu, mu0, floor_in_geometry)
    print(f"    -> rings.tsv `unlit_floor` for this ring system: {floor_in_geometry:.4f}")
    return geometry


def slab_geometry(tau, mu, mu0, floor):
    """The single-scattering slab's geometry term: what a unit scattering strength
    emits at these angles. Lit when mu and mu0 are on the same side, which for the
    reference geometries here is decided by whether a floor was fitted."""
    if floor <= 0.0:
        return mu0 / (mu + mu0) * (1.0 - np.exp(-tau * (1.0 / mu + 1.0 / mu0)))
    return mu0 / (mu0 - mu) * (np.exp(-tau / mu0) - np.exp(-tau / mu)) + floor * mu0


def build_rgba(profiles, geometry):
    """The (3, width, 4) array the file holds: one row per layer, rgb the scattering
    strength left when the observing geometry is divided out, alpha the occluded
    fraction at normal incidence."""
    from scipy.ndimage import uniform_filter1d

    transparency = profiles["transparency"]
    tau = optical_depth(transparency)
    empty = transparency >= 1.0  # no material: the quotient is 0/0, and the answer is 0
    tints = (profiles["color"], profiles["color"], UNLIT_COLOR)
    rgba = np.empty((3, transparency.size, 4), dtype=np.float32)
    for index, name in enumerate(LAYERS):
        observed = profiles[name]
        width = sample_run_length(observed)
        term = uniform_filter1d(slab_geometry(tau, *geometry[name]), width, mode="nearest")
        strength = np.where(empty | (term <= 0.0), 0.0, observed / np.maximum(term, 1e-30))
        stray = int((observed[empty] != 0.0).sum())
        print(f"    {name:<16} smoothed over {width} samples ({width * 5} km); "
              f"strength median {np.median(strength[~empty]):.3f}, "
              f"p99.9 {np.percentile(strength[~empty], 99.9):.3f}, max {strength.max():.2f}"
              + (f", {stray} stray nonzero sample(s) in empty space zeroed" if stray else ""))
        rgba[index, :, :3] = tints[index] * strength[:, None]
        rgba[index, :, 3] = 1.0 - transparency
    return rgba


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
        print(f"  layer {index} {name:<16} strength max {rgb.max():.4f}, "
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
    print(f"Saturn rings, from {arguments.source_dir}:")
    print("  observing geometry fitted from each profile's own tau dependence:")
    geometry = fit_reference_geometry(profiles)
    print("  scattering strength, with that geometry divided out:")
    rgba = build_rgba(profiles, geometry)
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
