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

BOTH LIT REFERENCES ARE PINNED AT THE GEOMETRY THE IMAGES WERE TAKEN AT, AND A FIT
CANNOT SUPPLY IT. The profiles are Voyager, 1980-81 -- within 1.5 years of Saturn's
1980 equinox, when the sun stood a FEW DEGREES above the ring plane. From that
equinox and Saturn's 7.37-year quarter period: Voyager 1 in November 1980 at 4.0 deg,
Voyager 2 in August 1981 at 7.8 deg. The unlit profile independently fits its own
shallower leg at 2.7 deg, which is the check that this is the right ballpark.

A lit fit cannot measure geometry, and the two Voyager 1 profiles prove it between
them. `forwardscattered` and `unlitside` are the SAME spacecraft at the SAME
encounter, so they see one sun elevation; a lit fit determines only the SUM
`k = 1/mu + 1/mu0`, and k can never be less than 1/mu0. Fitted, forwardscatter
returns k = 5.62 where the unlit profile's own sun elevation demands at least 14.4,
and backscatter returns 3.61 against at least 7.3. Both are impossible. What the fit
absorbs instead is the RADIAL VARIATION OF PARTICLE ALBEDO, which this model has no
term for: the C ring is darker than a constant-strength slab predicts, and the fit
buys that back by moving to an elevation with less saturation.

That matters enormously, because the reference decides how much radial contrast every
render carries. Pinned at Saturn's 26.7 deg maximum -- which is what shipped until
2026-09-06, chosen because the free fit wanted an impossible 33.5 and 26.7 was the
nearest possible angle -- the build divides out far too little saturation, and the B
ring against the C ring renders 3.81 at Saturn's widest and 2.23 at 12 deg. Pinned at
the encounter geometry it renders 9.67 and 5.51, against published Cassini radial
scans of 6 to 12 at low phase.

The second, independent check is the particle strength each reference implies. The C
ring and the Cassini Division are the known dark, contaminated regions -- Cassini puts
them at roughly 0.2 to 0.5 of the A and B rings' single-scattering albedo. At a 26.7
deg reference the C ring comes out at 0.83 of the A ring and the Cassini Division at
1.33, i.e. BRIGHTER, which no measurement supports; at 6 deg they come out at 0.39 and
0.73.

    backscatter      pinned at a 6.0 deg opening (Voyager 2, Aug 1981)
    forwardscatter   pinned at a 3.1 deg opening (Voyager 1, Nov 1980)
    unlitside        the sun's leg fitted at 2.7 deg (Voyager 1, same encounter)

A lit reference needs both legs and the data gives neither, so `mu = mu0` splits it:
exactly right for the backscatter profile, which the source states is phase 0 (the sun
and the camera ARE in the same direction there), and a weak assumption for the forward
one, where 1/mu0 dominates the sum -- moving the camera leg over its whole admissible
range moves that reference's k by less than half, against the factor of six between
the old reference and the new.

CLUMPING IS PINNED, NOT FITTED, BECAUSE NOTHING HERE MEASURES IT. Optical depth
varies across the beam -- self-gravity wakes -- and a clumpy layer saturates more
slowly than a homogeneous one. Taking tau as gamma-distributed about its mean with
shape n gives `<exp(-k tau)> = (1 + k tau / n)^-n`, whose n -> infinity limit is the
homogeneous slab exactly, and `--clumping` takes any member of that family.

The trap is that it always LOOKS fitted. Clumping controls transmission, so the lit
profile cannot constrain it -- that fit's R2 moves 0.8733 to 0.8787 across the entire
family -- and the unlit profile only appears to, because the fit's leverage on it came
from the flat deep end, which is the pedestal above. Scanned properly, on the live
radii after the pedestal is subtracted, the unlit fit's R2 moves 0.5680 to 0.5849
across the whole family -- 0.017, against a factor of FORTY in what the parameter
actually does (transmission through tau 2.5 at mu0 0.45 runs 0.153 to 0.0039). So it
is unconstrained by this data, and a fit returns a number that looks like an answer:
2.54 on the live radii, 22.77 with the pedestal left in as a free floor, 1.7 off the
lit profile. That last one shipped briefly and made the dense B ring pass 15 % of the
sun's light. Pinned at the homogeneous limit, the model has no free parameter here and
takes the DARKEST transmission of the family, which is also the closest to what real
unlit images show. The run prints the scan so the absence of a constraint is visible
rather than assumed.

