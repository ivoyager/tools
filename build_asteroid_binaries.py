#!/usr/bin/env python3
# build_asteroid_binaries.py
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
"""Build I, Voyager asteroid point-cloud binaries from AstDyS proper elements.

Reads AstDyS-2 osculating and synthetic proper elements and writes group- and
magnitude-binned `.ivbinary` files consumed by IVBinaryAsteroidsBuilder. Each
asteroid contributes a Keplerian element set, the proper secular frequencies its
orbit precesses at, an absolute magnitude, and -- for a Jupiter Trojan -- the
libration parameters the L-point shader oscillates it with.

Source data (download into `source_data/asteroids/` beside this script):

  AstDyS-2, https://newton.spacedys.com/astdys/
    allnum.cat    numbered asteroids, osculating Keplerian, one-line format
    ufitobs.cat   multiopposition asteroids, osculating Keplerian, one-line
    all.syn       synthetic proper elements: main belt and Hungarias
    tno.syn       synthetic proper elements: trans-Neptunian objects
    secres.syn    synthetic proper elements: secular-resonant asteroids
    tro.syn       synthetic proper elements: Jupiter Trojans
  JPL SBDB Query API, https://ssd-api.jpl.nasa.gov/doc/sbdb_query.html
    sbdb_names.json          asteroid names (~600 KB)
    sbdb_designations.json   number <-> discovery designation (~23 MB)
  `--fetch-sbdb` downloads both.

The four `.syn` files partition the population -- no asteroid appears in two --
and each supplies proper elements for catalog entries it can match by name. An
entry with no proper elements keeps its osculating orbit and a two-body mean
motion. An entry with no osculating row is dropped: the node, argument of
periapsis and mean anomaly exist only in the `.cat` files.

AstDyS UPDATES THE TWO SETS ON DIFFERENT CADENCES, and that is why SBDB supplies
designations as well as names. The catalogs re-key an asteroid the moment it is
numbered; the proper elements are republished every year or two. A body numbered
inside that window sits in the catalogs under its number and in the `.syn` files
under its discovery designation, and matches neither -- which costs a Trojan the
libration elements that put it in a cloud at all, so it is discarded as a suspect
rather than merely losing its precession rates. The 2024 proper elements against
the 2026 catalogs stranded 2926 Trojans this way.

ELEMENT CONVENTIONS. AstDyS synthetic proper elements publish (n, g, s) as the
frequencies of (mean longitude, longitude of perihelion, longitude of node). The
shader precesses `varpi` at g and needs a MEAN ANOMALY rate, which is therefore
`n - g`, not n. This matters everywhere and dominates for a resonant body: for a
Hilda, g is not a secular precession at all but the 3:2 resonance-locking rate
`3*n_J - 2*n` (holds for 96.9% of them within 50 "/yr), so treating n as the
mean-anomaly rate unlocks the Hilda pattern from Jupiter entirely. `m0` here is
anchored with `n - g` for the same reason. The binary stores n as published; the
shader applies the difference.

SECULAR-RESONANT ASTEROIDS keep their osculating eccentricity. `secres.syn`
replaces proper e with `de`, the amplitude of its resonant libration, and
publishes neither the libration frequency nor its phase -- so nothing here can
reconstruct e(t), and the instantaneous value is the only real one available.

Binary format (little-endian; consumed by binary_asteroids_builder.gd):
  Header:  magic b"IVAS" (uint32 0x53415649), version (uint32), count (uint32),
           flags (uint32; bit 0 set when block D is present)
  Block A: count * (e, i, lan, ap) float32              rad
  Block B: count * (a, m0, n) float32                   SI meters, rad, rad/s
  Block C: count * (s, g, mag) float32                  rad/s, rad/s, mag
  Block D: count * (da, dl, f, th0) float32             L4/L5 groups only
  Block E: uint32 byte length, then "\n"-separated UTF-8 names

The raw layout (not Godot store_var) lets the loader bulk-read each block with
FileAccess.get_buffer().to_float32_array() and split the names in one pass,
rather than decoding one Variant string per asteroid.

Group membership and magnitude cutoffs are read live from the Core plugin's
`tables/small_bodies_groups.tsv`, first row that passes; Trojan libration phase
is solved against Jupiter's row in `tables/orbits.tsv`. Neither is duplicated
here.

Usage (paths resolve relative to this script, so any working directory works):
    python addons/tools/build_asteroid_binaries.py                # build and write
    python addons/tools/build_asteroid_binaries.py --dry-run      # report, write nothing
    python addons/tools/build_asteroid_binaries.py --fetch-sbdb   # refresh JPL snapshots first
    python addons/tools/build_asteroid_binaries.py --verify       # self-checks only
"""

