#!/usr/bin/env python3
# capture_body_icons.py
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
"""Batch-render the `bodies_2d/` icon set from the running simulation.

Every icon is a render of the body as the simulator actually draws it: the sim is
launched, each body staged through `IVBody.make_body_visual()` into an off-screen
rig, and the frame read back. Cube shaders, cloud and limb shells, band-pattern
bodies and packed spacecraft models therefore need no special handling here --
whatever the app shows is what the icon shows. The in-sim half is
`body_2d_icon_suite.gd` (same directory), reached over the ivoyager_assistant TCP
server; this script owns the pose specification, the file naming and the deploy.

Two conventions make an icon reproducible rather than hand-posed:

  * A pose is a SUB-CAMERA LONGITUDE AND LATITUDE -- the point of the body facing
    the viewer -- not a turntable angle. It survives a re-bake, a re-source, or a
    change of map registration, and it is checkable against a published landmark.
  * Lighting is the engine's physical sunlight with its compensating camera:
    one directional source at `metering_key / albedo`, which is the light energy
    IVExposureManager settles on once a body fills the view. No per-body
    brightness tuning, and an icon's tonality matches the app's.

The pose table is per project -- which face of Mars is interesting is an asset
decision, not a tool default -- so `--specs` takes a TSV and any body absent from
it captures at its defaults. Columns (all optional but `prefix`; a blank cell
takes the default):

    prefix        the body's file_prefix, e.g. Mars; names the output file. The
                  single row `*` sets the defaults for every row after it, which
                  is where a set-wide choice such as `ev` belongs -- one
                  authoritative number rather than the same cell copied down
    variant       suffix appended to the output file stem, so several candidate
                  poses for one body can be rendered side by side in one run;
                  blank -- the deployable case -- gives `<prefix>.256.png`
    longitude     sub-camera longitude, degrees east          [0]
    latitude      sub-camera latitude, degrees north          [0]
    roll          screen rotation, degrees counter-clockwise, or `auto`
                  to lay the silhouette's long axis on the lower-left to
                  upper-right diagonal                        [0]
    camera_radii  camera distance in the body's own camera radii. The default 6 is
                  twice VIEW_ZOOM's, which reads as wide-angle on a banded planet;
                  the projection is perspective and the distance alone sets it, so
                  lower this only to put a body back on the app's own framing, or
                  where the reference radius is a placeholder (a spacecraft) [6.0]
    fill          fraction of the frame the rendered body is fitted to; capped
                  at 1, and 0 disables the measured fit entirely  [1.0]
    zoom          multiplies the fitted answer: <1 enlarges and crops (how a
                  craft's boom is allowed off-frame), >1 leaves margin  [1.0]
    pan_x pan_y   frame offsets from centered, fractions of the framed size  [0]
    auto_center   1 to center the rendered silhouette before `pan`; 0 to pan from
                  the turntable pivot, which an irregular body's silhouette is
                  not centered on                                       [1]
    light_left    key-light offset from head-on, degrees left [15]
    light_up      key-light offset from head-on, degrees up; negative is down [-10]
    brightness    light energy override; blank = metering_key / albedo
    ev            exposure compensation in stops on top of that; the engine meters
                  on a body's subsolar luminance, which is the whole disc at the
                  near-zero phase an icon is lit at, so a stop down is often
                  wanted where the app needs none               [--ev]
    ev_auto       1 to apply `ev` only where it is earned: a body clipping no more
                  than `clip_limit` of its own texels at ev 0 keeps ev 0 and so
                  renders at exactly the app's level                     [0]
    clip_limit    the clipped fraction `ev_auto` tolerates              [0.01]
    ambient       body-shader ambient_light (the unlit hemisphere)  [0]
    env_ambient   Environment ambient energy -- what reaches a packed
                  spacecraft model, whose materials take no shader uniform  [0]
    exposure      iv_exposure for the render, which scales SELF-LUMINOUS output
                  (a star's photosphere, an emission map). 0 suppresses it, and
                  is right for a body lit at near-zero phase; a star needs its
                  own value                                     [0]
    hide_shells   shells.tsv tags to hide, e.g. `LIMB,CLOUDS`; `-` keeps every
                  shell, which is the default. A limb shell was hidden until
                  2026-08-25, when the premultiplied readback that made it print
                  a near-black border was fixed; a body whose surface takes its
                  sunlight THROUGH its air cannot have the air left out anyway  [-]
    shells        per-shell visibility by index, e.g. `1,0,1`; applied after
                  `hide_shells`
    width height  output size; a square one names the file `<prefix>.<size>.png`
                  and any other `<prefix>.<width>x<height>.png`  [256, 256]
    notes         ignored; keep the reason for a pose next to the pose

Usage (run from the project directory):
    python addons/tools/capture_body_icons.py --launch --specs <specs.tsv> \\
        --out-dir <preview-dir> --bodies Mars,Moon
    python addons/tools/capture_body_icons.py --launch --specs <specs.tsv> \\
        --mirror <lfs-master>/bodies_2d          # writes the deployed set
    python addons/tools/capture_body_icons.py --launch --list

Deployed icons are replaced in place, so their `.import` sidecars stay valid; a
NEW icon needs one written before Godot will see it. Refresh imports after a
deploy: `<godot-console> --path . --import --headless`.
"""

