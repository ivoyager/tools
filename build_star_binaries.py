#!/usr/bin/env python3
# build_star_binaries.py
# This file is part of I, Voyager
# https://ivoyager.dev
# *****************************************************************************
# Copyright 2019-2026 Charlie Whitfield
# I, Voyager is a registered trademark of Charlie Whitfield in the US
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# *****************************************************************************
"""Build I, Voyager star point-cloud binaries from the ESA Hipparcos and Tycho-2 catalogues.

Writes magnitude-binned `.ivbinary` files consumed by IVStarsVisual. Each star
contributes an ecliptic-frame direction, a distance, a Johnson V magnitude and a
B-V colour index.

Two catalogues, because neither alone is the product. Hipparcos (VizieR I/239)
holds 118k stars and is complete only to about V 7.5; Tycho-2 (VizieR I/259)
holds 2.54M and is 99 % complete to V 11.0. But Tycho-2 carries no parallax,
while Hipparcos carries a measured V, B-V and parallax for its own stars. So
Hipparcos wins wherever the two overlap (matched on Tycho-2's HIP column) and
Tycho-2 supplies everything else. `suppl_1.dat` adds the Tycho-1 stars that
Tycho-2 itself dropped; `suppl_2.dat` is the false/disturbed list and is not read.

Positions apply the J2000 obliquity rotation (see IVAstronomy
`get_ecliptic_unit_vector_from_equatorial_angles`) so stars land in the sim's
ecliptic world frame, sharing the planets' coordinates. Hipparcos positions are
J1991.25 and Tycho-2's are J2000, so the Hipparcos ones are carried forward by
their own proper motion -- 8.75 years is under an arcsecond for most stars and
90 arcseconds for Barnard's Star, which is two pixels at a long focal length.
Distance is 1/parallax when the parallax is positive and its signal-to-noise
clears a threshold, else a far shell (default 1 kpc). Distance barely matters for
a backdrop, but a true distance gives the correct (tiny) parallax for the nearest
stars.

Binary format v2 (little-endian; consumed by stars_visual.gd). Every field is
quantized to what the renderer can resolve, which is what makes 2.5M stars a
20 MiB asset rather than a 50 MiB one:

  Header (44 B):
    magic b"IVST" (uint32 0x54535649), version (uint32) = 2, count (uint32),
    parallax_count (uint32), shell_pc, max_distance_pc, parallax_scale,
    magnitude_min, magnitude_step, b_v_min, b_v_step (float32)
  Block A: count * 8 B, two uint32 per star:
    word 0: direction x (uint16) | direction y (uint16) << 16
    word 1: direction z (uint16) | V code (uint8) << 16 | B-V code (uint8) << 24
    direction component = (code - 32768) / 32767; the vector is left unnormalized,
    the quantization leaving |v| within 3e-5 of 1 (0.003 % of a distance)
  Block B: parallax_count * 2 B, uint16 parallax code = round(mas * parallax_scale)

The first `parallax_count` stars of Block A are exactly the ones Block B covers,
so the loader needs no per-star flag and no second index. A direction quantized to
16 bits per axis is good to 5.5 arcsec, an eighth of a pixel at the narrowest
focal length the camera widget offers.

The raw layout (not Godot store_var) keeps the baker pure-Python and lets the
loader bulk-read with FileAccess.get_buffer().to_int32_array().

Source archives, into --source-dir (about 220 MB, none of it tracked):
    https://cdsarc.cds.unistra.fr/ftp/I/239/hip_main.dat
    https://cdsarc.cds.unistra.fr/ftp/I/259/tyc2.dat.{00..19}.gz
    https://cdsarc.cds.unistra.fr/ftp/I/259/suppl_1.dat.gz

Usage (paths resolve relative to this script, so any working directory works):
    python addons/tools/build_star_binaries.py            # source_data/stars -> assets/starmaps
    python addons/tools/build_star_binaries.py --dry-run  # parse + report counts, write nothing
"""