import argparse
import array
import json
import math
import os
import re
import struct
import sys
import urllib.parse
import urllib.request

from _project import project_dir

# Units; match ivoyager_units/units.gd. The internal length unit is the SI meter.
AU_M = 149597870700.0
YEAR_S = 365.25 * 86400.0  # Julian year, exactly IVUnits.YEAR
DAY_S = 86400.0
CENTURY_S = 36525.0 * DAY_S
ARCSEC_RAD = math.pi / (180.0 * 3600.0)
GM_SUN = 1.32712440042e20  # m^3 s^-2; only for an entry with no proper mean motion
MJD_J2000 = 51544.5  # MJD of J2000.0 TT, the zero point of the shader's iv_time

MAGIC = b"IVAS"
VERSION = 1
FLAG_LIBRATION = 1

# Bin upper edges. An asteroid goes in the first bin whose edge >= its magnitude.
# MUST match IVBinaryAsteroidsBuilder.BINARY_FILE_MAGNITUDES.
BIN_EDGES = [11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.5, 15.0, 15.5, 16.0, 16.5, 17.0,
        17.5, 18.0, 18.5, 99.9]

OSCULATING_FILES = ["allnum.cat", "ufitobs.cat"]
PROPER_FILES = ["all.syn", "tno.syn", "secres.syn"]
TROJAN_FILE = "tro.syn"

SBDB_URL = "https://ssd-api.jpl.nasa.gov/sbdb_query.api"
SBDB_NAME_QUERY = {"fields": "pdes,name", "sb-kind": "a", "sb-cdata": '{"AND":["name|DF"]}'}
# Numbered objects carry their discovery designation in `full_name`, which is what
# resolves a .syn row keyed on a designation the catalogs have since retired. Cut at
# H < 17.5 -- half a magnitude past the faintest group cutoff, against catalog-to-catalog
# H revisions measured at +-0.05 -- because unconstrained this is a 1.4-million-row query.
SBDB_DESIGNATION_QUERY = {"fields": "pdes,full_name", "sb-kind": "a",
        "sb-cdata": '{"AND":["H|LT|17.5"]}'}
# "848821 (2005 AR85)", "1 Ceres (A801 AA)" -- number, optional name, then designation.
FULL_NAME_PATTERN = re.compile(r"^\s*(\d+)\s+.*\(([^()]+)\)\s*$")

# Packed per-asteroid working record. Angles rad, rates rad/s, lengths au.
(E_A, E_E, E_I, E_LAN, E_AP, E_M, E_N, E_MAG, E_S, E_G, E_LP, E_DA, E_DL, E_F,
        E_TH0) = range(15)
N_ELEM = 15

TAU = 2.0 * math.pi


def wrap_tau(angle):
    return angle % TAU


def wrap_pi(angle):
    return (angle + math.pi) % TAU - math.pi


# ----- ivoyager table reading

# The meta rows are keyed on field 0, never on row index: their order differs
# between tables (some carry no Default row at all).
_META_FIELDS = ("Type", "Default", "Unit")

# Table unit -> this script's working unit. Failing on an unlisted unit rather than
# assuming one keeps a retuned table from silently changing what a criterion means.
# A rate is deliberately absent: its time unit has to be reconciled with whatever the
# caller is integrating over, so those columns are converted at their point of use.
_UNIT_FACTOR = {"": 1.0, "au": 1.0, "deg": math.pi / 180.0, "km": 1.0e3}