THE UNLIT PROFILE'S DEEP END IS THE SOURCE IMAGE'S BACKGROUND, AND IT MUST BE
SUBTRACTED RATHER THAN MODELLED. Binned against its own optical depth the unlit
profile falls to 0.046 by tau 1.4 and then STAYS there -- median 0.049 / 0.046 /
0.046 / 0.049 and p10 0.039 / 0.039 / 0.041 / 0.041 across tau 1.4-2, 2-3, 3-5 and
5-13.8. Over that decade of tau single scattering falls by a factor of 1e6, and even
conservative two-stream diffuse transmission -- the most generous physical model
there is -- falls by 3.1. A quantity flat to 4 % across it is not transmitted light
by any mechanism: it is a pedestal. The publisher says as much about what the data
means -- "completely black areas are either completely transparent or contain so much
material that no sunlight passes through them" -- while in the file the opaque B ring
never goes below 0.0366, and every one of the 1031 exactly-zero samples is EMPTY
space.

Fitting it as a constant `unlit_floor` and carrying the same constant in the shader
(which is what shipped until 2026-09-06) puts a floor under the unlit face that does
not fall with tau AT ALL, so an opaque ring glows. So: subtract it, and where
subtracting it leaves nothing -- the deep B ring, where the source measured only its
own background -- take the strength from the LIT layer instead. That is sound because
S is a particle property, and measured it is: over the 8400 radii where the unlit
signal exceeds twice the pedestal, a 50x range in tau, the unlit/lit strength ratio
is FLAT at 0.56 (p16-p84 0.44-0.65), and it collapses only where the pedestal has
eaten the signal. One measured constant replaces a fitted floor, and the unlit face's
whole tau response becomes the model's.

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
# The gamma shape at which the clumped transmission IS the homogeneous slab; the
# shader takes that branch explicitly above the same threshold.
HOMOGENEOUS = 1.0e6
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


def _fit_unlit(model, tau, observed, shape):
    """The unlit two-exponential fit at a FIXED clumping, which is the only way it is
    used: see the header on why the shape is pinned rather than fitted."""
    from scipy.optimize import curve_fit

    (level, a, b), _ = curve_fit(lambda t, lv, aa, bb: model(t, lv, aa, bb, shape),
                                 tau, observed, p0=[1.0, 1.5, 20.0],
                                 bounds=([0.05, 0.2, 0.2], [20.0, 400.0, 400.0]),
                                 maxfev=400000)
    return level, a, b


def measure_pedestal(values, tau, deep=1.4):
    """The profile's own background: the level it stops falling at, with the evidence.

    A transmitted-light profile must keep falling as tau rises; one that goes flat has
    reached the source image's background, and every sample past that is background
    rather than ring light. What is returned is the MINIMUM over the flat part, so
    nothing real is subtracted anywhere.
    """
    octaves, edges = [], [deep, deep * 2.0, deep * 4.0, np.inf]
    for lo, hi in zip(edges[:-1], edges[1:]):
        band = (tau >= lo) & (tau < hi)
        if band.sum() > 20:
            octaves.append((lo, hi, int(band.sum()), float(np.median(values[band]))))
    flat = tau >= deep
    pedestal = float(values[flat].min())
    levels = [octave[3] for octave in octaves]
    span = max(levels) / max(min(levels), 1e-9)
    lines = [f"pedestal {pedestal:.4f}, the minimum over {int(flat.sum())} radii at "
             f"tau >= {deep} where the profile has stopped falling:"]
    for lo, hi, count, median in octaves:
        lines.append(f"      tau {lo:>5.1f} to {hi:>5.1f}  n={count:>5}  "
                     f"median {median:.4f}")
    lines.append(f"      flat to {span:.2f}x across that range, where single scattering "
                 f"would fall ~1e6 and diffuse transmission ~3")
    return pedestal, "\n    ".join(lines)