import argparse
import configparser
import csv
import pathlib
import shutil
import sys
import time

from _project import project_dir

PROJECT_DIR = project_dir()

# Reuse the assistant plugin's launcher + TCP client (its tools dir isn't a package).
ASSISTANT_TOOLS = PROJECT_DIR / "addons" / "ivoyager_assistant" / "tools"
sys.path.insert(0, str(ASSISTANT_TOOLS))
from assistant_test import AssistantClient, GodotLauncher          # noqa: E402
from orbit_accuracy_test import find_godot_executable              # noqa: E402

SUITE_SCRIPT = "res://addons/tools/body_2d_icon_suite.gd"
SUITE_KEY = "Body2DIconSuite"
OVERRIDE_CFG = PROJECT_DIR / "ivoyager_override2.cfg"
SUITE_SECTION = "assistant_test_suites"
DEFAULT_OUT_DIR = PROJECT_DIR / "addons" / "ivoyager_assets" / "bodies_2d"

# Defaults for every column the specs TSV may omit. The light angle is deliberately
# one value for the whole set: a consistent key direction is what makes a grid of
# icons read as one family rather than as separate renders.
DEFAULTS = {
    "longitude": 0.0,
    "latitude": 0.0,
    "roll": 0.0,
    "fill": 1.0,
    "zoom": 1.0,
    "pan_x": 0.0,
    "pan_y": 0.0,
    "light_left": 15.0,
    "light_up": -10.0,
    "brightness": 0.0,      # 0 = metering_key / albedo
    "ev": 0.0,
    "ev_auto": 0.0,
    "clip_limit": 0.01,
    "camera_radii": 6.0,
    "auto_center": 1.0,
    "exposure": 0.0,
    "ambient": 0.0,
    "env_ambient": 0.0,
    "width": 256,
    "height": 256,
    "variant": "",
    "hide_shells": [],
}
FLOAT_KEYS = ("longitude", "latitude", "fill", "zoom", "pan_x", "pan_y", "light_left",
              "light_up", "brightness", "ev", "ev_auto", "clip_limit", "camera_radii",
              "auto_center", "exposure", "ambient", "env_ambient")
INT_KEYS = ("width", "height")

POLL_INTERVAL = 0.2
POLL_TIMEOUT = 120.0
START_TIMEOUT = 180.0


# =============================================================================
# Spec table
# =============================================================================

def read_specs(path):
    """Return {prefix: spec-dict} from a TSV, defaults filled in. Missing file -> {}."""
    if not path:
        return {}
    specs = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(_uncommented(handle), delimiter="\t"):
            prefix = (row.get("prefix") or "").strip()
            if not prefix:
                continue
            if prefix == "*":
                DEFAULTS.update(_parse_row(row))
                continue
            spec = _parse_row(row)
            spec["prefix"] = prefix
            specs[prefix + spec["variant"]] = spec
    return specs


def _uncommented(handle):
    for line in handle:
        if not line.startswith("#"):
            yield line