def read_db_table(path):
    """Parse an ivoyager db-style .tsv. Returns (rows, units), where rows is a list of
    dicts of raw cell strings keyed by column name (plus "name" for column 0, prefixed)
    and units maps column name -> the Unit row's cell."""
    with open(path, "r", encoding="utf-8") as handle:
        lines = [line.rstrip("\n") for line in handle]
    columns = lines[0].split("\t")
    columns[0] = "name"
    units = dict.fromkeys(columns, "")
    defaults = dict.fromkeys(columns, "")
    prefixes = dict.fromkeys(columns, "")
    name_prefix = ""
    rows = []
    for line in lines[1:]:
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        fields += [""] * (len(columns) - len(fields))
        first = fields[0]
        if first in _META_FIELDS or first.startswith("Prefix"):
            target = {"Unit": units, "Default": defaults, "Type": None}.get(first)
            if first.startswith("Prefix"):
                target = prefixes
                if "/" in first:
                    name_prefix = first.split("/", 1)[1]
            if target is not None:
                for column, value in zip(columns, fields):
                    target[column] = value
            continue
        row = {}
        for column, value in zip(columns, fields):
            if not value:
                value = defaults[column]
            if value and prefixes[column]:
                value = prefixes[column] + value
            row[column] = value
        row["name"] = name_prefix + fields[0]
        rows.append(row)
    return rows, units


def table_float(row, column, units):
    """Float cell in this script's working units. NAN for an empty cell, as the
    plugin's own missing-value convention does."""
    raw = row[column]
    if not raw:
        return float("nan")
    unit = units[column]
    if unit not in _UNIT_FACTOR:
        sys.exit(f"Unhandled unit '{unit}' on column '{column}'; add it to _UNIT_FACTOR.")
    return float(raw.replace(",", "").replace("_", "")) * _UNIT_FACTOR[unit]


def table_degrees_per(row, column, units, expected_unit):
    """A per-time rate cell in rad per that cell's own time unit. The caller states the
    unit it is integrating against, so a retuned table fails loudly instead of scaling."""
    assert units[column] == expected_unit, (
            f"column '{column}' is now '{units[column]}', not '{expected_unit}'")
    return math.radians(float(row[column])) if row[column] else 0.0


def table_int(row, column):
    return int(row[column]) if row[column] else -1  # plugin imputes -1 for a missing INT


def table_bool(row, column):
    return row[column] in ("x", "TRUE", "True", "true")


# ----- Jupiter, for Trojan libration phase

def jupiter_mean_longitude(orbits_path, time_s):
    """Jupiter's mean longitude (rad) at TT seconds from J2000, and its J2000 semi-major
    axis (m). Reproduces IVRealPlanetOrbit, which evaluates JPL's approximate-elements
    polynomial: the orbit's mean motion is set to dL/dt - (d_ap + d_lan)/dt, so
    M + lan + ap collapses to JPL's own mean-longitude series."""
    rows, units = read_db_table(orbits_path)
    row = next((r for r in rows if r["name"].endswith("PLANET_JUPITER")), None)
    if row is None:
        sys.exit(f"No PLANET_JUPITER row in {orbits_path}")
    l0 = (table_float(row, "mean_anomaly_at_epoch", units)
            + table_float(row, "longitude_ascending_node", units)
            + table_float(row, "argument_periapsis", units))
    # This column holds JPL's dL/dt, not the mean-anomaly rate the engine derives from it.
    dl_dt = table_degrees_per(row, "mean_motion", units, "deg/d") * CENTURY_S / DAY_S
    b = table_degrees_per(row, "mean_anomaly_correction_b", units, "deg/Cy^2")
    c = table_float(row, "mean_anomaly_correction_c", units)
    s = table_float(row, "mean_anomaly_correction_s", units)
    f = table_degrees_per(row, "mean_anomaly_correction_f", units, "deg/Cy")
    centuries = time_s / CENTURY_S
    clamped = min(max(centuries, -50.0), 10.0)  # validity_begin/end; c and s stay unclamped
    longitude = (l0 + dl_dt * centuries + b * clamped * clamped
            + c * math.cos(f * centuries) + s * math.sin(f * centuries))
    return wrap_tau(longitude), table_float(row, "semi_major_axis", units)


def solve_cos_theta(delta_longitude, amplitude, leading_sign):
    """Invert the L-point shader's longitude oscillator for cos(theta).

    The shader offsets a Trojan's mean longitude by
        dl * cos(th) * (1.0 + dl * dl * abs(cos(th) + leading_sign))
    (orbiting_positions_lp_id.gdshader), whose non-linear tail exaggerates the excursion
    when distal to Jupiter. Monotone in cos(th) across the published amplitudes, so
    bisection converges; the caller asserts the amplitude range that guarantees it."""

    def oscillator(cos_theta):
        tail = amplitude * amplitude * abs(cos_theta + leading_sign)
        return amplitude * cos_theta * (1.0 + tail)

    low, high = -1.0, 1.0
    if delta_longitude <= oscillator(low):
        return low
    if delta_longitude >= oscillator(high):
        return high
    for _ in range(60):
        middle = 0.5 * (low + high)
        if oscillator(middle) < delta_longitude:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