import argparse
import array
import glob
import gzip
import math
import os
import struct
import sys

from _project import project_dir

# Length constants; match planetarium/units.gd and IVAstronomy.
AU_M = 149597870700.0
PARSEC_M = 648000.0 * AU_M / math.pi
OBLIQUITY = math.radians(23.4392911)  # IVAstronomy.OBLIQUITY_OF_THE_ECLIPTIC (J2000)
COS_OBL = math.cos(OBLIQUITY)
SIN_OBL = math.sin(OBLIQUITY)

MAGIC = b"IVST"
VERSION = 2

# Quantization. MUST match IVStarsVisual's decode. The V range covers Sirius at
# -1.44 and everything Tycho-2's faint tail reaches; the B-V range is exactly the
# span color_from_b_v() clamps to, so no code is spent outside it.
MAGNITUDE_MIN = -2.0
MAGNITUDE_STEP = 18.0 / 255.0  # covers V -2 .. 16; 3.3 % worst-case flux error
B_V_MIN = -0.4
B_V_STEP = 2.4 / 255.0
PARALLAX_SCALE = 64.0  # codes per mas; the uint16 reaches 1024 mas, past every known star

# Bin upper edges (Vmag). MUST match IVStarsVisual.BINARY_FILE_MAGNITUDES. A star
# goes in the first bin whose edge >= its Vmag; the first bin catches everything
# brighter and the final 99.9 bin the faint tail. The loader loads bins up to a
# magnitude_cutoff, and a project rendering at a fixed fov drops the bins that fov
# can never show -- see the culling table in the ivoyager_assets README.
BIN_EDGES = [2.0] + [2.0 + 0.5 * i for i in range(1, 23)] + [99.9]

# hip_main.dat pipe-delimited field indices (VizieR I/239 "hip_main"; the field
# index is the catalogue's own H-number).
H_HIP = 1     # HIP identifier
H_VMAG = 5    # Johnson V magnitude
H_RADEG = 8   # RA degrees, ICRS epoch J1991.25
H_DEDEG = 9   # Dec degrees
H_PLX = 11    # Trigonometric parallax (mas)
H_PMRA = 12   # Proper motion mu_alpha * cos(delta) (mas/yr)
H_PMDE = 13   # Proper motion mu_delta (mas/yr)
H_E_PLX = 16  # Standard error on parallax (mas)
H_BV = 37     # Johnson B-V colour index

HIPPARCOS_EPOCH_TO_J2000 = 2000.0 - 1991.25  # years
MAS_TO_DEG = 1.0 / 3.6e6

# tyc2.dat and suppl_1.dat fixed-column slices (VizieR I/259), 0-based.
TYC2_COLUMNS = {"ra_mean": (15, 27), "dec_mean": (28, 40), "bt": (110, 116), "vt": (123, 129),
        "hip": (142, 148), "ra_obs": (152, 164), "dec_obs": (165, 177)}
SUPPL_COLUMNS = {"flag": (13, 14), "ra_obs": (15, 27), "dec_obs": (28, 40), "bt": (83, 89),
        "vt": (96, 102), "hip": (115, 121)}


def parse_float(text):
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_column(line, span):
    return parse_float(line[span[0]:span[1]]) if len(line) >= span[1] else None


def johnson_from_tycho(bt_mag, vt_mag, default_bv):
    """Johnson (V, B-V) from Tycho BT/VT, per the I/259 ReadMe Note (7).

    Either magnitude may be absent. With only one of them the colour is not
    measured at all, so the default colour is assumed and the magnitude carried
    through the same relation -- which keeps the pair self-consistent rather than
    mixing a Tycho magnitude with a Johnson colour.
    """
    if bt_mag is not None and vt_mag is not None:
        bt_vt = bt_mag - vt_mag
        return vt_mag - 0.090 * bt_vt, 0.850 * bt_vt
    bt_vt = default_bv / 0.850
    if vt_mag is not None:
        return vt_mag - 0.090 * bt_vt, default_bv
    if bt_mag is not None:
        return bt_mag - bt_vt - 0.090 * bt_vt, default_bv
    return None, None