def icon_file_name(key, spec):
    """`<key>.256.png` for the square case, `<key>.<width>x<height>.png` otherwise --
    the naming the shipped set already uses (`Sun_slice.128x1024.png`)."""
    if spec["width"] == spec["height"]:
        return "%s.%d.png" % (key, spec["width"])
    return "%s.%dx%d.png" % (key, spec["width"], spec["height"])


def _parse_row(row):
    spec = dict(DEFAULTS)
    for key in FLOAT_KEYS:
        value = (row.get(key) or "").strip()
        if value:
            spec[key] = float(value)
    for key in INT_KEYS:
        value = (row.get(key) or "").strip()
        if value:
            spec[key] = int(value)
    roll = (row.get("roll") or "").strip().lower()
    spec["auto_roll"] = roll == "auto"
    spec["roll"] = 0.0 if spec["auto_roll"] or not roll else float(roll)
    shells = (row.get("shells") or "").strip()
    spec["shells"] = [bool(int(flag)) for flag in shells.split(",")] if shells else None
    hide = (row.get("hide_shells") or "").strip()
    if hide == "-":
        spec["hide_shells"] = []
    elif hide:
        spec["hide_shells"] = [tag.strip() for tag in hide.split(",") if tag.strip()]
    spec["variant"] = (row.get("variant") or "").strip()
    return spec


def spec_to_params(spec, body_name, out_path):
    params = {
        "body": body_name,
        "out_path": str(out_path),
        "longitude": spec["longitude"],
        "latitude": spec["latitude"],
        "roll": spec["roll"],
        "auto_roll": spec["auto_roll"],
        "fill": spec["fill"],
        "zoom": spec["zoom"],
        "pan": [spec["pan_x"], spec["pan_y"]],
        "light_left": spec["light_left"],
        "light_up": spec["light_up"],
        "brightness": spec["brightness"],
        "ev": spec["ev"],
        "ev_auto": bool(spec["ev_auto"]),
        "auto_center": bool(spec["auto_center"]),
        "clip_limit": spec["clip_limit"],
        "camera_radii": spec["camera_radii"],
        "exposure": spec["exposure"],
        "ambient": spec["ambient"],
        "env_ambient": spec["env_ambient"],
        "width": spec["width"],
        "height": spec["height"],
        "hide_shell_tags": spec["hide_shells"],
    }
    if spec["shells"] is not None:
        params["shells"] = spec["shells"]
    return params


# =============================================================================
# Suite registration
# =============================================================================

class SuiteRegistration:
    """Adds the icon suite to ivoyager_override2.cfg for the run, then puts the file
    back. The assistant server reads suites only from the two override configs, so
    there is nowhere else to register one; restoring keeps the project repo clean for
    a tool whose home is this submodule."""

    def __init__(self, path):
        self.path = path
        self._original = None

    def __enter__(self):
        parser = configparser.ConfigParser()
        parser.optionxform = str
        if self.path.exists():
            self._original = self.path.read_text(encoding="utf-8")
            parser.read_string(self._original)
        if not parser.has_section(SUITE_SECTION):
            parser.add_section(SUITE_SECTION)
        if parser.get(SUITE_SECTION, SUITE_KEY, fallback=None) == f'"{SUITE_SCRIPT}"':
            return self
        parser.set(SUITE_SECTION, SUITE_KEY, f'"{SUITE_SCRIPT}"')
        with open(self.path, "w", encoding="utf-8") as handle:
            parser.write(handle, space_around_delimiters=False)
        return self

    def __exit__(self, *_exc):
        if self._original is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_text(self._original, encoding="utf-8", newline="")
        return False


# =============================================================================
# Capture
# =============================================================================

def call_checked(client, method, params=None):
    response = client.call(method, params)
    if "error" in response:
        raise RuntimeError(f"{method}: {response['error']}")
    result = response.get("result", {})
    if isinstance(result, dict) and "_error" in result:
        raise RuntimeError(f"{method}: {result['_error']}")
    return result


def wait_for_start(client):
    info = call_checked(client, "get_project_info")
    if info.get("wait_for_start") and not info.get("started"):
        call_checked(client, "start_game")
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if call_checked(client, "get_state").get("started"):
            return
        time.sleep(1.0)
    raise RuntimeError("Simulator did not start")