# ----- source data

def read_osculating(source_dir, max_magnitude):
    """Read the AstDyS one-line catalogs. Returns (keys, index_of, elements, epoch_mjd,
    counts). Entries fainter than any group will accept are dropped here: the group loop
    breaks on a magnitude cutoff, so no later group could take them."""
    keys = []
    index_of = {}
    elements = array.array("d")
    epoch_mjd = None
    n_read = n_faint = n_duplicate = 0
    for file_name in OSCULATING_FILES:
        path = os.path.join(source_dir, file_name)
        with open(path, "r") as handle:
            for line in handle:
                if line.startswith("!"):
                    break  # the column legend closes the header; data follows
            for line in handle:
                fields = line.split()
                if len(fields) < 9:
                    continue
                n_read += 1
                magnitude = 99.0 if fields[8] == "-9.99" else float(fields[8])
                if magnitude > max_magnitude:
                    n_faint += 1
                    continue
                epoch = float(fields[1])
                if epoch_mjd is None:
                    epoch_mjd = epoch
                elif epoch != epoch_mjd:
                    sys.exit(f"Inconsistent Epoch(MJD) in {file_name}: {epoch} vs {epoch_mjd}. "
                            "Re-download the .cat files as one set.")
                key = fields[0].replace("'", "")
                if key in index_of:
                    n_duplicate += 1
                    continue
                semi_major_axis = float(fields[2])
                record = [0.0] * N_ELEM
                record[E_A] = semi_major_axis
                record[E_E] = float(fields[3])
                record[E_I] = math.radians(float(fields[4]))
                record[E_LAN] = math.radians(float(fields[5]))
                record[E_AP] = math.radians(float(fields[6]))
                record[E_M] = math.radians(float(fields[7]))
                record[E_N] = math.sqrt(GM_SUN / (semi_major_axis * AU_M) ** 3)  # two-body
                record[E_MAG] = magnitude
                record[E_LP] = -1.0
                index_of[key] = len(keys)
                keys.append(key)
                elements.extend(record)
    return keys, index_of, elements, epoch_mjd, (n_read, n_faint, n_duplicate)


def apply_proper_elements(source_dir, index_of, elements, report):
    """Override osculating elements with synthetic proper ones. The four files partition
    the population, so order cannot matter."""
    for file_name in PROPER_FILES:
        is_secular_resonant = file_name == "secres.syn"
        n_applied = n_unmatched = 0
        with open(os.path.join(source_dir, file_name), "r") as handle:
            for line in handle:
                if line.startswith("%"):
                    continue
                fields = line.split()
                index = index_of.get(fields[0])
                if index is None:
                    n_unmatched += 1
                    continue
                base = index * N_ELEM
                elements[base + E_A] = float(fields[2])
                if not is_secular_resonant:
                    elements[base + E_E] = float(fields[3])  # secres col 3 is de, not e
                elements[base + E_I] = math.asin(float(fields[4]))
                elements[base + E_N] = math.radians(float(fields[5])) / YEAR_S
                elements[base + E_G] = float(fields[6]) * ARCSEC_RAD / YEAR_S
                elements[base + E_S] = float(fields[7]) * ARCSEC_RAD / YEAR_S
                n_applied += 1
        report.append((file_name, n_applied, n_unmatched))

    n_applied = n_unmatched = 0
    with open(os.path.join(source_dir, TROJAN_FILE), "r") as handle:
        for line in handle:
            if line.startswith("%"):
                continue
            fields = line.split()
            index = index_of.get(fields[0])
            if index is None:
                n_unmatched += 1
                continue
            base = index * N_ELEM
            elements[base + E_DA] = float(fields[2])
            elements[base + E_DL] = math.radians(float(fields[3]))
            elements[base + E_F] = math.radians(float(fields[4])) / YEAR_S
            elements[base + E_E] = float(fields[5])
            elements[base + E_G] = float(fields[6]) * ARCSEC_RAD / YEAR_S
            elements[base + E_I] = math.asin(float(fields[7]))
            elements[base + E_S] = float(fields[8]) * ARCSEC_RAD / YEAR_S
            elements[base + E_LP] = float(fields[9])
            n_applied += 1
    report.append((TROJAN_FILE, n_applied, n_unmatched))