def ecliptic_direction(ra_deg, dec_deg):
    # Equatorial (ICRS) angles -> ecliptic Cartesian unit vector. Rotation matches
    # IVAstronomy exactly so stars align with bodies.
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    cos_dec = math.cos(dec)
    x_eq = cos_dec * math.cos(ra)
    y_eq = cos_dec * math.sin(ra)
    z_eq = math.sin(dec)
    return x_eq, y_eq * COS_OBL + z_eq * SIN_OBL, -y_eq * SIN_OBL + z_eq * COS_OBL


def pack_words(x, y, z, vmag, b_v):
    codes = []
    for component in (x, y, z):
        code = int(round(component * 32767.0)) + 32768
        codes.append(min(max(code, 0), 65535))
    magnitude_code = min(max(int(round((vmag - MAGNITUDE_MIN) / MAGNITUDE_STEP)), 0), 255)
    b_v_code = min(max(int(round((b_v - B_V_MIN) / B_V_STEP)), 0), 255)
    return codes[0] | (codes[1] << 16), codes[2] | (magnitude_code << 16) | (b_v_code << 24)


def bin_index(vmag):
    for i, edge in enumerate(BIN_EDGES):
        if vmag <= edge:
            return i
    return len(BIN_EDGES) - 1


class Bins:
    """Per-bin accumulators, holding parallax stars apart from shell stars.

    The format puts every parallax star before every shell star in a bin so that
    the distance block needs no index; collecting the two separately is what makes
    that ordering free.
    """

    def __init__(self, count):
        self.parallax_words = [array.array("I") for _ in range(count)]
        self.parallax_codes = [array.array("H") for _ in range(count)]
        self.shell_words = [array.array("I") for _ in range(count)]
        self.max_distance_pc = [0.0] * count

    def add(self, vmag, b_v, x, y, z, parallax_mas):
        index = bin_index(vmag)
        word_0, word_1 = pack_words(x, y, z, vmag, b_v)
        if parallax_mas is None:
            self.shell_words[index].extend((word_0, word_1))
            return
        code = min(max(int(round(parallax_mas * PARALLAX_SCALE)), 1), 65535)
        self.parallax_words[index].extend((word_0, word_1))
        self.parallax_codes[index].append(code)
        distance_pc = 1000.0 * PARALLAX_SCALE / code
        if distance_pc > self.max_distance_pc[index]:
            self.max_distance_pc[index] = distance_pc

    def count(self, index):
        return (len(self.parallax_words[index]) + len(self.shell_words[index])) // 2

    def size(self, index):
        return 44 + self.count(index) * 8 + len(self.parallax_codes[index]) * 2


def read_hipparcos(path, bins, args):
    """Loads hip_main.dat; returns the set of HIP numbers taken, for the merge."""
    hip_numbers = set()
    n_parallax = n_shell = n_skipped = 0
    with open(path, "r", encoding="latin-1") as source:
        for line in source:
            fields = line.split("|")
            if len(fields) <= H_BV:
                continue
            vmag = parse_float(fields[H_VMAG])
            ra_deg = parse_float(fields[H_RADEG])
            dec_deg = parse_float(fields[H_DEDEG])
            if vmag is None or ra_deg is None or dec_deg is None:
                n_skipped += 1
                continue
            hip_number = parse_float(fields[H_HIP])
            if hip_number is not None:
                hip_numbers.add(int(hip_number))
            pm_ra = parse_float(fields[H_PMRA]) or 0.0
            pm_dec = parse_float(fields[H_PMDE]) or 0.0
            cos_dec = max(math.cos(math.radians(dec_deg)), 1e-6)
            ra_deg += pm_ra * HIPPARCOS_EPOCH_TO_J2000 * MAS_TO_DEG / cos_dec
            dec_deg += pm_dec * HIPPARCOS_EPOCH_TO_J2000 * MAS_TO_DEG
            b_v = parse_float(fields[H_BV])
            if b_v is None:
                b_v = args.default_bv
            parallax = parse_float(fields[H_PLX])
            e_parallax = parse_float(fields[H_E_PLX])
            if (parallax is not None and parallax > 0.0 and e_parallax is not None
                    and e_parallax > 0.0 and parallax / e_parallax >= args.parallax_snr):
                n_parallax += 1
            else:
                parallax = None
                n_shell += 1
            x, y, z = ecliptic_direction(ra_deg, dec_deg)
            bins.add(vmag, b_v, x, y, z, parallax)
    print("  hip_main.dat     %9d stars (%d true-parallax, %d far-shell), %d skipped"
            % (n_parallax + n_shell, n_parallax, n_shell, n_skipped))
    return hip_numbers