def sample_run_length(values):
    """The profile's own radial resolution: the median run of identical consecutive
    values. The imaging profiles were resampled up to the occultation's 5 km grid, so
    this is how many of those samples one real measurement spans."""
    change = np.flatnonzero(np.diff(values) != 0.0)
    if change.size < 3:
        return 1
    return int(np.median(np.diff(change))) | 1  # odd, for a centred box


def fit_reference_geometry(profiles, opening_deg, forward_opening_deg, clumping):
    """The geometry each published profile describes, and the clumping that lets a real one
    explain it. Returns (layer -> (mu, mu0), clumping, layer -> pedestal); see
    RE-REFERENCING.

    Only radii with material AND a measured transmission vote: transparency exactly 1
    is empty space (both sides of the ratio are 0) and exactly 0 is the occultation
    seeing nothing through, a lower bound on tau rather than a measurement of it.
    """
    from scipy.optimize import curve_fit

    transparency = profiles["transparency"]
    tau = optical_depth(transparency)
    measured = (transparency > 0.0) & (transparency < 1.0)
    x, geometry = tau[measured], {}

    def report(name, r_squared, mu, mu0):
        print(f"    {name:<16} mu {mu:.4f} ({np.degrees(np.arcsin(mu)):5.2f} deg), "
              f"mu0 {mu0:.4f} ({np.degrees(np.arcsin(mu0)):5.2f} deg)   "
              f"R2 {r_squared:.3f}")

    # CLUMPING IS PINNED -- see the header. The scan below is what says it has to be:
    # it reports the whole family's residual so an absent constraint is visible.
    name = LAYERS[2]
    observed = profiles[name][measured]

    # The pedestal is SUBTRACTED, not fitted -- see the header. What is left is fitted
    # only where it survives that subtraction by a clear margin; past there the source
    # measured its own background, and residual noise about zero would drag the geometry
    # and the clumping toward whatever shape happens to fit it.
    pedestal, pedestal_report = measure_pedestal(observed, x)
    print(f"    {pedestal_report}")
    net = observed - pedestal
    live = net > 2.0 * pedestal
    print(f"    fitting the unlit geometry on the {int(live.sum())} of {live.size} radii "
          f"where the net signal exceeds twice that (tau to {x[live].max():.2f})")

    def unlit_model(t, level, a, b, shape):
        return level * (beam_transmission(t, a, shape)
                        - beam_transmission(t, b, shape))

    scan = []
    for shape in (1.0, 4.0, 16.0, 64.0, HOMOGENEOUS):
        try:
            level, a, b = _fit_unlit(unlit_model, x[live], net[live], shape)
        except RuntimeError:
            continue
        residual = net[live] - unlit_model(x[live], level, a, b, shape)
        scan.append((shape, 1.0 - residual.var() / net[live].var()))
    span = max(r for _, r in scan) - min(r for _, r in scan)
    print(f"    clumping is PINNED at {clumping:g}; scanned, this profile does not "
          f"constrain it -- R2 moves {span:.4f} across the family "
          + ", ".join(f"({s:g}: {r:.4f})" for s, r in scan))
    level, a, b = _fit_unlit(unlit_model, x[live], net[live], clumping)
    unlit_r_squared = 1.0 - (net[live] - unlit_model(x[live], level, a, b, clumping)).var() \
            / net[live].var()

    # BOTH lit references are PINNED at the encounter geometry -- see the header. A lit fit
    # returns only k = 1/mu + 1/mu0, and for both of these profiles the value it returns is
    # BELOW the 1/mu0 their own encounter demands, so it is not a geometry at all; what it
    # absorbs is the radial variation of particle albedo. The R2 reported here is therefore
    # the residual at the pinned geometry, not a goodness of fit that chose it.
    for name, opening in zip(LAYERS[:2], (opening_deg, forward_opening_deg)):
        observed = profiles[name][measured]
        mu = np.sin(np.radians(opening))
        k = 2.0 / mu
        (level,), _ = curve_fit(
            lambda t, level, k=k: level * (1.0 - beam_transmission(t, k, clumping)),
            x, observed, p0=[0.85], maxfev=200000)
        r_squared = 1.0 - (observed - level * (1.0 - beam_transmission(x, k, clumping))
                           ).var() / observed.var()
        geometry[name] = (mu, mu, True)
        report(name, r_squared, mu, mu)

    # WHICH LEG IS THE SUN'S is not something the fit can tell -- swapping mu and mu0
    # multiplies the term by mu/mu0 and leaves its SHAPE identical, so the radial data
    # cannot distinguish them and the level absorbs the difference. Physics can:
    # Voyager 1 met Saturn in November 1980, eight months after the ring-plane
    # crossing, with the sun about 3 deg above the plane -- so the SMALLER elevation is
    # the sun's, and the fit recovering 2.7 deg for it with nothing told to it is a
    # real check on the whole re-referencing. Assigned the other way round (which is
    # what shipped until 2026-09-06) the sun comes out at 39 deg, which Saturn's
    # 26.7 deg maximum opening makes impossible.
    mu0, mu = 1.0 / max(a, b), 1.0 / min(a, b)
    geometry[LAYERS[2]] = (mu, mu0, False)
    report(LAYERS[2], unlit_r_squared, mu, mu0)
    return geometry, {LAYERS[2]: pedestal}