def fetch_sbdb(path, query, what):
    url = f"{SBDB_URL}?{urllib.parse.urlencode(query)}"
    print(f"fetching {what} from {url} ...")
    with urllib.request.urlopen(url, timeout=600) as response:
        payload = response.read()
    with open(path, "wb") as handle:
        handle.write(payload)
    print(f"  wrote {path} ({len(payload) / 1048576.0:.1f} MiB)")


def apply_designation_aliases(path, index_of):
    """Let a `.syn` row keyed on a provisional designation reach the catalog row that
    now carries a number instead. AstDyS re-keys an asteroid the moment it is numbered
    but republishes proper elements only every year or two, so a body numbered inside
    that window is present in both sets under two different names and matches neither.
    Unmatched, a Trojan loses the libration elements that put it in a cloud at all and
    is discarded as a suspect; everything else silently drops to a two-body orbit.

    An alias is added only where the designation is not itself a live catalog key, so
    this can never displace a direct match."""
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    n_aliased = 0
    for designation, full_name in payload["data"]:
        if not designation.isdigit() or designation not in index_of:
            continue
        match = FULL_NAME_PATTERN.match(full_name or "")
        if match is None:
            continue
        provisional = match.group(2).replace(" ", "")
        if provisional in index_of:
            continue
        index_of[provisional] = index_of[designation]
        n_aliased += 1
    return len(payload["data"]), n_aliased


def apply_names(path, keys, index_of):
    """Name each numbered asteroid "<number> <Name>". SBDB's `pdes` is the bare number
    for a numbered object, which is exactly the AstDyS key, so no translation is needed.
    Everything else keeps its AstDyS designation."""
    names = list(keys)
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    n_named = 0
    for designation, name in payload["data"]:
        index = index_of.get(designation)
        if index is None or not name:
            continue
        names[index] = f"{designation} {name}"
        n_named += 1
    return names, len(payload["data"]), n_named


# ----- Trojan libration phase

def derive_trojan_phases(elements, trojan_indices, epoch_time, orbits_path):
    """Solve each Trojan's libration phase at J2000, the zero point of the shader's
    iv_time. The shader puts a Trojan's semi-major axis and mean longitude in quadrature
    about Jupiter's, so the osculating pair measures the phase directly.

    Longitude alone fixes the angle and the axis supplies only its sign. Feeding both to
    atan2 instead re-normalizes the pair, and it is not a unit vector -- a harmonic model
    against osculating elements, whose short-period terms neither `da` nor `D` describes
    -- so the renormalization lands the reconstructed longitude off by the shortfall.
    Longitude is what a viewer sees; the axis oscillation is a few hundredths of an au."""
    longitude_jupiter, semi_major_axis_jupiter = jupiter_mean_longitude(orbits_path, epoch_time)
    semi_major_axis_jupiter /= AU_M
    max_amplitude = max(elements[i * N_ELEM + E_DL] for i in trojan_indices)
    assert max_amplitude < 1.0, (  # below 1 rad the oscillator is monotone in cos(theta)
            f"libration amplitude {max_amplitude:.3f} rad breaks solve_cos_theta bisection")
    unit_radii = []
    for index in trojan_indices:
        base = index * N_ELEM
        leading_sign = 1.0 if elements[base + E_LP] == 4.0 else -1.0
        longitude_lagrange = longitude_jupiter + leading_sign * math.pi / 3.0
        sin_theta = (elements[base + E_A] - semi_major_axis_jupiter) / elements[base + E_DA]
        delta_longitude = wrap_pi(elements[base + E_LAN] + elements[base + E_AP]
                + elements[base + E_M] - longitude_lagrange)
        cos_theta = solve_cos_theta(delta_longitude, elements[base + E_DL], leading_sign)
        unit_radii.append(sin_theta * sin_theta + cos_theta * cos_theta)
        theta = math.acos(cos_theta)
        if sin_theta < 0.0:
            theta = -theta
        elements[base + E_TH0] = wrap_tau(theta - elements[base + E_F] * epoch_time)
    return longitude_jupiter, semi_major_axis_jupiter, unit_radii


# ----- group assignment