def capture_one(client, params):
    call_checked(client, "capture_body_icon", params)
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        result = call_checked(client, "poll_icon_capture")
        if result.get("done"):
            return result
    raise RuntimeError(f"Capture timed out for {params['body']}")


def report_line(prefix, result):
    mean = result["mean_rgb"]
    peak = result["peak_rgb"]
    return ("  %-16s lon %7.2f lat %6.2f roll %7.2f  ev %+4.1f  fills %.3f  "
            "clip %.3f  mean %.3f %.3f %.3f" % (
                prefix, result["longitude"], result["latitude"], result["roll"],
                result["ev"], result["extent_fraction"], result["clip_fraction"],
                mean[0], mean[1], mean[2]))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--specs", help="Pose specification TSV (see module docstring)")
    parser.add_argument("--ev", type=float, default=None,
                        help="Exposure compensation in stops for every body without its "
                             "own `ev` cell")
    parser.add_argument("--bodies", help="Comma-separated file prefixes; default is "
                                         "every prefix in --specs")
    parser.add_argument("--all", action="store_true",
                        help="Capture every capturable body, specs or not")
    parser.add_argument("--out-dir", default=None,
                        help=f"Output directory (default {DEFAULT_OUT_DIR})")
    parser.add_argument("--mirror", action="append", default=[],
                        help="Extra directory to copy each written icon into; repeatable")
    parser.add_argument("--list", action="store_true",
                        help="List capturable bodies and exit")
    parser.add_argument("--launch", action="store_true", help="Launch Godot first")
    parser.add_argument("--godot", default=None, help="Path to the Godot executable")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=29071)
    args = parser.parse_args()

    out_dir = pathlib.Path(args.out_dir) if args.out_dir else DEFAULT_OUT_DIR
    mirrors = [pathlib.Path(mirror) for mirror in args.mirror]
    specs = read_specs(args.specs)
    if args.ev is not None:
        DEFAULTS["ev"] = args.ev
        for spec in specs.values():
            spec.setdefault("ev", args.ev)
    launcher = None
    failures = []

    with SuiteRegistration(OVERRIDE_CFG):
        if args.launch:
            godot = args.godot or find_godot_executable(str(PROJECT_DIR))
            print(f"Launching {godot}")
            launcher = GodotLauncher(godot, str(PROJECT_DIR))
            launcher.start()
        client = AssistantClient(host=args.host, port=args.port)
        try:
            client.connect()
            wait_for_start(client)
            listing = call_checked(client, "list_icon_bodies")
            by_prefix = {row["prefix"]: row for row in listing["bodies"]}
            print(f"metering_key {listing['metering_key']}; "
                  f"{len(by_prefix)} capturable bodies")
            if args.list:
                for prefix, row in sorted(by_prefix.items()):
                    print("  %-14s %-8s albedo %.3f  %s" % (
                        prefix, row["model_kind"], row["albedo"], row["name"]))
            else:
                if args.bodies:
                    wanted = [name.strip() for name in args.bodies.split(",") if name.strip()]
                elif args.all:
                    wanted = sorted(by_prefix)
                else:
                    wanted = list(specs)
                if not wanted:
                    raise RuntimeError("Nothing to capture: pass --specs, --bodies or --all")
                out_dir.mkdir(parents=True, exist_ok=True)
                for key in wanted:
                    spec = specs.get(key, dict(DEFAULTS, auto_roll=False, shells=None,
                                               prefix=key))
                    row = by_prefix.get(spec["prefix"])
                    if row is None:
                        failures.append(f"{key}: not a capturable body")
                        print(f"  {key}: SKIPPED (not in the running simulation)")
                        continue
                    out_path = out_dir / icon_file_name(key, spec)
                    try:
                        result = capture_one(client, spec_to_params(
                            spec, row["name"], out_path))
                    except RuntimeError as error:
                        failures.append(str(error))
                        print(f"  {key}: FAILED ({error})")
                        continue
                    print(report_line(key, result))
                    for mirror in mirrors:
                        mirror.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(out_path, mirror / out_path.name)
            call_checked(client, "end_icon_capture")
            client.call("quit", {"force": True})
        finally:
            client.close()
            if launcher:
                launcher.shutdown_and_report()

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for failure in failures:
            print("  " + failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