def beam_transmission(tau, rate, clumping):
    """<exp(-rate * tau)> with tau gamma-distributed about its mean with shape `clumping`.

    A real ring is not a uniform sheet -- self-gravity wakes make optical depth vary across
    the beam, and a clumpy layer saturates far more slowly than a homogeneous one, because
    the thin lanes keep contributing after the dense parts have gone opaque. `clumping`
    -> infinity is the homogeneous slab, exactly.
    """
    if clumping > 1e6:
        return np.exp(-rate * tau)
    return (1.0 + rate * tau / clumping) ** (-clumping)


def slab_geometry(tau, mu, mu0, lit, clumping=np.inf):
    """The single-scattering slab's geometry term: what a unit scattering strength
    emits at these angles, with the camera and the sun on the same side of the plane
    ([param lit]) or on opposite sides."""
    if lit:
        return mu0 / (mu + mu0) * (1.0 - beam_transmission(tau, 1.0 / mu + 1.0 / mu0,
                                                           clumping))
    return mu0 / (mu0 - mu) * (beam_transmission(tau, 1.0 / mu0, clumping)
                               - beam_transmission(tau, 1.0 / mu, clumping))


def build_rgba(profiles, geometry, clumping, pedestals):
    """The (3, width, 4) array the file holds: one row per layer, rgb the scattering
    strength left when the observing geometry is divided out, alpha the occluded
    fraction at normal incidence.

    A layer with a pedestal has it subtracted first, and wherever that leaves nothing
    the strength is taken from layer 0 instead -- see the header. Dividing the residue
    of a subtraction by a near-zero geometry term is the one operation this build must
    never do, and it is also the operation that hides a dead measurement behind a
    plausible-looking number.
    """
    from scipy.ndimage import uniform_filter1d

    transparency = profiles["transparency"]
    tau = optical_depth(transparency)
    empty = transparency >= 1.0  # no material: the quotient is 0/0, and the answer is 0
    tints = (profiles["color"], profiles["color"], UNLIT_COLOR)
    rgba = np.empty((3, transparency.size, 4), dtype=np.float32)
    lit_strength = None
    for index, name in enumerate(LAYERS):
        pedestal = pedestals.get(name, 0.0)
        observed = profiles[name] - pedestal
        width = sample_run_length(profiles[name])
        term = uniform_filter1d(slab_geometry(tau, *geometry[name], clumping=clumping),
                                width, mode="nearest")
        usable = ~empty & (term > 0.0) & (observed > 2.0 * pedestal)
        strength = np.where(usable, observed / np.maximum(term, 1e-30), 0.0)
        note = ""
        if pedestal > 0.0 and lit_strength is not None:
            # The ratio to layer 0 does two jobs and is NOT one number, so it is measured
            # twice, each time over the material its job is about. It is a ratio of two
            # PHASE FUNCTIONS -- an unlit view is a high-phase view -- and the dustier C
            # ring and Cassini Division forward-scatter more than the B ring, so it really
            # does vary with radius: 17.7 at tau 0.02-0.1 falling to 6.2 by tau 0.7-1.2.
            # The source's own two lit profiles say the same (forward/back C/B is 0.43
            # against 0.26). It reads FLAT only while both references are fitted, because
            # then both fits absorb the same radial albedo variation and it cancels in the
            # quotient -- which is why it looked like a constant until the lit reference
            # was pinned at the encounter geometry.
            per_radius = strength / np.maximum(lit_strength, 1e-30)
            ratio = float(np.median(per_radius[usable]))
            spread = np.percentile(per_radius[usable], [16, 84])
            # The FALLBACK extrapolates into the deep B ring, so its own ratio is measured
            # on the densest material that still has signal rather than over the whole
            # profile, where the dusty rings would drag it up by half again.
            deep = usable & (tau >= np.percentile(tau[usable], 90.0))
            deep_ratio = float(np.median(per_radius[deep]))
            fallback = ~empty & ~usable
            strength = np.where(fallback, deep_ratio * lit_strength, strength)
            note = (f"\n    {'':16} pedestal {pedestal:.4f} subtracted; strength measured "
                    f"on {int(usable.sum())} radii, ratio to layer 0 {ratio:.3f} "
                    f"(p16-p84 {spread[0]:.3f}-{spread[1]:.3f} -- a PHASE ratio, so it "
                    f"varies with radius; see the code)"
                    f"\n    {'':16} the {int(fallback.sum())} radii the pedestal left dead "
                    f"take {deep_ratio:.3f} x layer 0, measured on the densest tenth of the "
                    f"live material, which is what they adjoin"
                    f"\n    {'':16} -> rings.tsv `unlit_level` for this ring system: "
                    f"{1.0 / ratio:.4f}. It is 1/ratio, which puts BOTH faces on ONE "
                    f"scattering strength: the profiles are peak-normalized independently "
                    f"and were observed at different phase angles, and this undoes both")
        elif index == 0:
            lit_strength = strength
        stray = int((profiles[name][empty] != 0.0).sum())
        print(f"    {name:<16} smoothed over {width} samples ({width * 5} km); "
              f"strength median {np.median(strength[~empty]):.3f}, "
              f"p99.9 {np.percentile(strength[~empty], 99.9):.3f}, max {strength.max():.2f}"
              + (f", {stray} stray nonzero sample(s) in empty space zeroed" if stray else "")
              + note)
        rgba[index, :, :3] = tints[index] * strength[:, None]
        rgba[index, :, 3] = 1.0 - transparency
    return rgba