def read_groups(tables_dir):
    """Asteroid group criteria, in table row order -- an asteroid joins the first group
    it passes, which is what the table's own trailing comment states."""
    rows, units = read_db_table(os.path.join(tables_dir, "small_bodies_groups.tsv"))
    groups = []
    for row in rows:
        if row["sbg_class"] != "SBG_CLASS_ASTEROIDS":
            continue

        def bound(column, fallback):
            value = table_float(row, column, units)
            return fallback if math.isnan(value) else value

        groups.append({
            "alias": row["sbg_alias"],
            "mag_cutoff": table_float(row, "mag_cutoff", units),
            "skip": table_bool(row, "skip"),
            "lp_integer": table_int(row, "lp_integer"),
            "min_q": bound("min_q", 0.0),
            "max_q": bound("max_q", math.inf),
            "min_a": bound("min_a", 0.0),
            "max_a": bound("max_a", math.inf),
            "max_e": bound("max_e", math.inf),
            "max_i": bound("max_i", math.inf),
        })
    if not groups:
        sys.exit(f"No SBG_CLASS_ASTEROIDS rows in {tables_dir}/small_bodies_groups.tsv")
    return groups


def bin_index(magnitude):
    for index, edge in enumerate(BIN_EDGES):
        if magnitude <= edge:
            return index
    return len(BIN_EDGES) - 1


def assign_groups(elements, count, groups):
    """Returns {(alias, bin index): [asteroid index, ...]} and the number placed."""
    buckets = {}
    n_placed = 0
    for index in range(count):
        base = index * N_ELEM
        semi_major_axis = elements[base + E_A]
        eccentricity = elements[base + E_E]
        inclination = elements[base + E_I]
        magnitude = elements[base + E_MAG]
        periapsis = (1.0 - eccentricity) * semi_major_axis
        lp_integer = int(elements[base + E_LP])
        for group in groups:
            if semi_major_axis <= group["min_a"] or semi_major_axis > group["max_a"]:
                continue
            if periapsis <= group["min_q"] or periapsis > group["max_q"]:
                continue
            if eccentricity > group["max_e"] or inclination > group["max_i"]:
                continue
            if (lp_integer != -1) != (group["lp_integer"] != -1):
                continue
            if lp_integer != -1 and group["lp_integer"] != lp_integer:
                continue
            if group["skip"] or magnitude > group["mag_cutoff"]:
                break  # belongs here but is not shipped; no later group may claim it
            buckets.setdefault((group["alias"], bin_index(magnitude)), []).append(index)
            n_placed += 1
            break
    return buckets, n_placed


# ----- output

def build_blocks(indices, elements, names, epoch_time, has_libration):
    """Pack one file's asteroids into the binary's float blocks and its name blob."""
    block_a = array.array("f")
    block_b = array.array("f")
    block_c = array.array("f")
    block_d = array.array("f")
    blob = []
    for index in indices:
        base = index * N_ELEM
        mean_motion = elements[base + E_N]
        apsidal_rate = elements[base + E_G]
        # AstDyS n is the mean LONGITUDE rate, so the mean anomaly advances at n - g.
        mean_anomaly_at_epoch = wrap_tau(
                elements[base + E_M] - (mean_motion - apsidal_rate) * epoch_time)
        block_a.extend((elements[base + E_E], elements[base + E_I],
                elements[base + E_LAN], elements[base + E_AP]))
        block_b.extend((elements[base + E_A] * AU_M, mean_anomaly_at_epoch, mean_motion))
        block_c.extend((elements[base + E_S], apsidal_rate, elements[base + E_MAG]))
        if has_libration:
            block_d.extend((elements[base + E_DA] * AU_M, elements[base + E_DL],
                    elements[base + E_F], elements[base + E_TH0]))
        name = names[index]
        assert "\n" not in name, f"name {name!r} holds a newline"
        blob.append(name)
    return block_a, block_b, block_c, block_d, "\n".join(blob).encode("utf-8")


def write_binary(path, blocks, count, has_libration):
    block_a, block_b, block_c, block_d, blob = blocks
    if sys.byteorder == "big":  # array.tofile writes native order; every target is LE
        for block in (block_a, block_b, block_c, block_d):
            block.byteswap()
    with open(path, "wb") as out:
        out.write(MAGIC)
        out.write(struct.pack("<III", VERSION, count,
                FLAG_LIBRATION if has_libration else 0))
        block_a.tofile(out)
        block_b.tofile(out)
        block_c.tofile(out)
        if has_libration:
            block_d.tofile(out)
        out.write(struct.pack("<I", len(blob)))
        out.write(blob)