def read_tycho2(paths, bins, hip_numbers, args):
    n_taken = n_matched = n_skipped = 0
    for path in paths:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="latin-1") as source:
            for line in source:
                hip_number = parse_column(line, TYC2_COLUMNS["hip"])
                if hip_number is not None and int(hip_number) in hip_numbers:
                    n_matched += 1
                    continue
                ra_deg = parse_column(line, TYC2_COLUMNS["ra_mean"])
                dec_deg = parse_column(line, TYC2_COLUMNS["dec_mean"])
                if ra_deg is None or dec_deg is None:
                    # pflag 'X': no mean position, so the observed one stands in.
                    ra_deg = parse_column(line, TYC2_COLUMNS["ra_obs"])
                    dec_deg = parse_column(line, TYC2_COLUMNS["dec_obs"])
                vmag, b_v = johnson_from_tycho(parse_column(line, TYC2_COLUMNS["bt"]),
                        parse_column(line, TYC2_COLUMNS["vt"]), args.default_bv)
                if ra_deg is None or dec_deg is None or vmag is None:
                    n_skipped += 1
                    continue
                x, y, z = ecliptic_direction(ra_deg, dec_deg)
                bins.add(vmag, b_v, x, y, z, None)
                n_taken += 1
    print("  tyc2.dat.*       %9d stars taken (%d already in Hipparcos), %d skipped"
            % (n_taken, n_matched, n_skipped))


def read_supplement(path, bins, hip_numbers, args):
    """Loads suppl_1.dat -- the Tycho-1 stars Tycho-2 dropped.

    Hipparcos-sourced rows are skipped whether or not hip_main.dat held them: this
    file's VTmag column carries Hp rather than VT for those, which is a different
    photometric system.
    """
    n_taken = n_matched = n_skipped = 0
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="latin-1") as source:
        for line in source:
            if line[SUPPL_COLUMNS["flag"][0]:SUPPL_COLUMNS["flag"][1]] != "T":
                n_matched += 1
                continue
            hip_number = parse_column(line, SUPPL_COLUMNS["hip"])
            if hip_number is not None and int(hip_number) in hip_numbers:
                n_matched += 1
                continue
            ra_deg = parse_column(line, SUPPL_COLUMNS["ra_obs"])
            dec_deg = parse_column(line, SUPPL_COLUMNS["dec_obs"])
            vmag, b_v = johnson_from_tycho(parse_column(line, SUPPL_COLUMNS["bt"]),
                    parse_column(line, SUPPL_COLUMNS["vt"]), args.default_bv)
            if ra_deg is None or dec_deg is None or vmag is None:
                n_skipped += 1
                continue
            x, y, z = ecliptic_direction(ra_deg, dec_deg)
            bins.add(vmag, b_v, x, y, z, None)
            n_taken += 1
    print("  suppl_1.dat      %9d stars taken (%d Hipparcos-sourced or matched), %d skipped"
            % (n_taken, n_matched, n_skipped))


