# tools

A collection of tools (mainly Python scripts) used to generate assets and convert data for use in I, Voyager.

Every script carries its own specification in its module docstring — data source, frame and unit conventions, what it does and doesn't claim — and lists its arguments under `--help`. **This file is only an index**: which pipelines exist, and which script or document to open. Read that script (or its linked document) before running it.

## Conventions

**Add this submodule at `<project>/addons/tools` — not at the project root.** The location is load-bearing, not a preference: scripts that reach project files find the project by climbing three levels up from their own path, so anywhere else (`<project>/tools`, say) resolves *outside* the project, where every derived path is well-formed but wrong. `_project.project_dir()` centralizes that climb and refuses to return a directory holding no `project.godot`, so a misplaced submodule fails immediately and says so — **a new script that needs project files should call it rather than climbing on its own.** Nothing else about the location matters, so any project using the standard ivoyager layout works.

- **Run from the project directory** (the one holding `project.godot`): `python addons/tools/<script>.py ...`.
- **Sibling submodules a pipeline needs.** The trajectory scripts need `addons/ivoyager_core` (the tables they write into), and — for `verify_trajectory.py` and `horizons_trajectory.py --pre-fix` — `addons/ivoyager_assistant`, to drive the running sim; `verify_trajectory.py` won't even import without it. The asset builders need `addons/ivoyager_assets` unless given `--out-dir`.
- **Outputs** go to `addons/ivoyager_assets/` (`models/`, `maps/`, `starmaps/`) or to the Core plugin's `tables/`. The assets directory is not Git-tracked in any project — the editor plugin downloads it — so a regenerated asset reaches other people only through an [asset_downloads](https://github.com/ivoyager/asset_downloads) release.
- **A generated asset needs an attribution entry**, in `IVOYAGER_WORKS.md` (our original work, plus the public-domain source data it derives from) or `3RD_PARTY.md` (third-party files). Both are mastered in the asset_downloads repo.
- **Downloaded source data** belongs in `source_data/` here (Git-ignored); each script's docstring cites the archive to fetch it from.
- **Dependencies:** the trajectory scripts are stdlib-only. The asset builders need `numpy`, `Pillow`, `tifffile`, and `pygltflib` (the `.glb` writers).
- **After writing a `.tsv` or a new asset, refresh imports before launching:** `<godot-console> --path . --import --headless`. Headless runs use cached table data and silently ignore un-reimported edits. Godot keys reimport on the source's md5, so a regenerate that reproduces identical bytes is correctly a no-op (mtime is not consulted). Note that `--editor --headless --quit` quits *before* the async import scan completes — use `--import`, which waits for imports then exits.

## Spacecraft trajectories & ephemerides

Patched-conic trajectory data for real craft (Voyager 1 & 2, Pioneer 10, Juno, New Horizons), converted from NASA/JPL HORIZONS osculating elements into the Core plugin's `orbits.tsv`, `trajectories.tsv` and `spacecrafts.tsv`.

- `horizons_trajectory.py` — the converter. Segmentation is hand-maintained in its `CRAFT` dict.
- `verify_trajectory.py` — drives the running sim to check each segment's primary and its joins.
- `orbital.py` — the shared two-body and Lambert math (a library, not a command).

**[TRAJECTORIES.md](TRAJECTORIES.md) is the reference** for the model, the HORIZONS source and query, the element → column mapping, gap closing, segmentation, the time base, and known imprecisions. Read it before creating or editing a trajectory.

## Body models from a DEM

`build_body_model.py` turns an equirectangular DEM plus an albedo map into a displaced-ellipsoid `.glb` whose vertices are in kilometers (so it drops in at `model_scale = 1000`, like the NASA-derived models). Two modes: `mesh` (the default — displaced mesh with an embedded detail normal map) and `normal` (a full equirectangular normal map only, for bodies that stay generic spheroids). It prints the `file_adjustments.tsv` row to add.

`make_iapetus_dem.py` synthesizes an idealized Iapetus DEM (the fossil bulge and equatorial ridge) to feed that script, since no measured global DEM of Iapetus exists. It is **not** measured topography — see its docstring for what it does and does not claim.

Per-body sources for real DEMs are cited in `IVOYAGER_WORKS.md`.

## Body models from a measured shape model

`build_shape_model.py` builds the `.glb` from a measured vertex-facet shape model (Gaskell stereophotoclinometry, PDS `..._ver###q.tab`) — for genuinely irregular bodies such as Phoebe, where displacing a sphere doesn't apply. It uses the same authoring frame as the DEM path, so the two line up in-engine.

## Surface texture channels

`build_earth_roughness.py` builds Earth's roughness map (a land/sea specular mask) from maps already shipped in `ivoyager_assets`, so the coastline tracks them exactly and nothing needs downloading. Its output is linear data and must be imported non-sRGB (`compress/channel_pack=1`); the docstring explains why.

## Star field

`build_star_binaries.py` bakes the ESA Hipparcos Main Catalogue (`hip_main.dat`, VizieR I/239) into the magnitude-binned `.ivbinary` point clouds that `IVStarsVisual` loads on init. Stdlib-only. Its magnitude bin edges must stay matched to `IVStarsVisual.BINARY_FILE_MAGNITUDES`.