# ----- self-checks

def verify(tables_dir, core_dir):
    """Check this script against the two things it must agree with but cannot import:
    JPL's published Jupiter mean longitude, and the Core loader's magnitude bins."""
    ok = True

    _, _ = jupiter_mean_longitude(os.path.join(tables_dir, "orbits.tsv"), 0.0)
    rows, units = read_db_table(os.path.join(tables_dir, "orbits.tsv"))
    row = next(r for r in rows if r["name"].endswith("PLANET_JUPITER"))
    l0 = math.degrees(table_float(row, "mean_anomaly_at_epoch", units)
            + table_float(row, "longitude_ascending_node", units)
            + table_float(row, "argument_periapsis", units))
    published = 34.33479152  # JPL approximate elements 3000 BC - 3000 AD, Jupiter L0
    print(f"  Jupiter L0            {l0:.8f} deg vs JPL {published:.8f} "
            f"(delta {abs(l0 - published) * 3600.0:.4f} arcsec)")
    if abs(l0 - published) > 1.0e-6:
        print("    FAIL: orbits.tsv Jupiter row no longer reproduces JPL's mean longitude")
        ok = False

    path = os.path.join(core_dir, "program", "binary_asteroids_builder.gd")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    # Skip past the type hint's own bracket in `: Array[String] = [`.
    start = source.index("= [", source.index("BINARY_FILE_MAGNITUDES"))
    body = source[start:source.index("]", start)]
    core_edges = [float(text) for text in body.split('"')[1::2]]
    print(f"  magnitude bins        {len(core_edges)} in Core, {len(BIN_EDGES)} here")
    if core_edges != BIN_EDGES:
        print(f"    FAIL: BIN_EDGES must match IVBinaryAsteroidsBuilder: {core_edges}")
        ok = False

    # solve_cos_theta bisects, so the oscillator must stay monotone in cos(theta).
    worst = 0.0
    for amplitude in (0.1, 0.4, 0.7, 0.99):
        for leading_sign in (1.0, -1.0):
            previous = -math.inf
            for step in range(201):
                cos_theta = -1.0 + step / 100.0
                value = (amplitude * cos_theta
                        * (1.0 + amplitude * amplitude * abs(cos_theta + leading_sign)))
                if value <= previous:
                    print(f"    FAIL: oscillator not monotone at amplitude {amplitude}")
                    ok = False
                previous = value
            worst = max(worst, amplitude)
    print(f"  oscillator monotone   up to amplitude {worst:.2f} rad")
    return ok