def write_bin(out_path, bins, index, shell_pc):
    parallax_words = bins.parallax_words[index]
    shell_words = bins.shell_words[index]
    parallax_codes = bins.parallax_codes[index]
    max_distance_pc = max(bins.max_distance_pc[index], shell_pc if shell_words else 0.0)
    if sys.byteorder == "big":  # array.tofile writes native order; every target is LE
        parallax_words.byteswap()
        shell_words.byteswap()
        parallax_codes.byteswap()
    with open(out_path, "wb") as out:
        out.write(MAGIC)
        out.write(struct.pack("<III", VERSION, bins.count(index), len(parallax_codes)))
        out.write(struct.pack("<7f", shell_pc, max_distance_pc, PARALLAX_SCALE, MAGNITUDE_MIN,
                MAGNITUDE_STEP, B_V_MIN, B_V_STEP))
        parallax_words.tofile(out)
        shell_words.tofile(out)
        parallax_codes.tofile(out)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Build Hipparcos/Tycho-2 star binaries for "
            "IVStarsVisual.")
    parser.add_argument("--source-dir", default=os.path.join(here, "source_data", "stars"),
            help="directory holding hip_main.dat, tyc2.dat.*.gz and suppl_1.dat.gz")
    parser.add_argument("--out-dir", default=None,
            help="output directory for .ivbinary files "
                 "(default: <project>/addons/ivoyager_assets/starmaps)")
    parser.add_argument("--prefix", default="hipparcos_tycho2_stars",
            help="output file basename prefix")
    parser.add_argument("--parallax-snr", type=float, default=5.0,
            help="min parallax/error for a true distance; below this a star uses the far shell")
    parser.add_argument("--shell-pc", type=float, default=1000.0,
            help="far-shell distance (pc) for stars with unreliable parallax")
    parser.add_argument("--default-bv", type=float, default=0.5,
            help="B-V assigned to stars lacking a colour index")
    parser.add_argument("--no-tycho2", action="store_true",
            help="build from Hipparcos alone (the pre-2026-09 asset)")
    parser.add_argument("--dry-run", action="store_true", help="parse and report, write no files")
    args = parser.parse_args()
    if args.out_dir is None:
        args.out_dir = str(project_dir() / "addons" / "ivoyager_assets" / "starmaps")

    bins = Bins(len(BIN_EDGES))
    print("Reading catalogues from %s" % args.source_dir)
    hip_numbers = read_hipparcos(os.path.join(args.source_dir, "hip_main.dat"), bins, args)
    if not args.no_tycho2:
        tycho_paths = sorted(glob.glob(os.path.join(args.source_dir, "tyc2.dat.*")))
        if not tycho_paths:
            sys.exit("No tyc2.dat.* in %s (see the docstring for the archive)" % args.source_dir)
        read_tycho2(tycho_paths, bins, hip_numbers, args)
        supplement = os.path.join(args.source_dir, "suppl_1.dat.gz")
        if os.path.exists(supplement):
            read_supplement(supplement, bins, hip_numbers, args)

    total = sum(bins.count(i) for i in range(len(BIN_EDGES)))
    parallax_total = sum(len(codes) for codes in bins.parallax_codes)
    print("\nTotal %d stars (%d with a measured distance).\n" % (total, parallax_total))
    if not args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)
    written = 0
    for index, edge in enumerate(BIN_EDGES):
        name = "%s.%s.ivbinary" % (args.prefix, format(edge, ".1f"))
        count = bins.count(index)
        print("  %-38s %8d stars %10d B" % (name, count, bins.size(index) if count else 0))
        if args.dry_run or count == 0:
            continue
        write_bin(os.path.join(args.out_dir, name), bins, index, args.shell_pc)
        written += bins.size(index)
    if not args.dry_run:
        print("\nWrote %.2f MiB to %s" % (written / 1048576.0, args.out_dir))


if __name__ == "__main__":
    main()