LUMA = np.array([0.2126, 0.7152, 0.0722])


def apply_color_gain(rgba, gain, radius_weight):
    """White-balance the stored strength, holding its level exactly.

    Joensson's `color` profile is peak-normalized in every one of its rows, so it carries
    the ring's radial colour ORDERING and no white balance at all -- which is why the
    asset rendered bluer than the Sun until this existed. The correction is one
    per-channel gain: the file is a chromaticity per radius times a scalar brightness, so
    a gain moves the balance and leaves every radial ratio untouched.

    It is renormalized here so the area-weighted mean luma comes back exactly, because
    `scattering_scale` is anchored on published photometry through a luma-weighted
    integral and a colour change must not move a level. The caller therefore supplies a
    DIRECTION and gets a pure rotation of the colour, whatever it passes.
    """
    gain = np.asarray(gain, dtype=np.float64)
    before = float((rgba[..., :3] @ LUMA * radius_weight).sum())
    out = rgba.copy()
    out[..., :3] *= gain
    after = float((out[..., :3] @ LUMA * radius_weight).sum())
    out[..., :3] *= before / after
    return out, gain * before / after


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

    # THE SHARPER TEST, AND THE ONE THAT ACTUALLY SETTLES IT: the R2 comparison above is
    # nearly a wash on the lit profile (0.69 against 0.67), but the fitted GEOMETRY is not.
    # The lit exponent is `k = 1/mu + 1/mu0`, and mu = mu0 splits it, so a reading of the
    # data implies a viewing elevation -- and a reading that needs mu > 1 implies no
    # geometry at all. Read as published the profiles want 33 and 21 degrees; sRGB-decoded
    # they are too contrasty for a single-scattering slab at ANY geometry.
    print("  what elevation each reading implies (mu > 1 is not a geometry):")
    for name in ("backscattered", "forwardscattered"):
        line = f"  {name:<16}"
        for label, values in (("as published", profiles[name][keep]),
                              ("sRGB-decoded", srgb_to_linear(profiles[name][keep]))):
            (_, exponent), _ = curve_fit(lit, kept, values / values.max(),
                                         p0=[1.0, 2.0], maxfev=40000)
            mu = 2.0 / max(exponent, 1e-6)
            angle = (f"{np.degrees(np.arcsin(mu)):5.1f} deg" if mu <= 1.0
                     else "  IMPOSSIBLE")
            line += f"   {label} mu {mu:.3f} ({angle})"
        print(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR,
                        help=f"directory holding the five .txt profiles (default {SOURCE_DIR})")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="output directory (default <project>/addons/ivoyager_assets/rings)")
    parser.add_argument("--reference-opening", type=float, default=6.0,
                        help="ring opening angle, in degrees, the BACKSCATTER profile was "
                             "observed at (default 6.0: Voyager 2, August 1981, sixteen "
                             "months after Saturn's 1980 equinox -- see RE-REFERENCING)")
    parser.add_argument("--forward-reference-opening", type=float, default=3.1,
                        help="the same for the FORWARDSCATTER profile (default 3.1: "
                             "Voyager 1, November 1980, and the unlit profile fits its own "
                             "sun leg at 2.7 from the same encounter)")
    parser.add_argument("--clumping", type=float, default=HOMOGENEOUS,
                        help="gamma shape of the optical depth across the beam; "
                             "the default is the homogeneous limit, and nothing in "
                             "these profiles constrains it (see the header)")
    parser.add_argument("--color-gain", type=float, nargs=3, metavar=("R", "G", "B"),
                        default=None,
                        help="per-channel white balance for the stored strength. The "
                             "source `color` profile is peak-normalized per row, so it "
                             "carries the radial ordering and no white balance; this "
                             "supplies one. Renormalized to hold the area-weighted mean "
                             "luma, so it can never move the level.")
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
    print("  observing geometry, pinned and fitted:")
    clumping = arguments.clumping
    geometry, pedestals = fit_reference_geometry(
            profiles, arguments.reference_opening, arguments.forward_reference_opening,
            clumping)
    print("  scattering strength, with that geometry divided out:")
    rgba = build_rgba(profiles, geometry, clumping, pedestals)
    if arguments.color_gain:
        radius = INNER_RADIUS_KM + np.arange(width) * (
            (OUTER_RADIUS_KM - INNER_RADIUS_KM) / (width - 1))
        was = rgba[0, :, :3].sum(axis=0)
        rgba, applied = apply_color_gain(rgba, arguments.color_gain, radius)
        now = rgba[0, :, :3].sum(axis=0)
        print(f"  colour: gain {arguments.color_gain} applied as "
              f"{applied[0]:.4f} {applied[1]:.4f} {applied[2]:.4f} after holding luma; "
              f"layer 0 area-weighted R/B {was[0] / was[2]:.4f} -> {now[0] / now[2]:.4f}")
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