# ----- main

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
            description="Build AstDyS asteroid binaries for IVBinaryAsteroidsBuilder.")
    parser.add_argument("--source-dir", default=os.path.join(here, "source_data", "asteroids"),
            help="directory holding the AstDyS .cat and .syn files")
    parser.add_argument("--out-dir", default=None,
            help="output directory for .ivbinary files "
                 "(default: <project>/addons/ivoyager_assets/asteroid_binaries)")
    parser.add_argument("--names-file", default=None,
            help="SBDB names JSON (default: <source-dir>/sbdb_names.json)")
    parser.add_argument("--designations-file", default=None,
            help="SBDB designations JSON (default: <source-dir>/sbdb_designations.json)")
    parser.add_argument("--fetch-sbdb", action="store_true",
            help="download both SBDB snapshots before building")
    parser.add_argument("--dry-run", action="store_true", help="parse and report, write no files")
    parser.add_argument("--verify", action="store_true",
            help="run self-checks against orbits.tsv and the Core loader, then exit")
    args = parser.parse_args()
    core_dir = str(project_dir() / "addons" / "ivoyager_core")
    tables_dir = os.path.join(core_dir, "tables")
    if args.names_file is None:
        args.names_file = os.path.join(args.source_dir, "sbdb_names.json")
    if args.designations_file is None:
        args.designations_file = os.path.join(args.source_dir, "sbdb_designations.json")
    if args.out_dir is None:
        args.out_dir = str(project_dir() / "addons" / "ivoyager_assets" / "asteroid_binaries")

    if args.verify:
        print("verifying ...")
        sys.exit(0 if verify(tables_dir, core_dir) else 1)

    if args.fetch_sbdb:
        fetch_sbdb(args.names_file, SBDB_NAME_QUERY, "names")
        fetch_sbdb(args.designations_file, SBDB_DESIGNATION_QUERY, "designations")

    groups = read_groups(tables_dir)
    max_magnitude = max(group["mag_cutoff"] for group in groups if not group["skip"])
    print(f"read {len(groups)} asteroid groups; faintest cutoff {max_magnitude}")

    print(f"reading osculating elements from {args.source_dir} ...")
    keys, index_of, elements, epoch_mjd, counts = read_osculating(args.source_dir, max_magnitude)
    n_read, n_faint, n_duplicate = counts
    count = len(keys)
    epoch_time = (epoch_mjd - MJD_J2000) * DAY_S
    print(f"  {n_read} catalog rows, {count} kept "
            f"({n_faint} fainter than {max_magnitude}, {n_duplicate} duplicate keys)")
    print(f"  epoch MJD {epoch_mjd:.6f} = {epoch_time:.1f} s from J2000")

    n_sbdb, n_aliased = apply_designation_aliases(args.designations_file, index_of)
    print(f"  {os.path.basename(args.designations_file):<12} {n_sbdb} designations, "
            f"{n_aliased} aliased to a numbered catalog row")

    report = []
    apply_proper_elements(args.source_dir, index_of, elements, report)
    for file_name, n_applied, n_unmatched in report:
        # Unmatched is absent from the catalogs OR already dropped as too faint; the two
        # are not separated here, which would cost holding every faint key in memory.
        print(f"  {file_name:<12} {n_applied:7d} applied, {n_unmatched:7d} unmatched")

    names, n_sbdb, n_named = apply_names(args.names_file, keys, index_of)
    print(f"  {os.path.basename(args.names_file):<12} {n_sbdb} names, {n_named} matched")

    trojan_indices = [i for i in range(count) if elements[i * N_ELEM + E_LP] != -1.0]
    longitude_jupiter, semi_major_axis_jupiter, unit_radii = derive_trojan_phases(
            elements, trojan_indices, epoch_time, os.path.join(tables_dir, "orbits.tsv"))
    unit_radii.sort()
    median = unit_radii[len(unit_radii) // 2]
    within = sum(1 for value in unit_radii if 0.5 <= value <= 2.0)
    print(f"  Jupiter at epoch: mean longitude {math.degrees(longitude_jupiter):.5f} deg, "
            f"a {semi_major_axis_jupiter:.7f} au")
    print(f"  {len(trojan_indices)} Trojan phases solved; libration fit sin^2+cos^2 "
            f"median {median:.3f}, {100.0 * within / len(unit_radii):.1f}% within [0.5, 2.0]")

    buckets, n_placed = assign_groups(elements, count, groups)
    n_shipped_names = sum(1 for indices in buckets.values() for i in indices
            if names[i] != keys[i])
    print(f"\n{n_placed} asteroids placed in {len(buckets)} files; "
            f"{n_shipped_names} named ({100.0 * n_shipped_names / n_placed:.1f}%)")
    if not args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)
    trojan_aliases = {group["alias"] for group in groups if group["lp_integer"] != -1}
    per_group = {}
    n_bytes = 0
    for (alias, index), indices in sorted(buckets.items()):
        indices.sort(key=lambda i: elements[i * N_ELEM + E_MAG])
        has_libration = alias in trojan_aliases
        blocks = build_blocks(indices, elements, names, epoch_time, has_libration)
        file_name = f"{alias}.{format(BIN_EDGES[index], '.1f')}.ivbinary"
        size = 16 + 4 + len(blocks[4]) + sum(len(block) * 4 for block in blocks[:4])
        n_bytes += size
        per_group[alias] = per_group.get(alias, 0) + len(indices)
        print(f"  {file_name:<24} {len(indices):7d} asteroids {size:9d} bytes")
        if args.dry_run:
            continue
        write_binary(os.path.join(args.out_dir, file_name), blocks, len(indices), has_libration)
    print(f"\n{'group':<8}{'asteroids':>12}")
    for alias in sorted(per_group):
        print(f"  {alias:<6}{per_group[alias]:>12d}")
    print(f"  {'TOTAL':<6}{n_placed:>12d}   {n_bytes} bytes, "
            f"{n_bytes / n_placed:.1f} per asteroid")
    if not args.dry_run:
        print(f"\nWrote binaries to {args.out_dir}")


if __name__ == "__main__":
    main()
