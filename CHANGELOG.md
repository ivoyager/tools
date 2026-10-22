# Changelog

This file documents changes to [tools](https://github.com/ivoyager/tools).

File format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).


## [v0.0.3] - UNRELEASED

Under development using Godot 4.7.2.

### Changed
* `horizons_trajectory.py`, `verify_trajectory.py` and `capture_body_icons.py` drive the sim through the members its classes declare (ivoyager_assistant's `describe`, `get` and `call`), and convert by the declared `IVUnits` constants. This fixes trajectory positions and GMs read in meters as kilometers since the Planetarium's `METER` became 1.0.
* Scripts that launch Godot choose the newest console build of the project's Godot major with ivoyager_assistant's `tools/godot_executable.py` (v0.0.5), and stop rather than run another major; `time_shader_compiles.py` had taken the newest file on disk.
* [API breaking] `capture_body_icons.py`'s in-sim half is `body_2d_icon_capture.gd` (was `body_2d_icon_suite.gd`), a program object that declares its members, which the script adds for the run and calls through ivoyager_assistant's `call`, now that the Assistant (v0.0.5) has no suites.


## [v0.0.2] - 2026-10-15

Released using Godot 4.7.2. This release coincides with Core plugin release v0.2.1.

Scripts that drive a running sim require ivoyager_assistant v0.0.3.

### Added
* `build_asteroid_binaries.py`, the asteroid `.ivbinary` pipeline from AstDyS-2 and JPL Small-Body Database sources, replacing the external ivbinary_maker project. Group criteria and magnitude bins are read from the Core plugin's tables at build time.
* `build_saturn_rings.py`, which bakes Björn Jónsson's radial ring profiles into the half-float `rings/saturn.rings.<w>.exr` texture array that `rings.gdshader` samples, with its own minimal `exr_writer.py`.
* `capture_body_icons.py` and its in-sim half `body_2d_icon_suite.gd`, which render the `bodies_2d/` icon set through the simulator's own body visuals, posed by a project's `icon_specs.tsv`.
* `time_shader_compiles.py` and `shader_compile_timer.gd`, a harness reporting per-shader compile cost for the Core plugin's shaders, one isolated Godot process per shader. See *Compiling shaders* in the Core plugin's GRAPHICS_PROFILING.md.

### Changed
* [Asset breaking] `build_star_binaries.py` merges Tycho-2 into Hipparcos (2.55M stars, complete to V 11.0) and writes a packed v2 record in half-magnitude bins, named `stars.*.ivbinary`. Files must be read by Core plugin v0.2.1 or later.
* [Asset breaking] `build_body_model.py` and `build_shape_model.py` author a mesh in the SphereMesh frame, fixing custom-mesh bodies that rendered mirrored; `build_body_model.py` drops `--map-offset-deg` and defaults `--u-dir` to -1. Rebuild any custom-mesh body from an equirectangular master centred on the prime meridian.
* The model builders no longer emit a `file_adjustments.tsv` row, now that the Core plugin reads a model's scale from its file name.
* `capture_body_icons.py`, `horizons_trajectory.py` and `verify_trajectory.py` launch Godot on a free Assistant port and check they reached their own instance, so runs can overlap.
* Removed `.gdignore`, so the editor now sees this directory and its in-sim `.gd` scripts.


## v0.0.1 - 2026-08-01

Released using Godot 4.7.1. This release coincides with Core plugin release v0.2.

### Added
* Asset and data pipeline scripts moved here from the Planetarium project: spacecraft trajectories from NASA/JPL HORIZONS (see [TRAJECTORIES.md](TRAJECTORIES.md)), body models from a DEM or a measured shape model, Earth's roughness map, the Hipparcos star binaries, and equirectangular-to-cubemap baking. [README.md](README.md) indexes them.


[v0.0.2]: https://github.com/ivoyager/tools/compare/v0.0.1...v0.0.2
